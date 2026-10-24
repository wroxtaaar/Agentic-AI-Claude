import inspect
import unittest
from pathlib import Path

from vpsagents import gitops
from vpsagents import states as S
from vpsagents.config import Project

from .helpers import Env, sh


class PublishEnv(Env):
    tier = 2
    deploy = False

    def setUp(self):
        super().setUp()
        self.remote = self.root / "remote.git"
        sh(self.root, "git", "clone", "-q", "--bare", str(self.repo), str(self.remote))
        sh(self.repo, "git", "remote", "add", "origin", str(self.remote))
        sh(self.repo, "git", "fetch", "-q", "origin")
        self.settings.projects[0].publish = "push_main"

    def remote_head(self):
        return sh(self.remote, "git", "rev-parse", "main")

    def remote_text(self):
        return sh(self.remote, "git", "show", "main:x.txt").strip()

    def other_clone_commit(self, name="other", fname="other.txt", text="from someone else\n"):
        d = self.root / name
        if not d.exists():
            sh(self.root, "git", "clone", "-q", str(self.remote), str(d))
        (d / fname).write_text(text)
        sh(d, "git", "add", "-A")
        sh(d, "git", "commit", "-qm", "someone else pushed")
        sh(d, "git", "push", "-q", "origin", "main")

    def local_head(self):
        return sh(self.repo, "git", "rev-parse", "HEAD")


class PublishFlow(PublishEnv):
    def test_approved_change_is_merged_and_pushed(self):
        j = self.submit()
        self.run_all()
        ap = self.pending()[0]
        self.assertIn("WILL PUSH to origin/main", ap["summary"])
        self.assertEqual(self.remote_text(), "old")  # nothing pushed before approval
        self.engine.decide_approval(ap["id"], True, "me")
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE, j["error"])
        self.assertTrue(j["data"]["pushed"])
        self.assertEqual(self.remote_text(), "new")
        self.assertEqual(self.remote_head(), self.local_head())
        self.assertIn("pushed", j["data"]["result"])
        self.assertTrue(any("deploy workflow takes over" in m for m in self.notes.msgs))

    def test_remote_ahead_is_fast_forwarded_before_work(self):
        self.other_clone_commit()
        j = self.submit()
        self.run_all()
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)
        self.assertTrue((self.repo / "other.txt").exists())  # included the other commit
        self.assertEqual(self.remote_head(), self.local_head())
        self.assertTrue(any(e["kind"] == "sync" for e in self.store.events(j["id"])))

    def test_remote_moves_during_review_rebases_and_asks_again(self):
        j = self.submit()
        self.run_all()
        first = self.approve_pending()
        self.other_clone_commit()  # lands on the remote after approval
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["data"]["rebases"], 1)
        self.assertNotEqual(self.pending()[0]["id"], first["id"])
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], S.DONE)
        self.assertEqual(self.remote_text(), "new")

    def test_push_rejected_rolls_back_local_merge(self):
        j = self.submit()
        self.run_all()
        self.approve_pending()
        pre = self.local_head()
        orig = gitops.push_branch

        def racing_push(repo, remote, branch, sha, token=None):
            self.other_clone_commit(name="racer", fname="race.txt")  # remote moves right before our push
            return orig(repo, remote, branch, sha, token)
        gitops.push_branch = racing_push
        try:
            self.run_all()
        finally:
            gitops.push_branch = orig
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("push to origin/main failed", j["error"])
        self.assertIn("local merge rolled back", j["error"])
        self.assertEqual(self.local_head(), pre)  # no unpushed merge left to be wiped by a later deploy reset
        self.assertEqual(self.checkout_text(), "old")
        self.assertNotEqual(self.remote_text(), "new")  # we never overwrote the remote

    def test_local_unpushed_commits_are_never_touched(self):
        (self.repo / "mine.txt").write_text("my unpushed work\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "my local commit")
        mine = self.local_head()
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("not on origin/main", j["error"])
        self.assertEqual(self.local_head(), mine)
        self.assertEqual(self.calls, [])  # the worker never ran

    def test_diverged_local_fails(self):
        (self.repo / "mine.txt").write_text("x\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-qm", "local")
        self.other_clone_commit()
        j = self.submit()
        self.run_all()
        self.assertIn("diverged", self.job(j["id"])["error"])

    def test_dirty_checkout_behind_remote_fails_without_touching_files(self):
        self.other_clone_commit()
        (self.repo / "wip.txt").write_text("uncommitted\n")
        j = self.submit()
        self.run_all()
        self.assertIn("uncommitted", self.job(j["id"])["error"])
        self.assertEqual((self.repo / "wip.txt").read_text(), "uncommitted\n")

    def test_non_publish_projects_never_touch_the_remote(self):
        self.settings.projects[0].publish = "none"
        before = self.remote_head()
        self.submit()
        self.run_all()
        self.approve_pending()
        self.run_all()
        self.assertEqual(self.remote_head(), before)
        self.assertEqual(self.checkout_text(), "new")


class PublishTier3(PublishEnv):
    tier = 3

    def test_auto_push_when_verified_reviewed_and_healthcheck(self):
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.DONE, j["error"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.remote_text(), "new")


class PublishTier3NoHealth(PublishEnv):
    tier = 3
    health = False

    def test_no_healthcheck_means_a_human_approves_the_push(self):
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(self.remote_text(), "old")


class AuthAndSafety(Env):
    def test_token_goes_via_scoped_env_not_argv(self):
        import base64
        sh(self.repo, "git", "remote", "add", "origin", "https://github.com/wroxtaaar/terabox-telegram-bot.git")
        env = gitops.auth_env(self.repo, "origin", "ghp_SECRETSECRETSECRET1234")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        value = env["GIT_CONFIG_VALUE_0"]
        self.assertTrue(value.startswith("Authorization: Basic "))
        self.assertEqual(base64.b64decode(value.split()[-1]).decode(), "x-access-token:ghp_SECRETSECRETSECRET1234")

    def test_no_token_or_ssh_remote_adds_nothing(self):
        sh(self.repo, "git", "remote", "add", "web", "https://github.com/a/b.git")
        sh(self.repo, "git", "remote", "add", "ssh", "ssh://git@example.com/a/b.git")
        self.assertEqual(gitops.auth_env(self.repo, "web", None), {})
        self.assertEqual(gitops.auth_env(self.repo, "ssh", "tok"), {})

    def test_secret_is_scrubbed_from_git_errors(self):
        with self.assertRaises(gitops.GitError) as cm:
            gitops.git(self.repo, "checkout", "tokSECRET123", secret="tokSECRET123")
        self.assertNotIn("tokSECRET123", str(cm.exception))

    def test_push_is_never_forced(self):
        src = inspect.getsource(gitops.push_branch)
        self.assertNotIn("--force", src)
        self.assertNotIn("+refs", src)
        self.assertNotIn('f"+', src)

    def test_config_rejects_dangerous_git_names(self):
        for bad in ("-evil", "a..b", "--upload-pack=x", "x/"):
            with self.assertRaises(ValueError):
                Project(name="p", path=Path("."), remote=bad)
            with self.assertRaises(ValueError):
                Project(name="p", path=Path("."), default_branch=bad)
        Project(name="p", path=Path("."), remote="origin", default_branch="release/1.0")


if __name__ == "__main__":
    unittest.main()


class CheckWarnings(PublishEnv):
    def test_publish_problems(self):
        import os
        from vpsagents.__main__ import publish_problems
        self.assertEqual(publish_problems(self.settings), [])  # local-path remote needs no token
        sh(self.repo, "git", "remote", "set-url", "origin", "https://github.com/x/y.git")
        os.environ.pop("VPSA_GITHUB_TOKEN", None)
        probs = publish_problems(self.settings)
        self.assertTrue(any("needs a token" in p for p in probs))
        os.environ["VPSA_GITHUB_TOKEN"] = "x"
        try:
            self.assertEqual(publish_problems(self.settings), [])
        finally:
            os.environ.pop("VPSA_GITHUB_TOKEN", None)
        self.settings.projects[0].remote = "nope"
        self.assertTrue(any("does not exist" in p for p in publish_problems(self.settings)))
        self.settings.projects[0].remote = "origin"
        self.settings.projects[0].deploy_command = ["true"]
        self.assertTrue(any("double deploy" in p for p in publish_problems(self.settings)))
