#!/usr/bin/env python3
"""
For You playlist generator: a balanced daily mix of your whole taste, from
your own Plex library.

Three buckets (default 50 tracks):

    Unheard        20  songs you've never played
    Rarely played  18  songs with 1..max(2, P40) plays, none in the last 30 days
    Classics       12  your most-played songs (>= max(5, P85) plays) not heard for 21+ days

P40 / P85 are percentiles of play counts over the songs you've played, so
the thresholds follow your library. "Song" = all copies of a track (live,
remaster, compilation) together.

Every candidate gets a 0..1 score from these signals (weights per bucket in
WEIGHTS below; a signal with no data, like clusters without a registry, is
left out and the rest renormalised):

    sonic       Plex sonic closeness to your seed tracks (recent + all-time)
    sonic_now   the same, recent seeds only ("fits what I'm into this month")
    artist      your affinity for the artist, or its similar-artist link to artists you play
    style       album styles/genres vs. your taste profile
    cluster     how much you play the album's cluster (optional, app's cluster registry)
    pop         Last.fm popularity (Plex ratingCount); for Unheard also the song's
                rank within its album, so an unheard album starts with its best-known song
    track_aff   how much you've played the song (log scale)
    forgotten   how long since you last heard it (1 - exp(-days / 60))

Feedback from Discover Weekly's and this playlist's past picks (played /
skipped / rated) re-weights artists, as in Discover Weekly.

Never in For You: tracks currently in On Repeat or Discover Weekly, Discover
Weekly's picks of the last HISTORY_WEEKS, 2-star-or-lower tracks, tracks
shorter than MIN_TRACK_SECONDS.

Daily rotation instead of a reshuffle: each day the tracks you played or
skipped since yesterday leave, then the oldest, until ROTATION_PERCENT of
the list is new. New picks are slotted in where they sound most like their
neighbours. Removed tracks rest COOLDOWN_DAYS before they can come back. The
list opens with a Classic and never has more than MAX_UNHEARD_RUN unheard
tracks in a row.

BACKTEST_DAYS=14 runs an offline check instead: the taste profile is built
from history up to 14 days ago, and it reports how many of the songs you
then played for the first time (or again, for rarely played songs) the
scoring ranked near the top, against popularity-only and random. Nothing is
written.

Units: Tautulli `date` is unix seconds; *_DAYS settings are days;
MIN_TRACK_SECONDS / MIN_LISTEN_SECONDS are seconds (Plex durations, which
are milliseconds, are converted in common.library); Plex userRating is
0-10 (= 0-5 stars).

Shared code lives in ../common (mount the whole scripts/ folder; see README).
"""

import math
import os
import random
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from plexapi.server import PlexServer  # noqa: E402

from common.clusters import load_album_clusters  # noqa: E402
from common.dj import sonic_links, dj_stretches, similarity  # noqa: E402
from common.feedback import compute_feedback  # noqa: E402
from common.library import (load_library, aggregate_songs, album_style_scores,  # noqa: E402
                            percentile, normalize)
from common.plexio import playlist_keys, replace_playlist, set_summary  # noqa: E402
from common.select import Selector, interleave, weighted_order  # noqa: E402
from common.state import load_state as _load_state, save_state as _save_state, read_runs  # noqa: E402
from common.tautulli import Tautulli, is_qualifying_play  # noqa: E402
from common.taste import (build_taste, sonic_neighbours, sonic_scores,  # noqa: E402
                          similar_artist_scores, closest_seed)


def _env_float(name, default):
    return float(os.environ.get(name, default))


def _env_int(name, default):
    return int(os.environ.get(name, default))


TAUTULLI_URL = os.environ["TAUTULLI_URL"].rstrip("/")
TAUTULLI_APIKEY = os.environ["TAUTULLI_APIKEY"]
PLEX_URL = os.environ["PLEX_URL"]
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
MUSIC_SECTION_ID = os.environ["MUSIC_SECTION_ID"]

PLAYLIST_TITLE = os.environ.get("PLAYLIST_TITLE", "For You")
ON_REPEAT_TITLE = os.environ.get("ON_REPEAT_TITLE", "On Repeat")
DISCOVER_TITLE = os.environ.get("DISCOVER_TITLE", "Discover Weekly")

BUCKETS = ("unheard", "rare", "classic")
BUCKET_SIZES = {
    "unheard": _env_int("UNHEARD", 20),
    "rare": _env_int("RARELY_PLAYED", 18),
    "classic": _env_int("CLASSICS", 12),
}
BUCKET_LABELS = {"unheard": "Unheard", "rare": "Rarely played", "classic": "Classic"}

# Bucket thresholds
RARE_PERCENTILE = _env_float("RARE_PERCENTILE", 40)        # % of played songs
RARE_MIN_DAYS = _env_float("RARE_MIN_DAYS", 30)            # days since last play
CLASSIC_PERCENTILE = _env_float("CLASSIC_PERCENTILE", 85)  # % of played songs
CLASSIC_MIN_PLAYS = _env_int("CLASSIC_MIN_PLAYS", 5)       # plays (Plex viewCount)
CLASSIC_MIN_DAYS = _env_float("CLASSIC_MIN_DAYS", 21)      # days since last play
FORGOTTEN_SCALE_DAYS = _env_float("FORGOTTEN_SCALE_DAYS", 60)
MAX_SKIP_RATIO = _env_float("MAX_SKIP_RATIO", 0.5)         # skips / (skips + plays)
MIN_TRACK_SECONDS = _env_int("MIN_TRACK_SECONDS", 90)

# Taste profile (same meaning as in Discover Weekly)
SEED_DAYS = _env_int("SEED_DAYS", 30)
SEED_HALF_LIFE_DAYS = _env_float("SEED_HALF_LIFE_DAYS", 14)
SEED_RECENT = _env_int("SEED_RECENT", 20)
SEED_ALLTIME = _env_int("SEED_ALLTIME", 5)
TASTE_HALF_LIFE_DAYS = _env_float("TASTE_HALF_LIFE_DAYS", 180)   # long-term side (Discover Weekly: 90)
SEED_LONG = _env_int("SEED_LONG", 20)          # long-term sonic seeds: most-played songs of all time
SEED_ARTISTS = _env_int("SEED_ARTISTS", 10)
SONIC_LIMIT = _env_int("SONIC_LIMIT", 40)
# Recency bias (0..1): how much Unheard / Rarely played follow what you've
# been playing lately. Every taste signal has a recent version (half-life
# RECENT_HALF_LIFE_DAYS) and a long-term one (TASTE_HALF_LIFE_DAYS, plus
# all-time seed tracks); the score uses bias x recent + (1 - bias) x long-term.
# 0 = your whole history counts the same as last month, 1 = only recent hits.
RECENCY_BIAS = min(1.0, max(0.0, _env_float("RECENCY_BIAS", 0.6)))
RECENT_HALF_LIFE_DAYS = _env_float("RECENT_HALF_LIFE_DAYS", 14)
MIN_LISTEN_SECONDS = _env_int("MIN_LISTEN_SECONDS", 30)

# Selection and order
MAX_PER_ARTIST = _env_int("MAX_PER_ARTIST", 2)
MAX_PER_ALBUM = _env_int("MAX_PER_ALBUM", 2)
MAX_CLUSTER_SHARE = _env_float("MAX_CLUSTER_SHARE", 0.35)  # of the playlist, with a registry
MAX_UNHEARD_RUN = _env_int("MAX_UNHEARD_RUN", 2)
POOL_FACTOR = _env_float("POOL_FACTOR", 4)
TEMPERATURE = _env_float("TEMPERATURE", 0.15)
DJ_STRETCH = _env_int("DJ_STRETCH", 20)
DJ_NEIGHBOR_LIMIT = _env_int("DJ_NEIGHBOR_LIMIT", 3000)
DJ_MAX_DISTANCE = _env_float("DJ_MAX_DISTANCE", 0.6)

# Rotation and schedule
ROTATION_PERCENT = _env_float("ROTATION_PERCENT", 25)
COOLDOWN_DAYS = _env_float("COOLDOWN_DAYS", 14)
REBUILD_WEEKDAY = os.environ.get("REBUILD_WEEKDAY", "").strip().lower()[:3]   # e.g. "mon"; empty = never
RUN_HOUR = _env_int("RUN_HOUR", 5)          # local time (container TZ)
RUN_MINUTE = _env_int("RUN_MINUTE", 30)
HISTORY_WEEKS = _env_int("HISTORY_WEEKS", 6)

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "state.json"))
DISCOVER_STATE_FILE = os.environ.get("DISCOVER_STATE_FILE",
                                     os.path.join(os.path.dirname(HERE), "discover-weekly", "state.json"))
CLUSTER_REGISTRY_PATH = os.environ.get("CLUSTER_REGISTRY_PATH", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
FULL_REBUILD = os.environ.get("FULL_REBUILD", "").lower() in ("1", "true", "yes")
BACKTEST_DAYS = _env_float("BACKTEST_DAYS", 0)
USER_ID = os.environ.get("USER_ID")  # optional

# Relative weights per bucket; normalised over the signals that have data.
WEIGHTS = {
    "unheard": {"sonic": 0.35, "artist": 0.25, "style": 0.10, "cluster": 0.10, "pop": 0.20},
    "rare": {"sonic": 0.30, "artist": 0.25, "style": 0.10, "cluster": 0.10, "pop": 0.25},
    "classic": {"sonic_now": 0.30, "cluster": 0.15, "track_aff": 0.35, "forgotten": 0.20},
}
SIGNAL_LABELS = {"sonic": "sounds like your seeds", "sonic_now": "fits your recent listening",
                 "artist": "artist you like", "style": "your styles", "cluster": "a cluster you play",
                 "pop": "popular", "track_aff": "a favourite", "forgotten": "not heard in a while"}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def load_state():
    return _load_state(STATE_FILE)


def save_state(state):
    _save_state(STATE_FILE, state)


# --- Profile ----------------------------------------------------------------

class Profile:
    """Everything the scoring needs, computed once per run."""

    def __init__(self, plex, tracks, albums, similar, history, now_ts, feedback_runs, log=print):
        self.tracks, self.albums, self.now_ts = tracks, albums, now_ts
        self.taste = build_taste(tracks, albums, history, now_ts,
                                 min_listen_seconds=MIN_LISTEN_SECONDS,
                                 taste_half_life_days=TASTE_HALF_LIFE_DAYS,
                                 seed_days=SEED_DAYS, seed_half_life_days=SEED_HALF_LIFE_DAYS,
                                 seed_recent=SEED_RECENT, seed_alltime=SEED_ALLTIME,
                                 recent_half_life_days=RECENT_HALF_LIFE_DAYS)
        style = self.taste.style
        self.multipliers, self.banned = compute_feedback(feedback_runs, tracks, albums, style)
        # Two seed sets: recent (most played in the last SEED_DAYS, decayed) and
        # long-term (most played of all time, max 2 per artist, log-weighted).
        self.long_seeds = self._long_seeds(tracks)
        neighbours = sonic_neighbours(plex, {**self.long_seeds, **self.taste.recent}, SONIC_LIMIT, log=log)
        self.sonic = sonic_scores(neighbours, self.long_seeds, SONIC_LIMIT)
        self.sonic_now = sonic_scores(neighbours, self.taste.recent, SONIC_LIMIT)
        self.seed_of = closest_seed(neighbours, self.long_seeds, SONIC_LIMIT)
        self.seed_of_now = closest_seed(neighbours, self.taste.recent, SONIC_LIMIT)
        self.link = similar_artist_scores(similar, self.taste.artist_aff, SEED_ARTISTS)
        self.link_now = similar_artist_scores(similar, self.taste.artist_recent, SEED_ARTISTS)
        self.album_style = album_style_scores(albums, style)
        self.album_style_now = album_style_scores(albums, self.taste.style_recent)

        self.album_cluster, self.cluster_names = load_album_clusters(CLUSTER_REGISTRY_PATH, log=log)
        self.cluster_profile = self._cluster_profile(self.taste.album_aff)
        self.cluster_profile_now = self._cluster_profile(self.taste.album_recent)

        self.songs = aggregate_songs(tracks)
        played = [s["views"] for s in self.songs.values() if s["views"] > 0]
        self.rare_max = max(2, percentile(played, RARE_PERCENTILE) or 2)
        self.classic_min = max(CLASSIC_MIN_PLAYS, percentile(played, CLASSIC_PERCENTILE) or CLASSIC_MIN_PLAYS)
        self.max_song_views = max(played, default=1)
        pops = [t["pop"] for t in tracks.values() if t["pop"]]
        self.log_max_pop = math.log1p(max(pops)) if pops else 1.0

        # Within-album popularity rank (0..1, 1 = the album's best-known song).
        by_album = defaultdict(list)
        for t in tracks.values():
            by_album[t["album_key"]].append(t)
        self.album_rank = {}
        for album_tracks in by_album.values():
            ranked = sorted((t for t in album_tracks if t["pop"]), key=lambda t: t["pop"])
            n = len(ranked)
            for i, t in enumerate(ranked):
                self.album_rank[t["rk"]] = i / (n - 1) if n > 1 else 0.5

    @staticmethod
    def _long_seeds(tracks):
        ranked = sorted((t for t in tracks.values() if t["views"]), key=lambda t: (-t["views"], t["rk"]))
        per_artist, seen_songs, seeds = defaultdict(int), set(), {}
        top = math.log1p(ranked[0]["views"]) if ranked else 1
        for t in ranked:
            if len(seeds) >= SEED_LONG:
                break
            if per_artist[t["artist_key"]] >= 2 or t["song"] in seen_songs:
                continue
            per_artist[t["artist_key"]] += 1
            seen_songs.add(t["song"])
            seeds[t["rk"]] = math.log1p(t["views"]) / top
        return seeds

    def _cluster_profile(self, album_weights):
        per_cluster = defaultdict(float)
        for album, w in album_weights.items():
            cid = self.album_cluster.get(album)
            if cid:
                per_cluster[cid] += w
        return normalize(per_cluster)

    def cluster_of(self, t):
        return self.album_cluster.get(t["album_key"])

    def parts(self, bucket, t, song):
        """(recent, long-term) values of the taste signals of one candidate,
        before blending: {signal: (now, long)}."""
        a = t["artist_key"]
        p = {}
        if bucket in ("unheard", "rare"):
            p["sonic"] = (max(self.sonic_now.get(c["rk"], 0) for c in song["copies"]),
                          max(self.sonic.get(c["rk"], 0) for c in song["copies"]))
            p["artist"] = (max(self.taste.artist_recent.get(a, 0), self.link_now.get(a, 0)),
                           max(self.taste.artist_aff.get(a, 0), self.link.get(a, 0)))
            if t["album_key"] in self.album_style:
                p["style"] = (self.album_style_now.get(t["album_key"], 0), self.album_style[t["album_key"]])
        if self.cluster_profile:
            cid = self.cluster_of(t)
            p["cluster"] = ((self.cluster_profile_now.get(cid, 0.0), self.cluster_profile.get(cid, 0.0))
                            if cid else (0.0, 0.0))
        return p

    def signals(self, bucket, t, song, bias=None):
        """{signal: 0..1} for one candidate; a signal without data is absent.
        Taste signals blend recent and long-term: bias x now + (1 - bias) x long."""
        bias = RECENCY_BIAS if bias is None else bias
        s = {k: bias * now + (1 - bias) * long for k, (now, long) in self.parts(bucket, t, song).items()}
        if bucket in ("unheard", "rare"):
            pop_global = math.log1p(t["pop"] or 0) / self.log_max_pop if self.log_max_pop else 0
            if bucket == "unheard":
                s["pop"] = 0.5 * pop_global + 0.5 * self.album_rank.get(t["rk"], 0.5)
            else:
                s["pop"] = pop_global
        else:
            s["sonic_now"] = max(self.sonic_now.get(c["rk"], 0) for c in song["copies"])
            s["track_aff"] = math.log1p(song["views"]) / math.log1p(self.max_song_views)
            days = (self.now_ts - song["last"]) / 86400 if song["last"] else 3650
            s["forgotten"] = 1 - math.exp(-days / FORGOTTEN_SCALE_DAYS)
        return s


def weighted_score(bucket, signals):
    weights = {k: w for k, w in WEIGHTS[bucket].items() if k in signals}
    total = sum(weights.values()) or 1
    return sum(w * signals[k] for k, w in weights.items()) / total


def reason(p, bucket, t, song, signals):
    """Short human explanation of a pick: its strongest weighted signal,
    with the seed track it sounds like and whether that's recent taste."""
    if bucket == "classic":
        days = int((p.now_ts - song["last"]) / 86400) if song["last"] else None
        return f"{song['views']} plays, last {days} days ago" if days is not None else f"{song['views']} plays"
    contrib = {k: WEIGHTS[bucket][k] * v for k, v in signals.items() if k in WEIGHTS[bucket]}
    top = max(contrib, key=contrib.get) if contrib else None
    parts = p.parts(bucket, t, song)
    recent = top in parts and RECENCY_BIAS * parts[top][0] >= (1 - RECENCY_BIAS) * parts[top][1]
    if top == "sonic":
        rks = [c["rk"] for c in song["copies"]]
        seed = next((p.seed_of_now[r] for r in rks if recent and r in p.seed_of_now), None) \
            or next((p.seed_of[r] for r in rks if r in p.seed_of), None)
        st = p.tracks.get(seed)
        text = (f"sounds like {st['artist']} - {st['title']}" if st else SIGNAL_LABELS["sonic"])
        text += " (recent)" if recent else ""
    elif top == "artist":
        own = p.taste.artist_recent if recent else p.taste.artist_aff
        text = ("artist you play" if own.get(t["artist_key"]) else "similar to artists you play")
        text += " lately" if recent else ""
    elif top in ("style", "cluster"):
        text = SIGNAL_LABELS[top] + (" lately" if recent else "")
    else:
        text = SIGNAL_LABELS.get(top, "")
    if bucket == "rare":
        text = f"{song['views']} play{'s' if song['views'] != 1 else ''}; {text}"
    return text


# --- Candidates -------------------------------------------------------------

CLASSIC_RELAX = ((None, None), (70, 14), (55, 7))   # (percentile, min days) fallbacks


def classify(p, song, classic_min, classic_min_days):
    """Bucket of a song, or None. Rarely played wins over Classic."""
    views = song["views"]
    days = (p.now_ts - song["last"]) / 86400 if song["last"] else None
    if views == 0:
        return "unheard"
    if views <= p.rare_max:
        if days is not None and days < RARE_MIN_DAYS:
            return None
        if song["skips"] >= 2 and song["skips"] / (song["skips"] + views) >= MAX_SKIP_RATIO:
            return None
        return "rare"
    if views >= classic_min and (days is None or days >= classic_min_days):
        return "classic"
    return None


def build_candidates(p, exclude_rks, blocked_songs, bias=None):
    """{bucket: [(score, track)]} and {rk: reason}. When fewer than 2 x the
    Classics quota qualify, the Classic thresholds are relaxed step by step
    (CLASSIC_RELAX), never below the rarely-played range."""
    buckets = {b: [] for b in BUCKETS}
    reasons = {}
    seen = set()

    def usable(t, check_banned):
        return (t["duration"] >= MIN_TRACK_SECONDS
                and not (t["rating"] and t["rating"] <= 4)
                and t["rk"] not in exclude_rks
                and not (check_banned and t["artist_key"] in p.banned))

    def add(key, song, bucket):
        copies = [c for c in song["copies"] if usable(c, bucket != "classic")]
        if not copies or any(c["rk"] in exclude_rks for c in song["copies"]):
            return            # unusable, or another copy is in On Repeat / Discover Weekly
        if bucket == "unheard":
            t = max(copies, key=lambda c: (c["pop"] or 0, c["rk"]))      # best-known copy
        else:
            t = max(copies, key=lambda c: (c["views"], c["rk"]))         # the copy you play
        signals = p.signals(bucket, t, song, bias)
        if bucket == "unheard" and not (signals.get("sonic") or signals.get("artist")
                                        or signals.get("style", 0) >= 0.3 or signals.get("cluster")):
            return            # nothing links it to your taste
        score = weighted_score(bucket, signals)
        if bucket != "classic":
            score *= p.multipliers.get(t["artist_key"], 1)
        elif song["skips"] >= 3 and song["skips"] / (song["skips"] + song["views"]) > MAX_SKIP_RATIO:
            score *= 0.5
        seen.add(key)
        buckets[bucket].append((score, t))
        reasons[t["rk"]] = reason(p, bucket, t, song, signals)

    played = [s["views"] for s in p.songs.values() if s["views"] > 0]
    p.classic_level = None
    for level, (pct, min_days) in enumerate(CLASSIC_RELAX):
        classic_min = p.classic_min if pct is None else max(p.rare_max + 1, percentile(played, pct) or 0)
        min_days = CLASSIC_MIN_DAYS if min_days is None else min(min_days, CLASSIC_MIN_DAYS)
        for key, song in p.songs.items():
            if key in blocked_songs or key in seen:
                continue
            bucket = classify(p, song, classic_min, min_days)
            if bucket is None or (level and bucket != "classic"):
                continue
            add(key, song, bucket)
        p.classic_level = (classic_min, min_days)
        if len(buckets["classic"]) >= 2 * BUCKET_SIZES["classic"]:
            break
    return buckets, reasons


def make_selector(p, size):
    cluster_cap = (max(1, math.ceil(size * MAX_CLUSTER_SHARE))
                   if p.cluster_profile and MAX_CLUSTER_SHARE > 0 else None)
    return Selector(BUCKETS, MAX_PER_ARTIST, MAX_PER_ALBUM, bucket_artist_cap={"unheard": 1},
                    group_of=p.cluster_of if cluster_cap else None, max_per_group=cluster_cap)


def select(sel, buckets, quotas):
    # Most constrained first: Classics are few and sit on the albums you play
    # most, so they'd lose the shared artist/album caps if picked last.
    for b in ("classic", "rare", "unheard"):
        if quotas[b] > 0:
            sel.take(b, weighted_order(buckets[b], quotas[b], POOL_FACTOR, TEMPERATURE), quotas[b])
    # Backfill short buckets so the playlist still reaches its size.
    total = sum(quotas.values())
    for b in ("rare", "unheard", "classic"):
        missing = total - sum(len(v) for v in sel.picked.values())
        if missing <= 0:
            break
        sel.take(b, sorted(buckets[b], key=lambda x: x[0], reverse=True), len(sel.picked[b]) + missing)
    for b in sel.picked:
        sel.picked[b].sort(key=lambda x: x[0], reverse=True)
    return sel.picked


# --- Ordering ---------------------------------------------------------------

def insert_new(order, new, direct, albums):
    """Slot each new (bucket, track) into the kept running order where it
    sounds most like its neighbours: gain = sim(prev,new) + sim(new,next) -
    sim(prev,next). Gaps that would put an artist twice in a row, make an
    unheard run longer than MAX_UNHEARD_RUN, or open the list with a
    non-Classic are heavily penalised (used only if nothing else is left)."""
    order = list(order)

    def unheard_run_ok(seq):
        run = 0
        for b, _ in seq:
            run = run + 1 if b == "unheard" else 0
            if run > MAX_UNHEARD_RUN:
                return False
        return True

    for b, t in new:
        best, best_gain = 0, None
        for i in range(len(order) + 1):
            prev = order[i - 1][1] if i > 0 else None
            nxt = order[i][1] if i < len(order) else None
            gain = 0.0
            if prev is not None:
                gain += similarity(direct, albums, prev, t)
            if nxt is not None:
                gain += similarity(direct, albums, t, nxt)
            if prev is not None and nxt is not None:
                gain -= similarity(direct, albums, prev, nxt)
            if (prev and prev["artist_key"] == t["artist_key"]) or (nxt and nxt["artist_key"] == t["artist_key"]):
                gain -= 10
            if b == "unheard" and not unheard_run_ok(order[max(0, i - MAX_UNHEARD_RUN):i] + [(b, t)]
                                                     + order[i:i + MAX_UNHEARD_RUN]):
                gain -= 10
            if i == 0 and b != "classic" and order:
                gain -= 5
            gain += random.random() * 1e-3
            if best_gain is None or gain > best_gain:
                best, best_gain = i, gain
        order.insert(best, (b, t))
    return order


# --- Rotation ---------------------------------------------------------------

def rebuild_due(state, now_ts):
    if FULL_REBUILD or not state.get("current"):
        return True
    if REBUILD_WEEKDAY in WEEKDAYS:
        now = datetime.fromtimestamp(now_ts)
        last = datetime.fromtimestamp(state.get("last_rebuild", 0))
        return WEEKDAYS[now.weekday()] == REBUILD_WEEKDAY and last.date() != now.date()
    return False


def rotate(state, tracks, exclude_rks, banned, now_ts):
    """Split the current list into kept and removed entries. Removed: tracks
    gone from the library or now excluded, then played or skipped since
    they were added, then the oldest, until ROTATION_PERCENT is replaced;
    plus anything over a bucket's (possibly lowered) size."""
    current = [c for c in state.get("current", []) if c["rk"] in tracks]
    gone = len(state.get("current", [])) - len(current)
    reasons = {}
    removed = []
    keep = []
    for c in current:
        t = tracks[c["rk"]]
        if c["rk"] in exclude_rks or (t["rating"] and t["rating"] <= 4) or (
                c["bucket"] != "classic" and t["artist_key"] in banned):
            reasons[c["rk"]] = "excluded"
        elif t["views"] > c["views"]:
            reasons[c["rk"]] = "played"
        elif t["skips"] > c["skips"]:
            reasons[c["rk"]] = "skipped"
        else:
            keep.append(c)
            continue
        removed.append(c)

    quota = max(1, round(len(current) * ROTATION_PERCENT / 100)) if current else 0
    by_age = sorted(keep, key=lambda c: (c["added_ts"], random.random()))
    while len(removed) + gone < quota and by_age:
        c = by_age.pop(0)
        reasons[c["rk"]] = "rotated"
        removed.append(c)
    removed_keys = {c["rk"] for c in removed}
    keep = [c for c in keep if c["rk"] not in removed_keys]

    per_bucket = defaultdict(list)
    for c in keep:
        per_bucket[c["bucket"]].append(c)
    for b, entries in per_bucket.items():
        extra = len(entries) - BUCKET_SIZES.get(b, 0)
        for c in sorted(entries, key=lambda c: c["added_ts"])[:max(0, extra)]:
            reasons[c["rk"]] = "over size"
            removed.append(c)
    removed_keys = {c["rk"] for c in removed}
    keep = [c for c in keep if c["rk"] not in removed_keys]
    return keep, removed, reasons


# --- Main -------------------------------------------------------------------

def gather(plex, now_ts, state, log=print):
    tracks, albums, similar = load_library(plex, MUSIC_SECTION_ID)
    history = Tautulli(TAUTULLI_URL, TAUTULLI_APIKEY).history(MUSIC_SECTION_ID, USER_ID)
    log(f"  library: {len(tracks)} tracks, {len(albums)} albums; {len(history)} Tautulli plays")
    return tracks, albums, similar, history


def run_once(state):
    now_ts = int(time.time())
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    tracks, albums, similar, history = gather(plex, now_ts, state)

    cutoff = now_ts - HISTORY_WEEKS * 7 * 86400
    state["runs"] = [r for r in state.get("runs", []) if r["ts"] >= cutoff]
    discover_runs = [r for r in read_runs(DISCOVER_STATE_FILE) if r.get("ts", 0) >= cutoff]
    p = Profile(plex, tracks, albums, similar, history, now_ts, state["runs"] + discover_runs)

    on_repeat = playlist_keys(plex, ON_REPEAT_TITLE)
    discover = playlist_keys(plex, DISCOVER_TITLE)
    discover_recent = {pick["rk"] for r in discover_runs for pick in r["picks"]}
    exclude = on_repeat | discover | discover_recent
    cooldown = {rk: until for rk, until in (state.get("cooldown") or {}).items() if until > now_ts}

    full = rebuild_due(state, now_ts)
    if full:
        keep, removed, why_removed = [], [], {}
    else:
        keep, removed, why_removed = rotate(state, tracks, exclude, p.banned, now_ts)
    for c in removed:
        cooldown[c["rk"]] = now_ts + COOLDOWN_DAYS * 86400

    kept_songs = {tracks[c["rk"]]["song"] for c in keep}
    buckets, reasons = build_candidates(p, exclude | set(cooldown), kept_songs)
    print(f"  thresholds: rarely played = 1..{p.rare_max} plays (not in {RARE_MIN_DAYS:g} days), "
          f"classic >= {p.classic_level[0]} plays (not in {p.classic_level[1]:g} days); "
          f"{sum(1 for s in p.songs.values() if s['views'])} played songs")
    print("  candidates: " + ", ".join(f"{BUCKET_LABELS[b]} {len(c)}" for b, c in buckets.items()))
    top_recent = sorted(p.taste.artist_recent.items(), key=lambda x: x[1], reverse=True)[:5]
    names = {t["artist_key"]: t["artist"] for t in tracks.values()}
    print(f"  recency bias {RECENCY_BIAS:g} (half-life {RECENT_HALF_LIFE_DAYS:g} days); lately: "
          + ", ".join(names.get(a, a) for a, _ in top_recent))
    print(f"  {len(p.taste.recent)} recent + {len(p.long_seeds)} all-time seeds, {len(p.sonic)} sonic neighbours, "
          f"{len(p.multipliers)} artists re-weighted, {len(p.banned)} benched; "
          f"excluded {len(on_repeat)} On Repeat, {len(discover | discover_recent)} Discover Weekly, "
          f"{len(cooldown)} cooling down"
          + (f"; {len(p.cluster_names)} clusters" if p.cluster_profile else ""))

    size = sum(BUCKET_SIZES.values())
    sel = make_selector(p, size)
    for c in keep:
        sel.preload(c["bucket"], tracks[c["rk"]])
    kept_per_bucket = defaultdict(int)
    for c in keep:
        kept_per_bucket[c["bucket"]] += 1
    quotas = {b: max(0, BUCKET_SIZES[b] - kept_per_bucket[b]) for b in BUCKETS}
    picked = select(sel, buckets, quotas)

    new_pairs = [(b, t) for b in BUCKETS for _, t in picked[b]]
    if not keep and not new_pairs:
        print("No candidates found; nothing to build.")
        return False

    items = {}
    for rk in [c["rk"] for c in keep] + [t["rk"] for _, t in new_pairs]:
        try:
            items[rk] = plex.fetchItem(int(rk))
        except Exception as e:
            print(f"  skip {rk}: {e}")
    keep = [c for c in keep if c["rk"] in items]
    new_pairs = [(b, t) for b, t in new_pairs if t["rk"] in items]
    all_keys = set(items)

    if full:
        direct = sonic_links(items.values(), DJ_NEIGHBOR_LIMIT, DJ_MAX_DISTANCE)
        new_picked = {b: [(0, t) for bb, t in new_pairs if bb == b] for b in BUCKETS}
        order = interleave(new_picked, opener="classic", opener_count=1)
        order = dj_stretches(order, direct, albums, DJ_STRETCH, run_limits={"unheard": MAX_UNHEARD_RUN})
    else:
        direct = defaultdict(dict)
        for a, links in (state.get("links") or {}).items():
            if a in all_keys:
                direct[a].update({b: w for b, w in links.items() if b in all_keys})
        direct = sonic_links([items[t["rk"]] for _, t in new_pairs], DJ_NEIGHBOR_LIMIT, DJ_MAX_DISTANCE,
                             direct=direct, among=all_keys)
        kept_order = [(c["bucket"], tracks[c["rk"]]) for c in keep]
        order = insert_new(kept_order, new_pairs, direct, albums)

    new_keys = {t["rk"] for _, t in new_pairs}
    mode = "full rebuild" if full else (f"rotation: {len(removed)} out ("
                                        + ", ".join(f"{v} {k}" for k, v in sorted(
                                            _count(why_removed.values()).items())) + f"), {len(new_keys)} in")
    print(f"  {mode}")
    for i, (b, t) in enumerate(order, 1):
        link = direct.get(order[i - 2][1]["rk"], {}).get(t["rk"]) if i > 1 else None
        match = f"{link:4.0%}" if link is not None else "  · "
        flag = "+" if t["rk"] in new_keys else " "
        why = reasons.get(t["rk"], "")
        print(f"  {i:2}.{flag}{match} [{BUCKET_LABELS[b]:<13}] {t['artist']} - {t['title']}"
              + (f"  ({why})" if why and t["rk"] in new_keys else ""))

    if DRY_RUN:
        print("DRY_RUN set: playlist and state left untouched.")
        return False

    playlist = replace_playlist(plex, PLAYLIST_TITLE, [items[t["rk"]] for _, t in order],
                                state.get("playlist_rating_key"))
    counts = _count(b for b, _ in order)
    set_summary(playlist,
                f"New, half-known and beloved tracks from your library, updated "
                f"{datetime.now():%d %b %Y}. "
                + " · ".join(f"{BUCKET_LABELS[b]} {counts[b]}" for b in BUCKETS if counts.get(b)))

    added = {c["rk"]: c for c in keep}
    state["playlist_rating_key"] = getattr(playlist, "ratingKey", None)
    state["current"] = [added.get(t["rk"]) or {"rk": t["rk"], "bucket": b, "added_ts": now_ts,
                                               "views": t["views"], "skips": t["skips"]}
                        for b, t in order]
    state["cooldown"] = cooldown
    state["links"] = {a: {b: round(w, 4) for b, w in links.items() if b in all_keys}
                      for a, links in direct.items() if a in all_keys}
    if new_pairs:
        state["runs"].append({"ts": now_ts, "picks": [
            {"rk": t["rk"], "bucket": b, "views": t["views"], "skips": t["skips"]} for b, t in new_pairs]})
    state["last_run"] = now_ts
    if full:
        state["last_rebuild"] = now_ts
    save_state(state)
    print(f"'{PLAYLIST_TITLE}' updated with {len(order)} tracks ({len(new_keys)} new).")
    return True


def _count(values):
    out = defaultdict(int)
    for v in values:
        out[v] += 1
    return dict(out)


# --- Backtest ---------------------------------------------------------------

def backtest(days, ks=(50, 200)):
    """Offline check of the Unheard and Rarely-played scoring. Rewinds the
    library to `days` ago (Plex play counts minus the qualifying Tautulli
    plays since then, last-played from Tautulli), builds the taste profile
    from the history before that, then measures recall@k: the share of songs
    you played in those `days` (first play for Unheard, a replay for Rarely
    played) that ranked in the top k. Compared with popularity-only and the
    random expectation."""
    real_now = int(time.time())
    cutoff = int(real_now - days * 86400)
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    tracks, albums, similar, history = gather(plex, real_now, {})

    train = [r for r in history if int(r.get("date", 0)) < cutoff]
    holdout = [r for r in history if int(r.get("date", 0)) >= cutoff
               and is_qualifying_play(r, MIN_LISTEN_SECONDS)]
    later = defaultdict(int)
    for r in holdout:
        later[str(r.get("rating_key"))] += 1
    last_train = {}
    for r in train:
        if is_qualifying_play(r, MIN_LISTEN_SECONDS):
            rk = str(r.get("rating_key"))
            last_train[rk] = max(last_train.get(rk, 0), int(r.get("date", 0)))
    for rk, t in tracks.items():
        t["views"] = max(0, t["views"] - later.get(rk, 0))
        if t["last"] >= cutoff:
            t["last"] = last_train.get(rk, 0) or (cutoff - 365 * 86400 if t["views"] else 0)

    p = Profile(plex, tracks, albums, similar, train, cutoff, [], log=lambda *_: None)
    played_songs = {tracks[rk]["song"] for rk in later if rk in tracks}
    print(f"Backtest: profile as of {datetime.fromtimestamp(cutoff):%Y-%m-%d}, "
          f"{len(holdout)} qualifying plays in the last {days:g} days. "
          f"RECENCY_BIAS is {RECENCY_BIAS:g}; other values for comparison.")
    biases = sorted({0.0, 0.3, 0.6, 0.9, RECENCY_BIAS})
    ranked = {bias: build_candidates(p, set(), set(), bias)[0] for bias in biases}
    for b in ("unheard", "rare"):
        cands = ranked[RECENCY_BIAS][b]
        targets = {t["song"] for _, t in cands} & played_songs
        if not cands or not targets:
            print(f"  {BUCKET_LABELS[b]}: {len(cands)} candidates, {len(targets)} played since — nothing to measure.")
            continue
        print(f"  {BUCKET_LABELS[b]}: {len(cands)} candidates, {len(targets)} played since.")
        popular = [t["song"] for _, t in sorted(cands, key=lambda x: (x[1]["pop"] or 0), reverse=True)]

        def row(label, order):
            cells = [f"@{k} {len(set(order[:k]) & targets) / len(targets):4.0%}" for k in ks]
            print(f"    {label:<22}" + "   ".join(cells))

        for bias in biases:
            order = [t["song"] for _, t in sorted(ranked[bias][b], key=lambda x: x[0], reverse=True)]
            row(f"engine, bias {bias:g}" + (" *" if bias == RECENCY_BIAS else ""), order)
        row("popularity only", popular)
        print(f"    {'random':<22}" + "   ".join(f"@{k} {min(1.0, k / len(cands)):4.0%}" for k in ks))


# --- Schedule ---------------------------------------------------------------

def next_due(state):
    """Today's RUN_HOUR:RUN_MINUTE (local time) if today's run hasn't happened yet, else tomorrow's."""
    last = state.get("last_run")
    if not last:
        return time.time()
    last_dt = datetime.fromtimestamp(last)
    due = last_dt.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if due <= last_dt:
        due += timedelta(days=1)
    return due.timestamp()


def main():
    if BACKTEST_DAYS > 0:
        backtest(BACKTEST_DAYS)
        return
    while True:
        state = load_state()
        due = next_due(state)
        if DRY_RUN or time.time() >= due:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running For You build...")
            try:
                run_once(state)
            except Exception:
                print("Run failed:")
                traceback.print_exc()
            if DRY_RUN:
                return
            state = load_state()
            due = max(next_due(state), time.time() + 3600)   # failed run: retry in an hour
        print(f"Next build at {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}; sleeping...")
        time.sleep(max(60, due - time.time()))


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing required environment variable: {e}")
