"""Test whether the Spotify app has access to deprecated endpoints.

200 on audio-features  -> app is grandfathered (created before 2024-11-27)
403 on audio-features  -> app is post-deprecation, audio features unavailable
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(__file__), ".env.local")
TEST_TRACK_ID = "11dFghVXANMlKmJXsNCbNl"  # Carly Rae Jepsen - Cut To The Feeling


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_token(client_id: str, client_secret: str) -> str:
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["access_token"]


def probe(token: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main() -> int:
    env = load_env(ENV_PATH)
    cid = env.get("SPOTIFY_CLIENT_ID")
    sec = env.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not sec:
        print("Missing SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET in .env.local")
        return 1

    print(f"Using client_id: {cid[:6]}...{cid[-4:]}")
    token = get_token(cid, sec)
    print("Got access token via client_credentials flow.\n")

    checks = [
        ("audio-features (deprecated)", f"/audio-features/{TEST_TRACK_ID}"),
        ("recommendations (deprecated)", "/recommendations?seed_genres=pop&limit=1"),
        ("track lookup (always available)", f"/tracks/{TEST_TRACK_ID}"),
    ]
    for label, path in checks:
        status, body = probe(token, path)
        verdict = "OK" if status == 200 else f"BLOCKED ({status})"
        print(f"  {label:42s} -> {verdict}")
        if status != 200:
            print(f"     body: {body}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
