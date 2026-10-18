"""Secret redaction + sensitive-path detection. Applied to everything that leaves a sandbox."""
import re
from pathlib import PurePosixPath

_PATTERNS = [
    (re.compile(r"(?i)\b([\w-]*(?:api[_-]?key|token|password|passwd|secret|private[_-]?key|credential)[\w-]*[\"']?\s*[=:]\s*[\"']?)([^\s\"',;}]+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?is)(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----)"), r"\1[REDACTED]\2"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^/\s@]+(@)"), r"\1[REDACTED]\2"),  # user:pass@host
]


def redact(text: str, limit: int = 20000) -> str:
    text = text or ""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    if len(text) > limit:
        # keep the tail: errors and conclusions are at the end
        text = "...[TRUNCATED]\n" + text[-limit:]
    return text


_SENSITIVE_NAMES = {".env", ".env.local", ".env.production", ".env.development", ".git-credentials",
                    "credentials", "credentials.json", "secrets.json", "secret.json",
                    "id_rsa", "id_ed25519", "id_ecdsa", ".npmrc", ".pypirc", ".netrc"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx"}
_SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", ".docker"}


def is_sensitive_path(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    name = p.name.lower()
    if name in _SENSITIVE_NAMES or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
        return True
    if p.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in _SENSITIVE_PARTS for part in p.parts)
