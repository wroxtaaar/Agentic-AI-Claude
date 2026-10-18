"""SQLite store: jobs, approvals, events. Every state change is a guarded UPDATE."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import states as S

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, role TEXT, task TEXT NOT NULL,
  status TEXT NOT NULL, stage TEXT NOT NULL, deploy INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL DEFAULT '{}', error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, action TEXT NOT NULL, summary TEXT NOT NULL,
  bind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  decided_at TEXT, decided_by TEXT);
CREATE INDEX IF NOT EXISTS approvals_job ON approvals(job_id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            if self._mem is not None:
                c = self._mem
                own = False
            else:
                c = sqlite3.connect(self.path, timeout=30)
                c.execute("PRAGMA journal_mode=WAL")
                own = True
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                if own:
                    c.close()

    # ---- jobs -------------------------------------------------------------
    def create_job(self, *, project: str, kind: str, task: str, role: str | None = None,
                   deploy: bool = False, data: dict | None = None) -> dict:
        jid = secrets.token_hex(8)
        ts = now()
        with self._conn() as c:
            c.execute("INSERT INTO jobs(id,project,kind,role,task,status,stage,deploy,data,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (jid, project, kind, role, task, S.QUEUED, "start", int(deploy), json.dumps(data or {}), ts, ts))
        self.event(jid, "created", f"{kind} job for {project}")
        return self.get_job(jid)

    @staticmethod
    def _job(row) -> dict:
        d = dict(row)
        d["data"] = json.loads(d["data"])
        d["deploy"] = bool(d["deploy"])
        return d

    def get_job(self, jid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, status: str | None = None, project: str | None = None, limit: int = 100) -> list[dict]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if project:
            q += " AND project=?"; args.append(project)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"; args.append(max(1, min(limit, 500)))
        with self._conn() as c:
            return [self._job(r) for r in c.execute(q, args)]

    def transition(self, jid: str, new: str, *, stage: str | None = None, error: str | None = None) -> dict:
        """Validated status change; raises InvalidTransition."""
        with self._conn() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise KeyError(jid)
            if new not in S.TRANSITIONS[row["status"]]:
                raise S.InvalidTransition(f"{row['status']} -> {new}")
            sets, args = ["status=?", "updated_at=?"], [new, now()]
            if stage is not None:
                sets.append("stage=?"); args.append(stage)
            if error is not None:
                sets.append("error=?"); args.append(error)
            args += [jid, row["status"]]
            changed = c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=? AND status=?", args).rowcount
            if not changed:
                raise S.InvalidTransition("concurrent modification")
        return self.get_job(jid)

    def set_stage(self, jid: str, stage: str) -> None:
        if stage not in S.STAGES:
            raise ValueError(stage)
        with self._conn() as c:
            c.execute("UPDATE jobs SET stage=?, updated_at=? WHERE id=?", (stage, now(), jid))

    def update_data(self, jid: str, **fields) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
            data = json.loads(row["data"])
            data.update(fields)
            c.execute("UPDATE jobs SET data=?, updated_at=? WHERE id=?", (json.dumps(data), now(), jid))
        return data

    def claim_next(self) -> dict | None:
        """Atomically take the oldest queued job whose project has nothing running."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM jobs j WHERE status=? AND NOT EXISTS "
                "(SELECT 1 FROM jobs r WHERE r.project=j.project AND r.status=?) "
                "ORDER BY created_at, rowid LIMIT 1", (S.QUEUED, S.RUNNING)).fetchone()
            if not row:
                return None
            ok = c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                           (S.RUNNING, now(), row["id"], S.QUEUED)).rowcount
        return self.get_job(row["id"]) if ok else None

    def recover_interrupted(self) -> list[str]:
        with self._conn() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status=?", (S.RUNNING,))]
            c.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE status=?",
                      (S.FAILED, "interrupted by control-plane restart (side effects unknown; inspect before retrying)",
                       now(), S.RUNNING))
        for i in ids:
            self.event(i, "interrupted", "control plane restarted while running")
        return ids

    # ---- events -----------------------------------------------------------
    def event(self, jid: str, kind: str, message: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events(job_id,ts,kind,message) VALUES(?,?,?,?)", (jid, now(), kind, message[:4000]))

    def events(self, jid: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT ts,kind,message FROM events WHERE job_id=? ORDER BY id", (jid,))]

    # ---- approvals --------------------------------------------------------
    def ensure_pending_approval(self, jid: str, action: str, summary: str, bind: dict, ttl_minutes: int) -> dict:
        """One pending approval per (job, action, bind). `bind` pins what exactly is being approved."""
        bind_s = json.dumps(bind, sort_keys=True)
        with self._conn() as c:
            row = c.execute("SELECT * FROM approvals WHERE job_id=? AND action=? AND bind=? AND status='pending' AND expires_at>?",
                            (jid, action, bind_s, now())).fetchone()
            if row:
                return self._approval(row)
            aid = secrets.token_hex(8)
            exp = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds")
            c.execute("INSERT INTO approvals(id,job_id,action,summary,bind,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                      (aid, jid, action, summary[:6000], bind_s, "pending", now(), exp))
            row = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return self._approval(row)

    @staticmethod
    def _approval(row) -> dict:
        d = dict(row)
        d["bind"] = json.loads(d["bind"])
        return d

    def get_approval(self, aid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return self._approval(row) if row else None

    def list_approvals(self, status: str | None = "pending") -> list[dict]:
        with self._conn() as c:
            if status == "pending":
                rows = c.execute("SELECT * FROM approvals WHERE status='pending' AND expires_at>? ORDER BY created_at", (now(),)).fetchall()
            elif status:
                rows = c.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 200").fetchall()
        return [self._approval(r) for r in rows]

    def decide_approval(self, aid: str, approve: bool, by: str = "api") -> dict:
        new = "approved" if approve else "rejected"
        with self._conn() as c:
            ok = c.execute("UPDATE approvals SET status=?, decided_at=?, decided_by=? "
                           "WHERE id=? AND status='pending' AND expires_at>?", (new, now(), by, aid, now())).rowcount
            if not ok:
                raise S.InvalidTransition("approval is not pending (already decided or expired)")
        return self.get_approval(aid)

    def consume_approval(self, jid: str, action: str, bind: dict) -> bool:
        """Single use: an approved, unexpired approval that matches `bind` exactly is consumed."""
        bind_s = json.dumps(bind, sort_keys=True)
        with self._conn() as c:
            row = c.execute("SELECT id FROM approvals WHERE job_id=? AND action=? AND bind=? AND status='approved' AND expires_at>? "
                            "ORDER BY created_at LIMIT 1", (jid, action, bind_s, now())).fetchone()
            if not row:
                return False
            return bool(c.execute("UPDATE approvals SET status='consumed' WHERE id=? AND status='approved'", (row["id"],)).rowcount)

    def has_rejection(self, jid: str, action: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM approvals WHERE job_id=? AND action=? AND status='rejected'", (jid, action)).fetchone() is not None

    # ---- counters ---------------------------------------------------------
    def counter_get(self, key: str) -> int:
        return int(self.kv_get(key) or 0)

    def counter_incr(self, key: str, by: int = 1) -> int:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(kv.v AS INTEGER)+?",
                      (key, str(by), by))
            return int(c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()["v"])

    # ---- kv ---------------------------------------------------------------
    def kv_get(self, k: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, k: str, v: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
