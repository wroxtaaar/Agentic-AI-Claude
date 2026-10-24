import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

os.environ["VPSA_ALLOW_UNSAFE_LOCAL"] = "1"

from vpsagents.config import Project, SandboxSettings, Settings
from vpsagents.engine import Engine
from vpsagents.notify import Notifier
from vpsagents.runner import Runner
from vpsagents.sandbox import LocalSandbox
from vpsagents.store import Store
from vpsagents.workers import CallableWorker

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def make_repo(root: Path, name="proj") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "x.txt").write_text("old\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "init")
    return repo


class Notes(Notifier):
    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)


VERIFY_OK = ["python3", "-c", "import sys; sys.exit(0 if open('x.txt').read().strip()=='new' else 1)"]


class Env(unittest.TestCase):
    """A real git repo + real engine; only the model worker and the sandbox are stubbed."""
    tier = 2
    deploy = True
    verify = True
    health = True
    review = True
    plan = False  # new tiering features are off unless a test opts in
    escalate = None

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.repo = make_repo(self.root)
        proj = Project(name="proj", path=self.repo, tier=self.tier,
                       deploy_command=["true"] if self.deploy else None,
                       health_url="http://health.invalid/" if self.health else None,
                       verify_commands=[VERIFY_OK] if self.verify else [])
        self.settings = Settings(data_dir=self.root / "data", sandbox=SandboxSettings(mode="local"),
                                 projects=[proj], review_pass=self.review, architect_plan=self.plan,
                                 escalate_after_failures=self.escalate, health_timeout_seconds=1, max_repairs=2)
        self.store = Store(":memory:")
        self.notes = Notes()
        self.healthy = True
        self.calls = []
        self.verdict = "VERDICT: APPROVE"
        self.write = "new\n"
        self.engine = Engine(self.settings, self.store, LocalSandbox(), CallableWorker(self._worker), self.notes,
                             probe=lambda url: self.healthy, sleep=lambda s: time.sleep(0.02))
        self.runner = Runner(self.engine)

    def tearDown(self):
        self._td.cleanup()

    def _worker(self, task):
        self.calls.append(task.label)
        if task.label == "rev":
            return f"looks fine\n{self.verdict}"
        if task.label == "plan":
            return "Files: x.txt. Steps: write 'new'."
        if task.label == "diag":
            return "x.txt must contain 'new'."
        if task.label == "ro":
            return "report: all good"
        content = self.write(task) if callable(self.write) else self.write
        (task.workdir / "x.txt").write_text(content)
        return "edited"

    def submit(self, kind="change", task="make x new", **kw):
        return self.engine.submit("proj", kind, task, **kw)

    def run_all(self, limit=10):
        for _ in range(limit):
            if not self.runner.run_once():
                return

    def job(self, jid):
        return self.store.get_job(jid)

    def pending(self):
        return self.store.list_approvals("pending")

    def approve_pending(self):
        ap = self.pending()[0]
        self.engine.decide_approval(ap["id"], True, "test")
        return ap

    def checkout_text(self):
        return (self.repo / "x.txt").read_text().strip()
