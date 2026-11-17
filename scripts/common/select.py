"""Picking tracks from scored bucket candidates, with variety caps, and
spreading buckets through the playlist."""

import math
import random
from collections import defaultdict


def weighted_order(cands, n, pool_factor, temperature):
    """[(score, track)] -> weighted random order over the top
    max(n, n x pool_factor) candidates, then the rest by score. Weight
    exp((score - top) / span / temperature): lower temperature = closer to
    plain best-first."""
    cands = sorted(cands, key=lambda x: x[0], reverse=True)
    pool_size = max(n, int(n * pool_factor))
    pool, rest = cands[:pool_size], cands[pool_size:]
    if not pool:
        return []
    top = pool[0][0]
    span = (top - pool[-1][0]) or 1
    keyed = []
    for score, t in pool:
        w = math.exp((score - top) / span / temperature)
        keyed.append((math.log(random.random() or 1e-12) / w, score, t))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [(s, t) for _, s, t in keyed] + rest


class Selector:
    """Takes tracks into buckets under shared caps:
      - one copy per song (song_key),
      - max_per_artist overall, bucket_artist_cap[bucket] within a bucket,
      - max_per_album overall,
      - optionally max_per_group per group (e.g. cluster), via group_of(track).
    preload() counts tracks that are already in the playlist (rotation)."""

    def __init__(self, buckets, max_per_artist, max_per_album, bucket_artist_cap=None,
                 group_of=None, max_per_group=None):
        self.max_per_artist = max_per_artist
        self.max_per_album = max_per_album
        self.bucket_artist_cap = bucket_artist_cap or {}
        self.group_of = group_of
        self.max_per_group = max_per_group
        self.per_artist = defaultdict(int)
        self.per_album = defaultdict(int)
        self.per_group = defaultdict(int)
        self.per_bucket_artist = defaultdict(int)
        self.used_songs = set()
        self.picked = {b: [] for b in buckets}

    def _group(self, t):
        return self.group_of(t) if self.group_of else None

    def fits(self, bucket, t):
        artist_cap = self.bucket_artist_cap.get(bucket, self.max_per_artist)
        group = self._group(t)
        return not (t["song"] in self.used_songs
                    or self.per_artist[t["artist_key"]] >= self.max_per_artist
                    or self.per_bucket_artist[(bucket, t["artist_key"])] >= artist_cap
                    or self.per_album[t["album_key"]] >= self.max_per_album
                    or (group is not None and self.max_per_group is not None
                        and self.per_group[group] >= self.max_per_group))

    def _count(self, bucket, t):
        self.used_songs.add(t["song"])
        self.per_artist[t["artist_key"]] += 1
        self.per_bucket_artist[(bucket, t["artist_key"])] += 1
        self.per_album[t["album_key"]] += 1
        group = self._group(t)
        if group is not None:
            self.per_group[group] += 1

    def preload(self, bucket, t):
        """Count a track already in the playlist against the caps (not added to picked)."""
        self._count(bucket, t)

    def take(self, bucket, cands, n):
        """Add (score, track) from cands in order until the bucket holds n picks."""
        for score, t in cands:
            if len(self.picked[bucket]) >= n:
                break
            if not self.fits(bucket, t):
                continue
            self._count(bucket, t)
            self.picked[bucket].append((score, t))
        return self.picked[bucket]


def interleave(picked, opener=None, opener_count=1):
    """Open with opener_count best picks of the `opener` bucket, then spread
    the buckets evenly (never the same bucket twice in a row while another
    is left), each time taking the best remaining track of the chosen bucket
    whose artist didn't just play. `picked` = {bucket: [(score, t)]}
    best first. Returns [(bucket, track)]."""
    queues = {b: list(v) for b, v in picked.items()}
    order = []
    if opener is not None:
        for _ in range(opener_count):
            if queues.get(opener):
                order.append((opener, queues[opener].pop(0)[1]))
    targets = {b: max(1, len(v)) for b, v in picked.items()}
    placed = defaultdict(int)
    if opener is not None:
        placed[opener] = len(order)
    prev = opener
    while any(queues.values()):
        prev_artist = order[-1][1]["artist_key"] if order else None
        options = [b for b in queues if queues[b]]
        options = [b for b in options if b != prev] or options
        b = min(options, key=lambda b: placed[b] / targets[b])
        queue = queues[b]
        i = next((i for i, (_, t) in enumerate(queue) if t["artist_key"] != prev_artist), 0)
        order.append((b, queue.pop(i)[1]))
        placed[b] += 1
        prev = b
    return order
