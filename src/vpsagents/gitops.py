"""Git operations done by the control plane (never by the agent).

Agents only edit files in a throwaway worktree. The control plane commits, checks the diff, and merges.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .redact import is_sensitive_path, redact

MAX_DIFF_BYTES = 2_000_000
MAX_FILES = 200

# no hooks, no pager, no prompts, no global config surprises
_BASE = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.ext.allow=never"]
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "vps-agents", "GIT_AUTHOR_EMAIL": "agents@localhost",
        "GIT_COMMITTER_NAME": "vps-agents", "GIT_COMMITTER_EMAIL": "agents@localhost"}


class GitError(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    import os
    env = {**os.environ, **_ENV}
    p = subprocess.run([*_BASE, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise GitError(redact((p.stderr or p.stdout).strip(), 2000))
    return p.stdout


def head(cwd: Path, ref: str = "HEAD") -> str:
    return git(cwd, "rev-parse", ref).strip()


def current_branch(cwd: Path) -> str:
    return git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()


def is_clean(cwd: Path) -> bool:
    return git(cwd, "status", "--porcelain", "--untracked-files=normal").strip() == ""


@dataclass
class Worktree:
    path: Path
    branch: str
    base_sha: str


def create_worktree(repo: Path, root: Path, job_id: str, base_ref: str, *, branch: bool = True) -> Worktree:
    """New worktree at the current tip of `base_ref`. branch=False gives a detached (read-only jobs)."""
    base_sha = head(repo, base_ref)
    path = root / job_id
    root.mkdir(parents=True, exist_ok=True)
    name = f"agent/{job_id}"
    if branch:
        git(repo, "worktree", "add", "-b", name, str(path), base_sha)
    else:
        git(repo, "worktree", "add", "--detach", str(path), base_sha)
    return Worktree(path=path, branch=name if branch else "", base_sha=base_sha)


def remove_worktree(repo: Path, wt_path: Path, branch: str | None = None) -> None:
    git(repo, "worktree", "remove", "--force", str(wt_path), check=False)
    if branch:
        git(repo, "branch", "-D", branch, check=False)


class UnsafeDiff(Exception):
    pass


def commit_changes(wt: Worktree, message: str) -> str | None:
    """Stage everything, vet it, commit. Returns new head sha, or None when nothing changed."""
    git(wt.path, "add", "-A", "--", ".")
    names = git(wt.path, "diff", "--cached", "--name-only", "-z").split("\0")
    names = [n for n in names if n]
    if not names:
        return None
    if len(names) > MAX_FILES:
        raise UnsafeDiff(f"too many changed files ({len(names)} > {MAX_FILES})")
    for n in names:
        if is_sensitive_path(n):
            raise UnsafeDiff(f"change touches a sensitive path: {n}")
        if n == ".git" or n.startswith(".git/") or "/.git/" in n:
            raise UnsafeDiff(f"change touches git internals: {n}")
    # symlinks (mode 120000) can point outside the repo
    for line in git(wt.path, "diff", "--cached", "--raw", "-z").split("\0"):
        if line.startswith(":") and " 120000 " in line.replace("\t", " ") + " ":
            raise UnsafeDiff("change adds or modifies a symlink")
    size = len(git(wt.path, "diff", "--cached", "--binary"))
    if size > MAX_DIFF_BYTES:
        raise UnsafeDiff(f"diff too large ({size} bytes)")
    git(wt.path, "commit", "--no-verify", "-q", "-m", message)
    return head(wt.path)


def diff_summary(wt: Worktree, limit: int = 12000) -> dict:
    stat = git(wt.path, "diff", "--stat", f"{wt.base_sha}..HEAD").strip()
    patch = git(wt.path, "diff", f"{wt.base_sha}..HEAD")
    return {"stat": stat, "patch": redact(patch, limit), "truncated": len(patch) > limit}


def base_moved(repo: Path, wt: Worktree, default_branch: str) -> bool:
    return head(repo, default_branch) != wt.base_sha


def rebase(wt: Worktree, onto_sha: str) -> bool:
    """Rebase the job branch onto the new base. False (and aborted) on conflict."""
    p = subprocess.run([*_BASE, "rebase", onto_sha], cwd=wt.path, capture_output=True, text=True)
    if p.returncode != 0:
        git(wt.path, "rebase", "--abort", check=False)
        return False
    wt.base_sha = onto_sha
    return True


def merge_into_checkout(repo: Path, branch: str, expected_head: str, default_branch: str) -> dict:
    """Fast-forward the project checkout to the approved commit. Refuses anything that could clobber local work."""
    if current_branch(repo) != default_branch:
        raise GitError(f"checkout is on '{current_branch(repo)}', not '{default_branch}'; refusing to merge")
    if not is_clean(repo):
        raise GitError("checkout has uncommitted changes; refusing to merge (commit or stash first)")
    tip = head(repo, branch)
    if tip != expected_head:
        raise GitError("job branch changed after approval; refusing to merge")
    pre = head(repo)
    git(repo, "merge", "--ff-only", expected_head)
    return {"pre_sha": pre, "merged_sha": head(repo)}


def rollback_checkout(repo: Path, default_branch: str, merged_sha: str, pre_sha: str) -> None:
    """Undo OUR merge only: HEAD must still be the commit we created and the tree must be clean."""
    if current_branch(repo) != default_branch or head(repo) != merged_sha:
        raise GitError("checkout moved since the merge; not rolling back automatically")
    if not is_clean(repo):
        raise GitError("checkout is dirty; not rolling back automatically")
    git(repo, "reset", "--hard", pre_sha)
