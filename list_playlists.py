"""List all of the user's playlists with owner + track count.

Auto-flags playlists whose names suggest spin/cycling so Bryan can confirm
which ones to use as the taste-signal seed.
"""
import json
import re
import sys
import urllib.parse
import urllib.request

from spotify_auth import get_access_token

SPIN_PATTERN = re.compile(r"\b(spin|cycle|cycling|ride|peloton|cyclebar|soulcycle|rpm)\b", re.I)


def api_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def list_all_playlists(token: str) -> list[dict]:
    me = api_get(token, "/me")
    my_id = me["id"]
    out: list[dict] = []
    next_path = "/me/playlists?limit=50"
    while next_path:
        page = api_get(token, next_path)
        out.extend(page["items"])
        nxt = page.get("next")
        next_path = nxt.replace("https://api.spotify.com/v1", "") if nxt else None
    return out, my_id


def main() -> int:
    token = get_access_token()
    playlists, my_id = list_all_playlists(token)
    print(f"Found {len(playlists)} playlists.\n")

    flagged, others = [], []
    for p in playlists:
        row = {
            "id": p["id"],
            "name": p["name"],
            "owner": p["owner"]["display_name"] or p["owner"]["id"],
            "owned_by_me": p["owner"]["id"] == my_id,
            "tracks": p["tracks"]["total"],
            "public": p["public"],
        }
        if SPIN_PATTERN.search(p["name"]):
            flagged.append(row)
        else:
            others.append(row)

    def print_row(r):
        owner_tag = "MINE" if r["owned_by_me"] else f"by {r['owner']}"
        print(f"  [{r['tracks']:>3}t] {r['name']:<60s}  {owner_tag:<25s}  {r['id']}")

    print(f"=== Spin-pattern matches ({len(flagged)}) ===")
    for r in flagged:
        print_row(r)

    print(f"\n=== Other playlists ({len(others)}) ===")
    for r in others:
        print_row(r)

    return 0


if __name__ == "__main__":
    sys.exit(main())
