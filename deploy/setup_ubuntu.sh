#!/usr/bin/env bash
# One-shot provisioner for the TradeStation bot on a fresh Ubuntu 22.04/24.04 server.
# Run as root AFTER copying the project to /opt/tradestation-bot (incl. .env):
#     sudo bash /opt/tradestation-bot/deploy/setup_ubuntu.sh
set -euo pipefail

APP_DIR=/opt/tradestation-bot
APP_USER=tsbot

echo "==> 1/8 system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https ufw

echo "==> 2/8 Caddy (TLS-capable reverse proxy)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
mkdir -p /var/log/caddy

echo "==> 3/8 service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> 4/8 python venv + dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> 5/8 sanity: config + secrets present"
test -f "$APP_DIR/config.yaml" || { echo "MISSING config.yaml"; exit 1; }
test -f "$APP_DIR/.env" || { echo "MISSING .env (copy it from your machine — it holds the API key/secret/refresh token)"; exit 1; }

echo "==> 6/8 permissions"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> 7/8 systemd service + Caddy config"
cp "$APP_DIR/deploy/tradestation-bot.service" /etc/systemd/system/
cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now tradestation-bot
systemctl reload caddy || systemctl restart caddy

echo "==> 8/8 firewall (SSH + 80 + 443)"
ufw allow OpenSSH || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true
yes | ufw enable || true

echo
echo "==================================================================="
echo " DONE. Quick checks:"
echo "   systemctl status tradestation-bot --no-pager"
echo "   journalctl -u tradestation-bot -f"
echo "   curl -s http://127.0.0.1/health"
echo
echo " Your TradingView webhook URL:  http://<THIS_SERVER_IP>/webhook"
echo "==================================================================="
