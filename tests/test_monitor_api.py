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
                           logs=lambda c: "ERROR boom password=hunter2hunter2")

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
