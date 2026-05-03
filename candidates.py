"""Candidate query API for the playlist builder.

Returns ranked candidates per spin variant (sit/jog/climb) with half-time
matching, source-frequency scoring, and optional past-track exclusion.

CLI:
  python3 candidates.py sit
  python3 candidates.py jog --include-past --limit 50
  python3 candidates.py climb --q matoma
"""
import argparse
import random
import sqlite3

from bpm_enrichment import db_connect


# Variant -> list of (low, high) BPM windows. Half-time windows included so
# a 120 BPM track can serve a ~60 BPM standing climb.
VARIANT_BPM_RANGES: dict[str, list[tuple[float, float]]] = {
    "sit":   [(100, 130)],
    "jog":   [(70, 80), (140, 160)],
    "climb": [(55, 65), (110, 130)],
}

# Midpoints of each BPM window. Used to compute proximity tiebreak — a 115 BPM
# track outranks a 100 BPM track in sit because it sits dead-center.
VARIANT_MIDPOINTS: dict[str, list[float]] = {
    "sit":   [115],
    "jog":   [75, 150],
    "climb": [60, 120],
}

TIER_WEIGHTS: dict[str, int] = {"primary": 3, "secondary": 2, "mined": 1}

# Boost added when a track appears in any primary-tier playlist (past use).
# Large enough to dominate the weighted-overlap term so any past track
# outranks any never-used track.
PRIMARY_PRESENCE_BOOST = 1000


def _bpm_clause(variant: str, bpm_col: str = "tb.bpm") -> tuple[str, list]:
    ranges = VARIANT_BPM_RANGES[variant]
    parts = [f"({bpm_col} BETWEEN ? AND ?)" for _ in ranges]
    params: list = []
    for lo, hi in ranges:
        params.extend([lo, hi])
    return "(" + " OR ".join(parts) + ")", params


def _bpm_proximity_expr(variant: str, bpm_col: str = "tb.bpm") -> str:
    mids = VARIANT_MIDPOINTS[variant]
    parts = [f"ABS({bpm_col} - {m})" for m in mids]
    return parts[0] if len(parts) == 1 else f"MIN({', '.join(parts)})"


def get_candidates(
    variant: str,
    *,
    exclude_past: bool = True,
    q: str = "",
    limit: int = 200,
    shuffle: bool = False,
) -> list[dict]:
    if variant not in VARIANT_BPM_RANGES:
        raise ValueError(f"unknown variant {variant!r}; expected sit/jog/climb")

    bpm_clause, bpm_params = _bpm_clause(variant)

    where: list[str] = [bpm_clause, "tb.bpm IS NOT NULL"]
    params: list = list(bpm_params)

    if q:
        where.append("(LOWER(tb.artist) LIKE ? OR LOWER(tb.title) LIKE ?)")
        like = f"%{q.lower()}%"
        params.extend([like, like])

    # Tracks present in any primary-tier playlist (Bryan's past usage).
    past_subq = """
        SELECT DISTINCT pt.track_id
        FROM playlist_tracks pt
        JOIN playlists p ON p.id = pt.playlist_id
        WHERE p.signal_tier = 'primary'
    """

    if exclude_past:
        where.append(f"pt.track_id NOT IN ({past_subq})")

    where_sql = " AND ".join(where)

    proximity_expr = _bpm_proximity_expr(variant)

    sql = f"""
        SELECT
            pt.track_id,
            tb.artist,
            tb.title,
            tb.bpm,
            tb.duration_ms,
            tb.release_date,
            {proximity_expr} AS bpm_distance,
            SUM(CASE p.signal_tier
                    WHEN 'primary'   THEN {TIER_WEIGHTS['primary']}
                    WHEN 'secondary' THEN {TIER_WEIGHTS['secondary']}
                    WHEN 'mined'     THEN {TIER_WEIGHTS['mined']}
                    ELSE 0 END) AS weighted_overlap,
            MAX(CASE WHEN p.signal_tier = 'primary' THEN 1 ELSE 0 END) AS in_past,
            GROUP_CONCAT(DISTINCT p.name) AS source_playlists
        FROM playlist_tracks pt
        JOIN playlists  p  ON p.id = pt.playlist_id
        JOIN track_bpm  tb ON tb.spotify_track_id = pt.track_id
        WHERE {where_sql}
        GROUP BY pt.track_id
        ORDER BY (weighted_overlap + in_past * {PRIMARY_PRESENCE_BOOST}) DESC,
                 bpm_distance ASC,
                 (tb.release_date IS NULL), tb.release_date DESC,
                 tb.artist,
                 tb.title
        LIMIT ?
    """
    params.append(limit)

    conn = db_connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    cands = [
        {
            "track_id": r["track_id"],
            "artist": r["artist"],
            "title": r["title"],
            "bpm": r["bpm"],
            "duration_ms": r["duration_ms"],
            "release_date": r["release_date"],
            "bpm_distance": r["bpm_distance"],
            "score": r["weighted_overlap"] + r["in_past"] * PRIMARY_PRESENCE_BOOST,
            "weighted_overlap": r["weighted_overlap"],
            "in_past": bool(r["in_past"]),
            "source_playlists": (r["source_playlists"] or "").split(","),
        }
        for r in rows
    ]

    if shuffle:
        # Reshuffle within score bands so ranking is preserved but tiebreaks
        # come up randomly each time the user hits the shuffle button.
        bands: dict[int, list[dict]] = {}
        order: list[int] = []
        for c in cands:
            if c["score"] not in bands:
                bands[c["score"]] = []
                order.append(c["score"])
            bands[c["score"]].append(c)
        for s in bands:
            random.shuffle(bands[s])
        cands = [c for s in order for c in bands[s]]

    return cands


def _format_row(c: dict) -> str:
    flag = "★" if c["in_past"] else " "
    return (
        f"{flag} {c['bpm']:>5.1f}  score={c['score']:>5}  "
        f"{c['artist'][:30]:<30}  {c['title'][:50]}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("variant", choices=sorted(VARIANT_BPM_RANGES))
    p.add_argument("--include-past", action="store_true",
                   help="include tracks already in your primary playlists")
    p.add_argument("--q", default="", help="substring match on artist or title")
    p.add_argument("--limit", type=int, default=50)
    args = p.parse_args(argv)

    cands = get_candidates(
        args.variant,
        exclude_past=not args.include_past,
        q=args.q,
        limit=args.limit,
    )
    if not cands:
        print("(no candidates)")
        return 0

    print(f"# {len(cands)} candidates for variant={args.variant} "
          f"exclude_past={not args.include_past} q={args.q!r}")
    print(f"# {'★ = in your past primary playlists'}")
    for c in cands:
        print(_format_row(c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
