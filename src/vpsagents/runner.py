"""Background loop: claims queued jobs and steps them. One running job per project."""
from __future__ import annotations

import logging
import threading
import time

from .engine import Engine

log = logging.getLogger("vpsagents.runner")


class Runner:
    def __init__(self, engine: Engine, poll_seconds: float = 2.0):
        self.engine, self.poll = engine, poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_expire = 0.0

    def start(self) -> None:
        interrupted = self.engine.store.recover_interrupted()
        if interrupted:
            log.warning("marked %d interrupted jobs as failed: %s", len(interrupted), interrupted)
        for i in range(self.engine.settings.max_concurrent_jobs):
            t = threading.Thread(target=self._loop, name=f"runner-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def run_once(self) -> bool:
        """Claim and step one job. Returns True if a job was processed (used by tests)."""
        job = self.engine.store.claim_next()
        if not job:
            return False
        self.engine.step(job)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_expire > 60:
                    self._last_expire = time.monotonic()
                    self.engine.expire_stale()
                if not self.run_once():
                    self._stop.wait(self.poll)
            except Exception:  # noqa: BLE001
                log.exception("runner loop error")
                self._stop.wait(self.poll)
