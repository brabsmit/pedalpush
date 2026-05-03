"""BPM enrichment via GetSongBPM, cached in local SQLite.

CLI:
  python3 bpm_enrichment.py --playlist PLAYLIST_ID   # enrich one playlist
  python3 bpm_enrichment.py --primary-signal         # enrich all primary-taste playlists
  python3 bpm_enrichment.py --stats                  # show cache stats
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from spotify_auth import get_access_token

DB_PATH = os.path.join(os.path.dirname(__file__), "pedalpush.sqlite")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env.local")
GSB_BASE = "https://api.getsong.co/search/"
RATE_LIMIT_SECONDS = 1.1  # be polite; conservative spacing

# Mirror of analyze_taste.py — primary taste signal playlists
PRIMARY_PLAYLIST_IDS = {
    "Bryan's Spin 4/8/24": "3jUpsQ7OOoCD8mxmwDcKeI",
    "Bryan's Spin 3/18/24": "4cw0hrkIpjYNQgaIoyGnLD",
    "Bryan's Spin 3/1/23": "1sWliY8oGpSDCNMbvf3191",
    "Spin Ideas": "3dRasUYmZpWwRqOBwujhvj",
    "Bryan's Spin 2/27/24": "716rafxuMMX9hdgbxqUcCn",
    "Bryan's Spin 2/25/24": "0PFToxHTSvCnUYjENo4AQh",
    "Bryan's Spin 2/7/24": "1WpGRWWhr0GyPkkfqaRreX",
    "Bryan's Spin 2/6/24": "00C16l6Nis7WMsBaiV8gmR",
    "Bryan's Spin 2/5/24": "4NNSRNhLH26u8UlG6M2alI",
    "Bryan's Spin (untitled)": "3J5iZ0fg3Op0D2NaLmbqqW",
    "CB Ideas": "5yllvI6BDkYRYouZePyzvH",
    "CB Audition 1": "4hbYLmJNh4W0voiOuqN8Jj",
    "CB Audition 2": "4mzhbVQ0kTb2FSneEOVJce",
    "Finale Songs": "5bjzCU0HBKjXfS3GACmrwY",
    "Warmup Songs": "3jXBHq3KZJOYwt0h9a4mSk",
    "Cooldown Songs": "27BqaNuQtIkfdklnl2fp2R",
}


# ---------- env / db ----------

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_bpm (
            spotify_track_id TEXT PRIMARY KEY,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            duration_ms INTEGER,
            bpm REAL,
            time_sig TEXT,
            song_key TEXT,
            danceability INTEGER,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            raw_response TEXT,
            looked_up_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def cache_get(conn: sqlite3.Connection, track_id: str) -> dict | None:
    row = conn.execute(
        "SELECT spotify_track_id, artist, title, duration_ms, bpm, source, status "
        "FROM track_bpm WHERE spotify_track_id = ?", (track_id,)
    ).fetchone()
    if not row:
        return None
    keys = ["spotify_track_id", "artist", "title", "duration_ms", "bpm", "source", "status"]
    return dict(zip(keys, row))


def cache_put(conn: sqlite3.Connection, **kwargs) -> None:
    kwargs.setdefault("looked_up_at", datetime.now(timezone.utc).isoformat())
    cols = ",".join(kwargs.keys())
    placeholders = ",".join(["?"] * len(kwargs))
    conn.execute(
        f"INSERT OR REPLACE INTO track_bpm ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    conn.commit()


# ---------- spotify ----------

def sp_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_playlist_tracks(token: str, pid: str) -> list[dict]:
    """Returns: [{id, name, artist (primary), duration_ms}, ...]"""
    out: list[dict] = []
    next_path = f"/playlists/{pid}/tracks?limit=100&fields=items(track(id,name,duration_ms,artists(name))),next"
    while next_path:
        page = sp_get(token, next_path)
        for item in page.get("items", []):
            t = item.get("track")
            if t and t.get("id"):
                primary_artist = t["artists"][0]["name"] if t["artists"] else ""
                out.append({
                    "id": t["id"],
                    "name": t["name"],
                    "artist": primary_artist,
                    "duration_ms": t.get("duration_ms"),
                })
        nxt = page.get("next")
        next_path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
    return out


# ---------- getsongbpm ----------

_VERSION_KEYWORDS = (
    "feat", "featuring", "ft.", "remix", "edit", "mix", "version", "live",
    "remaster", "radio", "vip", "mashup", "mash-up", "extended", "club",
    "instrumental", "acoustic", "demo", "bonus",
)


def normalize_title(title: str) -> str:
    """Aggressive normalization to maximize GetSongBPM match rate.
    - Strip ' - <anything>' suffix (Spotify version delimiter)
    - Strip '[...]' bracketed suffixes
    - Strip '(...)' parenthetical only if it contains a version keyword
    """
    # Strip everything after first ' - '
    if " - " in title:
        title = title.split(" - ", 1)[0]
    # Strip [bracketed] tail
    if "[" in title:
        title = title.split("[", 1)[0]
    # Strip (parenthetical) only if it looks version-related
    if "(" in title:
        before, _, after = title.partition("(")
        inner = after.split(")", 1)[0].lower()
        if any(kw in inner for kw in _VERSION_KEYWORDS):
            title = before
    return title.strip()


def gsb_lookup(api_key: str, artist: str, title: str, max_retries: int = 3) -> tuple[str, dict | None]:
    """Returns (status, payload). status in {'ok', 'not_found', 'error'}.
    Retries network errors with exponential backoff. API key is never stored
    in returned payload (only the lookup terms are surfaced for debugging)."""
    title_norm = normalize_title(title)
    lookup = f"song:{title_norm} artist:{artist}"
    url = f"{GSB_BASE}?{urllib.parse.urlencode({'api_key': api_key, 'type': 'both', 'lookup': lookup})}"
    last_err = None
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(2 ** attempt)  # 2s, 4s
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = json.load(resp)
            search = body.get("search")
            if isinstance(search, list) and search:
                return "ok", body
            return "not_found", body
        except Exception as e:
            last_err = e
    return "error", {"error": str(last_err), "lookup": lookup}  # api_key NOT included


def extract_features(payload: dict) -> dict:
    """Pull tempo, time_sig, key, danceability from a successful response payload."""
    out: dict = {"bpm": None, "time_sig": None, "song_key": None, "danceability": None}
    search = payload.get("search")
    if not isinstance(search, list) or not search:
        return out
    first = search[0]
    for k in ("tempo", "bpm"):
        if first.get(k) not in (None, "", "0"):
            try:
                out["bpm"] = float(first[k])
                break
            except (TypeError, ValueError):
                pass
    out["time_sig"] = first.get("time_sig") or None
    out["song_key"] = first.get("key_of") or None
    d = first.get("danceability")
    if isinstance(d, (int, float)):
        out["danceability"] = int(d)
    elif isinstance(d, str) and d.isdigit():
        out["danceability"] = int(d)
    return out


# ---------- orchestration ----------

def enrich_tracks(conn: sqlite3.Connection, tracks: list[dict], api_key: str, verbose: bool = True) -> dict:
    stats = {"cached_hit": 0, "looked_up": 0, "ok": 0, "not_found": 0, "error": 0}
    for t in tracks:
        existing = cache_get(conn, t["id"])
        if existing:
            stats["cached_hit"] += 1
            continue

        time.sleep(RATE_LIMIT_SECONDS)
        status, payload = gsb_lookup(api_key, t["artist"], t["name"])
        feats = extract_features(payload) if status == "ok" else {"bpm": None, "time_sig": None, "song_key": None, "danceability": None}
        bpm = feats["bpm"]
        # If we got a search hit but no usable bpm, treat as not_found for our purposes
        recorded_status = "ok" if (status == "ok" and bpm is not None) else (
            "not_found" if status in ("not_found", "ok") else "error"
        )

        cache_put(
            conn,
            spotify_track_id=t["id"],
            artist=t["artist"],
            title=t["name"],
            duration_ms=t.get("duration_ms"),
            bpm=bpm,
            time_sig=feats["time_sig"],
            song_key=feats["song_key"],
            danceability=feats["danceability"],
            source="getsongbpm",
            status=recorded_status,
            raw_response=json.dumps(payload)[:5000],  # truncate to keep cache compact
        )
        stats["looked_up"] += 1
        stats[recorded_status] = stats.get(recorded_status, 0) + 1
        if verbose:
            bpm_str = f"{bpm:.1f}" if bpm is not None else "—"
            print(f"  [{recorded_status:>9s}] bpm={bpm_str:>5s}  {t['artist']} — {t['name']}")
    return stats


def cmd_playlist(playlist_id: str) -> int:
    env = load_env()
    api_key = env.get("GET_SONG_BPM_API_KEY")
    if not api_key:
        print("Missing GET_SONG_BPM_API_KEY in .env.local")
        return 1
    token = get_access_token()
    conn = db_connect()
    tracks = get_playlist_tracks(token, playlist_id)
    print(f"Enriching {len(tracks)} tracks from playlist {playlist_id}...")
    stats = enrich_tracks(conn, tracks, api_key)
    print(f"\nResult: {stats}")
    return 0


def cmd_primary_signal() -> int:
    env = load_env()
    api_key = env.get("GET_SONG_BPM_API_KEY")
    if not api_key:
        print("Missing GET_SONG_BPM_API_KEY in .env.local")
        return 1
    token = get_access_token()
    conn = db_connect()

    seen: set[str] = set()
    all_tracks: list[dict] = []
    for label, pid in PRIMARY_PLAYLIST_IDS.items():
        try:
            ts = get_playlist_tracks(token, pid)
        except Exception as e:
            print(f"WARN failed to fetch {label}: {e}", file=sys.stderr)
            continue
        for t in ts:
            if t["id"] not in seen:
                seen.add(t["id"])
                all_tracks.append(t)

    print(f"Enriching {len(all_tracks)} unique tracks from primary signal playlists...\n")
    stats = enrich_tracks(conn, all_tracks, api_key)
    print(f"\nResult: {stats}")
    print(f"Coverage: {stats.get('ok', 0) + stats.get('cached_hit', 0)} / {len(all_tracks)}")
    return 0


def cmd_retry_errors() -> int:
    env = load_env()
    api_key = env.get("GET_SONG_BPM_API_KEY")
    if not api_key:
        print("Missing GET_SONG_BPM_API_KEY in .env.local")
        return 1
    conn = db_connect()
    rows = conn.execute(
        "SELECT spotify_track_id, artist, title, duration_ms FROM track_bpm WHERE status='error'"
    ).fetchall()
    if not rows:
        print("No error rows to retry.")
        return 0
    print(f"Retrying {len(rows)} error rows...\n")
    # Wipe error rows so enrich_tracks doesn't skip them
    conn.execute("DELETE FROM track_bpm WHERE status='error'")
    conn.commit()
    tracks = [
        {"id": r[0], "artist": r[1], "name": r[2], "duration_ms": r[3]} for r in rows
    ]
    stats = enrich_tracks(conn, tracks, api_key)
    print(f"\nRetry result: {stats}")
    return 0


def cmd_retry_not_found() -> int:
    env = load_env()
    api_key = env.get("GET_SONG_BPM_API_KEY")
    if not api_key:
        print("Missing GET_SONG_BPM_API_KEY in .env.local")
        return 1
    conn = db_connect()
    rows = conn.execute(
        "SELECT spotify_track_id, artist, title, duration_ms FROM track_bpm WHERE status='not_found'"
    ).fetchall()
    if not rows:
        print("No not_found rows to retry.")
        return 0
    print(f"Retrying {len(rows)} not_found rows with current normalization...\n")
    conn.execute("DELETE FROM track_bpm WHERE status='not_found'")
    conn.commit()
    tracks = [
        {"id": r[0], "artist": r[1], "name": r[2], "duration_ms": r[3]} for r in rows
    ]
    stats = enrich_tracks(conn, tracks, api_key)
    print(f"\nRetry result: {stats}")
    return 0


def cmd_scrub_keys() -> int:
    """One-time cleanup: remove leaked api_key from cached raw_response rows."""
    conn = db_connect()
    rows = conn.execute("SELECT spotify_track_id, raw_response FROM track_bpm WHERE raw_response LIKE '%api_key%'").fetchall()
    if not rows:
        print("No raw_response rows contain api_key.")
        return 0
    import re as _re
    cleaned = 0
    for tid, raw in rows:
        new = _re.sub(r"api_key=[a-zA-Z0-9]+", "api_key=REDACTED", raw or "")
        conn.execute("UPDATE track_bpm SET raw_response=? WHERE spotify_track_id=?", (new, tid))
        cleaned += 1
    conn.commit()
    print(f"Scrubbed api_key from {cleaned} cached rows.")
    return 0


def cmd_stats() -> int:
    conn = db_connect()
    rows = conn.execute("SELECT status, COUNT(*) FROM track_bpm GROUP BY status").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM track_bpm").fetchone()[0]
    print(f"Cached: {total} tracks")
    for status, n in rows:
        print(f"  {status:>10s}: {n}")
    if total:
        ok_rows = conn.execute(
            "SELECT bpm FROM track_bpm WHERE status='ok' AND bpm IS NOT NULL"
        ).fetchall()
        if ok_rows:
            bpms = [r[0] for r in ok_rows]
            print(f"\nBPM range: {min(bpms):.0f} – {max(bpms):.0f}")
            buckets = {"<70": 0, "70-100": 0, "100-130": 0, "130-160": 0, ">160": 0}
            for b in bpms:
                if b < 70: buckets["<70"] += 1
                elif b < 100: buckets["70-100"] += 1
                elif b < 130: buckets["100-130"] += 1
                elif b < 160: buckets["130-160"] += 1
                else: buckets[">160"] += 1
            for k, v in buckets.items():
                print(f"  {k:>8s}: {v}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--playlist", help="Spotify playlist ID")
    g.add_argument("--primary-signal", action="store_true")
    g.add_argument("--retry-errors", action="store_true")
    g.add_argument("--retry-not-found", action="store_true")
    g.add_argument("--scrub-keys", action="store_true")
    g.add_argument("--stats", action="store_true")
    args = p.parse_args()

    if args.playlist:
        return cmd_playlist(args.playlist)
    if args.primary_signal:
        return cmd_primary_signal()
    if args.retry_errors:
        return cmd_retry_errors()
    if args.retry_not_found:
        return cmd_retry_not_found()
    if args.scrub_keys:
        return cmd_scrub_keys()
    if args.stats:
        return cmd_stats()
    return 1


if __name__ == "__main__":
    sys.exit(main())
