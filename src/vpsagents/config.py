"""Configuration: settings, project registry (with autonomy tiers)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Argv = list[str]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _check_argv(value: Argv | None, what: str) -> Argv | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{what} must be a non-empty list of strings (argv), never a shell string")
    return value


class Project(BaseModel):
    """One managed project. `tier` is the autonomy level (see policy.py)."""

    name: str
    path: Path
    tier: int = Field(ge=0, le=3, default=1)
    default_branch: str = "main"
    containers: list[str] = Field(default_factory=list)
    health_url: str | None = None
    deploy_command: Argv | None = None
    verify_commands: list[Argv] = Field(default_factory=list)
    # Network for verify runs. "none" is the safe default: dependencies must be baked into the image.
    verify_network: Literal["none", "bridge"] = "none"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("project name must match [A-Za-z0-9][A-Za-z0-9_.-]*")
        return v

    @field_validator("deploy_command")
    @classmethod
    def _deploy(cls, v):
        return _check_argv(v, "deploy_command")

    @field_validator("verify_commands")
    @classmethod
    def _verify(cls, v):
        for cmd in v:
            _check_argv(cmd, "verify_commands entry")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class SandboxSettings(BaseModel):
    mode: Literal["docker", "local"] = "docker"
    image: str = "vpsa-agent:latest"
    # Docker network that contains ONLY the model gateway (create it with --internal).
    gateway_network: str = "vpsa-gateway"
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 512
    timeout_seconds: int = Field(default=1800, ge=10, le=14400)
    user: str | None = None  # "uid:gid"; defaults to the control plane's own


class LLMSettings(BaseModel):
    base_url: str = "http://freellmapi:3001/v1"
    api_key_env: str = "FREELLMAPI_API_KEY"
    model: str = "auto"

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


class WorkerSettings(BaseModel):
    # argv template run INSIDE the sandbox. Placeholders: {prompt} {prompt_file} {workdir}
    command: Argv | None = None

    @field_validator("command")
    @classmethod
    def _cmd(cls, v):
        return _check_argv(v, "worker.command")


class Settings(BaseModel):
    data_dir: Path = Path("/var/lib/vps-agents")
    worktree_root: Path | None = None
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    roles_file: Path | None = None  # defaults to the packaged roles.yaml
    personas_dir: Path | None = None
    max_repairs: int = Field(default=2, ge=0, le=5)
    review_pass: bool = True
    approval_ttl_minutes: int = Field(default=720, ge=1)
    max_concurrent_jobs: int = Field(default=2, ge=1, le=8)
    deploy_timeout_seconds: int = 900
    health_timeout_seconds: int = 90
    projects: list[Project] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        names = [p.name for p in self.projects]
        if len(names) != len(set(names)):
            raise ValueError("duplicate project names")
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.worktree_root = (Path(self.worktree_root).expanduser().resolve()
                              if self.worktree_root else self.data_dir / "worktrees")
        return self

    def project(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        raise KeyError(f"unknown project: {name}")


def load_settings(path: str | Path) -> Settings:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Settings(**data)
