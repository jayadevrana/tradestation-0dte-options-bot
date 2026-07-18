"""One-time OAuth helper: produce a TradeStation REFRESH TOKEN.

Three ways to use it:

1) LOCAL (you log in yourself on your own PC):
       python scripts/get_refresh_token.py
   Opens the browser, captures the code on a local callback server, prints the token.

2) WITHOUT THE CLIENT SHARING THEIR PASSWORD (recommended for the client's account):
   a. You generate the login link and send it to the client:
          python scripts/get_refresh_token.py --auth-url
   b. The client opens the link, logs in on TradeStation's OWN page (password stays
      with them), and lands on a "can't reach localhost" page. They copy the FULL URL
      from the address bar (it contains ?code=...) and send it to you.
   c. You exchange it for the refresh token (do this within ~1 min — codes expire):
          python scripts/get_refresh_token.py --code "<paste the URL or the code>"

Requires in .env:  TS_CLIENT_ID, TS_CLIENT_SECRET, TS_REDIRECT_URI
TS_REDIRECT_URI must EXACTLY match what's registered on the API key.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import SIGNIN_BASE  # noqa: E402

load_dotenv()

CLIENT_ID = os.getenv("TS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TS_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("TS_REDIRECT_URI", "http://localhost:8182/callback")
AUDIENCE = "https://api.tradestation.com"
SCOPE = "openid profile offline_access MarketData ReadAccount Trade OptionSpreads"

_auth_code = {}


def build_auth_url() -> str:
    return f"{SIGNIN_BASE}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "audience": AUDIENCE,
        "scope": SCOPE,
        "state": "bot-auth",
    })


def extract_code(value: str) -> str:
    """Accept either a raw code or the full redirected URL and return the code."""
    value = value.strip()
    if "code=" in value:
        qs = urllib.parse.urlparse(value).query or value.split("?", 1)[-1]
        params = urllib.parse.parse_qs(qs)
        if "code" in params:
            return params["code"][0]
    return value  # assume they pasted just the code


def exchange(code: str) -> None:
    print("Exchanging code for tokens...")
    resp = requests.post(
        f"{SIGNIN_BASE}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed {resp.status_code}: {resp.text}\n"
                 "(Codes expire fast and are single-use — generate a fresh link and retry.)")
    data = resp.json()
    refresh = data.get("refresh_token")
    if not refresh:
        sys.exit(f"No refresh_token returned (need offline_access scope). Raw: {data}")
    print("\n" + "=" * 70)
    print("SUCCESS. Add this line to your .env file:\n")
    print(f"TS_REFRESH_TOKEN={refresh}")
    print("=" * 70 + "\n")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _auth_code["code"] = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code in callback. Check the redirect URI.")

    def log_message(self, *args):
        return


def local_flow() -> None:
    parsed = urllib.parse.urlparse(REDIRECT_URI)
    host, port = parsed.hostname or "localhost", parsed.port or 8182
    auth_url = build_auth_url()
    print("\nOpen this URL and log in (it should open automatically):\n")
    print(auth_url, "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print(f"Waiting for the login redirect on http://{host}:{port} ...")
    server = HTTPServer((host, port), _Handler)
    while "code" not in _auth_code:
        server.handle_request()
    exchange(_auth_code["code"])


def main():
    p = argparse.ArgumentParser(description="Generate a TradeStation refresh token.")
    p.add_argument("--auth-url", action="store_true",
                   help="just print the login link to send to the account owner")
    p.add_argument("--code", metavar="CODE_OR_URL",
                   help="exchange a code (or pasted redirect URL) for the refresh token")
    args = p.parse_args()

    # Building the login link only needs the API Key (Client ID).
    if args.auth_url:
        if not CLIENT_ID:
            sys.exit("Set TS_CLIENT_ID (your API Key) in .env first.")
        print("\nSend this link to the account owner. They log in (their password stays")
        print("with them) and send you back the URL they land on:\n")
        print(build_auth_url(), "\n")
        return

    # Exchanging a code, or the local flow, needs both ID and Secret.
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("Set TS_CLIENT_ID and TS_CLIENT_SECRET in .env first.")
    if args.code:
        exchange(extract_code(args.code))
        return
    local_flow()


if __name__ == "__main__":
    main()
