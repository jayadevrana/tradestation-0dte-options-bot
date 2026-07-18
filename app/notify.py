"""Telegram notifier — fire-and-forget trade alerts.

Messages are enqueued and sent by a background worker thread, so notifying NEVER
blocks or adds latency to the order hot path, and a Telegram outage can never break
trading. If no token/chat is configured, it's a silent no-op.

Secrets come from env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Toggle in config.yaml
(telegram.enabled).
"""
from __future__ import annotations

import logging
import queue
import threading

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, token: str, chat_id: str, enabled: bool = True):
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.enabled = bool(enabled and self.token and self.chat_id)
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self._session = requests.Session()
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="telegram").start()
            log.info("Telegram notifications ENABLED (chat %s).", self.chat_id)
        else:
            log.info("Telegram notifications disabled (no token/chat or telegram.enabled=false).")

    def send(self, text: str) -> None:
        """Enqueue a message and return immediately (microseconds)."""
        if not self.enabled:
            return
        try:
            self._q.put_nowait(text)
        except queue.Full:
            log.warning("Telegram queue full — dropping message.")

    def _redact(self, s: str) -> str:
        return s.replace(self.token, "***") if self.token else s

    def send_sync(self, text: str) -> tuple[bool, str]:
        """Blocking send used only by the test script."""
        if not (self.token and self.chat_id):
            return False, "no token/chat configured"
        try:
            r = self._session.post(self._url(), json=self._payload(text), timeout=10)
            return (r.status_code == 200), self._redact(r.text[:300])
        except Exception as e:  # noqa: BLE001
            return False, self._redact(str(e))

    # ── internals ──
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/sendMessage"

    def _payload(self, text: str) -> dict:
        return {"chat_id": self.chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True}

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            try:
                self._session.post(self._url(), json=self._payload(text), timeout=10)
            except Exception as e:  # noqa: BLE001 — never let notify crash trading
                log.warning("Telegram send failed: %s", self._redact(str(e)))  # redact token from logs
            finally:
                self._q.task_done()
