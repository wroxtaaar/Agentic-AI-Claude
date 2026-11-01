"""Notifications: Telegram bot or generic webhook (env-configured). Never raises."""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.message import EmailMessage
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


class EmailNotifier(Notifier):
    """SMTP alerts. Uses the same variable names as the old Agentic-AI monitor, so your .env.monitor works as is."""

    def __init__(self, host: str, port: int, username: str, password: str, sender: str, to: str,
                 use_ssl: bool = False, smtp_factory=None):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.sender, self.to, self.use_ssl = sender, to, use_ssl
        self._factory = smtp_factory

    def send(self, text: str) -> None:
        try:
            msg = EmailMessage()
            first = text.strip().splitlines()[0] if text.strip() else "notification"
            msg["Subject"] = f"[vps-agents] {first[:120]}"
            msg["From"], msg["To"] = self.sender, self.to
            msg.set_content(text[:20000])
            cls = self._factory or (smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP)
            with cls(self.host, self.port, timeout=15) as smtp:
                if not self.use_ssl and self._factory is None:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
        except Exception as e:  # noqa: BLE001 - notification failure must never break a job
            log.warning("email notify failed: %s", type(e).__name__)


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def send(self, text: str) -> None:
        for n in self.notifiers:
            n.send(text)


def _email_from_env() -> Notifier | None:
    e = os.environ
    if not (e.get("SMTP_HOST") and e.get("SMTP_USERNAME") and e.get("SMTP_PASSWORD") and e.get("ALERT_EMAIL_TO")):
        return None
    ssl = e.get("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    try:
        port = int(e.get("SMTP_PORT", "465" if ssl else "587"))
    except ValueError:
        return None
    return EmailNotifier(e["SMTP_HOST"], port, e["SMTP_USERNAME"], e["SMTP_PASSWORD"],
                         e.get("ALERT_EMAIL_FROM") or e["SMTP_USERNAME"], e["ALERT_EMAIL_TO"], ssl)


def from_env() -> Notifier:
    found: list[Notifier] = []
    if os.environ.get("VPSA_TELEGRAM_TOKEN") and os.environ.get("VPSA_TELEGRAM_CHAT_ID"):
        found.append(HttpNotifier("telegram", token=os.environ["VPSA_TELEGRAM_TOKEN"], chat_id=os.environ["VPSA_TELEGRAM_CHAT_ID"]))
    if os.environ.get("VPSA_WEBHOOK_URL"):
        found.append(HttpNotifier("webhook", url=os.environ["VPSA_WEBHOOK_URL"]))
    mail = _email_from_env()
    if mail:
        found.append(mail)
    if not found:
        return Notifier()
    return found[0] if len(found) == 1 else MultiNotifier(found)

