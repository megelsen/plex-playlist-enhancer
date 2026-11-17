#!/usr/bin/env python3
"""
On Repeat playlist generator (Spotify-style), looping container version.

Runs once immediately, then re-runs every RUN_INTERVAL_DAYS forever, so it
fits the same long-running-container pattern as your other docker apps
instead of needing a separate cron/systemd timer.

See prior chat for the full rationale behind the scoring approach.
"""

import math
import os
import sys
import time
import traceback
from collections import defaultdict

import requests
from plexapi.server import PlexServer

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

TAUTULLI_API = f"{TAUTULLI_URL}/api/v2"


def tautulli_get(cmd, **params):
    params.update({"apikey": TAUTULLI_APIKEY, "cmd": cmd})
    r = requests.get(TAUTULLI_API, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("response", {}).get("result") != "success":
        raise RuntimeError(f"Tautulli API error on {cmd}: {data}")
    return data["response"]["data"]


def fetch_raw_history(days, section_id, user_id=None):
    start_ts = int(time.time()) - days * 86400
    rows = []
    start = 0
    length = 250

    while True:
        params = dict(
            section_id=section_id,
            media_type="track",
            grouping=0,
            order_column="date",
            order_dir="desc",
            start=start,
            length=length,
            after=time.strftime("%Y-%m-%d", time.localtime(start_ts)),
        )
        if user_id:
            params["user_id"] = user_id

        data = tautulli_get("get_history", **params)
        page = data.get("data", [])
        if not page:
            break

        rows.extend(page)

        if len(page) < length:
            break
        start += length

    return [row for row in rows if int(row.get("date", 0)) >= start_ts]


def qualifying_listen_seconds(row):
    try:
        started = int(row.get("started", 0))
        stopped = int(row.get("stopped", 0))
        paused = int(row.get("paused_counter", 0))
        if started and stopped and stopped > started:
            return max(0, (stopped - started) - paused)
    except (TypeError, ValueError):
        pass
    return None


def is_qualifying_play(row):
    listened = qualifying_listen_seconds(row)
    if listened is not None:
        return listened >= MIN_LISTEN_SECONDS
    return int(row.get("watched_status", 0)) == 1


def track_title(row):
    return f"{row.get('grandparent_title', '')} - {row.get('title', '')}"


def score_recent(rows, now_ts, half_life_days):
    scores = defaultdict(float)
    play_counts = defaultdict(int)
    titles = {}

    for row in rows:
        if not is_qualifying_play(row):
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
        if not is_qualifying_play(row):
            continue
        rk = row.get("rating_key")
        if rk is None:
            continue
        counts[rk] += 1
        titles[rk] = track_title(row)
    return counts, titles


def collect_boost_keys(plex):
    boosted = set()
    try:
        for pl in plex.playlists():
            if pl.title == PLAYLIST_TITLE:
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
                    alltime_counts, alltime_titles, plex):
    titles = {**alltime_titles, **recent_titles}
    boosted_keys = collect_boost_keys(plex)

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

    existing = next((p for p in plex.playlists() if p.title == PLAYLIST_TITLE), None)
    if existing:
        existing.removeItems(existing.items())
        existing.addItems(tracks)
    else:
        plex.createPlaylist(PLAYLIST_TITLE, items=tracks)

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

    recent_rows = fetch_raw_history(DAYS, MUSIC_SECTION_ID, USER_ID)
    recent_scores, recent_plays, recent_titles = score_recent(recent_rows, now_ts, HALF_LIFE_DAYS)

    alltime_rows = fetch_raw_history(ALLTIME_DAYS, MUSIC_SECTION_ID, USER_ID)
    alltime_counts, alltime_titles = count_alltime(alltime_rows)

    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    build_playlist(recent_scores, recent_plays, recent_titles,
                    alltime_counts, alltime_titles, plex)


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
