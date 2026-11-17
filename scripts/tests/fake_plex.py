"""A small, deterministic fake Plex server + Tautulli for testing the
playlist scripts without a real server.

install(lib) puts a fake `plexapi` package into sys.modules and patches
requests.get so Tautulli calls are answered from the synthetic history.

Units match the real APIs:
  - Plex Track.duration: milliseconds; lastViewedAt / addedAt: datetime;
    userRating: 0-10; ratingCount: Last.fm listeners (int);
    sonicallySimilar results carry .distance in 0..1 (0 = identical),
    server default maxDistance 0.25.
  - Tautulli history rows: date / started / stopped are unix seconds,
    paused_counter is seconds.
"""

import math
import random
import sys
import types
from datetime import datetime
from urllib.parse import urlparse

import requests


class Tag:
    def __init__(self, tag):
        self.tag = tag


class FakeTrack:
    TYPE = "track"

    def __init__(self, server, rk, title, artist, album, vec, duration_ms,
                 views=0, skips=0, last=None, rating=None, pop=None):
        self._server = server
        self.ratingKey = rk
        self.title = title
        self.originalTitle = None
        self.grandparentTitle = artist.title
        self.grandparentRatingKey = artist.ratingKey
        self.parentRatingKey = album.ratingKey
        self.parentTitle = album.title
        self.vec = vec
        self.duration = duration_ms
        self.viewCount = views
        self.skipCount = skips
        self.lastViewedAt = last
        self.userRating = rating
        self.ratingCount = pop
        self.distance = None

    def sonicallySimilar(self, limit=None, maxDistance=None, **kwargs):
        max_d = 0.25 if maxDistance is None else maxDistance
        out = []
        for t in self._server._tracks.values():
            if t.ratingKey == self.ratingKey:
                continue
            d = self._server.distance(self, t)
            if d <= max_d:
                out.append((d, t.ratingKey))
        out.sort()
        if limit is None:
            limit = 50
        res = []
        for d, rk in out[:limit]:
            src = self._server._tracks[rk]
            copy = FakeTrack.__new__(FakeTrack)
            copy.__dict__.update(src.__dict__)
            copy.distance = round(d, 4)
            res.append(copy)
        return res


class FakeAlbum:
    def __init__(self, rk, title, artist, styles, genres, added):
        self.ratingKey = rk
        self.title = title
        self.artist = artist
        self.styles = [Tag(s) for s in styles]
        self.genres = [Tag(g) for g in genres]
        self.addedAt = added


class FakeArtist:
    def __init__(self, rk, title):
        self.ratingKey = rk
        self.title = title
        self.similar = []


class FakePlaylist:
    TYPE = "playlist"
    playlistType = "audio"

    def __init__(self, server, rk, title, items):
        self._server = server
        self.ratingKey = rk
        self.title = title
        self._items = list(items)
        self.summary = ""

    def items(self):
        return list(self._items)

    def removeItems(self, items):
        keys = {i.ratingKey for i in items}
        self._items = [i for i in self._items if i.ratingKey not in keys]

    def addItems(self, items):
        self._items.extend(items)

    def editSummary(self, summary):
        self.summary = summary

    def delete(self):
        self._server._playlists = [p for p in self._server._playlists if p is not self]


class FakeSection:
    def __init__(self, server):
        self._server = server

    def search(self, libtype=None, maxresults=None, **kwargs):
        s = self._server
        if libtype == "track":
            return list(s._tracks.values())
        if libtype == "album":
            return list(s._albums.values())
        if libtype == "artist":
            return list(s._artists.values())
        return []


class FakeLibrary:
    def __init__(self, server):
        self._section = FakeSection(server)

    def sectionByID(self, section_id):
        return self._section


class FakePlex:
    def __init__(self):
        self._tracks, self._albums, self._artists = {}, {}, {}
        self._playlists = []
        self._next_key = 900000
        self.library = FakeLibrary(self)
        self.calls = {"sonic": 0, "fetchItem": 0}

    def distance(self, a, b):
        d = math.sqrt(sum((x - y) ** 2 for x, y in zip(a.vec, b.vec)))
        return min(1.0, d)

    def fetchItem(self, key):
        self.calls["fetchItem"] += 1
        key = int(key)
        if key in self._tracks:
            return self._tracks[key]
        for p in self._playlists:
            if p.ratingKey == key:
                return p
        raise LookupError(f"no item {key}")

    def playlists(self):
        return list(self._playlists)

    def createPlaylist(self, title, items=None, **kwargs):
        self._next_key += 1
        p = FakePlaylist(self, self._next_key, title, items or [])
        self._playlists.append(p)
        return p


def build_library(seed=7, now_ts=1_763_000_000, n_artists=40):
    """A synthetic library: artists in 5 style 'scenes' with sonic vectors
    near their scene centre, a few similar-artist links, play counts and a
    matching Tautulli history. Returns (plex, history_rows)."""
    rng = random.Random(seed)
    plex = FakePlex()
    scenes = [("doom metal", "metal"), ("post-punk", "rock"), ("dub", "reggae"),
              ("synthpop", "electronic"), ("folk", "folk")]
    centres = [[rng.uniform(0, 2) for _ in range(4)] for _ in scenes]
    history = []
    tkey, akey, arkey = 1000, 500, 100
    for i in range(n_artists):
        scene = i % len(scenes)
        artist = FakeArtist(arkey, f"Artist {i:02d}")
        arkey += 1
        plex._artists[artist.ratingKey] = artist
        fan_level = rng.random() ** 2          # some artists heavily played, most not
        for j in range(rng.randint(1, 3)):
            added = datetime.fromtimestamp(now_ts - rng.randint(5, 900) * 86400)
            album = FakeAlbum(akey, f"Album {i:02d}-{j}", artist,
                              [scenes[scene][0]], [scenes[scene][1]], added)
            akey += 1
            plex._albums[album.ratingKey] = album
            album_played = rng.random() < 0.6
            for k in range(rng.randint(6, 11)):
                vec = [c + rng.gauss(0, 0.12) for c in centres[scene]]
                title = f"Song {i:02d}-{j}-{k}"
                if k == 3 and j > 0:
                    title = f"Song {i:02d}-0-{k} (Live)"   # a duplicate version
                pop = rng.randint(0, 60000) if rng.random() < 0.85 else None
                dur = rng.choice([45_000, 200_000, 240_000, 300_000])
                t = FakeTrack(plex, tkey, title, artist, album, vec, dur, pop=pop)
                tkey += 1
                plex._tracks[t.ratingKey] = t
                plays = 0
                if album_played and rng.random() < 0.5 + fan_level / 2:
                    plays = int(rng.expovariate(1 / (1 + 25 * fan_level)))
                pre_tautulli = rng.randint(0, 2) if plays else 0
                last = None
                for _ in range(plays):
                    ts = now_ts - int(rng.expovariate(1 / 120) * 86400) - rng.randint(0, 86400)
                    listened = rng.choice([dur // 1000, dur // 1000, 20])
                    history.append({
                        "rating_key": t.ratingKey, "date": ts, "started": ts,
                        "stopped": ts + listened, "paused_counter": 0,
                        "watched_status": 1 if listened > 30 else 0,
                        "title": t.title, "grandparent_title": artist.title,
                    })
                    last = max(last or 0, ts + listened)
                completed = sum(1 for h in history if h["rating_key"] == t.ratingKey
                                and h["stopped"] - h["started"] >= 0.9 * dur / 1000)
                t.viewCount = completed + pre_tautulli
                t.skipCount = rng.randint(0, 3) if rng.random() < 0.2 else 0
                t.lastViewedAt = datetime.fromtimestamp(last) if last else None
                if rng.random() < 0.05:
                    t.userRating = rng.choice([2.0, 4.0, 8.0, 10.0])
    arts = list(plex._artists.values())
    for a in arts:
        same = [b for b in arts if b is not a and int(b.title[-2:]) % 5 == int(a.title[-2:]) % 5]
        a.similar = [Tag(b.title) for b in rng.sample(same, min(3, len(same)))]
    history.sort(key=lambda h: h["date"], reverse=True)
    return plex, history


_INSTALLED = {}


def install(plex, history):
    """Fake plexapi.server.PlexServer -> `plex`; Tautulli get_history -> `history`."""
    plexapi = types.ModuleType("plexapi")
    server_mod = types.ModuleType("plexapi.server")
    server_mod.PlexServer = lambda url, token, *a, **k: plex
    plexapi.server = server_mod
    sys.modules["plexapi"] = plexapi
    sys.modules["plexapi.server"] = server_mod

    class Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None, **kwargs):
        params = dict(params or {})
        if params.get("cmd") != "get_history":
            return Resp({"response": {"result": "error", "message": "unsupported"}})
        rows = list(history)
        if params.get("after"):
            after = datetime.strptime(params["after"], "%Y-%m-%d").timestamp()
            rows = [r for r in rows if r["date"] >= after]
        start = int(params.get("start", 0))
        length = int(params.get("length", 25))
        return Resp({"response": {"result": "success",
                                  "data": {"data": rows[start:start + length]}}})

    if "requests_get" not in _INSTALLED:
        _INSTALLED["requests_get"] = requests.get
    requests.get = fake_get
    return plex
