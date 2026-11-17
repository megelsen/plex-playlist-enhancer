#!/usr/bin/env python3
"""
Discover Weekly playlist generator (Spotify-style, limited to the Plex library).

Seeds come from recent listening (Tautulli) plus a few all-time favourites.
Every unplayed candidate gets an affinity score:

    affinity = 0.45 * sonic   (Plex sonic analysis: closeness to seed tracks)
             + 0.30 * artist  (own affinity, or similar-artist link to seeds)
             + 0.25 * style   (album styles/genres vs. weighted taste profile)

and lands in one of four buckets:

    New to You   tracks from albums with zero plays (album's most popular track)
    Missed Hits  unplayed popular tracks (within-artist percentile) by artists you play
    Hidden Gems  unplayed low-popularity tracks that sound like your seeds
    Rediscover   old favourites (>= 3 plays) not played in 180+ days

Picks are weighted-random samples from each bucket's top pool, so the list
changes weekly. state.json remembers past picks (no repeats for HISTORY_WEEKS)
and their Plex play/skip counts, which feed back into the next run.

Order: buckets are interleaved with the strongest picks first, then each
DJ_STRETCH-track stretch is DJ-ordered (plex-playlist-enhancer's dj_order) so
neighbouring tracks sound alike and no artist plays twice in a row.

Runs in a loop like plex-on-repeat, but only rebuilds when the last build is
older than RUN_INTERVAL_DAYS, so container restarts don't reshuffle the list.
"""

import json
import math
import os
import random
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime

import requests
from plexapi.server import PlexServer

TAUTULLI_URL = os.environ["TAUTULLI_URL"].rstrip("/")
TAUTULLI_APIKEY = os.environ["TAUTULLI_APIKEY"]
PLEX_URL = os.environ["PLEX_URL"]
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
MUSIC_SECTION_ID = os.environ["MUSIC_SECTION_ID"]

PLAYLIST_TITLE = os.environ.get("PLAYLIST_TITLE", "Discover Weekly")
ON_REPEAT_TITLE = os.environ.get("ON_REPEAT_TITLE", "On Repeat")
BUCKET_SIZES = {
    "new": int(os.environ.get("NEW_TO_YOU", 24)),
    "hits": int(os.environ.get("MISSED_HITS", 16)),
    "gems": int(os.environ.get("HIDDEN_GEMS", 12)),
    "rediscover": int(os.environ.get("REDISCOVER", 8)),
}
BUCKET_LABELS = {"new": "New to You", "hits": "Missed Hits",
                 "gems": "Hidden Gems", "rediscover": "Rediscover"}

SEED_DAYS = int(os.environ.get("SEED_DAYS", 30))
SEED_HALF_LIFE_DAYS = float(os.environ.get("SEED_HALF_LIFE_DAYS", 14))
SEED_RECENT = int(os.environ.get("SEED_RECENT", 20))
SEED_ALLTIME = int(os.environ.get("SEED_ALLTIME", 5))
TASTE_HALF_LIFE_DAYS = float(os.environ.get("TASTE_HALF_LIFE_DAYS", 90))
SEED_ARTISTS = int(os.environ.get("SEED_ARTISTS", 10))
SONIC_LIMIT = int(os.environ.get("SONIC_LIMIT", 40))
MIN_LISTEN_SECONDS = int(os.environ.get("MIN_LISTEN_SECONDS", 30))

MIN_TRACK_SECONDS = int(os.environ.get("MIN_TRACK_SECONDS", 90))
MAX_PER_ARTIST = int(os.environ.get("MAX_PER_ARTIST", 2))
MAX_PER_ALBUM = int(os.environ.get("MAX_PER_ALBUM", 1))
REDISCOVER_MIN_PLAYS = int(os.environ.get("REDISCOVER_MIN_PLAYS", 3))
REDISCOVER_MIN_DAYS = int(os.environ.get("REDISCOVER_MIN_DAYS", 180))
RECENT_ADD_DAYS = int(os.environ.get("RECENT_ADD_DAYS", 90))
HISTORY_WEEKS = int(os.environ.get("HISTORY_WEEKS", 6))
POOL_FACTOR = float(os.environ.get("POOL_FACTOR", 5))
TEMPERATURE = float(os.environ.get("TEMPERATURE", 0.1))
DJ_STRETCH = int(os.environ.get("DJ_STRETCH", 20))
# Picks are spread across the whole library, so Plex's default 0.25 sonic
# radius almost never links two of them; 0.6 gives ~4-5 links per track.
DJ_NEIGHBOR_LIMIT = int(os.environ.get("DJ_NEIGHBOR_LIMIT", 3000))
DJ_MAX_DISTANCE = float(os.environ.get("DJ_MAX_DISTANCE", 0.6))
DJ_RECENT_ARTIST_WINDOW = 3     # songs back that count as "just heard this artist"
DJ_RECENT_ARTIST_PENALTY = 0.4

RUN_INTERVAL_DAYS = float(os.environ.get("RUN_INTERVAL_DAYS", 7))
STATE_FILE = os.environ.get("STATE_FILE", "/app/state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
USER_ID = os.environ.get("USER_ID")  # optional

TAUTULLI_API = f"{TAUTULLI_URL}/api/v2"

VERSION_WORDS = (r"live|demo|remaster\w*|remix|version|edit|mono|stereo|acoustic|"
                 r"bonus|instrumental|deluxe|mix|single|take|session")
BRACKET_RE = re.compile(rf"\s*[\(\[][^)\]]*\b({VERSION_WORDS})\b[^)\]]*[\)\]]", re.I)
DASH_RE = re.compile(rf"\s+-\s+.*\b({VERSION_WORDS})\b.*$", re.I)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# --- Tautulli ---------------------------------------------------------------

def tautulli_get(cmd, **params):
    params.update({"apikey": TAUTULLI_APIKEY, "cmd": cmd})
    r = requests.get(TAUTULLI_API, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("response", {}).get("result") != "success":
        raise RuntimeError(f"Tautulli API error on {cmd}: {data}")
    return data["response"]["data"]


def fetch_history(section_id, user_id=None):
    rows = []
    start = 0
    length = 1000
    while True:
        params = dict(section_id=section_id, media_type="track", grouping=0,
                      order_column="date", order_dir="desc",
                      start=start, length=length)
        if user_id:
            params["user_id"] = user_id
        page = tautulli_get("get_history", **params).get("data", [])
        rows.extend(page)
        if len(page) < length:
            break
        start += length
    return rows


def is_qualifying_play(row):
    try:
        started = int(row.get("started", 0))
        stopped = int(row.get("stopped", 0))
        paused = int(row.get("paused_counter") or 0)
        if started and stopped and stopped > started:
            return (stopped - started) - paused >= MIN_LISTEN_SECONDS
    except (TypeError, ValueError):
        pass
    return int(row.get("watched_status", 0)) == 1


# --- Helpers ----------------------------------------------------------------

def norm(text):
    return NON_ALNUM_RE.sub("", (text or "").lower())


def song_key(artist, title):
    """Same song across live/remaster/compilation copies -> same key."""
    title = BRACKET_RE.sub("", title or "")
    title = DASH_RE.sub("", title)
    return f"{norm(artist)}|{norm(title)}"


def to_ts(dt):
    return dt.timestamp() if dt else 0


def normalize(d, flatten=False):
    if not d:
        return {}
    top = max(d.values()) or 1
    return {k: (math.sqrt(v / top) if flatten else v / top) for k, v in d.items()}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"runs": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --- Library snapshot -------------------------------------------------------

def load_library(plex):
    section = plex.library.sectionByID(int(MUSIC_SECTION_ID))

    albums = {}
    for a in section.search(libtype="album", maxresults=100000):
        tags = {t.tag.lower() for t in (a.styles or [])} | {t.tag.lower() for t in (a.genres or [])}
        albums[str(a.ratingKey)] = {"tags": tags, "added": to_ts(a.addedAt)}

    similar = {}
    for a in section.search(libtype="artist", maxresults=100000):
        similar[norm(a.title)] = {norm(s.tag) for s in (a.similar or [])}

    tracks = {}
    for t in section.search(libtype="track", maxresults=100000):
        artist = t.originalTitle or t.grandparentTitle or ""
        tracks[str(t.ratingKey)] = {
            "rk": str(t.ratingKey),
            "title": t.title,
            "artist": artist,
            "artist_key": norm(artist),
            "album_key": str(t.parentRatingKey),
            "song": song_key(artist, t.title),
            "views": t.viewCount or 0,
            "skips": t.skipCount or 0,
            "last": to_ts(t.lastViewedAt),
            "rating": t.userRating,
            "pop": t.ratingCount,
            "duration": (t.duration or 0) / 1000,
        }
    return tracks, albums, similar


# --- Taste profile ----------------------------------------------------------

def build_taste(tracks, albums, history, now_ts):
    """Artist affinity, style profile and seed tracks from listening history."""
    artist_aff = defaultdict(float)
    style = defaultdict(float)
    recent = defaultdict(float)

    for row in history:
        if not is_qualifying_play(row):
            continue
        rk = str(row.get("rating_key"))
        t = tracks.get(rk)
        if not t:
            continue
        days_ago = max(0.0, (now_ts - int(row.get("date", now_ts))) / 86400)
        w = 0.5 ** (days_ago / TASTE_HALF_LIFE_DAYS)
        artist_aff[t["artist_key"]] += w
        for tag in albums.get(t["album_key"], {}).get("tags", ()):
            style[tag] += w
        if days_ago <= SEED_DAYS:
            recent[rk] += 0.5 ** (days_ago / SEED_HALF_LIFE_DAYS)

    # Plex lifetime counts cover plays from before Tautulli existed.
    for t in tracks.values():
        if t["views"]:
            artist_aff[t["artist_key"]] += 0.1 * t["views"]
            for tag in albums.get(t["album_key"], {}).get("tags", ()):
                style[tag] += 0.1 * t["views"]

    seeds = {}
    recent_norm = normalize(recent)
    for rk, s in sorted(recent_norm.items(), key=lambda x: x[1], reverse=True)[:SEED_RECENT]:
        seeds[rk] = s
    alltime = sorted((t for t in tracks.values() if t["views"]),
                     key=lambda t: t["views"], reverse=True)
    for t in [t for t in alltime if t["rk"] not in seeds][:SEED_ALLTIME]:
        seeds[t["rk"]] = 0.6

    return normalize(artist_aff, flatten=True), style, seeds


def sonic_scores(plex, seeds):
    scores = defaultdict(float)
    hits = defaultdict(int)
    for rk, weight in seeds.items():
        try:
            similar = plex.fetchItem(int(rk)).sonicallySimilar(limit=SONIC_LIMIT)
        except Exception as e:
            print(f"  warning: sonic lookup failed for {rk}: {e}")
            continue
        for rank, item in enumerate(similar):
            key = str(item.ratingKey)
            scores[key] = max(scores[key], weight * (1 - rank / SONIC_LIMIT))
            hits[key] += 1
    # Tracks that sound like several seeds get a small bonus.
    return {k: min(1.0, s + 0.1 * (hits[k] - 1)) for k, s in scores.items()}


def similar_artist_scores(similar, artist_aff):
    seed_artists = sorted(artist_aff.items(), key=lambda x: x[1], reverse=True)[:SEED_ARTISTS * 3]
    link = defaultdict(float)
    for artist, aff in seed_artists:
        for s in similar.get(artist, ()):
            link[s] = max(link[s], aff)
            for s2 in similar.get(s, ()):
                link[s2] = max(link[s2], 0.5 * aff)
    return link


# --- Feedback ---------------------------------------------------------------

def compute_feedback(state, tracks, albums, style):
    """Artist multipliers (and style boosts) from how past picks were received."""
    pos = defaultdict(int)
    neg = defaultdict(int)
    for run in state["runs"]:
        for pick in run["picks"]:
            if pick["bucket"] == "rediscover":
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


# --- Candidates -------------------------------------------------------------

def build_candidates(tracks, albums, artist_aff, style, seeds, sonic, link,
                     multipliers, banned, exclude_rks, now_ts):
    played_songs = {t["song"] for t in tracks.values() if t["views"]}
    album_plays = defaultdict(int)
    for t in tracks.values():
        album_plays[t["album_key"]] += t["views"]
    played_artists = {a for a, s in artist_aff.items() if s > 0}
    seed_artists = {a for a, _ in sorted(artist_aff.items(), key=lambda x: x[1],
                                         reverse=True)[:SEED_ARTISTS]}

    style_norm = normalize(style)
    album_style = {k: sum(style_norm.get(tag, 0) for tag in a["tags"]) / math.sqrt(len(a["tags"]))
                   for k, a in albums.items() if a["tags"]}
    album_style = normalize(album_style)

    # Within-artist popularity percentile, one entry per song.
    by_artist = defaultdict(dict)
    for t in tracks.values():
        if t["pop"]:
            by_artist[t["artist_key"]][t["song"]] = max(by_artist[t["artist_key"]].get(t["song"], 0), t["pop"])
    pct = {}
    for artist, songs in by_artist.items():
        ranked = sorted(songs, key=songs.get)
        n = len(ranked)
        for i, song in enumerate(ranked):
            pct[(artist, song)] = i / (n - 1) if n > 1 else 0.5

    def usable(t):
        return (t["duration"] >= MIN_TRACK_SECONDS
                and not (t["rating"] and t["rating"] <= 4)
                and t["rk"] not in exclude_rks
                and t["artist_key"] not in banned)

    def affinity(t):
        artist = max(artist_aff.get(t["artist_key"], 0), link.get(t["artist_key"], 0))
        return (0.45 * sonic.get(t["rk"], 0) + 0.30 * artist
                + 0.25 * album_style.get(t["album_key"], 0))

    buckets = {b: [] for b in BUCKET_SIZES}
    unplayed = [t for t in tracks.values() if t["song"] not in played_songs and usable(t)]

    # New to You: best-known track of each never-played album.
    best_on_album = {}
    for t in unplayed:
        if album_plays[t["album_key"]] or t["artist_key"] in seed_artists:
            continue
        cur = best_on_album.get(t["album_key"])
        if cur is None or (t["pop"] or 0) > (cur["pop"] or 0):
            best_on_album[t["album_key"]] = t
    for t in best_on_album.values():
        score = affinity(t) + (0.1 if t["artist_key"] not in played_artists else 0)
        buckets["new"].append((score, t))

    for t in unplayed:
        p = pct.get((t["artist_key"], t["song"]))
        aff = artist_aff.get(t["artist_key"], 0)

        # Missed Hits: popular-for-the-artist songs you never got to.
        if aff > 0 and p is not None and p >= 0.6:
            buckets["hits"].append((0.6 * p + 0.4 * aff, t))

        # Hidden Gems: obscure, but sounds like what you're into.
        if t["artist_key"] not in seed_artists and (p is None or p <= 0.5):
            s = sonic.get(t["rk"], 0)
            st = album_style.get(t["album_key"], 0)
            if s > 0 or st >= 0.5:
                album = albums.get(t["album_key"], {})
                fresh = (not album_plays[t["album_key"]]
                         and now_ts - album.get("added", 0) <= RECENT_ADD_DAYS * 86400)
                score = (0.55 * s + 0.25 * st
                         + 0.2 * max(aff, link.get(t["artist_key"], 0))
                         + (0.15 if fresh else 0))
                buckets["gems"].append((score, t))

    for b in ("new", "hits", "gems"):
        buckets[b] = [(s * multipliers.get(t["artist_key"], 1), t) for s, t in buckets[b]]

    # Rediscover: old favourites gone quiet. Aggregate plays across copies of a song.
    songs = defaultdict(lambda: {"views": 0, "last": 0, "best": None})
    for t in tracks.values():
        s = songs[t["song"]]
        s["views"] += t["views"]
        s["last"] = max(s["last"], t["last"])
        if t["views"] and (s["best"] is None or t["views"] > s["best"]["views"]):
            s["best"] = t
    # A young library has no 180-day-old favourites yet: scale the gap with its age.
    library_age = (now_ts - min((a["added"] for a in albums.values() if a["added"]),
                                default=now_ts)) / 86400
    min_days = min(REDISCOVER_MIN_DAYS, max(45, library_age / 2))
    for s in songs.values():
        t = s["best"]
        if not t or s["views"] < REDISCOVER_MIN_PLAYS or not usable(t):
            continue
        days = (now_ts - s["last"]) / 86400
        if days >= min_days:
            buckets["rediscover"].append((math.log1p(s["views"]) * min(days / 365, 2), t))

    return buckets


# --- Selection --------------------------------------------------------------

def weighted_order(cands, n):
    """Weighted random order over the top pool, then the rest by score."""
    cands = sorted(cands, key=lambda x: x[0], reverse=True)
    pool_size = max(n, int(n * POOL_FACTOR))
    pool, rest = cands[:pool_size], cands[pool_size:]
    if not pool:
        return []
    top = pool[0][0]
    span = (top - pool[-1][0]) or 1
    keyed = []
    for score, t in pool:
        w = math.exp((score - top) / span / TEMPERATURE)
        keyed.append((math.log(random.random() or 1e-12) / w, score, t))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [(s, t) for _, s, t in keyed] + rest


def select(buckets):
    per_artist = defaultdict(int)
    per_album = defaultdict(int)
    used_songs = set()
    per_bucket_artist = defaultdict(int)
    picked = {b: [] for b in BUCKET_SIZES}

    def take(bucket, cands, n):
        # Discovery buckets show an artist once, so one new band can't take two slots.
        artist_cap = 1 if bucket in ("new", "gems") else MAX_PER_ARTIST
        for score, t in cands:
            if len(picked[bucket]) >= n:
                break
            if (t["song"] in used_songs or per_artist[t["artist_key"]] >= MAX_PER_ARTIST
                    or per_bucket_artist[(bucket, t["artist_key"])] >= artist_cap
                    or per_album[t["album_key"]] >= MAX_PER_ALBUM):
                continue
            used_songs.add(t["song"])
            per_artist[t["artist_key"]] += 1
            per_bucket_artist[(bucket, t["artist_key"])] += 1
            per_album[t["album_key"]] += 1
            picked[bucket].append((score, t))

    for b in ("new", "hits", "gems", "rediscover"):
        take(b, weighted_order(buckets[b], BUCKET_SIZES[b]), BUCKET_SIZES[b])

    # Backfill short buckets so the playlist still reaches full size.
    total = sum(BUCKET_SIZES.values())
    for b in ("new", "gems", "hits"):
        missing = total - sum(len(v) for v in picked.values())
        if missing <= 0:
            break
        take(b, sorted(buckets[b], key=lambda x: x[0], reverse=True), len(picked[b]) + missing)

    for b in picked:
        picked[b].sort(key=lambda x: x[0], reverse=True)
    return picked


def interleave(picked):
    """Open with two safe New to You picks, then spread buckets evenly."""
    queues = {b: list(v) for b, v in picked.items()}
    order = []
    for _ in range(2):
        if queues["new"]:
            order.append(("new", queues["new"].pop(0)[1]))
    targets = {b: max(1, len(v)) for b, v in picked.items()}
    placed = defaultdict(int, {"new": len(order)})
    prev = "new"
    while any(queues.values()):
        prev_artist = order[-1][1]["artist_key"] if order else None
        options = [b for b in queues if queues[b]]
        options = [b for b in options if b != prev] or options
        b = min(options, key=lambda b: placed[b] / targets[b])
        queue = queues[b]
        # Take the best track in that bucket whose artist didn't just play.
        i = next((i for i, (_, t) in enumerate(queue) if t["artist_key"] != prev_artist), 0)
        order.append((b, queue.pop(i)[1]))
        placed[b] += 1
        prev = b
    return order


# --- DJ ordering (ported from plex-playlist-enhancer's live_playlists.dj_order)

def sonic_links(items):
    """Pairwise Plex sonic match (0..1) between playlist tracks: one
    sonicallySimilar call per track, kept only where the neighbour is also in
    the playlist."""
    keys = {str(i.ratingKey) for i in items}
    direct = defaultdict(dict)
    for item in items:
        a = str(item.ratingKey)
        try:
            matches = item.sonicallySimilar(limit=DJ_NEIGHBOR_LIMIT, maxDistance=DJ_MAX_DISTANCE)
        except Exception as e:
            print(f"  warning: sonic lookup failed while ordering: {e}")
            continue
        for m in matches:
            b = str(m.ratingKey)
            if b in keys and b != a:
                distance = getattr(m, "distance", None)
                w = 1 - float(distance) if distance is not None else 0.5
                direct[a][b] = max(direct[a].get(b, 0.0), w)
                direct[b][a] = max(direct[b].get(a, 0.0), w)
    return direct


def no_doubling_possible(counts, last_artist, n):
    """Can n tracks with these per-artist counts be ordered with no artist
    twice in a row, given the previous track was by last_artist?"""
    for artist, c in counts.items():
        if c > (n + 1) // 2 or (artist == last_artist and c > n // 2):
            return False
    return True


def dj_order(tracks, direct, albums, prev=None, recent=()):
    """Opener (the first track, or the one sounding most like `prev`), then
    always the remaining track that sounds most like the one before it, never
    the same artist twice in a row (with lookahead so the end can't get stuck).

    Transition score: 1 + sonic match if one track is in the other's
    sonicallySimilar list, else 0.6 for the same album, else album tag overlap
    (Jaccard) x 0.5; minus DJ_RECENT_ARTIST_PENALTY if the artist played in
    the last DJ_RECENT_ARTIST_WINDOW tracks; plus a tiny tie-breaking jitter."""
    def similarity(a, b):
        if b["rk"] in direct[a["rk"]]:
            return 1.0 + direct[a["rk"]][b["rk"]]
        if a["album_key"] == b["album_key"]:
            return 0.6
        ta = albums.get(a["album_key"], {}).get("tags", set())
        tb = albums.get(b["album_key"], {}).get("tags", set())
        return 0.5 * len(ta & tb) / len(ta | tb) if ta | tb else 0.0

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

    ordered = list(recent)
    current = prev
    remaining = list(tracks)
    while remaining:
        last_artist = current["artist_key"] if current else None
        recent_artists = {t["artist_key"] for t in ordered[-DJ_RECENT_ARTIST_WINDOW:]}
        pool = (eligible(remaining, last_artist)
                or [t for t in remaining if t["artist_key"] != last_artist]
                or remaining)
        if current is None:
            nxt = pool[0]
        else:
            nxt = max(pool, key=lambda t: similarity(current, t)
                      - (DJ_RECENT_ARTIST_PENALTY if t["artist_key"] in recent_artists else 0.0)
                      + random.random() * 1e-3)
        ordered.append(nxt)
        remaining.remove(nxt)
        counts[nxt["artist_key"]] -= 1
        current = nxt
    return ordered[len(recent):]


def dj_stretches(order, direct, albums):
    """Keep the interleave's shape (strongest picks early, buckets spread out)
    but DJ-order each DJ_STRETCH-track stretch, chaining every stretch onto the
    last track of the one before."""
    out = []
    for i in range(0, len(order), DJ_STRETCH):
        chunk = order[i:i + DJ_STRETCH]
        bucket_of = {t["rk"]: b for b, t in chunk}
        tracks = dj_order([t for _, t in chunk], direct, albums,
                          prev=out[-1][1] if out else None,
                          recent=[t for _, t in out[-DJ_RECENT_ARTIST_WINDOW:]])
        out.extend((bucket_of[t["rk"]], t) for t in tracks)
    return out


# --- Main -------------------------------------------------------------------

def run_once(state):
    now_ts = int(time.time())
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)

    tracks, albums, similar = load_library(plex)
    history = fetch_history(MUSIC_SECTION_ID, USER_ID)
    print(f"  library: {len(tracks)} tracks, {len(albums)} albums; "
          f"{len(history)} Tautulli plays")

    cutoff = now_ts - HISTORY_WEEKS * 7 * 86400
    state["runs"] = [r for r in state["runs"] if r["ts"] >= cutoff]

    artist_aff, style, seeds = build_taste(tracks, albums, history, now_ts)
    multipliers, banned = compute_feedback(state, tracks, albums, style)
    sonic = sonic_scores(plex, seeds)
    link = similar_artist_scores(similar, artist_aff)

    exclude = {p["rk"] for r in state["runs"] for p in r["picks"]}
    on_repeat = next((p for p in plex.playlists() if p.title == ON_REPEAT_TITLE), None)
    if on_repeat:
        exclude |= {str(i.ratingKey) for i in on_repeat.items()}

    buckets = build_candidates(tracks, albums, artist_aff, style, seeds, sonic, link,
                               multipliers, banned, exclude, now_ts)
    print("  candidates: " + ", ".join(f"{BUCKET_LABELS[b]} {len(c)}" for b, c in buckets.items()))
    print(f"  {len(seeds)} seeds, {len(sonic)} sonic neighbours, "
          f"{len(multipliers)} artists re-weighted, {len(banned)} benched by skips")

    order = interleave(select(buckets))
    if not order:
        print("No candidates found; nothing to build.")
        return False

    items = {}
    for _, t in order:
        try:
            items[t["rk"]] = plex.fetchItem(int(t["rk"]))
        except Exception as e:
            print(f"  skip {t['artist']} - {t['title']}: {e}")
    if not items:
        print("Resolved zero tracks from Plex; nothing to build.")
        return False
    order = [(b, t) for b, t in order if t["rk"] in items]

    direct = sonic_links(items.values())
    order = dj_stretches(order, direct, albums)
    items = [items[t["rk"]] for _, t in order]

    for i, (b, t) in enumerate(order, 1):
        link = direct[order[i - 2][1]["rk"]].get(t["rk"]) if i > 1 else None
        match = f"{link:4.0%}" if link is not None else "  · "
        print(f"  {i:2}. {match} [{BUCKET_LABELS[b]:<11}] {t['artist']} - {t['title']}")

    if DRY_RUN:
        print("DRY_RUN set: playlist and state left untouched.")
        return False

    existing = next((p for p in plex.playlists() if p.title == PLAYLIST_TITLE), None)
    if existing:
        existing.removeItems(existing.items())
        existing.addItems(items)
    else:
        existing = plex.createPlaylist(PLAYLIST_TITLE, items=items)
    try:
        counts = defaultdict(int)
        for b, _ in order:
            counts[b] += 1
        existing.editSummary(
            f"Your weekly mix of library discoveries, {datetime.now():%d %b %Y}. "
            + " · ".join(f"{BUCKET_LABELS[b]} {counts[b]}" for b in BUCKET_SIZES if counts[b]))
    except Exception as e:
        print(f"  warning: couldn't set playlist summary: {e}")

    state["runs"].append({
        "ts": now_ts,
        "picks": [{"rk": t["rk"], "bucket": b, "views": t["views"], "skips": t["skips"]}
                  for b, t in order],
    })
    state["last_run"] = now_ts
    save_state(state)
    print(f"'{PLAYLIST_TITLE}' updated with {len(items)} tracks.")
    return True


def main():
    while True:
        state = load_state()
        due = state.get("last_run", 0) + RUN_INTERVAL_DAYS * 86400
        if DRY_RUN or time.time() >= due:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running Discover Weekly build...")
            try:
                run_once(state)
            except Exception:
                print("Run failed:")
                traceback.print_exc()
            if DRY_RUN:
                return
            state = load_state()
            due = max(state.get("last_run", 0) + RUN_INTERVAL_DAYS * 86400,
                      time.time() + 3600)  # failed run: retry in an hour
        sleep_seconds = max(60, due - time.time())
        print(f"Next build at {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}; sleeping...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing required environment variable: {e}")
