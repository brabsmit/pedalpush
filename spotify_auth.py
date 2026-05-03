"""Token helper. After setup_oauth.py has run once, call get_access_token()
to mint a short-lived bearer token using the persisted refresh token.

Caches the access token in-process until 60s before expiry.
"""
import base64
import json
import os
import time
import urllib.parse
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env.local")

_cache: dict[str, float | str] = {"token": "", "expires_at": 0.0}


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_access_token() -> str:
    if _cache["token"] and time.time() < float(_cache["expires_at"]) - 60:
        return str(_cache["token"])

    env = _load_env()
    cid = env["SPOTIFY_CLIENT_ID"]
    sec = env["SPOTIFY_CLIENT_SECRET"]
    refresh = env["SPOTIFY_REFRESH_TOKEN"]

    creds = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }).encode(),
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = json.load(resp)

    _cache["token"] = body["access_token"]
    _cache["expires_at"] = time.time() + body["expires_in"]
    return body["access_token"]
