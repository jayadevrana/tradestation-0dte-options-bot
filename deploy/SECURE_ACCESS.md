# Secure, permanent server access (stop whitelisting dynamic IPs)

This box holds broker API keys that can trade real money. **Never open SSH (port 22)
to `0.0.0.0/0`** — with a weak password it will be brute-forced. Below are permanent,
IP-independent ways to reach it, so you never edit a security-group IP again.

Pick **ONE** of Option A or B, then do the SSH-key baseline, then lock the firewall.

---

## Option A — AWS SSM Session Manager (recommended; AWS-native, port 22 stays CLOSED)
Connect through AWS itself — no open SSH port, no IP management, works from anywhere.

1. **Attach an IAM role to the instance** with the managed policy
   `AmazonSSMManagedInstanceCore` (EC2 → Instance → Actions → Security → Modify IAM role).
2. The SSM agent ships on Ubuntu AWS AMIs. Verify it's running:
   ```bash
   sudo systemctl status amazon-ssm-agent || sudo snap services amazon-ssm-agent
   ```
3. Connect from anywhere (console **Connect → Session Manager**, or CLI):
   ```bash
   aws ssm start-session --target i-xxxxxxxxxxxx
   ```
4. Once SSM works, you can **delete the inbound SSH (22) rule entirely** — admin access
   no longer depends on any IP.

## Option B — Tailscale (simplest; private VPN, works on any device)
1. On the server (one-time, needs SSH or SSM once):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
2. Install Tailscale on your laptop, sign into the same account.
3. SSH to the server's **private tailnet IP** (e.g. `ssh ubuntu@100.x.y.z`) from anywhere.
4. **Remove the public SSH (22) inbound rule** — port 22 is now only reachable over the tailnet.

---

## Baseline (do this regardless) — key auth, no passwords
```bash
# on your laptop, if you don't have a key:
ssh-keygen -t ed25519 -C "tradestation-bot"

# copy your public key to the server (while you still have access):
ssh-copy-id ubuntu@<server>        # or paste into ~/.ssh/authorized_keys

# on the server, disable password auth:
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```
Then **change the `Test123` password** (or it no longer matters once password auth is off).

---

## Final firewall state (security group)
| Port | Source | Why |
|---|---|---|
| 22 (SSH) | **removed** (use SSM) **or** tailnet only | admin access without exposing 22 |
| 80 (HTTP webhook) | TradingView IPs: `52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7` | only TradingView should hit the webhook |
| 443 (HTTPS) | same TradingView IPs (once a domain + TLS is set) | optional, preferred over :80 |

> The webhook is the only thing that must accept inbound internet traffic, and even
> that should be locked to TradingView's four IPs + protected by the shared secret.
> Everything else (SSH/admin) goes through SSM or Tailscale.

## Temporary unblock (only until A or B is set up)
If you need me (or codex) to deploy right now before SSM/Tailscale is ready, add a
**temporary** rule `SSH 22 → <current egress IP>/32`, deploy, then **delete it** once
SSM/Tailscale is working. Don't leave a broad SSH rule in place.
