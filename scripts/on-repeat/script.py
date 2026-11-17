#!/usr/bin/env python3
"""
On Repeat playlist generator (Spotify-style), looping container version.

Your most-played tracks of the last DAYS, weighted by recency
(0.5 ** (days_ago / HALF_LIFE_DAYS) per qualifying Tautulli play), with the
top ALLTIME_TOP all-time tracks pinned first. Tracks that sit in one of your
other audio playlists get a SAVE_BOOST_MULTIPLIER boost.

Runs once immediately, then re-runs every RUN_INTERVAL_DAYS forever, so it
fits the same long-running-container pattern as your other docker apps
instead of needing a separate cron/systemd timer.

Units: Tautulli `date` is unix seconds; DAYS / HALF_LIFE_DAYS / ALLTIME_DAYS
are days; MIN_LISTEN_SECONDS is seconds actually listened.

Shared code lives in ../common (mount the whole scripts/ folder; see README).
"""

import os
import sys
import time
import traceback
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from plexapi.server import PlexServer  # noqa: E402

from common.plexio import replace_playlist  # noqa: E402
from common.state import load_state, save_state  # noqa: E402
from common.tautulli import Tautulli, is_qualifying_play  # noqa: E402

TAUTULLI_URL = os.environ["TAUTULLI_URL"].rstrip("/")
TAUTULLI_APIKEY = os.environ["TAUTULLI_APIKEY"]
PLEX_URL = os.environ["PLEX_URL"]
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
MUSIC_SECTION_ID = os.environ["MUSIC_SECTION_ID"]

DAYS = int(os.environ.get("DAYS", 30))
HALF_LIFE_DAYS = float(os.environ.get("HALF_LIFE_DAYS", 7))
MIN_LISTEN_SECONDS = int(os.environ.get("MIN_LISTEN_SECONDS", 30))
PLAYLIST_TITLE = os.environ.get("PLAYLIST_TITLE", "On Repeat")
PLAYLIST_SIZE = int(os.environ.get("PLAYLIST_SIZE", 25))
ALLTIME_TOP = int(os.environ.get("ALLTIME_TOP", 3))
ALLTIME_DAYS = int(os.environ.get("ALLTIME_DAYS", 3650))
SAVE_BOOST_MULTIPLIER = float(os.environ.get("SAVE_BOOST_MULTIPLIER", 1.3))
USER_ID = os.environ.get("USER_ID")  # optional
RUN_INTERVAL_DAYS = float(os.environ.get("RUN_INTERVAL_DAYS", 7))
# Only remembers the playlist's ratingKey, so a renamed playlist is still found.
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(HERE, "state.json"))


def track_title(row):
    return f"{row.get('grandparent_title', '')} - {row.get('title', '')}"


def score_recent(rows, now_ts, half_life_days):
    scores = defaultdict(float)
    play_counts = defaultdict(int)
    titles = {}

    for row in rows:
        if not is_qualifying_play(row, MIN_LISTEN_SECONDS):
            continue
        rk = row.get("rating_key")
        if rk is None:
            continue

        days_ago = max(0.0, (now_ts - int(row.get("date", now_ts))) / 86400)
        weight = 0.5 ** (days_ago / half_life_days)

        scores[rk] += weight
        play_counts[rk] += 1
        titles[rk] = track_title(row)

    return scores, play_counts, titles


def count_alltime(rows):
    counts = defaultdict(int)
    titles = {}
    for row in rows:
        if not is_qualifying_play(row, MIN_LISTEN_SECONDS):
            continue
        rk = row.get("rating_key")
        if rk is None:
            continue
        counts[rk] += 1
        titles[rk] = track_title(row)
    return counts, titles


def collect_boost_keys(plex, own_rating_key=None):
    boosted = set()
    try:
        for pl in plex.playlists():
            if pl.title == PLAYLIST_TITLE or (own_rating_key and pl.ratingKey == own_rating_key):
                continue
            if getattr(pl, "playlistType", None) not in (None, "audio"):
                continue
            try:
                for item in pl.items():
                    boosted.add(str(item.ratingKey))
            except Exception:
                continue
    except Exception as e:
        print(f"  warning: couldn't scan playlists for boost signal: {e}")
    return boosted


def build_playlist(recent_scores, recent_plays, recent_titles,
                   alltime_counts, alltime_titles, plex, state):
    titles = {**alltime_titles, **recent_titles}
    boosted_keys = collect_boost_keys(plex, state.get("playlist_rating_key"))

    scored = []
    for rk, score in recent_scores.items():
        if rk in boosted_keys:
            score *= SAVE_BOOST_MULTIPLIER
        scored.append((rk, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    alltime_ranked = sorted(alltime_counts.items(), key=lambda x: x[1], reverse=True)
    pinned = alltime_ranked[:ALLTIME_TOP]
    pinned_keys = {rk for rk, _ in pinned}

    fill = [(rk, s) for rk, s in scored if rk not in pinned_keys]
    ranked = pinned + fill
    ranked = ranked[:PLAYLIST_SIZE]

    if not ranked:
        print("No tracks met the qualifying-play threshold in the window.")
        return

    tracks = []
    for rk, _ in ranked:
        try:
            tracks.append(plex.fetchItem(int(rk)))
        except Exception as e:
            print(f"  skip rating_key={rk} ({titles.get(rk, '?')}): {e}")

    if not tracks:
        print("Resolved zero tracks from Plex; nothing to build.")
        return

    playlist = replace_playlist(plex, PLAYLIST_TITLE, tracks, state.get("playlist_rating_key"))
    state["playlist_rating_key"] = getattr(playlist, "ratingKey", None)

    print(f"'{PLAYLIST_TITLE}' updated with {len(tracks)} tracks "
          f"(top {ALLTIME_TOP} all-time pinned, rest recency-weighted over "
          f"last {DAYS} days, half-life {HALF_LIFE_DAYS}d, cap {PLAYLIST_SIZE}).")
    for rk, val in ranked:
        tag = " [all-time]" if rk in pinned_keys else ""
        boost = " [boosted]" if rk in boosted_keys and rk not in pinned_keys else ""
        plays = recent_plays.get(rk)
        plays_str = f"{plays} plays" if plays is not None else "all-time pin"
        print(f"  score={val:6.2f}  {plays_str:<14} {titles.get(rk, rk)}{tag}{boost}")


def run_once():
    now_ts = int(time.time())
    state = load_state(STATE_FILE, {})

    # One fetch covers both windows (the recent one is a subset).
    alltime_rows = Tautulli(TAUTULLI_URL, TAUTULLI_APIKEY).history(
        MUSIC_SECTION_ID, USER_ID, after_ts=now_ts - ALLTIME_DAYS * 86400, page_size=250)
    recent_cutoff = now_ts - DAYS * 86400
    recent_rows = [row for row in alltime_rows if int(row.get("date", 0)) >= recent_cutoff]

    recent_scores, recent_plays, recent_titles = score_recent(recent_rows, now_ts, HALF_LIFE_DAYS)
    alltime_counts, alltime_titles = count_alltime(alltime_rows)

    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    build_playlist(recent_scores, recent_plays, recent_titles,
                   alltime_counts, alltime_titles, plex, state)
    try:
        save_state(STATE_FILE, state)
    except OSError as e:
        print(f"  warning: couldn't save {STATE_FILE}: {e}")


def main():
    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running On Repeat build...")
        try:
            run_once()
        except Exception:
            print("Run failed:")
            traceback.print_exc()

        sleep_seconds = RUN_INTERVAL_DAYS * 86400
        print(f"Sleeping {RUN_INTERVAL_DAYS} days until next run...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        sys.exit(f"Missing required environment variable: {e}")
