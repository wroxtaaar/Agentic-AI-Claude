import unittest

from vpsagents import policy
from vpsagents import states as S
from vpsagents.redact import is_sensitive_path, redact
from vpsagents.roles import PKG, build_prompt, load_roles, parse_verdict, trim_persona
from vpsagents.sandbox import SandboxSpec, docker_args
from vpsagents.config import Settings, SandboxSettings, Project
from vpsagents.store import Store
from pathlib import Path
import tempfile


class PolicyTests(unittest.TestCase):
    def test_matrix(self):
        d = policy.decide
        self.assertEqual(d(0, policy.START_CHANGE).mode, policy.DENY)
        self.assertEqual(d(1, policy.START_CHANGE).mode, policy.APPROVAL)
        self.assertEqual(d(2, policy.START_CHANGE).mode, policy.AUTO)
        for tier in (0, 1, 2, 3):
            self.assertEqual(d(tier, policy.START_READONLY).mode, policy.AUTO)
        for act in (policy.APPLY, policy.DEPLOY, policy.RESTART):
            self.assertEqual(d(0, act).mode, policy.DENY)
            self.assertEqual(d(1, act).mode, policy.APPROVAL)
            self.assertEqual(d(2, act).mode, policy.APPROVAL)
            self.assertEqual(d(3, act).mode, policy.AUTO)

    def test_never_and_unknown(self):
        for a in policy.NEVER:
            for t in range(4):
                self.assertEqual(policy.decide(t, a).mode, policy.DENY)
        self.assertEqual(policy.decide(3, "rm_rf").mode, policy.DENY)
        self.assertEqual(policy.decide(9, policy.APPLY).mode, policy.DENY)

    def test_downgrade(self):
        auto = policy.decide(3, policy.APPLY)
        self.assertEqual(policy.downgrade_if_unsafe(auto, action=policy.APPLY, verified=False).mode, policy.APPROVAL)
        self.assertEqual(policy.downgrade_if_unsafe(auto, action=policy.APPLY, review_ok=False).mode, policy.APPROVAL)
        self.assertEqual(policy.downgrade_if_unsafe(auto, action=policy.APPLY).mode, policy.AUTO)
        dep = policy.decide(3, policy.DEPLOY)
        self.assertEqual(policy.downgrade_if_unsafe(dep, action=policy.DEPLOY, has_healthcheck=False).mode, policy.APPROVAL)
        # downgrade never upgrades
        appr = policy.decide(2, policy.APPLY)
        self.assertEqual(policy.downgrade_if_unsafe(appr, action=policy.APPLY).mode, policy.APPROVAL)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")
        self.j = self.s.create_job(project="p", kind="change", task="t")

    def test_transitions_validated(self):
        with self.assertRaises(S.InvalidTransition):
            self.s.transition(self.j["id"], S.DONE)  # queued -> done not allowed
        self.s.transition(self.j["id"], S.RUNNING)
        self.s.transition(self.j["id"], S.DONE)
        with self.assertRaises(S.InvalidTransition):
            self.s.transition(self.j["id"], S.RUNNING)  # terminal

    def test_claim_one_running_per_project(self):
        j2 = self.s.create_job(project="p", kind="change", task="t2")
        j3 = self.s.create_job(project="q", kind="change", task="t3")
        a = self.s.claim_next()
        self.assertEqual(a["id"], self.j["id"])
        b = self.s.claim_next()
        self.assertEqual(b["id"], j3["id"])  # p is busy, q is free
        self.assertIsNone(self.s.claim_next())
        self.assertEqual(self.s.get_job(j2["id"])["status"], S.QUEUED)

    def test_approval_single_use_and_binding(self):
        jid = self.j["id"]
        ap = self.s.ensure_pending_approval(jid, "apply", "s", {"head_sha": "aaa"}, 60)
        self.assertEqual(self.s.ensure_pending_approval(jid, "apply", "s", {"head_sha": "aaa"}, 60)["id"], ap["id"])
        self.assertFalse(self.s.consume_approval(jid, "apply", {"head_sha": "aaa"}))  # not approved yet
        self.s.decide_approval(ap["id"], True)
        self.assertFalse(self.s.consume_approval(jid, "apply", {"head_sha": "bbb"}))  # different content
        self.assertFalse(self.s.consume_approval(jid, "deploy", {"head_sha": "aaa"}))  # different action
        self.assertTrue(self.s.consume_approval(jid, "apply", {"head_sha": "aaa"}))
        self.assertFalse(self.s.consume_approval(jid, "apply", {"head_sha": "aaa"}))  # single use

    def test_expired_and_double_decision(self):
        ap = self.s.ensure_pending_approval(self.j["id"], "apply", "s", {}, -1)
        with self.assertRaises(S.InvalidTransition):
            self.s.decide_approval(ap["id"], True)
        ap2 = self.s.ensure_pending_approval(self.j["id"], "deploy", "s", {}, 60)
        self.s.decide_approval(ap2["id"], False)
        with self.assertRaises(S.InvalidTransition):
            self.s.decide_approval(ap2["id"], True)

    def test_recover_interrupted(self):
        self.s.transition(self.j["id"], S.RUNNING)
        self.assertEqual(self.s.recover_interrupted(), [self.j["id"]])
        self.assertEqual(self.s.get_job(self.j["id"])["status"], S.FAILED)


class RedactTests(unittest.TestCase):
    def test_redact(self):
        t = redact("API_KEY=abc123secret\nAuthorization: Bearer abcdefghij1234\nurl=postgres://u:hunter2@db/x\nkey sk-abcdefghijklmnopqrstuv ghp_abcdefghijklmnopqrstuvwx")
        for leaked in ("abc123secret", "abcdefghij1234", "hunter2", "sk-abcdefghijklmnop", "ghp_abcdef"):
            self.assertNotIn(leaked, t)

    def test_truncate_keeps_tail(self):
        t = redact("a" * 100 + "END", limit=10)
        self.assertTrue(t.endswith("END"))

    def test_sensitive_paths(self):
        for p in (".env", "a/.env.production", "k/id_rsa", "x.pem", ".ssh/config", "a/.aws/credentials", ".npmrc"):
            self.assertTrue(is_sensitive_path(p), p)
        for p in (".env.example", "src/app.py", "README.md", "environment.py"):
            self.assertFalse(is_sensitive_path(p), p)


class RoleTests(unittest.TestCase):
    def test_all_personas_exist_and_trim(self):
        roles, _ = load_roles()
        for r in roles.values():
            for f in r.personas:
                text = (PKG / "personas" / f).read_text(encoding="utf-8")
                out = trim_persona(text)
                self.assertTrue(out.strip(), f)
                self.assertLessEqual(len(out), 5100, f)
                self.assertNotIn("Identity & Memory", out, f)
                self.assertNotIn("Success Metrics", out, f)
                self.assertFalse(out.startswith("---"), f)

    def test_prompt_has_constraints_and_task(self):
        roles, _ = load_roles()
        p = build_prompt(roles["engineer"], task="fix the bug", project_name="proj", feedback="tests failed")
        self.assertIn("Operating constraints", p)
        self.assertIn("fix the bug", p)
        self.assertIn("Previous attempt failed", p)
        ro = build_prompt(roles["sre"], task="triage", project_name="proj")
        self.assertIn("READ-ONLY", ro)

    def test_verdict_fails_safe(self):
        self.assertEqual(parse_verdict("ok\nVERDICT: APPROVE"), "APPROVE")
        self.assertEqual(parse_verdict("VERDICT: APPROVE\nbut wait\nVERDICT: BLOCK"), "BLOCK")
        self.assertEqual(parse_verdict("I approve this"), "BLOCK")
        self.assertEqual(parse_verdict(""), "BLOCK")
        self.assertEqual(parse_verdict("VERDICT: APPROVE with caveats"), "BLOCK")


class SandboxArgTests(unittest.TestCase):
    def test_docker_args_hardened(self):
        with tempfile.TemporaryDirectory() as td:
            st = Settings(data_dir=td, sandbox=SandboxSettings(image="img:1", gateway_network="gw"))
            spec = SandboxSpec(name="n", command=["agent", "go"], workdir=Path(td) / "wt", network="gateway",
                               extra_ro_mounts=[(Path(td) / "task", "/task")])
            a = docker_args(st, spec, "/tmp/envfile")
            joined = " ".join(a)
            self.assertNotIn("docker.sock", joined)
            self.assertNotIn("--privileged", a)
            self.assertIn("--cap-drop", a)
            self.assertIn("no-new-privileges", a)
            self.assertIn("--read-only", a)
            self.assertEqual(a[a.index("--network") + 1], "gw")
            self.assertEqual(a.count("-v"), 2)  # worktree + task dir only
            self.assertIn("--env-file", a)
            self.assertEqual(a[-3:], ["img:1", "agent", "go"])

    def test_readonly_and_no_network(self):
        with tempfile.TemporaryDirectory() as td:
            st = Settings(data_dir=td)
            a = docker_args(st, SandboxSpec(name="n", command=["c"], workdir=Path(td), readonly=True, network="none"))
            self.assertIn(f"{td}:/work:ro", a)
            self.assertEqual(a[a.index("--network") + 1], "none")

    def test_secrets_not_in_argv(self):
        with tempfile.TemporaryDirectory() as td:
            st = Settings(data_dir=td)
            spec = SandboxSpec(name="n", command=["c"], workdir=Path(td), env={"LLM_API_KEY": "supersecret"})
            self.assertNotIn("supersecret", " ".join(docker_args(st, spec, "/tmp/e")))


class ConfigTests(unittest.TestCase):
    def test_argv_only(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                Project(name="p", path=Path(td), deploy_command="docker compose up -d")
            with self.assertRaises(ValueError):
                Project(name="p", path=Path(td), verify_commands=[["ok"], "pytest"])
            with self.assertRaises(ValueError):
                Project(name="../evil", path=Path(td))
            with self.assertRaises(ValueError):
                Project(name="p", path=Path(td), tier=4)
            with self.assertRaises(ValueError):
                Settings(projects=[Project(name="a", path=Path(td)), Project(name="a", path=Path(td))])


if __name__ == "__main__":
    unittest.main()


class RedactMoreTests(unittest.TestCase):
    def test_midline_and_json_forms(self):
        for raw, secret in [("starting with API_KEY=hunter2hunter2 ok", "hunter2hunter2"),
                            ('{"api_key": "abcd1234efgh"}', "abcd1234efgh"),
                            ("password: s3cr3tvalue", "s3cr3tvalue"),
                            ("export GITHUB_TOKEN=zzzz9999yyyy", "zzzz9999yyyy")]:
            self.assertNotIn(secret, redact(raw), raw)
