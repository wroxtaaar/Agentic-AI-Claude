import unittest

from vpsagents import states as S

from .helpers import Env
from .test_monitor_api import TOKEN, call
from vpsagents.api import create_app


class PerJobBudget(Env):
    tier = 2
    deploy = False

    def test_runaway_repair_loop_is_stopped(self):
        n = []
        self.write = lambda t: (n.append(1), f"wrong{len(n)}\n")[1]  # never passes verification
        self.settings.max_repairs = 5
        self.settings.max_agent_runs_per_job = 3
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.FAILED)
        self.assertIn("agent-run budget for this job exhausted (3/3)", j["error"])
        self.assertEqual(len(n), 3)  # the 4th run never started

    def test_normal_job_counts_work_plus_review(self):
        j = self.submit()
        self.run_all()
        self.assertEqual(self.job(j["id"])["data"]["agent_runs"], 2)  # one edit + one review
        self.assertEqual(self.engine.budget_status()["agent_runs_today"], 2)

    def test_review_out_of_budget_blocks_but_does_not_fail(self):
        self.settings.max_agent_runs_per_job = 1  # edit uses it all
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)  # change is verified; a human can still decide
        self.assertEqual(j["data"]["review_verdict"], "BLOCK")
        self.assertIn("review skipped", j["data"]["review"])
        self.assertEqual(self.checkout_text(), "old")


class DailyBudget(Env):
    tier = 2
    deploy = False

    def test_daily_cap_stops_new_jobs_and_reports_remaining(self):
        self.settings.max_agent_runs_per_day = 2
        a = self.submit()
        self.run_all()
        self.assertEqual(self.job(a["id"])["status"], S.AWAITING)  # used both runs
        b = self.submit("investigate", "q")
        self.run_all()
        b = self.job(b["id"])
        self.assertEqual(b["status"], S.FAILED)
        self.assertIn("daily agent-run budget reached", b["error"])
        self.assertNotIn("ro", self.calls)
        self.assertEqual(self.engine.budget_status()["remaining_today"], 0)

    def test_unlimited_by_default(self):
        self.assertIsNone(self.engine.budget_status()["max_agent_runs_per_day"])
        self.assertIsNone(self.engine.budget_status()["remaining_today"])

    def test_counter_is_atomic_increment(self):
        self.assertEqual(self.store.counter_incr("k"), 1)
        self.assertEqual(self.store.counter_incr("k", 4), 5)
        self.assertEqual(self.store.counter_get("k"), 5)
        self.assertEqual(self.store.counter_get("missing"), 0)

    def test_budget_endpoint_requires_auth(self):
        app = create_app(self.engine, TOKEN)
        self.assertEqual(call(app, "GET", "/budget", token=None)[0], 401)
        st, body = call(app, "GET", "/budget")
        self.assertEqual(st, 200)
        self.assertEqual(body["max_agent_runs_per_job"], 8)


if __name__ == "__main__":
    unittest.main()


class Tier3NoReview(Env):
    tier = 3
    deploy = False
    review = False

    def test_review_disabled_means_tier3_asks_before_applying(self):
        j = self.submit()
        self.run_all()
        j = self.job(j["id"])
        self.assertEqual(j["status"], S.AWAITING)
        self.assertEqual(j["stage"], "apply")
        self.assertEqual(self.calls, ["w0"])  # only the edit ran: one run saved
        self.assertEqual(self.checkout_text(), "old")
