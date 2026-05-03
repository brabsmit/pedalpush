"""Backfill track_bpm.release_date from Spotify /tracks/ batched 50/call.

Only fills rows where release_date IS NULL and bpm IS NOT NULL (i.e. tracks
that can actually appear as candidates).

  python3 enrich_release_dates.py
"""
import json
import sys
import urllib.parse
import urllib.request

from bpm_enrichment import db_connect
from spotify_auth import get_access_token


def fetch_release_dates(track_ids: list[str], token: str) -> dict[str, str]:
    """Returns {track_id: release_date} for the given IDs (up to 50)."""
    ids_param = urllib.parse.quote(",".join(track_ids))
    req = urllib.request.Request(
        f"https://api.spotify.com/v1/tracks?ids={ids_param}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    out: dict[str, str] = {}
    for tr in data.get("tracks") or []:
        if not tr:
            continue
        rd = (tr.get("album") or {}).get("release_date")
        if rd:
            out[tr["id"]] = rd
    return out


def main() -> int:
    conn = db_connect()
    rows = conn.execute(
        "SELECT spotify_track_id FROM track_bpm "
        "WHERE bpm IS NOT NULL AND release_date IS NULL"
    ).fetchall()
    pending = [r[0] for r in rows]
    print(f"Pending: {len(pending)} tracks need release_date")
    if not pending:
        return 0

    token = get_access_token()
    filled = 0
    for i in range(0, len(pending), 50):
        chunk = pending[i:i + 50]
        try:
            dates = fetch_release_dates(chunk, token)
        except Exception as e:
            print(f"  batch {i}: {e}", file=sys.stderr)
            continue
        for tid, rd in dates.items():
            conn.execute(
                "UPDATE track_bpm SET release_date = ? WHERE spotify_track_id = ?",
                (rd, tid),
            )
            filled += 1
        conn.commit()
        print(f"  {min(i + 50, len(pending))}/{len(pending)}  filled={filled}")
    print(f"Done. Filled {filled}/{len(pending)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
