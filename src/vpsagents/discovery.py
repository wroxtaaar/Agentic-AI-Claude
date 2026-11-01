"""Find git projects under a directory and draft config entries for them.

Read-only: it lists directories and file NAMES only, never file contents, and skips secrets/dependency folders.
Everything it suggests is a suggestion; the operator writes the final config (commands especially).
"""
from __future__ import annotations

from pathlib import Path

from .redact import is_sensitive_path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".gradle", "build", "dist", ".cache", ".idea"}


def find_repos(root: Path, max_depth: int = 4, limit: int = 100) -> list[Path]:
    root = root.expanduser().resolve()
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    seen: set[Path] = set()
    while queue and len(found) < limit:
        p, depth = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        if (p / ".git").exists():
            found.append(p)
            continue  # don't descend into a repo (submodules, vendored repos)
        if depth >= max_depth:
            continue
        try:
            children = sorted(p.iterdir())
        except OSError:
            continue
        for c in children:
            if c.is_symlink() or not c.is_dir() or c.name in SKIP_DIRS or is_sensitive_path(c.name):
                continue
            queue.append((c, depth + 1))
    return found


def detect(path: Path) -> dict:
    """Project type by marker file names + a suggested verify command (a starting point, not a promise)."""
    names = set()
    try:
        names = {c.name for c in path.iterdir() if not c.is_symlink()}
    except OSError:
        pass
    kinds, verify = [], []
    if names & {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"}:
        kinds.append("python")
        verify.append(["sh", "-c", "python -m venv /tmp/v && /tmp/v/bin/pip install -q -r requirements.txt && /tmp/v/bin/python -m pytest -q"]
                      if "requirements.txt" in names else ["python", "-m", "pytest", "-q"])
    if "package.json" in names:
        kinds.append("node")
        verify.append(["npm", "test", "--silent"])
    if names & {"build.gradle", "build.gradle.kts", "gradlew"}:
        kinds.append("gradle/kotlin")
        verify.append(["./gradlew", "test", "--offline"] if "gradlew" in names else ["gradle", "test"])
    if "pom.xml" in names:
        kinds.append("maven")
        verify.append(["mvn", "-q", "test"])
    if "go.mod" in names:
        kinds.append("go")
        verify.append(["go", "test", "./..."])
    if "Cargo.toml" in names:
        kinds.append("rust")
        verify.append(["cargo", "test", "--quiet"])
    compose = sorted(n for n in names if n in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"})
    return {"types": kinds, "verify": verify[:1], "compose": compose, "has_dockerfile": "Dockerfile" in names,
            "has_ci": (path / ".github" / "workflows").is_dir()}


def branch_and_remote(path: Path, git=None) -> tuple[str, str | None]:
    """Default branch + origin URL (credentials stripped). Uses git read-only."""
    import subprocess

    def run(*a):
        r = subprocess.run(["git", "-C", str(path), "-c", "core.hooksPath=/dev/null", *a], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""

    branch = run("symbolic-ref", "--short", "HEAD") or "main"
    url = run("remote", "get-url", "origin") or None
    if url and "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        url = f"{scheme}://{rest.split('@', 1)[1]}"
    return branch, url


def draft_yaml(projects: list[Path]) -> str:
    """Config snippet. Tier 1 (propose only) by default: you raise the tier yourself once it has earned it."""
    lines = ["# Drafted by `vpsagents discover`. Review every line. Tier 1 = propose only; raise it deliberately.", "projects:"]
    for p in projects:
        info = detect(p)
        branch, remote = branch_and_remote(p)
        lines += [f"  - name: {p.name.lower().replace(' ', '-').replace('_', '-')}",
                  f"    path: {p}",
                  "    tier: 1",
                  f"    default_branch: {branch}",
                  f"    # detected: {', '.join(info['types']) or 'unknown'}"
                  + (f"; compose: {', '.join(info['compose'])}" if info["compose"] else "")
                  + ("; has GitHub Actions (it may deploy on push)" if info["has_ci"] else "")]
        if info["verify"]:
            lines.append("    verify_commands:  # suggestion; check it works for this project")
            for v in info["verify"]:
                lines.append("      - " + _flow(v))
        else:
            lines.append("    verify_commands: []   # none detected: changes can never be auto-verified")
        lines.append("    # containers: []   # names from `docker ps`, for the incident monitor")
        lines.append("    # health_url: http://127.0.0.1:PORT/health")
        if remote:
            lines.append(f"    # remote: origin   # {remote}")
    return "\n".join(lines) + "\n"


def _flow(argv: list[str]) -> str:
    import json
    return json.dumps(argv)
