from tests.helpers import Env, VERIFY_OK
from vpsagents.config import LLMSettings


class Models(Env):
    plan = True
    escalate = 2

    def setUp(self):
        super().setUp()
        self.settings.llm = LLMSettings(base_url="http://gw/v1", model="mid",
                                        models={"architect": "big", "reviewer": "big2", "worker": "small",
                                                "security": "sec"})
        self.seen = []
        orig = self.engine.worker.fn if hasattr(self.engine.worker, "fn") else None
        self.engine.worker = type(self.engine.worker)(lambda t: (self.seen.append((t.label, t.model)), self._worker(t))[1])

    def test_models_by_role_class(self):
        j = self.submit()
        self.run_all()
        self.assertEqual(self.job(j["id"])["status"], "awaiting_approval")
        self.assertEqual(self.seen, [("plan", "big"), ("w0", "small"), ("rev", "big2")])
        self.assertIn("Files: x.txt", self.job(j["id"])["data"]["plan"])

    def test_role_name_beats_class_and_default(self):
        m = self.settings.llm
        self.assertEqual(m.model_for("security", "analyst"), "sec")
        self.assertEqual(m.model_for("sre", "analyst"), "mid")
        self.assertEqual(m.model_for("engineer", "worker"), "small")

    def test_plan_reaches_worker_prompt(self):
        prompts = []
        orig = self.engine._run_worker
        self.engine._run_worker = lambda job, task, role=None: (prompts.append((task.label, task.prompt)), orig(job, task, role))[1]
        self.submit()
        self.run_all()
        w = dict(prompts)["w0"]
        self.assertIn("Plan from the architect", w)
        self.assertIn("Steps: write 'new'", w)

    def test_escalation_after_two_failed_verifications(self):
        self.write = lambda task: f"wrong-{task.label}\n"
        j = self.submit()
        self.run_all()
        self.assertIn("diag", self.calls)
        self.assertIn("Architect diagnosis", self.job(j["id"])["data"].get("feedback", ""))

    def test_strong_budget_skips_plan_but_job_continues(self):
        self.settings.max_strong_runs_per_day = 1
        j = self.submit()
        self.run_all()
        d = self.job(j["id"])["data"]
        self.assertEqual(self.calls[:2], ["plan", "w0"])  # plan used the one strong run
        self.assertEqual(d["review_verdict"], "BLOCK")  # review had no strong run left -> fail safe
        self.assertEqual(self.engine.budget_status()["strong_runs_today"], 1)

    def test_planning_can_be_disabled(self):
        self.settings.architect_plan = False
        self.submit()
        self.run_all()
        self.assertNotIn("plan", self.calls)
