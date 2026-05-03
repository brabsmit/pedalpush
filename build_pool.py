"""Build the candidate track pool.

Phase A: pull tracks from primary + secondary-signal playlists already in
Bryan's library, populate playlists/playlist_tracks tables, BPM-enrich any
tracks not yet in the cache.

CLI:
  python3 build_pool.py --pull       # fetch playlists + tracks (idempotent)
  python3 build_pool.py --enrich     # BPM-enrich tracks not yet in cache
  python3 build_pool.py --mine       # Phase B: search public spin playlists, keep
                                     # those overlapping with primary signal
  python3 build_pool.py --stats      # pool size + per-tier breakdown
  python3 build_pool.py --all        # pull, then enrich, then stats (no mining)
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from spotify_auth import get_access_token
from bpm_enrichment import (
    DB_PATH, db_connect, enrich_tracks, load_env,
    PRIMARY_PLAYLIST_IDS,
)

# Secondary signal: instructor-curated and BPM-bucketed playlists Bryan used
# as inspiration. Lower weight than primary, higher weight than generic mining.
SECONDARY_PLAYLIST_IDS: dict[str, dict] = {
    # Bike Bar instructor-published
    "Bike Bar — lily (a)":     {"id": "6ifYGd2cEE8XkpTTfq5C7l", "source": "Bike Bar"},
    "Bike Bar — BB45 Lily":    {"id": "7Mz0NeFTSkgYUI9Bu1xZzt", "source": "Bike Bar"},
    "Bike Bar — NOACT":        {"id": "1vOb077VghylfGZi7u3mqt", "source": "Bike Bar"},
    "Bike Bar — lily (b)":     {"id": "1i63nV7xxqYpw8mcJhGVa4", "source": "Bike Bar"},
    # BBTV instructors
    "BBTV Zoe 1":              {"id": "3O0xz7atIMtVFwUkoWB2SW", "source": "BBTV"},
    "BBTV Georgia 1":          {"id": "15oVeHMtKoaDCM8JdZvVDk", "source": "BBTV"},
    "BBTV JD 1":               {"id": "0JW8kqFG0MxUCBOHSSTOCg", "source": "BBTV"},
    "BBTV Darcy 1":            {"id": "0aTH04UhQ3kCOeP6UVaByB", "source": "BBTV"},
    # BPM-bucketed pool
    "100-110 BPM (Kelsi)":     {"id": "4DIWgAnTXMi87tIy4C48l4", "source": "100-110 BPM"},
}


# ---------- schema ----------

def ensure_pool_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS playlists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            owner TEXT NOT NULL,
            signal_tier TEXT NOT NULL,
            source_label TEXT,
            snapshot_id TEXT,
            track_count INTEGER,
            fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id TEXT NOT NULL,
            track_id TEXT NOT NULL,
            position INTEGER,
            PRIMARY KEY (playlist_id, track_id)
        );
        CREATE INDEX IF NOT EXISTS ix_playlist_tracks_track
            ON playlist_tracks(track_id);
    """)
    conn.commit()


# ---------- spotify ----------

def sp_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def fetch_playlist(token: str, pid: str) -> tuple[dict, list[dict]]:
    """Return (meta, tracks). Each track: {id, artist, name, duration_ms, position}."""
    meta = sp_get(token, f"/playlists/{pid}?fields=id,name,owner(display_name,id),snapshot_id,tracks(total)")
    tracks: list[dict] = []
    pos = 0
    next_path = f"/playlists/{pid}/tracks?limit=100&fields=items(track(id,name,duration_ms,artists(name))),next"
    while next_path:
        page = sp_get(token, next_path)
        for item in page.get("items", []):
            t = item.get("track")
            if not t or not t.get("id"):
                pos += 1
                continue
            tracks.append({
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"] if t["artists"] else "",
                "duration_ms": t.get("duration_ms"),
                "position": pos,
            })
            pos += 1
        nxt = page.get("next")
        next_path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
    return meta, tracks


# ---------- pull ----------

PRIMARY_REGISTRY = {label: {"id": pid, "source": "Bryan primary"}
                    for label, pid in PRIMARY_PLAYLIST_IDS.items()}


def pull_all(conn: sqlite3.Connection, token: str) -> dict:
    ensure_pool_schema(conn)
    stats = {"playlists": 0, "tracks_inserted": 0, "tracks_seen": 0}
    now = datetime.now(timezone.utc).isoformat()

    plans: list[tuple[str, str, dict]] = (
        [(label, "primary", info) for label, info in PRIMARY_REGISTRY.items()]
        + [(label, "secondary", info) for label, info in SECONDARY_PLAYLIST_IDS.items()]
    )

    for label, tier, info in plans:
        pid = info["id"]
        try:
            meta, tracks = fetch_playlist(token, pid)
        except Exception as e:
            print(f"WARN failed {label} ({pid}): {e}", file=sys.stderr)
            continue

        conn.execute("""
            INSERT INTO playlists (id, name, owner, signal_tier, source_label, snapshot_id, track_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                owner=excluded.owner,
                signal_tier=excluded.signal_tier,
                source_label=excluded.source_label,
                snapshot_id=excluded.snapshot_id,
                track_count=excluded.track_count,
                fetched_at=excluded.fetched_at
        """, (
            meta["id"],
            meta["name"],
            meta["owner"].get("display_name") or meta["owner"]["id"],
            tier,
            info["source"],
            meta.get("snapshot_id"),
            len(tracks),
            now,
        ))

        # Refresh membership: clear and re-insert (handles removed tracks)
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
        conn.executemany(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            [(pid, t["id"], t["position"]) for t in tracks],
        )
        conn.commit()

        stats["playlists"] += 1
        stats["tracks_seen"] += len(tracks)
        print(f"  [{tier:>9s}] {len(tracks):>3}t  {label}")

    return stats


# ---------- mine (phase B) ----------

MINING_QUERIES = [
    "spin",
    "spin class",
    "spin playlist",
    "cycling",
    "indoor cycling",
    "cyclebar",
    "soulcycle",
    "bike bar",
]


def search_playlists(token: str, query: str, max_pages: int = 5) -> list[dict]:
    """Paginated playlist search. 50 per page; Spotify caps offset at 1000."""
    out: list[dict] = []
    for page_idx in range(max_pages):
        offset = page_idx * 50
        qs = urllib.parse.urlencode({
            "q": query, "type": "playlist", "limit": 50, "offset": offset,
        })
        try:
            page = sp_get(token, f"/search?{qs}")
        except Exception:
            break
        items = page.get("playlists", {}).get("items", []) or []
        items = [p for p in items if p]
        if not items:
            break
        out.extend(items)
        if len(items) < 50:
            break
    return out


def fetch_playlist_track_meta(token: str, pid: str) -> list[dict]:
    """Returns [{id, artist (primary, lowercased)}, ...]"""
    out: list[dict] = []
    next_path = f"/playlists/{pid}/tracks?limit=100&fields=items(track(id,artists(name))),next"
    while next_path:
        page = sp_get(token, next_path)
        for item in page.get("items", []) or []:
            t = item.get("track")
            if t and t.get("id"):
                primary_artist = t["artists"][0]["name"].lower() if t.get("artists") else ""
                out.append({"id": t["id"], "artist": primary_artist})
        nxt = page.get("next")
        next_path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
    return out


def mine(conn: sqlite3.Connection, token: str,
         min_shared_artists: int = 5,
         min_size: int = 8, max_size: int = 80) -> dict:
    ensure_pool_schema(conn)

    # Build Bryan's primary artist set (the comparison anchor).
    # Use track_bpm as the artist-name source for primary tracks.
    rows = conn.execute("""
        SELECT DISTINCT LOWER(tb.artist)
        FROM playlist_tracks pt
        JOIN playlists pl ON pl.id = pt.playlist_id
        JOIN track_bpm tb ON tb.spotify_track_id = pt.track_id
        WHERE pl.signal_tier = 'primary' AND tb.artist IS NOT NULL AND tb.artist != ''
    """).fetchall()
    primary_artists = {r[0] for r in rows}
    print(f"Primary artist set: {len(primary_artists)} unique artists\n")

    # Existing playlist IDs (don't re-process)
    existing_ids = {r[0] for r in conn.execute("SELECT id FROM playlists").fetchall()}

    # Search across all queries, dedupe candidates
    candidates: dict[str, dict] = {}  # id -> playlist meta
    for q in MINING_QUERIES:
        try:
            results = search_playlists(token, q)
        except Exception as e:
            print(f"WARN search '{q}' failed: {e}", file=sys.stderr)
            continue
        new_for_query = 0
        for p in results:
            pid = p.get("id")
            if not pid or pid in candidates or pid in existing_ids:
                continue
            owner_id = (p.get("owner") or {}).get("id", "")
            track_count = (p.get("tracks") or {}).get("total", 0)
            # Pre-filter: skip Spotify-owned (algorithmic), skip out-of-size playlists
            if owner_id == "spotify":
                continue
            if track_count < min_size or track_count > max_size:
                continue
            candidates[pid] = p
            new_for_query += 1
        print(f"  search '{q}': {new_for_query} new candidates after pre-filter")
    print(f"\nTotal unique candidates: {len(candidates)}\n")

    # For each candidate, fetch tracks and compute overlap
    now = datetime.now(timezone.utc).isoformat()
    stats = {"checked": 0, "kept": 0, "rejected": 0, "errors": 0,
             "tracks_added": 0}

    for i, (pid, p) in enumerate(candidates.items(), 1):
        try:
            tracks = fetch_playlist_track_meta(token, pid)
        except Exception as e:
            stats["errors"] += 1
            print(f"  [{i:>3}/{len(candidates)}] ERROR fetching {p.get('name')}: {e}",
                  file=sys.stderr)
            continue
        stats["checked"] += 1
        if not tracks:
            stats["rejected"] += 1
            continue
        cand_artists = {t["artist"] for t in tracks if t["artist"]}
        shared_artists = primary_artists & cand_artists
        if len(shared_artists) < min_shared_artists:
            stats["rejected"] += 1
            if len(shared_artists) >= max(1, min_shared_artists - 2):
                print(f"  [{i:>3}/{len(candidates)}] reject shared_artists={len(shared_artists):>2}  "
                      f"{len(tracks):>3}t  {p.get('name', '?')[:50]}", file=sys.stderr)
            continue

        # Persist as mined
        owner = (p.get("owner") or {}).get("display_name") or (p.get("owner") or {}).get("id", "?")
        artist_rate = len(shared_artists) / max(1, len(cand_artists))
        conn.execute("""
            INSERT INTO playlists (id, name, owner, signal_tier, source_label, snapshot_id, track_count, fetched_at)
            VALUES (?, ?, ?, 'mined', ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
        """, (pid, p.get("name", "?"), owner,
              f"mined:shared_artists={len(shared_artists)},rate={artist_rate:.2%}",
              p.get("snapshot_id"), len(tracks), now))
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
        conn.executemany(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
            [(pid, t["id"], idx) for idx, t in enumerate(tracks)],
        )
        conn.commit()
        stats["kept"] += 1
        stats["tracks_added"] += len(tracks)
        print(f"  [{i:>3}/{len(candidates)}] KEPT  shared_artists={len(shared_artists):>2} ({artist_rate:>4.0%})  "
              f"{len(tracks):>3}t  {p.get('name', '?')[:50]}  by {owner[:25]}")

    return stats


def cmd_mine(min_shared_artists: int) -> int:
    conn = db_connect()
    ensure_pool_schema(conn)
    token = get_access_token()
    # Wipe prior mined-tier so we re-evaluate cleanly
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id IN (SELECT id FROM playlists WHERE signal_tier='mined')")
    conn.execute("DELETE FROM playlists WHERE signal_tier='mined'")
    conn.commit()
    stats = mine(conn, token, min_shared_artists=min_shared_artists)
    print(f"\nMining result: {stats}")
    return 0


# ---------- enrich ----------

def cmd_enrich(conn: sqlite3.Connection) -> int:
    env = load_env()
    api_key = env.get("GET_SONG_BPM_API_KEY")
    if not api_key:
        print("Missing GET_SONG_BPM_API_KEY in .env.local")
        return 1
    token = get_access_token()

    # Find tracks in playlist_tracks but not yet in track_bpm
    rows = conn.execute("""
        SELECT DISTINCT pt.track_id
        FROM playlist_tracks pt
        LEFT JOIN track_bpm tb ON tb.spotify_track_id = pt.track_id
        WHERE tb.spotify_track_id IS NULL
    """).fetchall()
    new_ids = [r[0] for r in rows]
    if not new_ids:
        print("No new tracks to enrich.")
        return 0
    print(f"Enriching {len(new_ids)} new tracks not yet in BPM cache...\n")

    # Fetch artist/title/duration for each in batches via /v1/tracks?ids=
    tracks: list[dict] = []
    for i in range(0, len(new_ids), 50):
        batch = new_ids[i:i + 50]
        page = sp_get(token, "/tracks?ids=" + ",".join(batch))
        for t in page["tracks"]:
            if not t:
                continue
            tracks.append({
                "id": t["id"],
                "name": t["name"],
                "artist": t["artists"][0]["name"] if t["artists"] else "",
                "duration_ms": t.get("duration_ms"),
            })

    stats = enrich_tracks(conn, tracks, api_key)
    print(f"\nEnrichment result: {stats}")
    return 0


# ---------- stats ----------

def cmd_stats(conn: sqlite3.Connection) -> int:
    print("=== Playlists ===")
    rows = conn.execute("""
        SELECT signal_tier, COUNT(*), SUM(track_count)
        FROM playlists GROUP BY signal_tier ORDER BY signal_tier
    """).fetchall()
    for tier, n, total in rows:
        print(f"  {tier:>10s}: {n} playlists, {total} total tracks (with dups)")

    total_unique = conn.execute(
        "SELECT COUNT(DISTINCT track_id) FROM playlist_tracks"
    ).fetchone()[0]
    print(f"\nUnique tracks across all pulled playlists: {total_unique}")

    bpm_rows = conn.execute("""
        SELECT tb.status, COUNT(*)
        FROM track_bpm tb
        WHERE tb.spotify_track_id IN (SELECT track_id FROM playlist_tracks)
        GROUP BY tb.status
    """).fetchall()
    print("\nBPM cache status for pool tracks:")
    enriched_total = 0
    for status, n in bpm_rows:
        print(f"  {status:>10s}: {n}")
        enriched_total += n
    untagged = total_unique - enriched_total
    if untagged:
        print(f"  {'untagged':>10s}: {untagged}")

    print("\n=== Top 20 most-frequent tracks across pool ===")
    rows = conn.execute("""
        SELECT pt.track_id, COUNT(DISTINCT pt.playlist_id) AS freq,
               tb.artist, tb.title, tb.bpm
        FROM playlist_tracks pt
        LEFT JOIN track_bpm tb ON tb.spotify_track_id = pt.track_id
        GROUP BY pt.track_id
        ORDER BY freq DESC
        LIMIT 20
    """).fetchall()
    for tid, freq, artist, title, bpm in rows:
        bpm_str = f"{bpm:>5.0f}" if bpm else "    —"
        print(f"  freq={freq:>2}  bpm={bpm_str}  {artist or '?'} — {title or '?'}")

    return 0


# ---------- main ----------

def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pull", action="store_true")
    g.add_argument("--enrich", action="store_true")
    g.add_argument("--mine", action="store_true")
    g.add_argument("--stats", action="store_true")
    g.add_argument("--all", action="store_true")
    p.add_argument("--min-shared-artists", type=int, default=5,
                   help="Min shared artists with primary (mining filter)")
    args = p.parse_args()

    conn = db_connect()
    ensure_pool_schema(conn)

    if args.pull or args.all:
        token = get_access_token()
        print("Pulling primary + secondary playlists...\n")
        stats = pull_all(conn, token)
        print(f"\nPull result: {stats}")

    if args.mine:
        return cmd_mine(args.min_shared_artists)

    if args.enrich or args.all:
        cmd_enrich(conn)

    if args.stats or args.all:
        print()
        cmd_stats(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
