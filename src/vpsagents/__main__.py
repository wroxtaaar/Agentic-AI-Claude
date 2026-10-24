import argparse
import logging
import os
import shutil
import subprocess
import sys

from .config import load_settings


def publish_problems(settings) -> list[str]:
    import os
    out = []
    for p in settings.projects:
        if p.publish != "push_main" or not p.path.is_dir():
            continue
        r = subprocess.run(["git", "-C", str(p.path), "remote", "get-url", p.remote], capture_output=True, text=True)
        if r.returncode != 0:
            out.append(f"{p.name}: publish=push_main but git remote '{p.remote}' does not exist")
            continue
        if r.stdout.strip().startswith("https://") and not os.environ.get(p.push_token_env):
            out.append(f"{p.name}: https remote needs a token in env {p.push_token_env} (fine-grained PAT, Contents: read/write, this repo only)")
        if p.tier >= 3 and not p.health_url:
            out.append(f"{p.name}: tier 3 + publish without health_url will always ask before pushing")
        if p.deploy_command:
            out.append(f"{p.name}: publish=push_main AND deploy_command set; make sure your CI is not also deploying (double deploy)")
    return out


def _check(settings) -> int:
    problems = publish_problems(settings)
    for p in settings.projects:
        if not p.path.is_dir():
            problems.append(f"{p.name}: path does not exist: {p.path}")
            continue
        r = subprocess.run(["git", "-C", str(p.path), "rev-parse", "--verify", p.default_branch], capture_output=True)
        if r.returncode != 0:
            problems.append(f"{p.name}: not a git repo or branch '{p.default_branch}' missing")
        if p.tier >= 3 and not (p.health_url and p.deploy_command and p.verify_commands):
            problems.append(f"{p.name}: tier 3 without health_url + deploy_command + verify_commands will always fall back to approvals")
        if p.tier >= 1 and not p.verify_commands:
            problems.append(f"{p.name}: no verify_commands; changes can never be auto-verified")
    if settings.sandbox.mode == "docker":
        if not shutil.which("docker"):
            problems.append("docker not found on PATH")
        else:
            if subprocess.run(["docker", "image", "inspect", settings.sandbox.image], capture_output=True).returncode != 0:
                problems.append(f"sandbox image '{settings.sandbox.image}' not found (build it first)")
            if subprocess.run(["docker", "network", "inspect", settings.sandbox.gateway_network], capture_output=True).returncode != 0:
                problems.append(f"docker network '{settings.sandbox.gateway_network}' not found")
    if not settings.worker.command:
        problems.append("worker.command is not configured")
    if not settings.llm.api_key():
        problems.append(f"env {settings.llm.api_key_env} is empty (model gateway key)")
    for m in problems:
        print("PROBLEM:", m)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vpsagents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "check"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
    sub.choices["serve"].add_argument("--host", default="127.0.0.1")
    sub.choices["serve"].add_argument("--port", type=int, default=8787)
    sub.choices["serve"].add_argument("--no-monitor", action="store_true")
    a = ap.parse_args(argv)
    settings = load_settings(a.config)
    if a.cmd == "check":
        return _check(settings)

    import uvicorn

    from .api import create_app
    from .engine import Engine
    from .monitor import Monitor
    from .notify import from_env
    from .runner import Runner
    from .sandbox import make_sandbox
    from .store import Store
    from .workers import CommandWorker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(settings.data_dir / "state.db")
    sandbox = make_sandbox(settings)
    engine = Engine(settings, store, sandbox, CommandWorker(settings, sandbox), from_env())
    app = create_app(engine)
    Runner(engine).start()
    if not a.no_monitor:
        Monitor(engine).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
