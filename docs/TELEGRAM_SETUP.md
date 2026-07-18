# Telegram trade alerts — setup

You'll get a Telegram message on every trade: entries (buy→call / sell→put), exits,
stops, trims, trailing, the daily-loss kill-switch, plus bot online/offline and errors.
Alerts are sent on a background thread, so they **never add latency** to order placement.

## 1. Create a bot (30 seconds)
1. In Telegram, open **@BotFather**.
2. Send `/newbot`, pick a name and username.
3. BotFather replies with a **token** like `8123456789:AAH...`. Copy it.
4. Put it in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=8123456789:AAH...
   ```

## 2. Get your chat id
1. In Telegram, **send any message to your new bot** (search its username, tap Start, say "hi").
   - For a **group**: add the bot to the group and send a message there.
2. Run:
   ```bash
   python3 scripts/test_telegram.py --find-chat
   ```
3. It prints the chat id(s). Put yours in `.env`:
   ```
   TELEGRAM_CHAT_ID=123456789
   ```

## 3. Turn it on and test
1. In `config.yaml`, set:
   ```yaml
   telegram:
     enabled: true
   ```
2. Send a test message:
   ```bash
   python3 scripts/test_telegram.py
   ```
   You should get "✅ TradeStation bot — Telegram is connected." in Telegram.

That's it — restart the bot (or `POST /reload`) and every trade will post to your chat.

## What the alerts look like
```
🟢 BUY → CALL ✅
SPY
1x SPY 260622C747 · Market
order 958285519 · ⚡22ms
```
```
🔴 SELL → PUT ✅
SPY
2x SPY 260622P747 · Market
order 958285520 · ⚡19ms
```
```
⚪ EXIT ✅
SPY · SPY 260622C747
order 958285530 · ⚡20ms
```
```
🚨 DAILY LOSS LIMIT HIT
day PnL -$1002 <= -$1000
Flattening all positions and halting for the day.
```

## Toggles (config.yaml → telegram)
| Flag | Controls |
|---|---|
| `enabled` | master on/off |
| `notify_entries` | buy/sell entries |
| `notify_exits` | exits, stops, trims, trailing, kill-switch |
| `notify_errors` | failed/rejected orders, startup errors |
| `notify_lifecycle` | bot online / offline |

Notes:
- Token + chat id are **secrets** — they live in `.env` (gitignored), not in `config.yaml`.
- For a shared channel, use the channel's chat id and make the bot an admin.
