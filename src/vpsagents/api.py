"""HTTP API (Starlette). Bind to localhost / a private network; every route except /health needs the bearer token."""
from __future__ import annotations

import hmac
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import states as S
from .engine import Engine, PolicyDenied, SubmitError


def create_app(engine: Engine, token: str | None = None) -> Starlette:
    token = token or os.environ.get("VPSA_API_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("VPSA_API_TOKEN must be set to a random string of at least 24 characters")
    store = engine.store

    def guarded(fn):
        async def wrapper(request: Request):
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await fn(request)
            except PolicyDenied as e:
                return JSONResponse({"error": str(e)}, status_code=403)
            except SubmitError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except KeyError as e:
                return JSONResponse({"error": f"not found: {e.args[0] if e.args else ''}"}, status_code=404)
            except S.InvalidTransition as e:
                return JSONResponse({"error": str(e)}, status_code=409)
        return wrapper

    async def health(_):
        return JSONResponse({"status": "ok"})

    async def projects(_):
        return JSONResponse([{"name": p.name, "tier": p.tier, "containers": p.containers,
                              "deployable": bool(p.deploy_command), "verify": bool(p.verify_commands)}
                             for p in engine.settings.projects])

    async def budget(_):
        return JSONResponse(engine.budget_status())

    async def submit(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise SubmitError("body must be JSON") from None
        if not isinstance(body, dict):
            raise SubmitError("body must be a JSON object")
        job = engine.submit(str(body.get("project", "")), str(body.get("kind", "change")), str(body.get("task", "")),
                            deploy=bool(body.get("deploy", False)), role=body.get("role"))
        return JSONResponse(job, status_code=201)

    async def jobs(request: Request):
        q = request.query_params
        try:
            limit = int(q.get("limit", 50))
        except ValueError:
            raise SubmitError("limit must be an integer") from None
        return JSONResponse(store.list_jobs(q.get("status"), q.get("project"), limit))

    async def job(request: Request):
        jid = request.path_params["jid"]
        j = store.get_job(jid)
        if not j:
            raise KeyError(jid)
        return JSONResponse({**j, "events": store.events(jid),
                             "approvals": [a for a in store.list_approvals(None) if a["job_id"] == jid]})

    async def cancel(request: Request):
        return JSONResponse(engine.cancel(request.path_params["jid"]))

    async def approvals(request: Request):
        status = request.query_params.get("status", "pending")
        return JSONResponse(store.list_approvals(None if status == "all" else status))

    def decision(approve: bool):
        async def h(request: Request):
            aid = request.path_params["aid"]
            if not store.get_approval(aid):
                raise KeyError(aid)
            return JSONResponse(engine.decide_approval(aid, approve, by="api"))
        return h

    routes = [
        Route("/health", health),
        Route("/projects", guarded(projects)),
        Route("/budget", guarded(budget)),
        Route("/jobs", guarded(submit), methods=["POST"]),
        Route("/jobs", guarded(jobs), methods=["GET"]),
        Route("/jobs/{jid}", guarded(job)),
        Route("/jobs/{jid}/cancel", guarded(cancel), methods=["POST"]),
        Route("/approvals", guarded(approvals)),
        Route("/approvals/{aid}/approve", guarded(decision(True)), methods=["POST"]),
        Route("/approvals/{aid}/reject", guarded(decision(False)), methods=["POST"]),
    ]
    return Starlette(routes=routes)
