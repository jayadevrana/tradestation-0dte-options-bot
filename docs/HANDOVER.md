# TradeStation 0DTE Options Bot — Operations & Handover Runbook (§11)

A directional options-trading bot: a TradingView alert hits a webhook → the bot buys an
ATM 0DTE/next-DTE **CALL** (BUY) or **PUT** (SELL), manages the exit, and closes on an EXIT
signal. Single async Python process (FastAPI + uvicorn) with background loops for risk,
quotes, reconciliation, Telegram, and the order chaser.

---

## 1. Architecture at a glance
- **`app/main.py`** — FastAPI webhook server + lifespan that starts the background loops.
- **`app/ts_client.py`** — TradeStation v3 REST client (OAuth refresh, quotes, orders, positions).
- **`app/market_cache.py`** — keeps each symbol's ATM call/put + quotes + equity hot (low-latency hot path).
- **`app/options.py` / `app/orders.py`** — strike/contract selection, sizing, fill→order mapping, open/close.
- **`app/risk.py`** — risk monitor loop: stop / trims / trailing / break-even / take-profit / daily-loss / max-losing-trades.
- **`app/bracket.py`** — broker-side exits (hybrid stop backstop or full OCO TP/SL) attached at entry.
- **`app/chaser.py`** — limit-order retry/chase to a fill.
- **`app/watcher.py`** — reconciles bot state vs the broker; alerts on drift.
- **`app/tradelog.py` / `app/state.py`** — SQLite store + full §3.18 trade-log recording.
- **`app/notify.py` / `app/telegram_commands.py`** — Telegram alerts + `/` commands.
- **`app/dashboard.py`** — token-gated live web dashboard.
- **`config.yaml`** — all tunables. **`.env`** — secrets only.

## 2. Webhook (what TradingView sends)
`POST http://<server>/webhook` with JSON:
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY" }   // BUY  → ATM CALL
{ "secret": "<WEBHOOK_SECRET>", "action": "sell", "symbol": "SPY" }   // SELL → ATM PUT
{ "secret": "<WEBHOOK_SECRET>", "action": "exit", "symbol": "SPY" }   // close the position
```
Optional fields: `strategy_id` (separate multi-timeframe positions, e.g. `"SPY_5m"`), `id`
(idempotency key to dedupe resends), `dollars` / `percent` (sizing override), `expiration`
(`0DTE`|`NEXT`), `strike_selection` (`ATM`|`OTM`|`ITM`), `strike_offset`, `fill`
(`market`|`mid`|`ask`|`bid`), `score` (logged as Signal Score), `reason`/`comment` (Buy Reason).

## 3. Exit modes (`execution.exit_mode`)
- **`hybrid`** (recommended, current): broker holds a death-proof **StopMarket backstop** set
  *below* the bot's own stop (`entry × (1 − (stop_loss_pct + backstop_extra_pct))`), so it only
  fires if the bot/server is down; meanwhile the bot manages trailing/trims/break-even/take-profit.
- **`bracket`**: broker holds a static OCO `[TP limit + SL stop]`; the bot only reconciles (no trail/trim).
- **`monitor`**: bot-side only (no broker order rests).
A TradingView EXIT always cancels the broker order first; a trim resizes the backstop.

## 4. Risk controls (`risk:` in config.yaml)
`stop_loss_pct`, `take_profit_pct`, `trims[]`, `trailing`, `breakeven`, `daily_max_loss_usd`
(flatten + halt for the day), `max_losing_trades` (block NEW entries for the day after N losers;
open positions still managed). Per-symbol overrides via `groups:`. Bot-side exits are debounced
(`execution.exit_confirm_ticks`) and ignore implausibly wide quotes (`execution.max_spread_ratio`)
so a single bad option tick can't fire a phantom exit.

## 5. Operations (on the server, user `tsbot`, app at `/opt/tradestation-bot`)
```bash
sudo systemctl status tradestation-bot      # is it running?
sudo systemctl restart tradestation-bot     # restart (after editing .env)
sudo journalctl -u tradestation-bot -f      # live logs
curl -s http://127.0.0.1:8000/health        # quick health JSON
curl -s -X POST http://127.0.0.1:8000/reload   # hot-reload config.yaml (no restart)
curl -s -X POST http://127.0.0.1:8000/halt     # panic: flatten everything + halt for the day
```
Reverse proxy: Caddy `:80 → 127.0.0.1:8000`. Service unit: `deploy/tradestation-bot.service`.

## 6. Dashboard & Telegram
- Dashboard: `http://<server>/dashboard?token=<DASHBOARD_TOKEN>` — status, open positions (+ bracket
  protection), last signal, events, closed trades, live cache, watcher, active config.
- CSV: `http://<server>/export/trades?token=<DASHBOARD_TOKEN>` — **full §3.18 audit-trail fields**.
- Telegram commands: `/status` `/positions` `/pnl` `/closeall` (flatten + halt) `/resume` (clear halts) `/help`.

## 7. Secrets (`.env`, never in git)
`TS_CLIENT_ID`, `TS_CLIENT_SECRET`, `TS_REFRESH_TOKEN`, `TS_REDIRECT_URI`, `WEBHOOK_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_TOKEN`. Refresh token (one-time):
`python3 scripts/get_refresh_token.py --auth-url` then `--code "<redirect-url>"`.
**Rotate all secrets before going live.** Set `environment: live` only when ready for real money.

## 8. Deploy a new build
```bash
rsync -az --exclude .env --exclude .venv --exclude bot_state.db ./ <user>@<server>:/home/<user>/tsbot-new/
ssh <user>@<server> 'sudo bash /tmp/deploy_code.sh'   # rsync into /opt, chown tsbot, restart
```
DB migrations run automatically on startup (idempotent `ALTER TABLE`).

## 9. Troubleshooting
- **Orders rejected** → check `/health` `live_trading`/`account`, token validity in logs, market hours.
- **"unmanaged" / "qty_mismatch" in the watcher** → bot vs broker drift; investigate the named symbol.
- **No fills** → confirm market open; `entry_fill: market` for instant fills.
- **403 "Missing required scope"** → the refresh token needs the **OptionSpreads** scope (re-auth).
- **Phantom P&L** → all P&L is recorded from real broker fills; long options are valued at the BID.

## 10. §3 contract feature → where it lives
| § | Feature | Module / setting |
|---|---|---|
| 3.1–3.2 | TradingView + TradeStation integration | `main.py`, `ts_client.py` |
| 3.3–3.5 | Order types / mid-price / tick / speed | `orders.py`, `market_cache.py` |
| 3.6–3.7 | Options selection / sizing | `options.py`, `orders.py` |
| 3.8 | Trading schedule | `schedule.py`, `config schedule:` |
| 3.9 | Entry/exit retry (limit chase) | `chaser.py`, `config retry:` |
| 3.10 | Risk management | `risk.py`, `config risk:` |
| 3.11 | Max account exposure | `main._exposure_ok`, `sizing.max_exposure_pct` |
| 3.12 | Daily max loss / max losing trades | `risk.py`, `daily_max_loss_usd` / `max_losing_trades` |
| 3.13 | Strategy config groups | `groups.py`, `config groups:` |
| 3.14 | Multi-timeframe / multi-strategy | `strategy_id` keying |
| 3.15 | Telegram notifications | `notify.py`, `telegram_commands.py` |
| 3.16 | Watcher / verification | `watcher.py` |
| 3.17–3.18 | Dashboard + trade-log fields | `dashboard.py`, `tradelog.py`, `/export/trades` |
| 3.19 | Configurability | `config.yaml` + `/reload` |
| + | Broker bracket (added) | `bracket.py`, `execution.exit_mode` |
