"""Tautulli listening history, shared by the playlist scripts.

Units (Tautulli API): `date`, `started`, `stopped` are unix seconds;
`paused_counter` is seconds.

Two different notions of "a play" exist in these scripts — keep them apart:
  - plays_tautulli: a Tautulli history row that passes is_qualifying_play,
    i.e. >= MIN_LISTEN_SECONDS actually listened (default 30 s). Used for the
    taste profile and seeds.
  - views_plex: Plex's Track.viewCount, which Plex only bumps at ~90%
    completion of the track. Used for "did a pick get played" feedback and
    for lifetime play counts.
"""

import time

import requests


class Tautulli:
    def __init__(self, url, apikey, timeout=30):
        self.api = f"{url.rstrip('/')}/api/v2"
        self.apikey = apikey
        self.timeout = timeout

    def get(self, cmd, **params):
        params.update({"apikey": self.apikey, "cmd": cmd})
        r = requests.get(self.api, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("response", {}).get("result") != "success":
            raise RuntimeError(f"Tautulli API error on {cmd}: {data}")
        return data["response"]["data"]

    def history(self, section_id, user_id=None, after_ts=None, page_size=1000):
        """Track plays of a library section, newest first. With after_ts
        (unix seconds) only rows with date >= after_ts are returned; the API's
        `after` filter is day-granular (local date), so rows are filtered
        again on the exact timestamp."""
        rows = []
        start = 0
        while True:
            params = dict(section_id=section_id, media_type="track", grouping=0,
                          order_column="date", order_dir="desc",
                          start=start, length=page_size)
            if after_ts is not None:
                params["after"] = time.strftime("%Y-%m-%d", time.localtime(after_ts))
            if user_id:
                params["user_id"] = user_id
            page = self.get("get_history", **params).get("data", [])
            rows.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        if after_ts is not None:
            rows = [row for row in rows if int(row.get("date", 0)) >= after_ts]
        return rows


def listened_seconds(row):
    """Seconds actually listened (stopped - started - paused), or None when
    Tautulli has no usable timestamps for the row."""
    try:
        started = int(row.get("started", 0))
        stopped = int(row.get("stopped", 0))
        paused = int(row.get("paused_counter") or 0)
        if started and stopped and stopped > started:
            return max(0, (stopped - started) - paused)
    except (TypeError, ValueError):
        pass
    return None


def is_qualifying_play(row, min_listen_seconds):
    """A plays_tautulli play: >= min_listen_seconds listened, else (no
    timestamps) Tautulli's own watched flag."""
    listened = listened_seconds(row)
    if listened is not None:
        return listened >= min_listen_seconds
    return int(row.get("watched_status", 0)) == 1
