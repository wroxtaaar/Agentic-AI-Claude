"""End to end: the real CommandWorker + engine, with hostile worker commands. The container (here: a local shell in
the scratch copy) only ever touches a copy; the real worktree changes only through workcopy's checks."""
import os

from vpsagents import states as S
from vpsagents.config import LLMSettings, WorkerSettings
from vpsagents.workers import CommandWorker

from .helpers import Env, sh


class Isolation(Env):
    tier = 2
    deploy = False
    plan = True
    command = "echo new > x.txt"

    def setUp(self):
        super().setUp()
        self.settings.llm = LLMSettings(base_url="http://gw/v1", model="mid",
                                        models={"architect": "big", "reviewer": "big", "worker": "small"})
        self.settings.worker = WorkerSettings(command=["sh", "-c", self.command])
        self.engine.worker = CommandWorker(self.settings, self.engine.sandbox)

    def drive(self, jid, rounds=8):
        for _ in range(rounds):
            self.run_all()
            if self.job(jid)["status"] in S.TERMINAL:
                break
            if self.pending():
                self.approve_pending()

    def scratch_dir(self):
        return self.settings.data_dir / "scratch"

    def leftovers(self):
        d = self.scratch_dir()
        return list(d.iterdir()) if d.exists() else []


class HappyPath(Isolation):
    def test_job_completes_through_copies_and_leaves_nothing_behind(self):
        j = self.submit(task="make x new")
        self.drive(j["id"])
        job = self.job(j["id"])
        self.assertEqual(job["status"], S.DONE, job.get("error"))
        self.assertEqual(self.checkout_text(), "new")
        self.assertEqual(self.leftovers(), [])  # every scratch copy was removed
        notes = [e["message"] for e in self.store.events(j["id"]) if e["kind"] == "worker"]
        self.assertTrue(any("imported: 1 modified" in n for n in notes), notes)


class ReadOnlyCallsCannotWrite(Isolation):
    # plan/review run on the strong models; they "misbehave" and write a file. It must never reach the worktree.
    command = 'if [ "$LLM_MODEL" = small ]; then echo new > x.txt; else echo hi > leak.txt; fi'

    def test_only_the_worker_role_can_change_files(self):
        j = self.submit(task="make x new")
        for _ in range(8):
            self.run_all()
            if self.job(j["id"])["status"] == S.AWAITING and self.job(j["id"])["stage"] == "apply":
                break
            if self.pending():
                self.approve_pending()
        job = self.job(j["id"])
        wt = job["data"]["worktree"]
        self.assertFalse(os.path.exists(os.path.join(wt, "leak.txt")))
        self.assertEqual(open(os.path.join(wt, "x.txt")).read().strip(), "new")
        self.assertEqual(self.checkout_text(), "old")  # nothing applied before approval
        self.assertEqual(self.leftovers(), [])


class HostileWorkers(Isolation):
    def fails_with(self, text):
        j = self.submit(task="make x new")
        self.drive(j["id"])
        job = self.job(j["id"])
        self.assertEqual(job["status"], S.FAILED)
        self.assertIn("diff rejected", job["error"])
        self.assertIn(text, job["error"])
        self.assertEqual(self.checkout_text(), "old")
        self.assertEqual(self.leftovers(), [])
        wt = job["data"].get("worktree")
        if wt and os.path.isdir(wt):
            self.assertEqual(sh(wt, "git", "status", "--porcelain"), "")


class Symlink(HostileWorkers):
    command = "echo new > x.txt; ln -s /etc/passwd stolen"

    def test_rejected(self):
        self.fails_with("symlink")


class Secret(HostileWorkers):
    command = "echo new > x.txt; echo TOKEN=abc > .env"

    def test_rejected(self):
        self.fails_with("sensitive")


class NestedGit(HostileWorkers):
    command = "echo new > x.txt; mkdir sub && git init -q sub"

    def test_rejected(self):
        self.fails_with("nested .git")


class Fifo(HostileWorkers):
    command = "echo new > x.txt; mkfifo pipe"

    def test_rejected(self):
        self.fails_with("special file")


class Flood(HostileWorkers):
    command = "echo new > x.txt; i=0; while [ $i -lt 250 ]; do echo $i > f$i.txt; i=$((i+1)); done"

    def test_rejected(self):
        self.fails_with("too many changed files")


class TamperedScratchGit(Isolation):
    marker_name = "PWNED"
    command = "echo new > x.txt; printf '[core]\\n\\tfsmonitor = touch ../PWNED\\n' > .git/config; rm -f .git/HEAD"

    def test_hostile_scratch_repo_is_never_executed_or_trusted(self):
        j = self.submit(task="make x new")
        self.drive(j["id"])
        self.assertEqual(self.job(j["id"])["status"], S.DONE, self.job(j["id"]).get("error"))
        self.assertEqual(self.checkout_text(), "new")
        self.assertFalse((self.settings.data_dir / "PWNED").exists())
        self.assertFalse((self.settings.data_dir / "scratch" / "PWNED").exists())


class VerifyCannotTouchTheWorktree(Isolation):
    verify = True

    def setUp(self):
        super().setUp()
        self.settings.projects[0].verify_commands = [
            ["sh", "-c", "echo junk > verify-artifact.txt; rm -f x.txt.bak; grep -q new x.txt"]]

    def test_verify_artifacts_stay_in_the_copy(self):
        j = self.submit(task="make x new")
        for _ in range(8):
            self.run_all()
            job = self.job(j["id"])
            if job["status"] == S.AWAITING and job["stage"] == "apply":
                break
            if self.pending():
                self.approve_pending()
        job = self.job(j["id"])
        self.assertTrue(job["data"].get("verified"), job["data"].get("verify_log"))
        wt = job["data"]["worktree"]
        self.assertFalse(os.path.exists(os.path.join(wt, "verify-artifact.txt")))
        self.assertEqual(sh(wt, "git", "status", "--porcelain"), "")
        self.assertEqual(self.leftovers(), [])


class ReadonlyCommand(Env):
    def test_readonly_sessions_use_readonly_command_when_set(self):
        from vpsagents.sandbox import RunResult
        from vpsagents.workers import WorkerTask
        seen = []

        class Spy:
            def run(self, spec):
                seen.append(spec.command)
                return RunResult(0, "ok")
        self.settings.worker = WorkerSettings(command=["edit-cmd"], readonly_command=["ask-cmd", "{prompt_file}"])
        w = CommandWorker(self.settings, Spy())
        w.run(WorkerTask("j", "p", self.repo, readonly=True, label="rev"))
        w.run(WorkerTask("j", "p", self.repo, readonly=False, label="w0"))
        self.assertEqual(seen, [["ask-cmd", "/task/prompt.md"], ["edit-cmd"]])
        self.settings.worker = WorkerSettings(command=["edit-cmd"])
        seen.clear()
        CommandWorker(self.settings, Spy()).run(WorkerTask("j", "p", self.repo, readonly=True, label="rev"))
        self.assertEqual(seen, [["edit-cmd"]])  # falls back


if __name__ == "__main__":
    import unittest
    unittest.main()
