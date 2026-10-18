"""Executors: small, dumb, fixed-argv actions that run on the host AFTER policy/approval.

Commands come from the project registry (operator-written config), never from a model.
"""
from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request

from .config import Project, Settings
from .redact import redact


def run_argv(argv: list[str], cwd, timeout: int) -> dict:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "returncode": p.returncode, "output": redact(p.stdout + p.stderr, 8000)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "output": f"timed out after {timeout}s"}
    except FileNotFoundError as e:
        return {"ok": False, "returncode": 127, "output": str(e)}


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - operator-configured URL
            return 200 <= r.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_healthy(url: str, timeout: int, *, probe=http_ok, sleep=time.sleep, interval: float = 2.0) -> bool:
    """Healthy only if the endpoint answers OK on two consecutive probes (one lucky hit during a restart isn't enough)."""
    deadline = time.monotonic() + timeout
    streak = 0
    while True:
        streak = streak + 1 if probe(url) else 0
        if streak >= 2:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(interval)


def deploy(project: Project, settings: Settings, *, probe=http_ok, sleep=time.sleep) -> dict:
    if not project.deploy_command:
        return {"ok": False, "stage": "config", "output": "project has no deploy_command"}
    r = run_argv(project.deploy_command, project.path, settings.deploy_timeout_seconds)
    if not r["ok"]:
        return {"ok": False, "stage": "command", **r}
    if project.health_url:
        healthy = wait_healthy(project.health_url, settings.health_timeout_seconds, probe=probe, sleep=sleep)
        return {"ok": healthy, "stage": "health", "healthy": healthy, "output": r["output"]}
    return {"ok": True, "stage": "command", "healthy": None, "output": r["output"]}


def container_state(name: str) -> dict:
    r = run_argv(["docker", "inspect", "-f", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", name], None, 20)
    if not r["ok"]:
        return {"known": False, "error": r["output"]}
    status, _, health = r["output"].strip().partition("|")
    return {"known": True, "status": status, "health": health or None}


def restart_container(project: Project, container: str, settings: Settings, *, state=container_state, sleep=time.sleep) -> dict:
    if container not in project.containers:
        return {"ok": False, "output": f"'{container}' is not one of {project.name}'s containers"}
    r = run_argv(["docker", "restart", container], None, 120)
    if not r["ok"]:
        return {"ok": False, **r}
    deadline = time.monotonic() + settings.health_timeout_seconds
    while True:
        st = state(container)
        if st.get("known") and st["status"] == "running" and st.get("health") in (None, "healthy"):
            return {"ok": True, "output": r["output"], "state": st}
        if time.monotonic() >= deadline:
            return {"ok": False, "output": "container did not become running/healthy", "state": st}
        sleep(2)
