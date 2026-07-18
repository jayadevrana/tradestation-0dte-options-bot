"""Telegram command listener — lets the client CONTROL the bot from Telegram.

Long-polls getUpdates in the background and dispatches commands (only from the
authorized chat id) to a handler. Replies go back to the same chat.

Supported (see main.handle_telegram_command):
  /status     bot status + today's P&L
  /positions  current open positions
  /pnl        today's realized P&L
  /closeall   close ALL positions + halt new trades for the day
  /resume     clear the halt (allow entries again)
  /help       command list
"""
from __future__ import annotations

import asyncio
import logging

import requests

log = logging.getLogger("tgcmd")


class TelegramPoller:
    def __init__(self, token: str, chat_id: str, handler):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.handler = handler
        self.enabled = bool(self.token and self.chat_id)
        self._session = requests.Session()
        self._offset = None
        self._running = False

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _send(self, text: str) -> None:
        try:
            self._session.post(self._api("sendMessage"),
                               json={"chat_id": self.chat_id, "text": text},
                               timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("reply send failed: %s", str(e).replace(self.token, "***"))

    def _drain(self) -> None:
        """Skip any backlog so we only respond to commands sent AFTER startup."""
        try:
            r = self._session.get(self._api("getUpdates"), timeout=15)
            res = r.json().get("result", [])
            if res:
                self._offset = res[-1]["update_id"] + 1
        except Exception:  # noqa: BLE001
            pass

    def _poll_once(self) -> None:
        params = {"timeout": 25}
        if self._offset is not None:
            params["offset"] = self._offset
        r = self._session.get(self._api("getUpdates"), params=params, timeout=35)
        data = r.json()
        if not data.get("ok"):
            return
        for u in data.get("result", []):
            self._offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message") or {}
            chat = m.get("chat") or {}
            text = (m.get("text") or "").strip()
            if str(chat.get("id")) != self.chat_id:   # ONLY the authorized chat
                continue
            if not text.startswith("/"):
                continue
            log.info("Telegram command: %s", text)
            try:
                reply = self.handler(text)
            except Exception as e:  # noqa: BLE001
                reply = f"error: {e}"
            if reply:
                self._send(reply)

    async def run(self) -> None:
        if not self.enabled:
            log.info("Telegram command poller disabled (no token/chat).")
            return
        self._running = True
        await asyncio.to_thread(self._drain)
        log.info("Telegram command poller started.")
        while self._running:
            try:
                await asyncio.to_thread(self._poll_once)
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("telegram poll error")
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._running = False
