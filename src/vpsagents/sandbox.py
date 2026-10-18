"""Run untrusted agent/verify commands in a locked-down container.

The sandbox gets: one worktree, an optional read-only task dir, the model gateway network (workers only).
The sandbox never gets: the docker socket, the host home, project secrets, other worktrees, general internet
(unless a project explicitly opts in for verify runs).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .redact import redact


@dataclass
class SandboxSpec:
    name: str
    command: list[str]
    workdir: Path
    readonly: bool = False
    network: str = "none"          # none | gateway | bridge
    env: dict[str, str] = field(default_factory=dict)
    extra_ro_mounts: list[tuple[Path, str]] = field(default_factory=list)
    timeout: int = 1800


@dataclass
class RunResult:
    returncode: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Sandbox:
    def run(self, spec: SandboxSpec) -> RunResult:  # pragma: no cover - interface
        raise NotImplementedError


def docker_args(settings: Settings, spec: SandboxSpec, env_file: str | None = None) -> list[str]:
    s = settings.sandbox
    net = {"none": "none", "gateway": s.gateway_network, "bridge": "bridge"}[spec.network]
    user = s.user or f"{os.getuid()}:{os.getgid()}"
    args = ["docker", "run", "--rm", "--name", spec.name,
            "--network", net,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(s.pids_limit), "--memory", s.memory, "--cpus", s.cpus,
            "--user", user,
            "--read-only", "--tmpfs", "/tmp:rw,size=512m,exec", "--tmpfs", "/home/agent:rw,size=256m,exec,uid=%s" % user.split(":")[0],
            "-e", "HOME=/home/agent",
            "-v", f"{spec.workdir}:/work{':ro' if spec.readonly else ''}",
            "-w", "/work"]
    for host, ctr in spec.extra_ro_mounts:
        args += ["-v", f"{host}:{ctr}:ro"]
    if env_file:
        args += ["--env-file", env_file]
    return [*args, s.image, *spec.command]


class DockerSandbox(Sandbox):
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, spec: SandboxSpec) -> RunResult:
        # secrets go through a 0600 env-file, never argv (visible in `ps`)
        fd, env_path = tempfile.mkstemp(prefix="vpsa-env-")
        try:
            with os.fdopen(fd, "w") as f:
                for k, v in spec.env.items():
                    if "\n" in v or "\n" in k or "=" in k:
                        raise ValueError("invalid env entry")
                    f.write(f"{k}={v}\n")
            args = docker_args(self.settings, spec, env_path)
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=spec.timeout)
                return RunResult(p.returncode, redact(p.stdout + p.stderr, 60000))
            except subprocess.TimeoutExpired as e:
                subprocess.run(["docker", "kill", spec.name], capture_output=True)
                out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
                out = out.decode(errors="replace") if isinstance(out, bytes) else out
                return RunResult(124, redact(out, 60000), timed_out=True)
        finally:
            try:
                os.unlink(env_path)
            except OSError:
                pass


class LocalSandbox(Sandbox):
    """NO ISOLATION. For tests and local development only; refuses to run unless explicitly allowed."""

    def __init__(self):
        if os.environ.get("VPSA_ALLOW_UNSAFE_LOCAL") != "1":
            raise RuntimeError("sandbox.mode=local has no isolation; set VPSA_ALLOW_UNSAFE_LOCAL=1 to use it (dev/test only)")

    def run(self, spec: SandboxSpec) -> RunResult:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(spec.workdir), **spec.env}
        try:
            p = subprocess.run(spec.command, cwd=spec.workdir, capture_output=True, text=True, timeout=spec.timeout, env=env)
            return RunResult(p.returncode, redact(p.stdout + p.stderr, 60000))
        except subprocess.TimeoutExpired:
            return RunResult(124, "timed out", timed_out=True)
        except FileNotFoundError as e:
            return RunResult(127, str(e))


def make_sandbox(settings: Settings) -> Sandbox:
    return DockerSandbox(settings) if settings.sandbox.mode == "docker" else LocalSandbox()
