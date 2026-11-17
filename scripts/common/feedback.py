"""How past picks were received -> artist multipliers, benched artists and
style boosts. Reads the runs of every playlist script, so a skip in one
playlist also counts in the others.

A pick counts as played when Plex viewCount (views_plex, ~90% completion)
went up since it was picked, and as skipped when Plex skipCount went up.
"""

from collections import defaultdict

# Buckets of tracks you already knew well; their reception says nothing
# about discovery taste, so they don't feed back.
FAMILIAR_BUCKETS = frozenset({"rediscover", "classic"})


def compute_feedback(runs, tracks, albums, style, skip_buckets=FAMILIAR_BUCKETS):
    """(multipliers {artist_key: 0.8..1.5}, banned {artist_key}). Adds +1
    per well-received pick to `style` (in place)."""
    pos = defaultdict(int)
    neg = defaultdict(int)
    for run in runs:
        for pick in run["picks"]:
            if pick["bucket"] in skip_buckets:
                continue
            t = tracks.get(pick["rk"])
            if not t:
                continue
            played = t["views"] > pick["views"]
            skipped = t["skips"] > pick["skips"]
            rating = t["rating"] or 0
            if played or rating >= 8:
                pos[t["artist_key"]] += 1
                for tag in albums.get(t["album_key"], {}).get("tags", ()):
                    style[tag] += 1
            elif (skipped or 0 < rating <= 4):
                neg[t["artist_key"]] += 1

    multipliers, banned = {}, set()
    for artist in set(pos) | set(neg):
        if pos[artist]:
            multipliers[artist] = min(1.5, 1 + 0.25 * pos[artist])
        elif neg[artist] >= 2:
            banned.add(artist)
        else:
            multipliers[artist] = 0.8
    return multipliers, banned
