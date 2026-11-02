"""Isolated work copies: containers never see the real worktree or its .git pointer.

Why: a worktree's `.git` is a pointer into the project's real .git directory. Mounting that into a container (so git
works inside it) would let untrusted code plant git config (filters, fsmonitor, hooks...) that runs on the HOST the next
time the control plane touches the repo. So instead:

  create()          copies the TRACKED files into a scratch dir, makes a tiny standalone git repo there (so tools like
                    aider get a repo map), and records a sha256 baseline of every file.
  (container runs)  on the copy only.
  import_changes()  compares the copy to the baseline with plain Python (never runs git in the untrusted copy), applies
                    strict rules, and only then writes regular files into the real worktree.

Rejected: symlinks, special files, nested .git, secret-looking paths, too many/too large changes, writes through a
symlinked directory in the real worktree. Anything the real worktree's .gitignore ignores is dropped (and reported).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .redact import is_sensitive_path

MAX_FILE_BYTES = 1_000_000
ALWAYS_IGNORED_PARTS = {".aider.chat.history.md", ".aider.input.history", ".aider.tags.cache.v3", ".aider.tags.cache.v4",
                        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class UnsafeCopy(gitops.UnsafeDiff):
    """A change coming out of a work copy that must not reach the real worktree."""


@dataclass
class Report:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


@dataclass
class Scratch:
    path: Path
    baseline: dict

    def cleanup(self) -> None:
        _force_rmtree(self.path)


def _force_rmtree(path: Path) -> None:
    """The copy was written by untrusted code; it may contain dirs with no permissions."""
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _entry(p: str) -> tuple:
    """('l', target) | ('f', sha256, exec). Raises UnsafeCopy for anything else."""
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return ("l", os.readlink(p))
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeCopy(f"special file (not a regular file or symlink): {os.path.relpath(p)}")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    finally:
        os.close(fd)
    return ("f", h.hexdigest(), bool(st.st_mode & 0o111), st.st_size)


def _scan(root: Path) -> dict[str, tuple]:
    out: dict[str, tuple] = {}

    def fail(err):
        raise UnsafeCopy(f"unreadable directory in work copy: {err.filename}")

    for cur, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        rel_dir = os.path.relpath(cur, root)
        if rel_dir == ".":
            rel_dir = ""
            dirs[:] = [d for d in dirs if d != ".git"]  # our own scratch repo
            files[:] = [f for f in files if f != ".git"]
        for name in list(dirs) + list(files):
            if name == ".git":
                raise UnsafeCopy(f"work copy contains a nested .git: {os.path.join(rel_dir, name)}")
        for name in files:
            rel = os.path.join(rel_dir, name)
            out[rel] = _entry(os.path.join(cur, name))
        for name in dirs:  # symlinks to directories show up in `dirs` and are not followed
            full = os.path.join(cur, name)
            if os.path.islink(full):
                out[os.path.join(rel_dir, name)] = _entry(full)
    return out


def _strip_size(e: tuple) -> tuple:
    return e[:3] if e[0] == "f" else e


def create(src: Path, scratch_root: Path) -> Scratch:
    """Copy the tracked files of `src` (a trusted worktree) into a fresh standalone git repo."""
    scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = Path(tempfile.mkdtemp(prefix="vpsa-", dir=scratch_root))
    try:
        listing = gitops.git(src, "ls-files", "-z", "-s").split("\0")
        for line in listing:
            if not line:
                continue
            meta, _, rel = line.partition("\t")
            mode = meta.split()[0]
            if mode == "160000":  # submodule pointer: nothing to copy
                continue
            s, d = src / rel, dest / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                os.symlink(os.readlink(s), d)
            else:
                shutil.copyfile(s, d, follow_symlinks=False)
                os.chmod(d, 0o755 if mode == "100755" else 0o644)
        baseline = {k: _strip_size(v) for k, v in _scan(dest).items()}
        gitops.git(dest, "init", "-q")
        gitops.git(dest, "-c", "core.excludesFile=/dev/null", "add", "-A", "-f")
        gitops.git(dest, "commit", "-q", "--no-verify", "--allow-empty", "-m", "baseline")
        return Scratch(dest, baseline)
    except BaseException:
        _force_rmtree(dest)
        raise


@contextmanager
def scratch(src: Path, scratch_root: Path):
    s = create(src, scratch_root)
    try:
        yield s
    finally:
        s.cleanup()


def _drop_ignored(dest: Path, paths: list[str]) -> set[str]:
    """Ask the TRUSTED worktree which of these paths its .gitignore rules cover."""
    if not paths:
        return set()
    out = gitops.git(dest, "check-ignore", "-z", "--stdin", "--no-index", check=False,
                     input_text="\0".join(paths) + "\0")
    return {p for p in out.split("\0") if p}


def plan(scr: Scratch, dest: Path) -> tuple[Report, dict[str, tuple]]:
    """Validate what the container did. Raises UnsafeCopy; changes nothing."""
    current = _scan(scr.path)
    cur = {k: _strip_size(v) for k, v in current.items()}
    rep = Report()
    for k, v in cur.items():
        if k not in scr.baseline:
            rep.added.append(k)
        elif v != scr.baseline[k]:
            rep.modified.append(k)
    rep.deleted = [k for k in scr.baseline if k not in cur]

    junk = [k for k in rep.added + rep.modified if any(part in ALWAYS_IGNORED_PARTS or part.startswith(".aider")
                                                         for part in Path(k).parts)]
    ign = _drop_ignored(dest, [k for k in rep.added if k not in junk]) | set(junk)
    rep.ignored = sorted(ign)
    rep.added = [k for k in rep.added if k not in ign]
    rep.modified = [k for k in rep.modified if k not in ign]

    for k in rep.added + rep.modified:
        e = current[k]
        if e[0] == "l":
            raise UnsafeCopy(f"change adds or modifies a symlink: {k}")
        if is_sensitive_path(k):
            raise UnsafeCopy(f"change touches a sensitive path: {k}")
        if e[3] > MAX_FILE_BYTES:
            raise UnsafeCopy(f"file too large ({e[3]} bytes): {k}")
    n = len(rep.added) + len(rep.modified) + len(rep.deleted)
    if n > gitops.MAX_FILES:
        raise UnsafeCopy(f"too many changed files ({n} > {gitops.MAX_FILES})")
    total = sum(current[k][3] for k in rep.added + rep.modified)
    if total > gitops.MAX_DIFF_BYTES:
        raise UnsafeCopy(f"changes too large ({total} bytes)")
    return rep, current


def _safe_parent(dest: Path, rel: str, create_dirs: bool) -> Path:
    """dest/rel's parent, walking component by component and refusing symlinks in the REAL worktree."""
    cur = dest
    for part in Path(rel).parent.parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create_dirs:
                raise
            os.mkdir(cur, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UnsafeCopy(f"refusing to write through a non-directory or symlink in the worktree: {cur.relative_to(dest)}")
    return cur


def import_changes(scr: Scratch, dest: Path) -> Report:
    """Copy the validated changes into the real worktree. All checks run before the first write."""
    rep, current = plan(scr, dest)
    # re-read each file once, with O_NOFOLLOW, and verify it still matches what was validated
    payload: dict[str, bytes] = {}
    for k in rep.added + rep.modified:
        p = scr.path / k
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if hashlib.sha256(data).hexdigest() != current[k][1]:
            raise UnsafeCopy(f"file changed while importing: {k}")
        payload[k] = data
    for k in rep.added + rep.modified:  # dry-run the path checks too, so a refusal leaves the worktree untouched
        parent = dest
        for part in Path(k).parent.parts:
            parent = parent / part
            if os.path.lexists(parent) and (os.path.islink(parent) or not os.path.isdir(parent)):
                raise UnsafeCopy(f"refusing to write through a non-directory or symlink in the worktree: {Path(k).parent}")
        target = dest / k
        if os.path.isdir(target) and not os.path.islink(target):
            # allowed only when this change deletes everything inside it (a directory turned into a file)
            gone = set(rep.deleted)
            for cur, _dirs, files in os.walk(target, followlinks=False):
                for f in files:
                    if os.path.relpath(os.path.join(cur, f), dest) not in gone:
                        raise UnsafeCopy(f"a directory with other content exists where a file would be written: {k}")
    for k in rep.deleted:
        t = dest / k
        try:
            _safe_parent(dest, k, create_dirs=False)
            if os.path.lexists(t) and not os.path.isdir(t):
                os.unlink(t)
        except FileNotFoundError:
            continue
        for parent in t.parents:  # tidy emptied directories, never above the worktree
            if parent == dest:
                break
            try:
                parent.rmdir()
            except OSError:
                break
    for k, data in payload.items():
        parent = _safe_parent(dest, k, create_dirs=True)
        fd, tmp = tempfile.mkstemp(prefix=".vpsa-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o755 if current[k][2] else 0o644)
            os.replace(tmp, dest / k)  # replaces a symlink itself; never writes through it
        except BaseException:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            raise
    return rep
