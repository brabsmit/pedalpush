"""One-time Spotify OAuth setup.

Runs the Authorization Code flow:
  1. Opens browser to Spotify's auth page
  2. Catches the redirect on a local server (127.0.0.1:8888)
  3. Exchanges the code for tokens
  4. Persists SPOTIFY_REFRESH_TOKEN to .env.local
  5. Verifies by calling /me

After this runs once, future scripts use spotify_auth.get_access_token()
to mint short-lived access tokens from the refresh token.
"""
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env.local")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
]


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def upsert_env(path: str, key: str, value: str) -> None:
    """Write or update a single key in a dotenv file, preserving the rest."""
    lines: list[str] = []
    found = False
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        CallbackHandler.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Auth complete. You can close this tab."
        if "error" in params:
            msg = f"Auth error: {params['error']}. Check the terminal."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *_):
        pass  # silence default logging


def run_callback_server(state: str) -> dict[str, str]:
    server = http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Listening on 127.0.0.1:8888 for callback...")
    while "code" not in CallbackHandler.captured and "error" not in CallbackHandler.captured:
        thread.join(timeout=0.5)
    server.shutdown()
    captured = dict(CallbackHandler.captured)
    if captured.get("state") != state:
        raise RuntimeError(f"State mismatch: expected {state}, got {captured.get('state')}")
    if "error" in captured:
        raise RuntimeError(f"Auth error: {captured['error']}")
    return captured


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }).encode(),
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def verify_me(access_token: str) -> dict:
    req = urllib.request.Request(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> int:
    env = load_env(ENV_PATH)
    cid = env.get("SPOTIFY_CLIENT_ID")
    sec = env.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not sec:
        print("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET in .env.local")
        return 1

    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cid,
        "scope": " ".join(SCOPES),
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })

    print(f"Opening browser to authorize app...\n  {auth_url}\n")
    webbrowser.open(auth_url)

    captured = run_callback_server(state)
    print("Got authorization code, exchanging for tokens...")
    tokens = exchange_code(cid, sec, captured["code"])

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not refresh_token or not access_token:
        print(f"Missing tokens in response: {tokens}")
        return 1

    upsert_env(ENV_PATH, "SPOTIFY_REFRESH_TOKEN", refresh_token)
    print(f"Wrote SPOTIFY_REFRESH_TOKEN to {ENV_PATH}")

    me = verify_me(access_token)
    print(f"\nVerified as: {me.get('display_name')} ({me.get('id')})")
    print(f"  email: {me.get('email')}")
    print(f"  country: {me.get('country')}")
    print(f"  product: {me.get('product')}  (premium needed for some endpoints)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
