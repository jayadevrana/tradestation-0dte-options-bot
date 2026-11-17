# TradeStation 0DTE Options Bot

TradingView alert → webhook → TradeStation options order. This is the MVP: it
authenticates to TradeStation, picks the 0DTE/next ATM option, sizes by $ or %,
and **places buy/sell orders on your SIM account**. All parameters live in
`config.yaml` so the strategy is tunable without touching code.

> **Status:** Working — order placement (entry + exit), strike selection, sizing,
> fill types, AND the risk monitor (15% stop, partial trims, trailing stop, and the
> daily-loss kill-switch). All parameters live in `config.yaml`. Each risk path is
> unit-tested with a mocked broker. Remaining for production: a resting broker-side
> OCO backstop at entry, and anchoring the daily reset to 09:30 ET (currently rolls
> on calendar-day change).

---

## 1. Install

```bash
cd "tradestation-bot"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Get TradeStation API credentials

1. Email **ClientExperience@tradestation.com** to request an API key for your
   account. Ask specifically for scopes **MarketData ReadAccount Trade** plus
   **offline_access** with **non-rotating** refresh tokens.
2. Put the key + secret into `.env` as `TS_CLIENT_ID` / `TS_CLIENT_SECRET`.
3. Make sure the redirect URI on the key matches `TS_REDIRECT_URI`
   (default `http://localhost:8182/callback`).

## 3. Authorize once (get the refresh token)

```bash
python scripts/get_refresh_token.py
```

Log in when the browser opens; the script prints a `TS_REFRESH_TOKEN=...` line.
Paste it into `.env`. The bot now stays logged in unattended.

## 4. Demo — place a buy then sell on SIM

```bash
# dry run first: resolves the contract + sizing, places NOTHING
python scripts/place_test_order.py --dry-run

# real SIM order: buy SPY 0DTE ATM call, hold 3s, then sell to close
python scripts/place_test_order.py

# variations
python scripts/place_test_order.py --symbol QQQ --side put --dollars 300
python scripts/place_test_order.py --no-sell        # only the buy
```

It refuses to run unless `environment: sim`, so you can't fire a live order by accident.

## 5. Run the webhook server (for live TradingView alerts)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Point a TradingView alert's webhook URL at `http://YOUR_SERVER:8000/webhook`
with this JSON in the alert message:

```json
{
  "secret": "CHANGE_ME_to_a_long_random_string",
  "action": "buy",
  "side": "call",
  "symbol": "SPY",
  "strategy_id": "SPY_5m"
}
```

- `action: "buy"` opens a position; `action: "sell"` closes the open position for
  that `symbol` + `strategy_id`.
- `strategy_id` lets the **same ticker hold separate positions per timeframe**
  (e.g. `SPY_5m` vs `SPY_15m`).
- Optional overrides per alert: `fill`, `expiration`, `strike_selection`,
  `strike_offset`, `dollars`, `percent`.

### Endpoints

| Method / path | Purpose |
|---|---|
| `POST /webhook` | TradingView signal in (buy/sell) |
| `GET /health` | status, environment, halted flag, open count |
| `GET /positions` | open positions + day baseline + halt state |
| `POST /halt` | **panic button** — flatten everything + halt for the day |
| `POST /reload` | hot-reload `config.yaml` without restarting |

➡️ **Full TradingView + ngrok walkthrough (fire a real webhook today):**
[`docs/TRADINGVIEW_SETUP.md`](docs/TRADINGVIEW_SETUP.md)

### Risk monitor

A background task (started automatically with the server) checks every
`execution.poll_interval_sec` and enforces, on the live option premium:

- **15% stop loss** → market-close the position
- **partial trims** → sell a fraction at each `risk.trims` profit target
- **trailing stop** → close once price falls `trail_pct` off the peak (after `activate_at_pct`)
- **daily-loss kill-switch** → at `-daily_max_loss_usd` day PnL: cancel all orders,
  flatten all positions, and block new entries until the next session

The halt flag and day-start equity persist in `bot_state.db`, so a restart keeps a
tripped kill-switch. Run it as its own process if you prefer: `python scripts/run_monitor.py`.

---

## Endpoints used (TradeStation v3)

| Need | Endpoint |
|---|---|
| Auth | `POST signin.tradestation.com/oauth/token` |
| Accounts / balances | `GET /brokerage/accounts`, `.../{id}/balances` |
| Underlying & option quote | `GET /marketdata/quotes/{symbols}` |
| Expirations / strikes | `GET /marketdata/options/expirations|strikes/{underlying}` |
| Place / cancel order | `POST` / `DELETE /orderexecution/orders` |

## Project layout

```
config.yaml            all tunable parameters
.env                   secrets (API key/secret/refresh token)
app/
  config.py            load + validate config and secrets
  models.py            webhook payload schema
  ts_client.py         TradeStation REST client (auth, quotes, orders)
  options.py           expiration/strike selection + option symbol + quote
  orders.py            sizing, fill->order mapping, open/close flows
  market_cache.py      low-latency hot cache (ATM call/put + quotes + equity)
  risk.py              risk monitor: stop / trims / trailing / daily-loss kill-switch
  notify.py            Telegram trade alerts (non-blocking background queue)
  state.py             sqlite position store (keyed by symbol+strategy)
  main.py              FastAPI webhook server + background monitor + cache
scripts/
  get_refresh_token.py one-time OAuth -> refresh token
  place_test_order.py  DEMO buy+sell on sim
  run_monitor.py       run the risk monitor standalone
  test_telegram.py     verify Telegram token/chat + send a test alert
docs/
  TRADINGVIEW_SETUP.md TradingView + ngrok webhook walkthrough
  TELEGRAM_SETUP.md    Telegram bot + chat-id setup
deploy/
  DEPLOY.md            Ubuntu VPS deploy guide (step-by-step)
  SECURE_ACCESS.md     permanent IP-independent SSH (SSM/Tailscale) + key auth + firewall
  setup_ubuntu.sh      one-shot provisioner (Python, Caddy, systemd, firewall)
  tradestation-bot.service  systemd unit (auto-restart)
  Caddyfile            reverse proxy :80/:443 -> app (TradingView needs 80/443)
```

## Notes / caveats baked into the design

- **SPX 0DTE → use the `SPXW` root**, not `SPX` (monthly AM-settled).
- Sizing is computed off the **ask** so a spread cross can't oversize you.
- Limit prices are rounded to the **option tick** ($0.01 < $3, $0.05 ≥ $3).
- SIM gives instant fills — it proves the plumbing, not real fill quality. Verify
  with small live size before scaling.
- Exact v3 field names (option-symbol format, strikes `expiration` param format)
  should be confirmed against your live sim responses; they're isolated in
  `ts_client.py` / `options.py` for easy adjustment.

---

## Disclaimer

Trading automation is infrastructure, not financial advice. No profit guarantees.
Test in dry-run / SIM (paper) before risking real money.

## Author

Built by [Jayadev Rana](https://jayadevrana.in) — @bluealgocapital · [YouTube](https://www.youtube.com/@jayadevrana3657) · [GitHub](https://github.com/jayadevrana)

## License

MIT © 2026 [Jayadev Rana](https://jayadevrana.in). Free to use, modify and share: please keep the copyright notice and credit **Jayadev Rana** with a link back to this repository.
