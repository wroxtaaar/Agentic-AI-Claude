"""The engine: drives a job through stages, asking policy at every gate.

Stages: start -> work -> verify -> review -> apply -> deploy   (change jobs)
        start -> work(read-only report)                       (investigate / incident / security_review)
        start -> restart                                      (restart jobs)

A job that needs a human parks in AWAITING with a pending approval bound to exact content (commit sha, container...).
Approving re-queues it; the same stage re-runs, finds the approval, consumes it (single use) and proceeds.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import executors, gitops, policy
from . import states as S
from .config import Project, Settings
from .notify import Notifier
from .redact import redact
from .roles import (DIAGNOSE_FORMAT, PLAN_FORMAT, REVIEW_FORMAT, build_prompt, load_roles, parse_verdict,
                    role_class)
from .sandbox import Sandbox, SandboxSpec
from .store import Store
from .workers import Worker, WorkerTask

log = logging.getLogger("vpsagents.engine")
MAX_TASK_CHARS = 8000
MAX_REBASES = 2


class SubmitError(ValueError):
    pass


class PolicyDenied(SubmitError):
    pass


class BudgetExceeded(Exception):
    pass


class _Stop(Exception):
    """Internal: job reached a resting state; stop stepping."""


class Engine:
    def __init__(self, settings: Settings, store: Store, sandbox: Sandbox, worker: Worker,
                 notifier: Notifier | None = None, *, probe=executors.http_ok, sleep=time.sleep,
                 container_state=executors.container_state):
        self.settings, self.store, self.sandbox, self.worker = settings, store, sandbox, worker
        self.notifier = notifier or Notifier()
        self.probe, self.sleep, self.container_state = probe, sleep, container_state
        self.roles, self.job_roles = load_roles(settings.roles_file)

    # ------------------------------------------------------------------ submit
    def submit(self, project: str, kind: str, task: str, *, deploy: bool = False, role: str | None = None) -> dict:
        try:
            proj = self.settings.project(project)
        except KeyError:
            raise SubmitError(f"unknown project: {project}") from None
        if kind not in S.ALL_KINDS:
            raise SubmitError(f"unknown job kind '{kind}' (one of {sorted(S.ALL_KINDS)})")
        task = (task or "").strip()
        if not task or len(task) > MAX_TASK_CHARS:
            raise SubmitError(f"task must be 1..{MAX_TASK_CHARS} characters")
        if kind in S.CHANGE_KINDS:
            if policy.decide(proj.tier, policy.START_CHANGE).mode == policy.DENY:
                raise PolicyDenied(f"project '{project}' is tier {proj.tier} (observe only); changes are not allowed")
        elif kind in S.OPS_KINDS:
            if task not in proj.containers:
                raise SubmitError(f"'{task}' is not a registered container of {project}")
            if policy.decide(proj.tier, policy.RESTART).mode == policy.DENY:
                raise PolicyDenied(f"project '{project}' is tier {proj.tier}; restarts are not allowed")
        if deploy:
            if kind != "change":
                raise SubmitError("deploy=true only applies to change jobs")
            if not proj.deploy_command:
                raise SubmitError(f"project '{project}' has no deploy_command")
        if kind in S.OPS_KINDS:
            role = None
        else:
            role = role or self.job_roles[kind]
            if role not in self.roles:
                raise SubmitError(f"unknown role '{role}'")
            want = "change" if kind in S.CHANGE_KINDS else "readonly"
            if self.roles[role].kind != want:
                raise SubmitError(f"role '{role}' is {self.roles[role].kind}; a {kind} job needs a {want} role")
        job = self.store.create_job(project=project, kind=kind, task=task, role=role, deploy=deploy)
        return job

    # ------------------------------------------------------------------ approvals
    def decide_approval(self, aid: str, approve: bool, by: str = "api") -> dict:
        ap = self.store.decide_approval(aid, approve, by)
        job = self.store.get_job(ap["job_id"])
        if job and job["status"] == S.AWAITING:
            if approve:
                self.store.transition(job["id"], S.QUEUED)
                self.store.event(job["id"], "approved", f"{ap['action']} approved by {by}")
            else:
                self.store.transition(job["id"], S.REJECTED, error=f"{ap['action']} rejected by {by}")
                self.store.event(job["id"], "rejected", f"{ap['action']} rejected by {by}")
                self._cleanup(job)
        return ap

    def cancel(self, jid: str) -> dict:
        job = self.store.get_job(jid)
        if not job:
            raise KeyError(jid)
        if job["status"] == S.RUNNING:
            raise S.InvalidTransition("job is running; wait for it to reach a resting state")
        out = self.store.transition(jid, S.CANCELLED)
        self._cleanup(job)
        return out

    def expire_stale(self) -> list[str]:
        """Awaiting jobs whose approvals all expired can never resume; fail them visibly."""
        failed = []
        for job in self.store.list_jobs(status=S.AWAITING, limit=500):
            pend = [a for a in self.store.list_approvals("pending") if a["job_id"] == job["id"]]
            if not pend:
                self.store.transition(job["id"], S.FAILED, error="approval expired")
                self.store.event(job["id"], "expired", "approval expired")
                failed.append(job["id"])
        return failed

    # ------------------------------------------------------------------ stepping
    def step(self, job: dict) -> None:
        """Run a claimed (RUNNING) job until it rests. Never raises."""
        jid = job["id"]
        try:
            for _ in range(60):
                j = self.store.get_job(jid)
                if j["status"] != S.RUNNING:
                    return
                handler = getattr(self, f"_stage_{j['stage']}")
                handler(j, self.settings.project(j["project"]))
            self._fail(jid, "stage loop limit reached")
        except _Stop:
            return
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", jid)
            try:
                self._fail(jid, f"internal error: {type(e).__name__}: {redact(str(e), 500)}")
            except Exception:  # noqa: BLE001
                pass

    # ---- budget
    @staticmethod
    def _day_key() -> str:
        return "agent_runs:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _strong_key() -> str:
        return "strong_runs:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def budget_status(self) -> dict:
        st = self.settings
        used = self.store.counter_get(self._day_key())
        strong = self.store.counter_get(self._strong_key())
        return {"strong_runs_today": strong, "max_strong_runs_per_day": st.max_strong_runs_per_day,"agent_runs_today": used, "max_agent_runs_per_day": st.max_agent_runs_per_day,
                "max_agent_runs_per_job": st.max_agent_runs_per_job,
                "remaining_today": None if st.max_agent_runs_per_day is None else max(0, st.max_agent_runs_per_day - used)}

    def _run_worker(self, job: dict, task: WorkerTask, role=None):
        """Every worker session goes through here so free-tier quota can't be burned by a loop.
        `role` picks the model: strong classes (architect/reviewer/analyst) vs the worker class."""
        jid = job["id"]
        if role is not None:
            cls = role_class(role)
            task.model = self.settings.llm.model_for(role.name, cls)
            if cls != "worker":
                cap = self.settings.max_strong_runs_per_day
                if cap is not None and self.store.counter_get(self._strong_key()) >= cap:
                    raise BudgetExceeded(f"daily strong-model budget reached ({cap}); raise max_strong_runs_per_day")
        n = self.store.get_job(jid)["data"].get("agent_runs", 0)
        if n >= self.settings.max_agent_runs_per_job:
            raise BudgetExceeded(f"agent-run budget for this job exhausted ({n}/{self.settings.max_agent_runs_per_job})")
        limit = self.settings.max_agent_runs_per_day
        if limit is not None and self.store.counter_get(self._day_key()) >= limit:
            raise BudgetExceeded(f"daily agent-run budget reached ({limit}); resubmit tomorrow or raise max_agent_runs_per_day")
        self.store.counter_incr(self._day_key())
        if role is not None and role_class(role) != "worker":
            self.store.counter_incr(self._strong_key())
        self.store.update_data(jid, agent_runs=n + 1)
        return self.worker.run(task)

    # ---- helpers
    def _fail(self, jid: str, msg: str):
        msg = redact(msg, 1500)
        job = self.store.get_job(jid)
        if job["status"] in S.TERMINAL:
            return
        self.store.transition(jid, S.FAILED, error=msg)
        self.store.event(jid, "failed", msg)
        self.notifier.send(f"❌ job {jid} ({job['project']}/{job['kind']}) failed: {msg[:500]}")
        raise _Stop

    def _done(self, jid: str, **result):
        self.store.update_data(jid, **result)
        self.store.transition(jid, S.DONE)
        self.store.event(jid, "done", "completed")
        job = self.store.get_job(jid)
        self.notifier.send(f"✅ job {jid} ({job['project']}/{job['kind']}) done")
        self._cleanup(job)
        raise _Stop

    def _advance(self, jid: str, stage: str):
        self.store.set_stage(jid, stage)

    def _gate(self, job: dict, proj: Project, action: str, bind: dict, summary: str, **pre) -> None:
        """Returns if the action may proceed now; otherwise parks/fails the job and raises _Stop."""
        dec = policy.downgrade_if_unsafe(policy.decide(proj.tier, action), action=action, **pre)
        if dec.mode == policy.DENY:
            self._fail(job["id"], f"policy denied {action}: {dec.reason}")
        if dec.mode == policy.AUTO:
            self.store.event(job["id"], "auto", f"{action} allowed automatically ({dec.reason})")
            return
        if self.store.consume_approval(job["id"], action, bind):
            self.store.event(job["id"], "gate", f"{action}: approval consumed")
            return
        ap = self.store.ensure_pending_approval(job["id"], action, summary, bind, self.settings.approval_ttl_minutes)
        self.store.transition(job["id"], S.AWAITING)
        self.store.event(job["id"], "awaiting", f"{action}: waiting for approval {ap['id']} ({dec.reason})")
        self.notifier.send(f"🛂 approval needed: {job['project']} / {action}\njob {job['id']}  approval {ap['id']}\n{summary[:700]}")
        raise _Stop

    def _token(self, proj: Project) -> str | None:
        import os
        return os.environ.get(proj.push_token_env) or None

    def _sync_remote(self, job: dict, proj: Project) -> None:
        if proj.publish != "push_main":
            return
        try:
            r = gitops.sync_with_remote(proj.path, proj.remote, proj.default_branch, self._token(proj))
        except gitops.GitError as e:
            self._fail(job["id"], f"cannot sync with {proj.remote}/{proj.default_branch}: {e}")
        if r != "in_sync":
            self.store.event(job["id"], "sync", f"local {proj.default_branch} {r.replace('_', ' ')} to {proj.remote}")

    def _cleanup(self, job: dict) -> None:
        d = job["data"]
        wt = d.get("worktree")
        if wt:
            try:
                proj = self.settings.project(job["project"])
                gitops.remove_worktree(proj.path, Path(wt), d.get("branch") or None)
            except Exception:  # noqa: BLE001
                log.warning("worktree cleanup failed for %s", job["id"])

    def _worktree(self, job: dict, proj: Project, *, branch: bool) -> gitops.Worktree:
        d = job["data"]
        if d.get("worktree") and Path(d["worktree"]).is_dir():
            return gitops.Worktree(Path(d["worktree"]), d.get("branch", ""), d["base_sha"])
        wt = gitops.create_worktree(proj.path, self.settings.worktree_root / proj.name, job["id"],
                                    proj.default_branch, branch=branch)
        self.store.update_data(job["id"], worktree=str(wt.path), branch=wt.branch, base_sha=wt.base_sha)
        return wt

    def _verify_hint(self, proj: Project) -> str:
        return "\n".join(" ".join(c) for c in proj.verify_commands) or "(none configured)"

    # ------------------------------------------------------------------ stages
    def _stage_start(self, job, proj):
        jid = job["id"]
        if job["kind"] in S.READONLY_KINDS:
            self._gate(job, proj, policy.START_READONLY, {"kind": job["kind"]}, "")
            self._advance(jid, "work")
        elif job["kind"] in S.CHANGE_KINDS:
            bind = {"task_sha": hashlib.sha256(job["task"].encode()).hexdigest()[:16]}
            self._gate(job, proj, policy.START_CHANGE, bind, f"Start change job on {proj.name}:\n{job['task'][:600]}")
            planning = self.settings.architect_plan and "architect" in self.roles and self.roles[job["role"]].kind == "change"
            self._advance(jid, "plan" if planning else "work")
        else:
            self._advance(jid, "restart")

    def _stage_plan(self, job, proj):
        """Strong model writes a plan; the weaker worker follows it. Planning is best-effort: if it fails or
        the budget is gone, the worker proceeds without one."""
        jid, d = job["id"], job["data"]
        if not d.get("worktree"):
            self._sync_remote(job, proj)
        wt = self._worktree(job, proj, branch=True)
        arch = self.roles["architect"]
        prompt = build_prompt(arch, task=job["task"], project_name=proj.name, extra=PLAN_FORMAT)
        try:
            res = self._run_worker(job, WorkerTask(jid, prompt, wt.path, readonly=True, label="plan"), arch)
            plan = redact(res.output, 6000) if res.ok else ""
        except BudgetExceeded as e:
            plan = ""
            self.store.event(jid, "plan", f"skipped: {e}")
        self.store.update_data(jid, plan=plan)
        self.store.event(jid, "plan", "written" if plan else "none; worker proceeds without a plan")
        self._advance(jid, "work")

    def _diagnose(self, job, proj, wt, verify_text: str) -> str:
        """Strong model explains a failed verification to the weaker worker. Best-effort."""
        if "architect" not in self.roles:
            return ""
        arch = self.roles["architect"]
        diff = gitops.diff_summary(wt)
        extra = (f"## Current diff\n```diff\n{diff['patch'][:12000]}\n```\n\n"
                 f"## Verification output\n{verify_text[-3000:]}\n\n{DIAGNOSE_FORMAT}")
        prompt = build_prompt(arch, task=job["task"], project_name=proj.name, extra=extra)
        try:
            res = self._run_worker(job, WorkerTask(job["id"], prompt, wt.path, readonly=True, label="diag"), arch)
        except BudgetExceeded as e:
            self.store.event(job["id"], "diagnose", f"skipped: {e}")
            return ""
        return redact(res.output, 3000) if res.ok else ""

    def _stage_work(self, job, proj):
        jid, d = job["id"], job["data"]
        role = self.roles[job["role"]]
        if role.kind == "readonly":
            wt = self._worktree(job, proj, branch=False)
            prompt = build_prompt(role, task=job["task"], project_name=proj.name)
            try:
                res = self._run_worker(job, WorkerTask(jid, prompt, wt.path, readonly=True, label="ro"), role)
            except BudgetExceeded as e:
                self._fail(jid, str(e))
            report = redact(res.output, 20000)
            if not res.ok:
                self.store.update_data(jid, report=report)
                self._fail(jid, f"worker failed (rc={res.returncode}{', timeout' if res.timed_out else ''})")
            self._done(jid, report=report)
        if not d.get("worktree"):
            self._sync_remote(job, proj)  # start from what is actually on the remote
        wt = self._worktree(job, proj, branch=True)
        plan = d.get("plan", "")
        extra = ("## Plan from the architect (follow it; if it is wrong, make the smallest correct change and say why)\n"
                 + plan) if plan else ""
        prompt = build_prompt(role, task=job["task"], project_name=proj.name, extra=extra,
                              verify_hint=self._verify_hint(proj), feedback=d.get("feedback", ""))
        try:
            res = self._run_worker(job, WorkerTask(jid, prompt, wt.path, readonly=False, label=f"w{d.get('repairs', 0)}"), role)
        except BudgetExceeded as e:
            self._fail(jid, str(e))
        self.store.update_data(jid, worker_output=redact(res.output, 6000))
        self.store.event(jid, "worker", f"rc={res.returncode}{' (timeout)' if res.timed_out else ''}")
        if not res.ok:
            self._fail(jid, f"worker failed (rc={res.returncode}{', timeout' if res.timed_out else ''})")
        try:
            new_head = gitops.commit_changes(wt, f"agent: {job['task'][:60]} [{jid}]")
        except gitops.UnsafeDiff as e:
            self._fail(jid, f"diff rejected: {e}")
        if new_head is None:
            if not d.get("head_sha"):
                self._done(jid, result="worker made no changes")
            self._fail(jid, "verification failed and the repair attempt made no changes")
        self.store.update_data(jid, head_sha=new_head)
        self._advance(jid, "verify")

    def _stage_verify(self, job, proj):
        jid, d = job["id"], job["data"]
        wt = self._worktree(job, proj, branch=True)
        if not proj.verify_commands:
            self.store.update_data(jid, verified=False, verify_log="no verify_commands configured")
            self.store.event(jid, "verify", "skipped: no verify_commands configured")
            self._advance(jid, "review")
            return
        log_parts, ok = [], True
        for i, cmd in enumerate(proj.verify_commands):
            spec = SandboxSpec(name=f"vpsa-{jid}-v{i}-{d.get('repairs', 0)}", command=cmd, workdir=wt.path,
                               network=proj.verify_network, timeout=self.settings.sandbox.timeout_seconds)
            r = self.sandbox.run(spec)
            log_parts.append(f"$ {' '.join(cmd)}\n[rc={r.returncode}{' timeout' if r.timed_out else ''}]\n{r.output[-3000:]}")
            if not r.ok:
                ok = False
                break
        # verification may leave build artifacts; the worktree is disposable, restore it
        gitops.git(wt.path, "reset", "--hard", "-q", "HEAD", check=False)
        gitops.git(wt.path, "clean", "-fdxq", check=False)
        text = redact("\n\n".join(log_parts), 6000)
        self.store.update_data(jid, verified=ok, verify_log=text)
        self.store.event(jid, "verify", "passed" if ok else "failed")
        if ok:
            self._advance(jid, "review")
            return
        repairs = d.get("repairs", 0)
        if repairs < self.settings.max_repairs:
            feedback = text[-3000:]
            esc = self.settings.escalate_after_failures
            if esc is not None and repairs + 1 >= esc:
                diag = self._diagnose(job, proj, wt, text)
                if diag:
                    feedback = "## Architect diagnosis (follow this)\n" + diag + "\n\n## Raw verification output\n" + feedback[-1500:]
                    self.store.event(jid, "diagnose", "architect diagnosis added")
            self.store.update_data(jid, repairs=repairs + 1, feedback=feedback)
            self._advance(jid, "work")
            return
        self._fail(jid, f"verification failed after {repairs} repair attempt(s)")

    def _stage_review(self, job, proj):
        jid = job["id"]
        wt = self._worktree(job, proj, branch=True)
        diff = gitops.diff_summary(wt)
        self.store.update_data(jid, diff=diff["patch"], diffstat=diff["stat"])
        if not self.settings.review_pass or "reviewer" not in self.roles:
            self.store.update_data(jid, review_verdict=None, review="review disabled")
            self._advance(jid, "apply")
            return
        extra = f"## Diff to review\n```diff\n{diff['patch']}\n```\n\n{REVIEW_FORMAT}"
        prompt = build_prompt(self.roles["reviewer"], task=f"Review the change made for this task:\n{job['task']}",
                              project_name=proj.name, extra=extra)
        try:
            res = self._run_worker(job, WorkerTask(jid, prompt, wt.path, readonly=True, label="rev"),
                                   self.roles["reviewer"])
            report = redact(res.output, 6000)
            verdict = parse_verdict(report) if res.ok else "BLOCK"
        except BudgetExceeded as e:
            # the change itself is done and verified: skip the review (fail safe = BLOCK) and let a human decide
            report, verdict = f"review skipped: {e}", "BLOCK"
        self.store.update_data(jid, review=report, review_verdict=verdict)
        self.store.event(jid, "review", verdict)
        self._advance(jid, "apply")

    def _stage_apply(self, job, proj):
        jid, d = job["id"], job["data"]
        wt = self._worktree(job, proj, branch=True)
        self._sync_remote(job, proj)
        if gitops.base_moved(proj.path, wt, proj.default_branch):
            if d.get("rebases", 0) >= MAX_REBASES:
                self._fail(jid, "base branch keeps moving; giving up after rebases")
            new_base = gitops.head(proj.path, proj.default_branch)
            if not gitops.rebase(wt, new_base):
                self._fail(jid, "base branch moved and the change conflicts; re-submit the task")
            self.store.update_data(jid, base_sha=new_base, head_sha=gitops.head(wt.path),
                                   rebases=d.get("rebases", 0) + 1, verified=False, feedback="")
            self.store.event(jid, "rebase", "rebased onto new base; re-verifying")
            self._advance(jid, "verify")
            return
        head_sha = gitops.head(wt.path)
        publishes = proj.publish == "push_main"
        push_note = (f"\n*** WILL PUSH to {proj.remote}/{proj.default_branch}: this triggers whatever deploys on push to that branch ***\n"
                     if publishes else "")
        summary = (f"Apply to {proj.name}@{proj.default_branch}{push_note}\nTask: {job['task'][:400]}\n\n{d.get('diffstat', '')}\n\n"
                   f"verified: {d.get('verified')}   review: {d.get('review_verdict')}   commit: {head_sha[:10]}")
        self._gate(job, proj, policy.APPLY, {"head_sha": head_sha, "push": publishes}, summary,
                   verified=bool(d.get("verified")), review_ok=d.get("review_verdict") == "APPROVE",  # no review => not ok => tier 3 asks
                   publishes=publishes, has_healthcheck=bool(proj.health_url))
        try:
            m = gitops.merge_into_checkout(proj.path, wt.branch, head_sha, proj.default_branch)
        except gitops.GitError as e:
            self._fail(jid, f"merge refused: {e}")
        self.store.update_data(jid, **m)
        self.store.event(jid, "applied", f"{m['pre_sha'][:10]} -> {m['merged_sha'][:10]}")
        if publishes:
            try:
                gitops.push_branch(proj.path, proj.remote, proj.default_branch, m["merged_sha"], self._token(proj))
            except gitops.GitError as e:
                try:  # never leave an unpushed merge behind: the next deploy reset would silently drop it
                    gitops.rollback_checkout(proj.path, proj.default_branch, m["merged_sha"], m["pre_sha"])
                    undo = "local merge rolled back"
                except gitops.GitError as e2:
                    undo = f"local merge NOT rolled back ({e2})"
                self._fail(jid, f"push to {proj.remote}/{proj.default_branch} failed: {e}; {undo}")
            self.store.update_data(jid, pushed=True)
            self.store.event(jid, "pushed", f"{m['merged_sha'][:10]} -> {proj.remote}/{proj.default_branch}")
            self.notifier.send(f"🚀 {proj.name}: pushed {m['merged_sha'][:10]} to {proj.remote}/{proj.default_branch}; your deploy workflow takes over")
        if job["deploy"]:
            self._advance(jid, "deploy")
            return
        self._done(jid, result=f"applied {m['merged_sha'][:10]}" + (" and pushed; your CI/deploy workflow takes over" if publishes else ""))

    def _stage_deploy(self, job, proj):
        jid, d = job["id"], job["data"]
        merged = d.get("merged_sha")
        self._gate(job, proj, policy.DEPLOY, {"merged_sha": merged},
                   f"Deploy {proj.name} at {str(merged)[:10]}\n{' '.join(proj.deploy_command or [])}",
                   has_healthcheck=bool(proj.health_url), deploy_ok=bool(merged))
        r = executors.deploy(proj, self.settings, probe=self.probe, sleep=self.sleep)
        self.store.update_data(jid, deploy_result=r)
        if r["ok"]:
            self._done(jid, result=f"deployed {str(merged)[:10]}" + ("" if proj.health_url else " (no health check configured)"))
        # failure: always try to restore the previous known-good state
        rb = {"attempted": False}
        if d.get("pre_sha") and merged:
            rb["attempted"] = True
            try:
                gitops.rollback_checkout(proj.path, proj.default_branch, merged, d["pre_sha"])
                rb["checkout"] = "restored"
                r2 = executors.deploy(proj, self.settings, probe=self.probe, sleep=self.sleep)
                rb["redeploy_ok"] = r2["ok"]
            except gitops.GitError as e:
                rb["checkout"] = f"not restored: {e}"
        self.store.update_data(jid, rollback=rb)
        self._fail(jid, f"deploy failed at {r.get('stage')}; rollback: {rb}")

    def _stage_restart(self, job, proj):
        jid = job["id"]
        container = job["task"].strip()
        self._gate(job, proj, policy.RESTART, {"container": container},
                   f"Restart container {container} ({proj.name})", has_healthcheck=True)
        r = executors.restart_container(proj, container, self.settings, state=self.container_state, sleep=self.sleep)
        self.store.update_data(jid, restart_result=r)
        if not r["ok"]:
            self._fail(jid, f"restart failed: {r['output']}")
        self._done(jid, result=f"restarted {container}")
