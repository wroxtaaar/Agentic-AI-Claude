"""Incident monitor: watches registered containers/health URLs and opens read-only SRE triage jobs.

It never restarts or changes anything. A human (or a tier-3 policy via a restart job) decides what to do.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from . import executors
from .engine import Engine
from .redact import redact

log = logging.getLogger("vpsagents.monitor")
CONFIRM_MISSES = 2


class Monitor:
    def __init__(self, engine: Engine, interval: int = 120, cooldown_minutes: int = 60, *,
                 probe=executors.http_ok, state=executors.container_state, logs=None):
        self.engine, self.interval, self.cooldown = engine, interval, cooldown_minutes * 60
        self.probe, self.state = probe, state
        self.logs = logs or self._docker_logs
        self._misses: dict[str, int] = {}
        self._stop = threading.Event()

    @staticmethod
    def _docker_logs(container: str) -> str:
        r = executors.run_argv(["docker", "logs", "--tail", "80", container], None, 20)
        return redact(r["output"], 4000)

    def problems(self, proj) -> list[str]:
        out = []
        for c in proj.containers:
            st = self.state(c)
            if not st.get("known"):
                out.append(f"container {c}: not inspectable ({st.get('error', '')[:100]})")
            elif st["status"] != "running":
                out.append(f"container {c}: status={st['status']}")
            elif st.get("health") == "unhealthy":
                out.append(f"container {c}: unhealthy")
        if proj.health_url and not self.probe(proj.health_url):
            out.append(f"health check failed: {proj.health_url}")
        return out

    def tick(self) -> list[str]:
        """One pass. Returns ids of incident jobs opened."""
        opened = []
        store = self.engine.store
        for proj in self.engine.settings.projects:
            if not (proj.containers or proj.health_url):
                continue
            probs = self.problems(proj)
            key = f"incident:{proj.name}"
            if not probs:
                self._misses[proj.name] = 0
                if store.kv_get(key):
                    store.kv_set(key, "")
                    self.engine.notifier.send(f"🟢 {proj.name} recovered")
                continue
            self._misses[proj.name] = self._misses.get(proj.name, 0) + 1
            if self._misses[proj.name] < CONFIRM_MISSES:
                continue
            sig = hashlib.sha256("\n".join(sorted(probs)).encode()).hexdigest()[:12]
            last = json.loads(store.kv_get(key) or "null")
            if last and last["sig"] == sig and time.time() - last["ts"] < self.cooldown:
                continue
            logs = []
            for c in proj.containers:
                if any(p.startswith(f"container {c}") for p in probs):
                    logs.append(f"### logs: {c} (last 80 lines)\n{self.logs(c)}")
            task = ("Triage this incident. Findings from the monitor:\n- " + "\n- ".join(probs) +
                    ("\n\n" + "\n\n".join(logs) if logs else "") +
                    "\n\nDetermine the most likely cause using the logs above and the code. "
                    "Say what is observed vs guessed, and recommend next steps (do not run anything).")
            job = self.engine.submit(proj.name, "incident", task)
            store.kv_set(key, json.dumps({"sig": sig, "ts": time.time(), "job": job["id"]}))
            self.engine.notifier.send(f"🔴 incident on {proj.name}: {'; '.join(probs)[:400]}\ntriage job {job['id']}")
            opened.append(job["id"])
        return opened

    def start(self) -> None:
        threading.Thread(target=self._loop, name="monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("monitor tick failed")
            self._stop.wait(self.interval)
