"""DJ running order (ported from plex-playlist-enhancer's
live_playlists.dj_order): each next track is the one that sounds most like
the previous one, never the same artist twice in a row.

Sonic link weight = 1 - distance, where distance is Plex's sonic distance
(0..1, 0 = identical) from sonicallySimilar results.
"""

import random
from collections import defaultdict

DJ_RECENT_ARTIST_WINDOW = 3     # songs back that count as "just heard this artist"
DJ_RECENT_ARTIST_PENALTY = 0.4


def sonic_links(items, neighbor_limit, max_distance, direct=None, log=print):
    """Pairwise Plex sonic match (0..1) between playlist tracks: one
    sonicallySimilar call per item, kept only where the neighbour is also
    in `items`. Pass an existing `direct` to add links for new items to
    cached ones (it is updated in place and returned)."""
    keys = {str(i.ratingKey) for i in items}
    direct = direct if direct is not None else defaultdict(dict)
    for item in items:
        a = str(item.ratingKey)
        try:
            matches = item.sonicallySimilar(limit=neighbor_limit, maxDistance=max_distance)
        except Exception as e:
            log(f"  warning: sonic lookup failed while ordering: {e}")
            continue
        for m in matches:
            b = str(m.ratingKey)
            if b in keys and b != a:
                distance = getattr(m, "distance", None)
                w = 1 - float(distance) if distance is not None else 0.5
                direct[a][b] = max(direct[a].get(b, 0.0), w)
                direct[b][a] = max(direct[b].get(a, 0.0), w)
    return direct


def similarity(direct, albums, a, b):
    """Transition score a -> b: 1 + sonic match if one is in the other's
    sonicallySimilar list, else 0.6 for the same album, else album tag
    overlap (Jaccard) x 0.5."""
    if b["rk"] in direct.get(a["rk"], {}):
        return 1.0 + direct[a["rk"]][b["rk"]]
    if a["album_key"] == b["album_key"]:
        return 0.6
    ta = albums.get(a["album_key"], {}).get("tags", set())
    tb = albums.get(b["album_key"], {}).get("tags", set())
    return 0.5 * len(ta & tb) / len(ta | tb) if ta | tb else 0.0


def no_doubling_possible(counts, last_artist, n):
    """Can n tracks with these per-artist counts be ordered with no artist
    twice in a row, given the previous track was by last_artist?"""
    for artist, c in counts.items():
        if c > (n + 1) // 2 or (artist == last_artist and c > n // 2):
            return False
    return True


def _run_length(ordered, bucket_of, bucket):
    n = 0
    for t in reversed(ordered):
        if bucket_of.get(t["rk"]) != bucket:
            break
        n += 1
    return n


def dj_order(tracks, direct, albums, prev=None, recent=(), run_limits=None, bucket_of=None,
             window=DJ_RECENT_ARTIST_WINDOW, penalty=DJ_RECENT_ARTIST_PENALTY):
    """Opener (the first track, or the one sounding most like `prev`), then
    always the remaining track that sounds most like the one before it,
    never the same artist twice in a row (with lookahead so the end can't
    get stuck).

    run_limits {bucket: n} with bucket_of {rk: bucket}: no more than n
    tracks of that bucket in a row, unless nothing else is left.

    Score: similarity(); minus `penalty` if the artist played in the last
    `window` tracks; plus a tiny tie-breaking jitter."""
    run_limits = run_limits or {}
    bucket_of = bucket_of or {}
    counts = defaultdict(int)
    for t in tracks:
        counts[t["artist_key"]] += 1

    def eligible(candidates, last_artist):
        out = []
        for t in candidates:
            artist = t["artist_key"]
            if artist == last_artist:
                continue
            counts[artist] -= 1
            ok = no_doubling_possible(counts, artist, len(candidates) - 1)
            counts[artist] += 1
            if ok:
                out.append(t)
        return out

    def within_runs(pool, ordered):
        if not run_limits:
            return pool
        ok = [t for t in pool
              if bucket_of.get(t["rk"]) not in run_limits
              or _run_length(ordered, bucket_of, bucket_of[t["rk"]]) < run_limits[bucket_of[t["rk"]]]]
        return ok or pool

    ordered = list(recent)
    current = prev
    remaining = list(tracks)
    while remaining:
        last_artist = current["artist_key"] if current else None
        recent_artists = {t["artist_key"] for t in ordered[-window:]}
        pool = (eligible(remaining, last_artist)
                or [t for t in remaining if t["artist_key"] != last_artist]
                or remaining)
        pool = within_runs(pool, ordered)
        if current is None:
            nxt = pool[0]
        else:
            nxt = max(pool, key=lambda t: similarity(direct, albums, current, t)
                      - (penalty if t["artist_key"] in recent_artists else 0.0)
                      + random.random() * 1e-3)
        ordered.append(nxt)
        remaining.remove(nxt)
        counts[nxt["artist_key"]] -= 1
        current = nxt
    return ordered[len(recent):]


def dj_stretches(order, direct, albums, stretch, run_limits=None):
    """Keep the interleave's shape (strongest picks early, buckets spread
    out) but DJ-order each `stretch`-track stretch, chaining every stretch
    onto the last track of the one before. order = [(bucket, track)]."""
    out = []
    for i in range(0, len(order), stretch):
        chunk = order[i:i + stretch]
        bucket_of = {t["rk"]: b for b, t in chunk}
        if run_limits:
            bucket_of.update({t["rk"]: b for b, t in out[-DJ_RECENT_ARTIST_WINDOW:]})
        tracks = dj_order([t for _, t in chunk], direct, albums,
                          prev=out[-1][1] if out else None,
                          recent=[t for _, t in out[-DJ_RECENT_ARTIST_WINDOW:]],
                          run_limits=run_limits, bucket_of=bucket_of)
        out.extend((bucket_of[t["rk"]], t) for t in tracks)
    return out
