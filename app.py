"""PedalPush drag-and-drop playlist builder.

Run:
  python3 app.py            # serves on http://127.0.0.1:8765
  python3 app.py --port 9000
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from candidates import VARIANT_BPM_RANGES, get_candidates
from spotify_auth import get_access_token


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def spotify_api(method: str, path: str, body: dict | None = None) -> dict:
    token = get_access_token()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def publish_playlist(name: str, track_ids: list[str], description: str = "") -> dict:
    me = spotify_api("GET", "/me")
    user_id = me["id"]
    playlist = spotify_api(
        "POST",
        f"/users/{urllib.parse.quote(user_id)}/playlists",
        {"name": name, "public": False, "description": description},
    )
    pid = playlist["id"]
    # Spotify caps add-tracks at 100 per call.
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i:i + 100]
        uris = [f"spotify:track:{t}" for t in chunk]
        spotify_api("POST", f"/playlists/{pid}/tracks", {"uris": uris})
    return {
        "id": pid,
        "url": playlist["external_urls"]["spotify"],
        "name": playlist["name"],
        "track_count": len(track_ids),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet default access log
        return

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/candidates":
            qs = urllib.parse.parse_qs(parsed.query)
            variant = (qs.get("variant", [""])[0] or "").lower()
            if variant not in VARIANT_BPM_RANGES:
                self._send_json(400, {"error": f"variant must be one of {sorted(VARIANT_BPM_RANGES)}"})
                return
            include_past = qs.get("include_past", ["0"])[0] in ("1", "true", "yes")
            shuffle = qs.get("shuffle", ["0"])[0] in ("1", "true", "yes")
            q = qs.get("q", [""])[0]
            try:
                limit = max(1, min(500, int(qs.get("limit", ["200"])[0])))
            except ValueError:
                limit = 200
            cands = get_candidates(
                variant,
                exclude_past=not include_past,
                q=q,
                limit=limit,
                shuffle=shuffle,
            )
            self._send_json(200, {"variant": variant, "count": len(cands), "candidates": cands})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/publish":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return
            name = (payload.get("name") or "").strip()
            track_ids = payload.get("track_ids") or []
            if not name:
                self._send_json(400, {"error": "name is required"})
                return
            if not track_ids or not isinstance(track_ids, list):
                self._send_json(400, {"error": "track_ids must be a non-empty list"})
                return
            try:
                result = publish_playlist(name, track_ids, payload.get("description", ""))
            except urllib.error.HTTPError as e:
                self._send_json(502, {"error": f"spotify {e.code}: {e.read().decode(errors='replace')}"})
                return
            self._send_json(200, result)
            return
        self.send_error(404)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"PedalPush running at http://{args.host}:{args.port}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
