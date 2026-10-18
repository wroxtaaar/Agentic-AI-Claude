"""Notifications: Telegram bot or generic webhook (env-configured). Never raises."""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("vpsagents.notify")


class Notifier:
    def send(self, text: str) -> None:  # default: log only
        log.info("NOTIFY: %s", text)


class HttpNotifier(Notifier):
    def __init__(self, kind: str, **cfg):
        self.kind, self.cfg = kind, cfg

    def send(self, text: str) -> None:
        try:
            if self.kind == "telegram":
                url = f"https://api.telegram.org/bot{self.cfg['token']}/sendMessage"
                body = urllib.parse.urlencode({"chat_id": self.cfg["chat_id"], "text": text[:3900]}).encode()
                req = urllib.request.Request(url, data=body)
            else:
                req = urllib.request.Request(self.cfg["url"], data=json.dumps({"text": text[:3900]}).encode(),
                                             headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()  # noqa: S310
        except Exception as e:  # noqa: BLE001 - notification failure must never break a job
            log.warning("notify failed: %s", type(e).__name__)


def from_env() -> Notifier:
    if os.environ.get("VPSA_TELEGRAM_TOKEN") and os.environ.get("VPSA_TELEGRAM_CHAT_ID"):
        return HttpNotifier("telegram", token=os.environ["VPSA_TELEGRAM_TOKEN"], chat_id=os.environ["VPSA_TELEGRAM_CHAT_ID"])
    if os.environ.get("VPSA_WEBHOOK_URL"):
        return HttpNotifier("webhook", url=os.environ["VPSA_WEBHOOK_URL"])
    return Notifier()
