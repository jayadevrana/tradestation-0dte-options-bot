# TradingView → Bot setup (fire a real webhook today)

This connects a TradingView alert to the bot's `/webhook` endpoint. Works the same
for the sim demo and for live later.

---

## 1. Start the bot

```bash
cd "tradestation-bot"
source .venv/bin/activate            # or your venv
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Check it: open `http://localhost:8000/health` → should show `environment: sim`.

## 2. Expose it to the internet with ngrok

TradingView can only reach a public URL. ngrok gives you one in seconds.

```bash
# install (mac):  brew install ngrok    (or download from ngrok.com)
ngrok config add-authtoken <YOUR_NGROK_TOKEN>     # free account, one-time
ngrok http 8000
```

ngrok prints a public URL like `https://a1b2c3d4.ngrok-free.app`.
Your webhook URL is that **+ `/webhook`**:

```
https://a1b2c3d4.ngrok-free.app/webhook
```

> For production, replace ngrok with a real VPS + domain + Caddy (auto-TLS). ngrok
> is perfect for the demo and client testing.

## 3. Set the shared secret

The secret now lives in `.env` (not `config.yaml`), so it stays out of git:

```
WEBHOOK_SECRET=YOUR_WEBHOOK_SECRET
```

The exact same string must appear in every alert's JSON `secret` field. Rotate it
before going live (it was shared during setup).

## 4. Create the TradingView alert

1. Open your indicator/strategy on the chart.
2. **Important:** webhook order signals need a Pine **strategy** (not a plain
   indicator) if you want `{{strategy.order.action}}`. For a fixed signal you can
   use a plain indicator + `alertcondition` and a static JSON.
3. Click the **alarm clock → Create Alert**.
4. Under **Notifications**, tick **Webhook URL** and paste your ngrok `/webhook` URL.
5. In the **Message** box, paste one of the JSON payloads below.
6. Make sure **2-Factor Authentication is enabled** on your TradingView account —
   it's required for webhooks.

### Payloads to paste in the alert "Message" box  (DIRECTIONAL model)

> **BUY → buys a CALL.  SELL → buys a PUT.  EXIT → closes the position.**

**BUY signal (opens a CALL):**
```json
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "buy", "symbol": "SPY" }
```

**SELL signal (opens a PUT):**
```json
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "sell", "symbol": "SPY" }
```

**EXIT signal (closes the open position):**
```json
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "exit", "symbol": "SPY" }
```

Notes:
- If a SELL arrives while a CALL is open (or vice-versa), the bot **reverses** —
  closes the open side, then opens the new one (set by `reverse_on_opposite`).
- A repeated identical signal within `dedupe_window_sec` is ignored (resend-safe).

**Two timeframes, same ticker, managed separately** — add a different `strategy_id`
per timeframe (one alert each):
```json
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "buy", "symbol": "SPY", "strategy_id": "SPY_5m" }
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "buy", "symbol": "SPY", "strategy_id": "SPY_15m" }
```

**Optional per-alert overrides** (otherwise config defaults apply):
```json
{ "secret": "YOUR_WEBHOOK_SECRET", "action": "buy", "symbol": "QQQ",
  "fill": "market", "expiration": "0DTE", "strike_selection": "OTM",
  "strike_offset": 1, "dollars": 300, "id": "{{timenow}}" }
```
(`id` is an optional idempotency key — use TradingView's `{{timenow}}` to make each
fire unique so legitimate repeats aren't dropped.)

### Dynamic Pine strategy (optional, sends action automatically)

```pinescript
//@version=5
strategy("Bot Bridge", overlay=true)
S = '"secret":"YOUR_WEBHOOK_SECRET"'
// ... your entry/exit conditions ...
if (longCondition)            // bullish -> buy a CALL
    strategy.entry("L", strategy.long)
    alert('{' + S + ',"action":"buy","symbol":"SPY"}', alert.freq_once_per_bar_close)
if (shortCondition)           // bearish -> buy a PUT
    strategy.entry("S", strategy.short)
    alert('{' + S + ',"action":"sell","symbol":"SPY"}', alert.freq_once_per_bar_close)
if (exitCondition)            // flat -> close
    strategy.close_all()
    alert('{' + S + ',"action":"exit","symbol":"SPY"}', alert.freq_once_per_bar_close)
```

## 5. Test it WITHOUT TradingView (instant feedback)

```bash
# health + what the hot cache currently holds
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:8000/cache  | python3 -m json.tool

# BUY -> opens a CALL
curl -s -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"YOUR_WEBHOOK_SECRET","action":"buy","symbol":"SPY"}' \
  | python3 -m json.tool

# SELL -> opens a PUT   |   EXIT -> closes
curl -s -X POST http://localhost:8000/webhook -H 'Content-Type: application/json' \
  -d '{"secret":"YOUR_WEBHOOK_SECRET","action":"sell","symbol":"SPY"}' | python3 -m json.tool
curl -s -X POST http://localhost:8000/webhook -H 'Content-Type: application/json' \
  -d '{"secret":"YOUR_WEBHOOK_SECRET","action":"exit","symbol":"SPY"}' | python3 -m json.tool

# see open positions
curl -s http://localhost:8000/positions | python3 -m json.tool

# panic button: flatten + halt for the day
curl -s -X POST http://localhost:8000/halt | python3 -m json.tool
```

The response includes `latency_ms` (server-side time to place the order). A wrong
secret returns `401`; a valid one places a real order on the sim account.

## 6. Security checklist before live

- [ ] Long random `shared_secret`, never shared in screenshots.
- [ ] Restrict inbound to TradingView's webhook IPs:
      `52.89.214.238`, `34.212.75.30`, `54.218.53.128`, `52.32.178.7`.
- [ ] Real TLS (Caddy/VPS), not ngrok, for production.
- [ ] `environment: sim` until fully validated; flip to `live` only deliberately.
- [ ] Confirm `/health` shows `live_trading:false` while testing.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 unauthorized` | `secret` in JSON ≠ `webhook.shared_secret` in config.yaml |
| `422 bad payload` | JSON malformed, or missing `secret`/`action` |
| `502 ... Missing credentials` | `.env` not filled / refresh token not set |
| TradingView "alert message could not be sent" | ngrok not running, wrong URL, or 2FA off |
| `exit` says "No open position" | nothing open for that `symbol`+`strategy_id` to close |
| Order failed "Market orders … accepted Monday–Friday 9:30 AM…" | market is closed (after-hours/holiday) — DAY/market orders only fill during RTH |
