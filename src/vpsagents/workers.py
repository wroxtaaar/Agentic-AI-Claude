"""Worker adapters: the thing that actually edits code. Any CLI coding agent fits behind CommandWorker."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings
from .sandbox import RunResult, Sandbox, SandboxSpec


@dataclass
class WorkerTask:
    job_id: str
    prompt: str
    workdir: Path
    readonly: bool
    label: str = "work"
    model: str | None = None  # None = settings.llm.model


class Worker:
    def run(self, task: WorkerTask) -> RunResult:  # pragma: no cover - interface
        raise NotImplementedError


class CommandWorker(Worker):
    """Runs `settings.worker.command` inside the sandbox with the prompt and model-gateway env."""

    def __init__(self, settings: Settings, sandbox: Sandbox):
        if not settings.worker.command:
            raise ValueError("worker.command is not configured")
        self.settings, self.sandbox = settings, sandbox

    def run(self, task: WorkerTask) -> RunResult:
        llm = self.settings.llm
        with tempfile.TemporaryDirectory(prefix="vpsa-task-") as td:
            (Path(td) / "prompt.md").write_text(task.prompt, encoding="utf-8")
            subs = {"{prompt}": task.prompt, "{prompt_file}": "/task/prompt.md", "{workdir}": "/work"}
            cmd = []
            for part in self.settings.worker.command:
                for k, v in subs.items():
                    part = part.replace(k, v)
                cmd.append(part)
            key = llm.api_key()
            model = task.model or llm.model
            env = {"LLM_BASE_URL": llm.base_url, "LLM_MODEL": model,
                   "OPENAI_BASE_URL": llm.base_url, "OPENAI_API_BASE": llm.base_url, "OPENAI_MODEL": model}
            if key:
                env.update({"LLM_API_KEY": key, "OPENAI_API_KEY": key})
            spec = SandboxSpec(name=f"vpsa-{task.job_id}-{task.label}", command=cmd, workdir=task.workdir,
                               readonly=task.readonly, network="gateway", env=env,
                               extra_ro_mounts=[(Path(td), "/task")], timeout=self.settings.sandbox.timeout_seconds)
            return self.sandbox.run(spec)


class CallableWorker(Worker):
    """Test/dev worker: a python callable edits the workdir directly."""

    def __init__(self, fn: Callable[[WorkerTask], str]):
        self.fn = fn

    def run(self, task: WorkerTask) -> RunResult:
        try:
            return RunResult(0, self.fn(task) or "")
        except Exception as e:  # noqa: BLE001
            return RunResult(1, f"worker error: {e}")
