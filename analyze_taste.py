"""Analyze the genre / artist profile of Bryan's primary spin playlists.

Pulls tracks from the primary-taste-signal playlists, fetches artist genre tags
from Spotify, and reports a track-weighted genre distribution + top artists.
"""
import collections
import json
import sys
import urllib.parse
import urllib.request

from spotify_auth import get_access_token

# Primary taste signal: Bryan's own spin playlists + segment-specific
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


def api_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_playlist_tracks(token: str, pid: str) -> list[dict]:
    out: list[dict] = []
    next_path = f"/playlists/{pid}/tracks?limit=100&fields=items(track(id,name,artists(id,name))),next"
    while next_path:
        page = api_get(token, next_path)
        for item in page.get("items", []):
            t = item.get("track")
            if t and t.get("id"):
                out.append(t)
        nxt = page.get("next")
        next_path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
    return out


def get_artists(token: str, artist_ids: list[str]) -> dict[str, dict]:
    """Batch /v1/artists in groups of 50."""
    result: dict[str, dict] = {}
    for i in range(0, len(artist_ids), 50):
        batch = artist_ids[i:i + 50]
        page = api_get(token, "/artists?ids=" + ",".join(batch))
        for a in page["artists"]:
            if a:
                result[a["id"]] = a
    return result


def main() -> int:
    token = get_access_token()

    # Pull tracks from each playlist
    all_tracks: dict[str, dict] = {}  # track_id -> track
    track_counts: collections.Counter = collections.Counter()
    per_playlist_counts: dict[str, int] = {}
    for label, pid in PRIMARY_PLAYLIST_IDS.items():
        try:
            tracks = get_playlist_tracks(token, pid)
        except Exception as e:
            print(f"WARN: failed to fetch {label} ({pid}): {e}", file=sys.stderr)
            continue
        per_playlist_counts[label] = len(tracks)
        for t in tracks:
            all_tracks[t["id"]] = t
            track_counts[t["id"]] += 1

    print("=== Playlist track counts ===")
    for label, n in per_playlist_counts.items():
        print(f"  {n:>3}  {label}")
    total_unique = len(all_tracks)
    total_with_dups = sum(per_playlist_counts.values())
    print(f"\nTotal tracks (with dups across playlists): {total_with_dups}")
    print(f"Unique tracks: {total_unique}")

    # Collect unique artists, fetch genres
    artist_ids: set[str] = set()
    for t in all_tracks.values():
        for a in t["artists"]:
            artist_ids.add(a["id"])
    artists = get_artists(token, sorted(artist_ids))

    # Genre counts: weight each genre tag by # tracks the artist appears on
    artist_track_counts: collections.Counter = collections.Counter()
    for t in all_tracks.values():
        for a in t["artists"]:
            artist_track_counts[a["id"]] += 1

    genre_weight: collections.Counter = collections.Counter()
    for aid, weight in artist_track_counts.items():
        a = artists.get(aid)
        if not a:
            continue
        for g in a.get("genres", []):
            genre_weight[g] += weight

    print("\n=== Top genres (weighted by track appearances) ===")
    for genre, w in genre_weight.most_common(25):
        print(f"  {w:>4}  {genre}")

    print("\n=== Top artists (by track count across spin playlists) ===")
    for aid, n in artist_track_counts.most_common(20):
        a = artists.get(aid, {})
        name = a.get("name", "?")
        gs = ", ".join(a.get("genres", [])[:3])
        print(f"  {n:>3}  {name:<35s}  [{gs}]")

    # Artists with no genre tags (often newer or less-tagged)
    untagged = [artists[aid].get("name", "?") for aid in artist_track_counts
                if aid in artists and not artists[aid].get("genres")]
    if untagged:
        print(f"\n{len(untagged)} artists had no genre tags from Spotify (out of {len(artist_track_counts)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
