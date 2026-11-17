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
and their Plex play/skip counts, which feed back into the next run (together
with For You's picks, when its state file is there).

Order: buckets are interleaved with the strongest picks first, then each
DJ_STRETCH-track stretch is DJ-ordered so neighbouring tracks sound alike and
no artist plays twice in a row.

Runs in a loop like plex-on-repeat, but only rebuilds when the last build is
older than RUN_INTERVAL_DAYS, so container restarts don't reshuffle the list.

Shared code lives in ../common (mount the whole scripts/ folder; see README).
"""

import math
import os
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from plexapi.server import PlexServer  # noqa: E402

from common.dj import sonic_links, dj_stretches  # noqa: E402
from common.feedback import compute_feedback  # noqa: E402
from common.library import (load_library, aggregate_songs,  # noqa: E402
                            artist_song_percentiles, album_style_scores)
from common.plexio import playlist_keys, replace_playlist, set_summary  # noqa: E402
from common.select import Selector, interleave, weighted_order  # noqa: E402
from common.state import load_state as _load_state, save_state as _save_state, read_runs  # noqa: E402
from common.tautulli import Tautulli  # noqa: E402
from common.taste import build_taste, sonic_neighbours, sonic_scores, similar_artist_scores  # noqa: E402

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

RUN_INTERVAL_DAYS = float(os.environ.get("RUN_INTERVAL_DAYS", 7))
# Default: next to this script (= /app/state.json in the old one-folder layout).
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "state.json"))
FOR_YOU_STATE_FILE = os.environ.get("FOR_YOU_STATE_FILE",
                                    os.path.join(os.path.dirname(HERE), "for-you", "state.json"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
USER_ID = os.environ.get("USER_ID")  # optional


def load_state():
    return _load_state(STATE_FILE)


def save_state(state):
    _save_state(STATE_FILE, state)


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

    album_style = album_style_scores(albums, style)
    pct = artist_song_percentiles(tracks)

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
    songs = aggregate_songs(tracks)
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

def select(buckets):
    # Discovery buckets show an artist once, so one new band can't take two slots.
    sel = Selector(BUCKET_SIZES, MAX_PER_ARTIST, MAX_PER_ALBUM,
                   bucket_artist_cap={"new": 1, "gems": 1})

    for b in ("new", "hits", "gems", "rediscover"):
        sel.take(b, weighted_order(buckets[b], BUCKET_SIZES[b], POOL_FACTOR, TEMPERATURE),
                 BUCKET_SIZES[b])

    # Backfill short buckets so the playlist still reaches full size.
    picked = sel.picked
    total = sum(BUCKET_SIZES.values())
    for b in ("new", "gems", "hits"):
        missing = total - sum(len(v) for v in picked.values())
        if missing <= 0:
            break
        sel.take(b, sorted(buckets[b], key=lambda x: x[0], reverse=True), len(picked[b]) + missing)

    for b in picked:
        picked[b].sort(key=lambda x: x[0], reverse=True)
    return picked


# --- Main -------------------------------------------------------------------

def run_once(state):
    now_ts = int(time.time())
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)

    tracks, albums, similar = load_library(plex, MUSIC_SECTION_ID)
    # Whole history: plays from every year count (decayed) in the taste profile.
    history = Tautulli(TAUTULLI_URL, TAUTULLI_APIKEY).history(MUSIC_SECTION_ID, USER_ID)
    print(f"  library: {len(tracks)} tracks, {len(albums)} albums; "
          f"{len(history)} Tautulli plays")

    cutoff = now_ts - HISTORY_WEEKS * 7 * 86400
    state["runs"] = [r for r in state["runs"] if r["ts"] >= cutoff]

    taste = build_taste(tracks, albums, history, now_ts,
                        min_listen_seconds=MIN_LISTEN_SECONDS,
                        taste_half_life_days=TASTE_HALF_LIFE_DAYS,
                        seed_days=SEED_DAYS, seed_half_life_days=SEED_HALF_LIFE_DAYS,
                        seed_recent=SEED_RECENT, seed_alltime=SEED_ALLTIME)
    artist_aff, style, seeds = taste.artist_aff, taste.style, taste.seeds
    other_runs = [r for r in read_runs(FOR_YOU_STATE_FILE) if r.get("ts", 0) >= cutoff]
    multipliers, banned = compute_feedback(state["runs"] + other_runs, tracks, albums, style)
    sonic = sonic_scores(sonic_neighbours(plex, seeds, SONIC_LIMIT), seeds, SONIC_LIMIT)
    link = similar_artist_scores(similar, artist_aff, SEED_ARTISTS)

    exclude = {p["rk"] for r in state["runs"] for p in r["picks"]}
    exclude |= playlist_keys(plex, ON_REPEAT_TITLE)

    buckets = build_candidates(tracks, albums, artist_aff, style, seeds, sonic, link,
                               multipliers, banned, exclude, now_ts)
    print("  candidates: " + ", ".join(f"{BUCKET_LABELS[b]} {len(c)}" for b, c in buckets.items()))
    print(f"  {len(seeds)} seeds, {len(sonic)} sonic neighbours, "
          f"{len(multipliers)} artists re-weighted, {len(banned)} benched by skips")

    order = interleave(select(buckets), opener="new", opener_count=2)
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

    direct = sonic_links(items.values(), DJ_NEIGHBOR_LIMIT, DJ_MAX_DISTANCE)
    order = dj_stretches(order, direct, albums, DJ_STRETCH)
    items = [items[t["rk"]] for _, t in order]

    for i, (b, t) in enumerate(order, 1):
        link = direct[order[i - 2][1]["rk"]].get(t["rk"]) if i > 1 else None
        match = f"{link:4.0%}" if link is not None else "  · "
        print(f"  {i:2}. {match} [{BUCKET_LABELS[b]:<11}] {t['artist']} - {t['title']}")

    if DRY_RUN:
        print("DRY_RUN set: playlist and state left untouched.")
        return False

    playlist = replace_playlist(plex, PLAYLIST_TITLE, items, state.get("playlist_rating_key"))
    counts = defaultdict(int)
    for b, _ in order:
        counts[b] += 1
    set_summary(playlist,
                f"Your weekly mix of library discoveries, {datetime.now():%d %b %Y}. "
                + " · ".join(f"{BUCKET_LABELS[b]} {counts[b]}" for b in BUCKET_SIZES if counts[b]))

    state["playlist_rating_key"] = getattr(playlist, "ratingKey", None)
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
