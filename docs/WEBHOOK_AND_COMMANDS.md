# TradeStation 0DTE Bot — Webhook Syntax & Telegram Commands (Full Reference)

Everything you can send the bot and every command it understands. Copy-paste the JSON
blocks into your TradingView alert message box.

- **Webhook URL:** `http://54.89.203.149/webhook`  (HTTP POST, `Content-Type: application/json`)
- **Dashboard:** `http://54.89.203.149/dashboard?token=<DASHBOARD_TOKEN>`
- **Auth:** every alert must include `"secret": "<WEBHOOK_SECRET>"` (ask the developer for the value; it lives in the server `.env`, not in this doc).

---

## 1. The model in one line
> **BUY → buy an ATM CALL · SELL → buy an ATM PUT · EXIT → close that position.**

The bot is directional and long-premium only (it buys options, never sells naked).

---

## 2. Minimum payloads (uses all config defaults: 0DTE, ATM, market fill, configured size)

```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY" }
```
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "sell", "symbol": "SPY" }
```
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "exit", "symbol": "SPY" }
```
`buy` → BUYTOOPEN a CALL · `sell` → BUYTOOPEN a PUT · `exit` → SELLTOCLOSE the open position.

---

## 3. All payload fields

| Field | Required | Values | Default | Meaning |
|---|---|---|---|---|
| `secret` | **yes** | string | — | Shared secret; must match the server. |
| `action` | **yes** | `buy` / `sell` / `exit` (+ synonyms below) | — | Direction. |
| `symbol` | no | underlying ticker (`SPY`,`QQQ`,`SPXW`,…) | `defaults.underlying` (SPY) | The UNDERLYING, not the option. |
| `strategy_id` | no | any string, e.g. `SPY_5m` | `default` | Lets the same symbol hold separate positions per timeframe/strategy. |
| `id` | no | any string | — | Idempotency key — dedupes TradingView resends. |
| `fill` | no | `market` / `mid` / `ask` / `bid` | `defaults.entry_fill` (market) | Order type (see §4). |
| `dollars` | no | number | group/global sizing | Spend ~$N on this trade. |
| `percent` | no | number | — | Spend N% of account equity (overrides dollars). |
| `tp_pct` | no | fraction `0`–`5` | config group | Take-profit, fraction of option premium (`0.20` = +20%). |
| `sl_pct` | no | fraction `0.001`–`0.95` | config group | Stop-loss, fraction of premium (`0.15` = −15%). |
| `trim_pct` | no | fraction `0.01`–`0.99` | config group | Fraction of the position to sell at the trim (`0.50` = 50%). Needs `trim_at`. |
| `trim_at` | no | fraction `0.01`–`5` | config group | Profit level that triggers the trim (`0.30` = +30%). Needs `trim_pct`. |
| `be_at` | no | fraction `>0`–`5` | config group | Arm break-even (move stop to entry) once up this much (`0.30` = +30%). |
| `expiration` | no | `0DTE` / `NEXT` | `0DTE` | Same-day or next expiry. |
| `strike_selection` | no | `ATM` / `OTM` / `ITM` | `ATM` | Strike relative to spot. |
| `strike_offset` | no | integer | `0` | Strikes away from ATM (1 = one strike OTM/ITM). |
| `side` | no | `call` / `put` | derived from action | Force the option right (overrides buy→call/sell→put). |
| `score` | no | number | — | Signal score — logged in the trade record (§3.18). |
| `reason` / `comment` | no | string | — | Free text — logged as the trade's "Buy Reason". |

**Action synonyms** (all accepted):
- BUY: `buy`, `long`, `buytoopen`, `call`, `bull`
- SELL: `sell`, `short`, `put`, `bear`
- EXIT: `exit`, `close`, `flat`, `flatten`, `closeall`, `close_all`

---

## 4. Market vs Limit orders (the `fill` field)

| `fill` | Order placed | Behaviour |
|---|---|---|
| `market` *(default)* | **Market** | Instant fill at the going price. Use for guaranteed entry/exit. |
| `mid` | **Limit @ mid** | Sits at the bid/ask midpoint, then **chases** toward marketable. |
| `ask` | **Limit @ ask** | Marketable for a buy (fills fast), priced-protected. |
| `bid` | **Limit @ bid** | Passive; waits for price to come to you. |

> `"fill":"limit"` is accepted and behaves like `mid`.

**How limit chasing works (retry / §3.9):** a limit order that doesn't fill within
`retry.interval_sec` (2s) is cancelled and re-priced toward the marketable side, up to
`retry.max_attempts` (20) times; if still unfilled it is **cancelled** (no silent market
fallback — `market_fallback: false`). Market orders are never chased (they fill at once).

### Market order examples
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "QQQ", "fill": "market" }
```
### Limit order examples
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY", "fill": "mid" }
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY", "fill": "ask" }
{ "secret": "<WEBHOOK_SECRET>", "action": "exit", "symbol": "SPY", "fill": "mid" }
```

---

## 5. Sizing

Precedence: **payload `dollars`/`percent`  →  strategy group  →  global default.**
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "SPY", "dollars": 1000 }
{ "secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "SPY", "percent": 5 }
```
Contracts are sized off the **ask** and capped by `sizing.max_contracts`. A new entry is
blocked if it would push open exposure past `sizing.max_exposure_pct` of equity (§3.11).

---

## 6. Strike / expiry selection
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "SPY", "expiration": "0DTE", "strike_selection": "ATM", "strike_offset": 0 }
{ "secret": "<WEBHOOK_SECRET>", "action": "buy", "symbol": "SPY", "strike_selection": "OTM", "strike_offset": 2 }
{ "secret": "<WEBHOOK_SECRET>", "action": "sell","symbol": "QQQ", "expiration": "NEXT" }
```
For SPX 0DTE use `"symbol":"SPXW"` (not `SPX`).

---

## 7. Multi-timeframe / multi-strategy (§3.14)
Use a distinct `strategy_id` so the same underlying holds independent positions:
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY", "strategy_id": "SPY_5m" }
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "SPY", "strategy_id": "SPY_15m" }
{ "secret": "<WEBHOOK_SECRET>", "action": "exit", "symbol": "SPY", "strategy_id": "SPY_5m" }
```
Each `strategy_id` is tracked, risk-managed, and P&L-logged separately. *(Note: if two
strategies pick the exact same contract, the broker nets them by symbol — the bot protects
each strategy's own share.)*

---

## 8. Reverse on opposite signal
If a `sell` arrives while a `call` is open (or vice-versa) and `reverse_on_opposite: true`,
the bot **closes the existing position first, then opens the opposite**. If the close fails,
it does **not** open the new side (never holds both). Send a plain `exit` to just flatten.

---

## 8b. Per-signal take-profit / stop-loss (`tp_pct` / `sl_pct`)
Override the config-group TP/SL for a single trade. Values are a **fraction of the option
premium**. Omit them to use the symbol's group defaults.
```json
{ "secret":"...", "action":"buy", "symbol":"SPY", "tp_pct":0.20, "sl_pct":0.15 }   // +20% TP / -15% SL
{ "secret":"...", "action":"buy", "symbol":"AAPL", "tp_pct":0.50 }                  // +50% TP, SL from config
{ "secret":"...", "action":"buy", "symbol":"QQQ", "sl_pct":0.10 }                   // -10% SL, TP from config
```
- These set the **broker OCO** (take-profit limit + stop-loss backstop) priced off the real
  fill, AND the bot-side stop. The recorded trade log shows the exact TP/SL used.
- Out-of-range values are ignored (fall back to config): `tp_pct` 0–5, `sl_pct` 0.001–0.95.
- TP/SL are about the **exit targets**; the `fill` field is a separate thing (entry order *type*).

## 8c. Two ready-made scenarios (self-contained alerts, any ticker)

**Scenario 1 — Buy (market) · 20% stop · exit on signal.** Two alerts:
```json
{ "secret":"<WEBHOOK_SECRET>", "action":"buy",  "symbol":"{{ticker}}", "sl_pct":0.20, "id":"{{ticker}}-{{timenow}}" }
```
```json
{ "secret":"<WEBHOOK_SECRET>", "action":"exit", "symbol":"{{ticker}}", "id":"{{ticker}}-x-{{timenow}}" }
```

**Scenario 2 — Buy (market) · 20% stop · trim 50% at +30% & move stop to break-even · sell remainder at exit or BE.** Two alerts:
```json
{ "secret":"<WEBHOOK_SECRET>", "action":"buy", "symbol":"{{ticker}}",
  "sl_pct":0.20, "trim_pct":0.50, "trim_at":0.30, "be_at":0.30, "id":"{{ticker}}-{{timenow}}" }
```
```json
{ "secret":"<WEBHOOK_SECRET>", "action":"exit", "symbol":"{{ticker}}", "id":"{{ticker}}-x-{{timenow}}" }
```
Everything is market. `{{ticker}}` auto-fills the chart's symbol, so the **same alert works on any ticker** — put Scenario 1's alerts on one chart and Scenario 2's on another. What each does:
- **buy** → market buy the ATM 0DTE CALL, attach a 20% stop (broker + bot).
- **+30%** (Scenario 2) → sell 50% and move the stop to entry (break-even).
- **exit** signal → close the remainder at market. (Break-even also closes it if price falls back to entry.)

## 9. How exits actually happen (current mode: **hybrid**)
On every entry fill the bot attaches a **broker-side OCO** to the position:
- **Take-profit LIMIT** at `entry × (1 + take_profit_pct)` — evaluated by TradeStation on real fills.
- **Stop-loss backstop** at `entry × (1 − (stop_loss_pct + backstop_extra_pct))` — death-proof, survives a bot/server outage.

Meanwhile the bot also manages, in-process, on the live (median-smoothed) price:
**hard stop · trailing stop · break-even stop · partial trims.**
A TradingView `exit` cancels the broker OCO first, then closes. Account-level
**daily-loss kill-switch** and **max-losing-trades** halt apply on top.

Exit modes (config `execution.exit_mode`): `hybrid` (default, above) · `bracket` (broker OCO
only, no bot trail/trim) · `monitor` (bot-side only, no resting broker order).

---

## 10. Webhook responses
**Success (sync):**
```json
{ "ok": true, "message": "BUYTOOPEN 3x SPY 260630C745 (Market)...", "order_id": "959852040", "latency_ms": 120.5 }
```
**Duplicate (within dedupe window):** `{ "ok": true, "message": "duplicate ignored", "duplicate": true }`
**Errors:** `401` bad/missing secret · `422` bad payload / unknown action · `502` broker error.
A successful order always returns the real TradeStation `order_id` (verifiable in the account).

---

## 11. Telegram commands
Send these to the bot in the authorized chat:

| Command | Aliases | Does |
|---|---|---|
| `/status` | `/s`, `/health` | Env, risk on/off, halt state, open count, equity, **TradeStation P&L**. |
| `/positions` | `/pos`, `/p`, `/position` | Every open position with entry → current and unrealized %. |
| `/pnl` | — | **Real TradeStation P&L** (realized + open). |
| `/closeall` | `/close`, `/stop`, `/flatten`, `/panic`, `/stopall` | **Market-close ALL positions + halt new trades for the day.** |
| `/resume` | `/go`, `/unhalt` | Clear the halt (daily-loss + max-losing) — re-allow entries. |
| `/help` | `/start`, `/commands`, `/menu` | Command list. |

The bot also **pushes alerts** automatically: entry (buy→call / sell→put), every exit
(stop/trim/trailing/take-profit/signal), bracket set/failed, kill-switch, errors, and
online/offline.

---

## 12. Other HTTP endpoints
| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Status JSON (env, live, account, halted, open count). |
| `/positions` | GET | Open positions + cached equity. |
| `/dashboard?token=…` | GET | Live web dashboard. |
| `/api/dashboard?token=…` | GET | Dashboard data as JSON. |
| `/export/trades?token=…` | GET | Full §3.18 trade-log CSV. |
| `/halt` | POST | Panic: flatten everything + halt (same as `/closeall`). |
| `/reload` | POST | Hot-reload `config.yaml` (no restart). |

---

## 13. TradingView alert setup

### A Pine STRATEGY — ONE message box for everything (recommended)
A strategy alert has a **single** message box and fires on every order. Paste this **once** —
it handles long entries, short entries, exits, and reversals automatically:
```json
{
  "secret": "<WEBHOOK_SECRET>",
  "symbol": "{{ticker}}",
  "strategy_id": "{{ticker}}_{{interval}}",
  "action": "{{strategy.order.action}}",
  "market_position": "{{strategy.market_position}}",
  "id": "{{ticker}}-{{interval}}-{{timenow}}"
}
```
**Why this works:** `{{strategy.order.action}}` only emits `buy`/`sell` — it can't tell an
entry from an exit (closing a long also emits `sell`). `{{strategy.market_position}}` is the
position *after* the order, so the bot maps it correctly:

| `market_position` | Bot does |
|---|---|
| `long` | open / hold a **CALL** |
| `short` | open / hold a **PUT** |
| `flat` | **close** the position (exit) |

Reversals (long→short) flip the position → the bot closes the call and opens the put.

Add fixed TP/SL or sizing to the same box if you want them applied to every entry:
```json
{
  "secret": "<WEBHOOK_SECRET>", "symbol": "{{ticker}}", "strategy_id": "{{ticker}}_{{interval}}",
  "action": "{{strategy.order.action}}", "market_position": "{{strategy.market_position}}",
  "tp_pct": 0.20, "sl_pct": 0.15, "dollars": 500, "id": "{{ticker}}-{{interval}}-{{timenow}}"
}
```

### A Pine INDICATOR — separate alerts per condition
Indicators let you make one alert per `alertcondition`, so hard-code the action in each:
```json
{ "secret": "<WEBHOOK_SECRET>", "action": "buy",  "symbol": "{{ticker}}", "strategy_id": "{{ticker}}_{{interval}}" }
{ "secret": "<WEBHOOK_SECRET>", "action": "sell", "symbol": "{{ticker}}", "strategy_id": "{{ticker}}_{{interval}}" }
{ "secret": "<WEBHOOK_SECRET>", "action": "exit", "symbol": "{{ticker}}", "strategy_id": "{{ticker}}_{{interval}}" }
```

### Steps
1. Alert → **Webhook URL** = `http://54.89.203.149/webhook` (Notifications tab → Webhook URL).
2. Alert **message** = the JSON above (put your real secret in).
3. For a strategy alert, set the condition to **"alert() function calls only"** or **"Order fills only"** so it fires on every order.
4. The `id` with `{{timenow}}` keeps each signal unique so duplicate resends are ignored.

---

## 14. Tuning (no code changes)
All behaviour is in `config.yaml` — sizing, risk %, groups, schedule, retry, exit mode,
quote-robustness, etc. Edit it and `POST /reload` (or restart). See `docs/HANDOVER.md`.
