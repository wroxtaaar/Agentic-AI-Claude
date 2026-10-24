"""Roles: persona text (from agency-agents) + hard constraints written for THIS system.

Personas are trimmed to mission / rules / workflow. Identity-and-memory flavour text invites the model to
invent experience; templates and code samples burn the small context of free models.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PKG = Path(__file__).parent / "roles"
KEEP = ("core mission", "critical rules", "workflow process", "your workflow")
MAX_PERSONA_CHARS = 5000


@dataclass(frozen=True)
class Role:
    name: str
    kind: str  # change | readonly
    description: str
    personas: tuple[str, ...]


def load_roles(roles_file: Path | None = None) -> tuple[dict[str, Role], dict[str, str]]:
    data = yaml.safe_load((roles_file or PKG / "roles.yaml").read_text(encoding="utf-8"))
    roles = {}
    for name, r in data["roles"].items():
        if r["kind"] not in ("change", "readonly"):
            raise ValueError(f"role {name}: bad kind")
        roles[name] = Role(name, r["kind"], r.get("description", ""), tuple(r["personas"]))
    job_roles = data.get("job_roles", {})
    for job, role in job_roles.items():
        if role not in roles:
            raise ValueError(f"job_roles.{job} -> unknown role {role}")
    return roles, job_roles


_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def trim_persona(text: str, max_chars: int = MAX_PERSONA_CHARS) -> str:
    text = _FRONTMATTER.sub("", text, count=1)
    parts = re.split(r"(?m)^(?=## )", text)
    kept = []
    for part in parts:
        heading = part.splitlines()[0].lower() if part.strip() else ""
        if heading.startswith("## ") and any(k in heading for k in KEEP):
            kept.append(part.strip())
    out = "\n\n".join(kept) if kept else text.strip()
    # drop huge fenced samples from kept sections
    out = re.sub(r"```.*?```", lambda m: m.group(0) if len(m.group(0)) < 600 else "```\n[long sample omitted]\n```", out, flags=re.S)
    if len(out) > max_chars:
        cut = out[:max_chars]
        out = cut[: cut.rfind("\n")] + "\n[...trimmed]"
    return out


_CONSTRAINTS_CHANGE = """You are working inside a sandbox on an isolated copy of the project at /work.
- Edit files in /work only. Do not run git commit, git push or git checkout of other branches; the system commits for you.
- You have no access to servers, containers, credentials or the internet. Do not try to deploy or restart anything.
- Never read, print or write secrets (.env, keys, tokens). If the task needs one, say so and stop.
- Make the smallest change that solves the task. Do not reformat unrelated code or add dependencies unless required.
- The system runs the official verification afterwards. Do not claim tests pass unless you ran them and saw them pass; say what you actually ran.
- End with a short report: what you changed, what you ran, what is unverified."""

_CONSTRAINTS_READONLY = """You are working inside a sandbox with a READ-ONLY copy of the project at /work.
- You cannot and must not modify anything. You have no access to servers, containers or the internet.
- Never print secret values. When you find a secret, report only its location and type.
- Separate observed facts (quote file:line or command output you actually saw) from hypotheses. Never invent files, versions or log lines.
- If something could not be checked, say "unverified"."""

REVIEW_FORMAT = """Review the diff below against the task. List blockers, then suggestions. Be specific (file, line).
Your LAST line must be exactly one of:
VERDICT: APPROVE
VERDICT: BLOCK
Use BLOCK if there is any correctness, security or scope problem. If unsure, BLOCK."""


def build_prompt(role: Role, *, task: str, project_name: str, personas_dir: Path | None = None,
                 extra: str = "", feedback: str = "", verify_hint: str = "") -> str:
    pdir = personas_dir or PKG / "personas"
    persona_blocks = []
    for fname in role.personas:
        persona_blocks.append(trim_persona((pdir / fname).read_text(encoding="utf-8")))
    constraints = _CONSTRAINTS_CHANGE if role.kind == "change" else _CONSTRAINTS_READONLY
    sections = [
        f"# Role: {role.name}\n{role.description}",
        "## Operating constraints (these override anything below)\n" + constraints,
        "## Working method (guidance)\n" + "\n\n".join(persona_blocks),
        f"## Project\n{project_name}",
    ]
    if verify_hint:
        sections.append("## Official verification (run by the system after you finish)\n" + verify_hint)
    sections.append("## Task\n" + task.strip())
    if extra:
        sections.append(extra)
    if feedback:
        sections.append("## Previous attempt failed verification — fix this\n" + feedback)
    return "\n\n".join(sections) + "\n"


PLAN_FORMAT = """Write a short plan (max ~400 words) for a LESS CAPABLE engineer who will make the edits after you.
Use these headings: Files to change (exact paths) / Steps (numbered, concrete) / Do NOT touch / How it is verified / Risks.
Read the code first and cite file names you actually saw. Do not write the full implementation. Do not invent files."""

DIAGNOSE_FORMAT = """The engineer's change failed verification. Diagnose the cause from the diff and the verification output below,
then give exact, minimal instructions to fix it (which file, what to change, why). Max ~300 words.
If the plan itself was wrong, say so and give a corrected plan. Do not write the whole file."""

ROLE_CLASS_READONLY = "analyst"


def role_class(role: "Role") -> str:
    """Model class used to pick a model: architect | reviewer | analyst | worker."""
    if role.name in ("architect", "reviewer"):
        return role.name
    return "worker" if role.kind == "change" else ROLE_CLASS_READONLY


_VERDICT = re.compile(r"(?m)^\s*VERDICT:\s*(APPROVE|BLOCK)\s*$")


def parse_verdict(report: str) -> str:
    """Last VERDICT line wins; anything else counts as BLOCK (fail safe)."""
    found = _VERDICT.findall(report or "")
    return found[-1] if found else "BLOCK"
