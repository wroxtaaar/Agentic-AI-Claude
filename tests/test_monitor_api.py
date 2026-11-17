import asyncio
import json
import unittest

from vpsagents import states as S
from vpsagents.api import create_app
from vpsagents.monitor import Monitor

from .helpers import Env

TOKEN = "t" * 32


def call(app, method, path, body=None, token=TOKEN, query=""):
    async def run():
        sent = []
        payload = json.dumps(body).encode() if body is not None else b""
        headers = [(b"content-type", b"application/json")]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": headers, "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("c", 1), "root_path": ""}
        got = False

        async def receive():
            nonlocal got
            if not got:
                got = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)
        await app(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, json.loads(data or b"null")
    return asyncio.run(run())


class ApiTests(Env):
    tier = 2
    deploy = False

    def setUp(self):
        super().setUp()
        self.app = create_app(self.engine, TOKEN)

    def test_auth_required(self):
        self.assertEqual(call(self.app, "GET", "/health", token=None)[0], 200)
        self.assertEqual(call(self.app, "GET", "/jobs", token=None)[0], 401)
        self.assertEqual(call(self.app, "GET", "/jobs", token="wrong" * 8)[0], 401)
        self.assertEqual(call(self.app, "POST", "/approvals/x/approve", token=None)[0], 401)

    def test_short_token_refused(self):
        with self.assertRaises(RuntimeError):
            create_app(self.engine, "short")

    def test_submit_approve_flow(self):
        st, job = call(self.app, "POST", "/jobs", {"project": "proj", "kind": "change", "task": "make x new"})
        self.assertEqual(st, 201)
        self.run_all()
        st, aps = call(self.app, "GET", "/approvals")
        self.assertEqual((st, len(aps)), (200, 1))
        st, ap = call(self.app, "POST", f"/approvals/{aps[0]['id']}/approve")
        self.assertEqual((st, ap["status"]), (200, "approved"))
        self.assertEqual(call(self.app, "POST", f"/approvals/{aps[0]['id']}/approve")[0], 409)  # already decided
        self.run_all()
        st, detail = call(self.app, "GET", f"/jobs/{job['id']}")
        self.assertEqual(detail["status"], S.DONE)
        self.assertTrue(detail["events"])
        self.assertEqual(self.checkout_text(), "new")

    def test_errors_map_to_http(self):
        self.assertEqual(call(self.app, "POST", "/jobs", {"project": "nope", "task": "t"})[0], 400)
        self.assertEqual(call(self.app, "GET", "/jobs/doesnotexist")[0], 404)
        self.assertEqual(call(self.app, "POST", "/approvals/nope/approve")[0], 404)
        self.assertEqual(call(self.app, "GET", "/jobs", query="limit=abc")[0], 400)

    def test_projects_listing_hides_paths(self):
        st, ps = call(self.app, "GET", "/projects")
        self.assertEqual(ps[0]["name"], "proj")
        self.assertNotIn("path", ps[0])

    def test_tier0_submit_is_403(self):
        self.settings.projects[0].tier = 0
        st, body = call(self.app, "POST", "/jobs", {"project": "proj", "kind": "change", "task": "t"})
        self.assertEqual(st, 403)


class MonitorTests(Env):
    tier = 1

    def setUp(self):
        super().setUp()
        proj = self.settings.projects[0]
        proj.containers.append("web")
        self.state = {"known": True, "status": "running", "health": "healthy"}
        self.url_ok = True
        self.mon = Monitor(self.engine, probe=lambda u: self.url_ok, state=lambda c: self.state,
                           logs=lambda c: "ERROR boom password=hunter2hunter2", stats=lambda c: {})

    def test_needs_two_consecutive_misses_then_opens_one_incident(self):
        self.state = {"known": True, "status": "exited", "health": None}
        self.assertEqual(self.mon.tick(), [])  # first miss: could be a blip
        opened = self.mon.tick()
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.mon.tick(), [])  # same problem inside cooldown: no duplicate
        job = self.job(opened[0])
        self.assertEqual(job["kind"], "incident")
        self.assertEqual(job["role"], "sre")
        self.assertIn("status=exited", job["task"])
        self.assertIn("ERROR boom", job["task"])
        self.assertTrue(any("incident" in m for m in self.notes.msgs))

    def test_incident_includes_exit_code_oom_and_restart_count(self):
        self.state = {"known": True, "status": "restarting", "health": None, "exit_code": 137, "oom_killed": True, "restarts": 2666}
        self.mon.tick()
        task = self.job(self.mon.tick()[0])["task"]
        self.assertIn("status=restarting", task)
        self.assertIn("restarts=2666", task)
        self.assertIn("last exit code=137", task)
        self.assertIn("OOM-killed", task)

    def test_crash_loop_with_changing_restart_count_opens_one_incident(self):
        n = 100
        for _ in range(12):
            n += 7  # a crash-looping container's restart count changes on every sample
            self.state = {"known": True, "status": "restarting", "health": None, "exit_code": 1, "restarts": n}
            self.mon.tick()
        incidents = [j for j in self.store.list_jobs(None, None, 50) if j["kind"] == "incident"]
        self.assertEqual(len(incidents), 1)

    def test_changing_cpu_and_memory_numbers_do_not_look_like_a_new_problem(self):
        from vpsagents.monitor import signature
        a = signature(["container web: high memory 91% (467MiB / 512MiB, threshold 90%)", "container web: high CPU 97% (threshold 95%)"])
        b = signature(["container web: high memory 96% (492MiB / 512MiB, threshold 90%)", "container web: high CPU 99% (threshold 95%)"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, signature(["container web: status=exited"]))
        self.assertNotEqual(signature(["container web: status=exited (exit code 1)"]), signature(["container web: status=exited (exit code 137)"]))

    def test_unresolved_problem_backs_off_up_to_a_day_and_recovery_resets_it(self):
        now = [1000.0]
        mon = Monitor(self.engine, probe=lambda u: True, state=lambda c: self.state, logs=lambda c: "log",
                      stats=lambda c: {}, clock=lambda: now[0])
        self.state = {"known": True, "status": "exited", "health": None, "exit_code": 1}
        mon.tick()
        self.assertEqual(len(mon.tick()), 1)                    # first incident
        opened = []
        for hours in range(1, 60):                              # then check every hour for 2.5 days
            now[0] += 3600
            opened += mon.tick()
        # cooldown 60min doubles each time, capped at 24h: re-opened at +1h, +3h, +7h, +15h, +31h, +55h
        self.assertEqual(len(opened), 6)
        # recovery resets the backoff
        self.state = {"known": True, "status": "running", "health": "healthy"}
        mon.tick()
        self.state = {"known": True, "status": "exited", "health": None, "exit_code": 1}
        mon.tick()
        now[0] += 10
        self.assertEqual(len(mon.tick()), 1)                    # a fresh incident right away

    def test_blip_does_not_alert(self):
        self.url_ok = False
        self.mon.tick()
        self.url_ok = True
        self.mon.tick()
        self.url_ok = False
        self.assertEqual(self.mon.tick(), [])

    def test_recovery_notifies(self):
        self.state = {"known": True, "status": "exited", "health": None}
        self.mon.tick(); self.mon.tick()
        self.state = {"known": True, "status": "running", "health": "healthy"}
        self.mon.tick()
        self.assertTrue(any("recovered" in m for m in self.notes.msgs))

    def test_incident_job_runs_read_only_even_at_tier0(self):
        self.settings.projects[0].tier = 0
        self.state = {"known": True, "status": "exited", "health": None}
        self.mon.tick()
        ids = self.mon.tick()
        self.run_all()
        j = self.job(ids[0])
        self.assertEqual(j["status"], S.DONE)
        self.assertEqual(self.calls, ["ro"])


if __name__ == "__main__":
    unittest.main()
