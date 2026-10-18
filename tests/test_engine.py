import os
import unittest

from vpsagents import gitops
from vpsagents import states as S
from vpsagents.engine import PolicyDenied, SubmitError

from .helpers import Env, sh


class Tier2Flow(Env):
    tier = 2
    deploy = False

    def test_full_flow_waits_for_apply_approval_then_merges(self):
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["stage"], "apply")
        self.assertTrue(j["data"]["verified"])
        self.assertEqual(j["data"]["review_verdict"], "APPROVE")
        self.assertEqual(self.checkout_text(), "old")  # nothing touched the real checkout yet
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.pending()[0]["action"], "apply")
        self.assertTrue(any("approval needed" in m for m in self.notes.msgs))
        self.approve_pending()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE, j["error"])
        self.assertEqual(self.checkout_text(), "new")
        self.assertFalse(os.path.exists(j["data"]["worktree"]))  # cleaned up
        self.assertEqual(sh(self.repo, "git", "log", "-1", "--format=%an"), "vps-agents")

    def test_reject_ends_job_and_leaves_checkout(self):
        j = self.submit()
        self.run_all()
        self.engine.decide_approval(self.pending()[0]["id"], False, "me")
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.REJECTED)
        self.assertEqual(self.checkout_text(), "old")

    def test_approval_is_bound_to_exact_commit(self):
        j = self.submit()
        self.run_all()
        first = self.approve_pending()
        # someone changes the job branch after approval
        wt = self.job(j["id"])["data"]["worktree"]
        (self.root / "dummy").write_text("x")
        with open(os.path.join(wt, "x.txt"), "w") as f:
            f.write("new\nextra\n")
        sh(wt, "git", "commit", "-qam", "sneaky")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)  # old approval does NOT cover the new commit
        self.assertEqual(self.checkout_text(), "old")
        new = self.pending()[0]
        self.assertNotEqual(new["id"], first["id"])

    def test_dirty_checkout_refuses_merge(self):
        j = self.submit()
        self.run_all()
        self.approve_pending()
        (self.repo / "wip.txt").write_text("my uncommitted work")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("uncommitted", j["error"])
        self.assertTrue((self.repo / "wip.txt").exists())

    def test_wrong_branch_refuses_merge(self):
        j = self.submit()
        self.run_all()
        self.approve_pending()
        sh(self.repo, "git", "checkout", "-q", "-b", "feature")
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.FAILED)
        self.assertEqual(self.checkout_text(), "old")

    def test_base_moved_rebases_reverifies_and_reapproves(self):
        j = self.submit()
        self.run_all()
        first = self.approve_pending()
        (self.repo / "other.txt").write_text("user work\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "user commit")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["data"]["rebases"], 1)
        self.assertNotEqual(self.pending()[0]["id"], first["id"])
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)
        self.assertEqual(self.checkout_text(), "new")
        self.assertTrue((self.repo / "other.txt").exists())  # user commit preserved

    def test_base_moved_with_conflict_fails(self):
        j = self.submit()
        self.run_all()
        self.approve_pending()
        (self.repo / "x.txt").write_text("conflicting\n")
        sh(self.repo, "git", "commit", "-qam", "user edit same line")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("conflicts", j["error"])
        self.assertEqual(self.checkout_text(), "conflicting")

    def test_repair_loop_feeds_failure_back(self):
        seen = []

        def write(task):
            seen.append("Previous attempt failed" in task.prompt)
            return "new\n" if len(seen) > 1 else "wrong\n"
        self.write = write
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(seen, [False, True])
        self.assertEqual(j["data"]["repairs"], 1)
        self.assertTrue(j["data"]["verified"])
        self.assertEqual(j["status"], S.AWAITING)

    def test_repair_that_changes_nothing_fails_clearly(self):
        self.write = "wrong\n"
        j = self.submit()
        self.run_all()
        self.assertIn("verification failed", self.job(j["id"])["error"])

    def test_verification_exhausted_fails_and_keeps_worktree(self):
        n = []
        self.write = lambda t: (n.append(1), f"wrong{len(n)}\n")[1]
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("verification failed", j["error"])
        self.assertTrue(os.path.exists(j["data"]["worktree"]))
        self.assertEqual(self.checkout_text(), "old")

    def test_secret_in_diff_rejected(self):
        def write(task):
            (task.workdir / ".env").write_text("TOKEN=abc\n")
            return "x"
        self.write = lambda t: (write(t), "new\n")[1]
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("sensitive", j["error"])

    def test_symlink_in_diff_rejected(self):
        def write(task):
            os.symlink("/etc/passwd", task.workdir / "link")
            return "new\n"
        self.write = write
        j = self.submit()
        self.run_all()
        self.assertIn("symlink", self.job(j["id"])["error"])

    def test_no_changes_is_done_not_applied(self):
        self.write = lambda t: "old\n"
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE)
        self.assertEqual(j["data"]["result"], "worker made no changes")
        self.assertEqual(self.pending(), [])

    def test_readonly_job_reports_and_cleans_up(self):
        j = self.submit("investigate", "how does x work?")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE)
        self.assertIn("all good", j["data"]["report"])
        self.assertEqual(self.calls, ["ro"])
        self.assertFalse(os.path.exists(j["data"]["worktree"]))

    def test_secrets_in_worker_output_are_redacted(self):
        self.write = lambda t: "new\n"
        orig = self.engine.worker.fn
        self.engine.worker.fn = lambda t: (orig(t), "leak API_KEY=hunter2hunter2")[1]
        j = self.submit()
        self.run_all()
        self.assertNotIn("hunter2", self.job(j["id"])["data"]["worker_output"])

    def test_cancel_awaiting(self):
        j = self.submit()
        self.run_all()
        self.engine.cancel(j["id"])
        self.assertEqual(self.job(j["id"])["status"], S.CANCELLED)

    def test_expired_approval_fails_job(self):
        j = self.submit()
        self.run_all()
        self.store._conn().__enter__().execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.engine.expire_stale(), [j["id"]])
        self.assertEqual(self.job(j["id"])["status"], S.FAILED)

    def test_review_block_does_not_stop_tier2_but_is_shown(self):
        self.verdict = "VERDICT: BLOCK"
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["data"]["review_verdict"], "BLOCK")
        self.assertIn("review: BLOCK", self.pending()[0]["summary"])


class Tier1Flow(Env):
    tier = 1
    deploy = False

    def test_start_needs_approval_then_apply_needs_approval(self):
        j = self.submit()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.AWAITING)
        self.assertEqual(self.pending()[0]["action"], "start_change")
        self.assertEqual(self.calls, [])  # worker has not run
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.pending()[0]["action"], "apply")
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)
        self.assertEqual(self.checkout_text(), "new")


class Tier0Flow(Env):
    tier = 0

    def test_changes_denied_readonly_allowed(self):
        with self.assertRaises(PolicyDenied):
            self.submit()
        self.settings.projects[0].containers.append("web")
        with self.assertRaises(PolicyDenied):
            self.engine.submit("proj", "restart", "web")
        j = self.submit("security_review", "look for leaked secrets")
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)


class Tier3Flow(Env):
    tier = 3

    def test_auto_apply_and_deploy_no_humans(self):
        j = self.submit(deploy=True)
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE, j["error"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.checkout_text(), "new")
        self.assertIn("deployed", j["data"]["result"])

    def test_review_block_falls_back_to_approval(self):
        self.verdict = "VERDICT: BLOCK"
        j = self.submit(deploy=True)
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.AWAITING)
        self.assertEqual(self.pending()[0]["action"], "apply")
        self.assertEqual(self.checkout_text(), "old")

    def test_garbled_review_falls_back_to_approval(self):
        self.verdict = "I think it's great!"
        j = self.submit()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.AWAITING)

    def test_failed_deploy_rolls_back_checkout(self):
        self.healthy = False
        j = self.submit(deploy=True)
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertEqual(j["data"]["rollback"]["checkout"], "restored")
        self.assertEqual(self.checkout_text(), "old")
        self.assertTrue(any("failed" in m for m in self.notes.msgs))

    def test_rollback_refused_if_user_committed_in_between(self):
        self.healthy = False
        orig = self.engine._stage_deploy

        def user_commits_then_deploy(job, proj):
            (self.repo / "u.txt").write_text("u")
            sh(self.repo, "git", "add", "-A")
            sh(self.repo, "git", "commit", "-qm", "user commit during deploy")
            return orig(job, proj)
        self.engine._stage_deploy = user_commits_then_deploy
        j = self.submit(deploy=True)
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("not restored", j["data"]["rollback"]["checkout"])
        self.assertTrue((self.repo / "u.txt").exists())  # user's work untouched


class Tier3NoSafetyNet(Env):
    tier = 3
    verify = False

    def test_no_verify_commands_means_no_auto_apply(self):
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertFalse(j["data"]["verified"])


class Tier3NoHealth(Env):
    tier = 3
    health = False

    def test_no_healthcheck_means_deploy_needs_approval(self):
        j = self.submit(deploy=True)
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["stage"], "deploy")
        self.assertEqual(self.checkout_text(), "new")  # applied; deploy waits
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)


class SubmitValidation(Env):
    tier = 2

    def test_validation(self):
        with self.assertRaises(SubmitError):
            self.engine.submit("nope", "change", "t")
        with self.assertRaises(SubmitError):
            self.submit("bogus")
        with self.assertRaises(SubmitError):
            self.submit(task="  ")
        with self.assertRaises(SubmitError):
            self.submit(task="x" * 9000)
        with self.assertRaises(SubmitError):
            self.submit(role="sre")  # readonly role for a change job
        with self.assertRaises(SubmitError):
            self.submit("investigate", "q", deploy=True)
        with self.assertRaises(SubmitError):
            self.submit("restart", "not-a-registered-container")


class RestartFlow(Env):
    tier = 2

    def setUp(self):
        super().setUp()
        self.settings.projects[0].containers.append("web")
        self.restarted = []
        self.engine.container_state = lambda n: {"known": True, "status": "running", "health": "healthy"}
        from vpsagents import executors
        self._orig = executors.run_argv
        executors.run_argv = lambda argv, cwd, timeout: (self.restarted.append(argv) or {"ok": True, "returncode": 0, "output": ""})

    def tearDown(self):
        from vpsagents import executors
        executors.run_argv = self._orig
        super().tearDown()

    def test_restart_needs_approval_and_only_registered_container(self):
        j = self.submit("restart", "web")
        self.run_all()
        self.assertEqual(self.restarted, [])
        self.assertEqual(self.pending()[0]["action"], "restart")
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)
        self.assertEqual(self.restarted, [["docker", "restart", "web"]])


class GitSafety(Env):
    def test_merge_refuses_changed_branch(self):
        wt = gitops.create_worktree(self.repo, self.root / "wts", "abc", "main")
        (wt.path / "x.txt").write_text("new\n")
        h = gitops.commit_changes(wt, "m")
        with self.assertRaises(gitops.GitError):
            gitops.merge_into_checkout(self.repo, wt.branch, "0" * 40, "main")
        r = gitops.merge_into_checkout(self.repo, wt.branch, h, "main")
        self.assertEqual(r["merged_sha"], h)

    def test_rollback_guard(self):
        wt = gitops.create_worktree(self.repo, self.root / "wts", "abc", "main")
        (wt.path / "x.txt").write_text("new\n")
        h = gitops.commit_changes(wt, "m")
        m = gitops.merge_into_checkout(self.repo, wt.branch, h, "main")
        with self.assertRaises(gitops.GitError):
            gitops.rollback_checkout(self.repo, "main", "f" * 40, m["pre_sha"])  # HEAD isn't that commit
        gitops.rollback_checkout(self.repo, "main", m["merged_sha"], m["pre_sha"])
        self.assertEqual(self.checkout_text(), "old")


class Recovery(Env):
    def test_interrupted_jobs_failed_on_start(self):
        j = self.submit()
        self.store.claim_next()
        self.assertEqual(self.store.recover_interrupted(), [j["id"]])
        self.assertEqual(self.job(j["id"])["status"], S.FAILED)


if __name__ == "__main__":
    unittest.main()
