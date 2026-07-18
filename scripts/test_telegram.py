"""Verify Telegram alerts.

1. Create a bot with @BotFather, put the token in .env as TELEGRAM_BOT_TOKEN.
2. Send any message to your new bot in Telegram (so it has a chat to reply to).
3. Run this to discover your chat id:
       python3 scripts/test_telegram.py --find-chat
   Put the printed id in .env as TELEGRAM_CHAT_ID.
4. Run this to send a test message:
       python3 scripts/test_telegram.py
"""
from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def find_chat():
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=15)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"getUpdates failed: {data}")
    seen = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not seen:
        print("No chats found. Send a message to your bot in Telegram first, then re-run.")
        return
    print("Chats that have messaged your bot:")
    for cid, name in seen.items():
        print(f"  TELEGRAM_CHAT_ID={cid}   ({name})")


def send_test():
    if not (TOKEN and CHAT):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first "
                 "(use --find-chat to get the chat id).")
    from app.notify import Notifier
    n = Notifier(TOKEN, CHAT, enabled=True)
    ok, info = n.send_sync(
        "✅ <b>TradeStation bot</b> — Telegram is connected.\n"
        "You'll get an alert here on every trade (entries, exits, stops, kill-switch)."
    )
    print("sent OK" if ok else f"FAILED: {info}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--find-chat", action="store_true", help="list chat ids that messaged your bot")
    args = p.parse_args()
    if not TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first.")
    find_chat() if args.find_chat else send_test()
