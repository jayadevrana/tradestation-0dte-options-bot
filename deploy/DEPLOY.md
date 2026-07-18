# Deploying the bot to the VPS (Ubuntu)

Target: an always-on server reachable by TradingView. Your VPS is `51.79.225.13`.

> ⚠️ Your VPS is currently **Windows** (`Administrator`). This kit is for **Ubuntu**,
> which is strongly recommended for a 24/7 trading bot (clean auto-restart via
> systemd, free HTTPS via Caddy). In the OVH panel, **reinstall the VPS with
> Ubuntu 22.04 or 24.04**, then follow the steps below. (A Windows path is possible
> but more fragile — ask if you must stay on Windows.)

---

## 1. Copy the project to the server

From your Mac (note the quotes — the local path has spaces). **Include `.env`** — it
holds the API key/secret/refresh token and is gitignored, so copy it explicitly.

```bash
# create the target dir on the server
ssh root@51.79.225.13 'mkdir -p /opt/tradestation-bot'

# copy the project with rsync, excluding junk + local-only state.
# (Excludes macOS ._* files, caches, the local venv, the local DB, and .git so the
#  server starts with fresh position state and reconciles against the broker.)
rsync -av --delete \
  --exclude='._*' --exclude='.DS_Store' --exclude='__pycache__' \
  --exclude='.venv' --exclude='bot_state.db' --exclude='.git' \
  "/Volumes/PortableSSD/trade station automation /untitled folder/tradestation-bot/" \
  root@51.79.225.13:/opt/tradestation-bot/
```

> No `rsync`? Use `scp -r ".../tradestation-bot/." root@51.79.225.13:/opt/tradestation-bot/`
> then on the server delete junk: `find /opt/tradestation-bot -name '._*' -delete; rm -f /opt/tradestation-bot/bot_state.db`.

Confirm `.env` made it:
```bash
ssh root@51.79.225.13 'ls -la /opt/tradestation-bot/.env /opt/tradestation-bot/config.yaml'
```

## 2. Run the provisioner (installs Python, Caddy, the service, firewall)

```bash
ssh root@51.79.225.13
sudo bash /opt/tradestation-bot/deploy/setup_ubuntu.sh
```

It creates a `tsbot` user, a venv, installs deps, installs the systemd service +
Caddy reverse proxy (port 80 → app on 127.0.0.1:8000), and opens the firewall.

## 3. Verify

```bash
# on the server
systemctl status tradestation-bot --no-pager
journalctl -u tradestation-bot -f          # live logs (Ctrl-C to stop)
curl -s http://127.0.0.1/health
```

From your Mac (proves it's reachable from the internet):
```bash
curl -s http://51.79.225.13/health
```
You should see `{"status":"ok","environment":"sim",...}`.

## 4. Point TradingView at it

Webhook URL (HTTP on port 80 — TradingView allows 80/443 only):
```
http://51.79.225.13/webhook
```
Paste the BUY/SELL/EXIT JSON from `docs/TRADINGVIEW_SETUP.md` into the alert message.

Test without TradingView:
```bash
curl -s -X POST http://51.79.225.13/webhook -H 'Content-Type: application/json' \
  -d '{"secret":"j5yCKE1WcQq42fwQs3twUL8u59E3Z1Sc","action":"buy","symbol":"SPY"}'
```

## 5. Updating the code later

```bash
# from your Mac, re-copy changed files, then on the server:
ssh root@51.79.225.13 'systemctl restart tradestation-bot'
# config-only change? no restart needed:
curl -s -X POST http://51.79.225.13/reload
```

## Permanent, secure access (stop whitelisting dynamic IPs)
Don't keep editing the SSH IP rule (and never open 22 to `0.0.0.0/0`). Set up AWS SSM
Session Manager or Tailscale + SSH key auth, then close port 22. Full steps:
**[`deploy/SECURE_ACCESS.md`](SECURE_ACCESS.md)**.

## 6. (Recommended for production) Add a domain + HTTPS

1. Point a DNS A-record (e.g. `bot.yourdomain.com`) at `51.79.225.13`.
2. Edit `/etc/caddy/Caddyfile`: replace `:80 {` with `bot.yourdomain.com {`.
3. `systemctl reload caddy` — Caddy auto-issues a Let's Encrypt cert.
4. New webhook URL: `https://bot.yourdomain.com/webhook`.

## Operations cheat-sheet

| Action | Command |
|---|---|
| Status | `systemctl status tradestation-bot` |
| Live logs | `journalctl -u tradestation-bot -f` |
| Restart | `systemctl restart tradestation-bot` |
| Stop trading | `systemctl stop tradestation-bot` |
| Panic flatten + halt | `curl -X POST http://51.79.225.13/halt` |
| What's cached | `curl http://51.79.225.13/cache` |
| Open positions | `curl http://51.79.225.13/positions` |

## Notes / gotchas

- **Region & latency:** OVH `51.79.x` is North America. TradeStation's API is in AWS
  **us-east-1**; the closer the server, the lower the order round-trip. If latency is
  critical, an us-east VPS beats a west/central one.
- **Time sync:** Ubuntu runs `systemd-timesyncd` by default — good (accurate
  timestamps matter for 0DTE). Verify with `timedatectl`.
- **Secrets:** `.env` is `chmod 600` and owned by `tsbot`. Rotate the API secret +
  this server's password before going live (they were shared in chat).
- **Still on `sim`:** `config.yaml` has `environment: sim`. Going live is a deliberate
  one-line change + `systemctl restart`.
