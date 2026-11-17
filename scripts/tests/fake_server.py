"""A fake Plex Media Server + Tautulli over real HTTP, so the playlist
scripts can be run end to end with the real plexapi and requests libraries
(e.g. in their docker containers) without a real server.

    python scripts/tests/fake_server.py [--port 32499] [--seed 7] [--taste-shift]

Plex endpoints (XML) and Tautulli's /api/v2?cmd=get_history (JSON) are
served on the same port; GET /debug/playlists returns the playlists as JSON.
The synthetic library is fake_plex.build_library around the current time.
Only the endpoints the scripts use are implemented.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import quoteattr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_plex  # noqa: E402

MACHINE_ID = "fake-machine-id"
LOCK = threading.RLock()


def _ts(dt):
    return str(int(dt.timestamp())) if dt else None


def _attrs(d):
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in d.items() if v is not None)


def track_xml(t, extra=None):
    a = {
        "ratingKey": t.ratingKey, "key": f"/library/metadata/{t.ratingKey}", "type": "track",
        "title": t.title, "grandparentTitle": t.grandparentTitle,
        "grandparentRatingKey": t.grandparentRatingKey, "parentRatingKey": t.parentRatingKey,
        "parentTitle": t.parentTitle, "viewCount": t.viewCount or None, "skipCount": t.skipCount or None,
        "lastViewedAt": _ts(t.lastViewedAt), "userRating": t.userRating, "ratingCount": t.ratingCount,
        "duration": t.duration, "librarySectionID": 1,
    }
    a.update(extra or {})
    return f"<Track {_attrs(a)} />"


def album_xml(al):
    a = {"ratingKey": al.ratingKey, "key": f"/library/metadata/{al.ratingKey}/children", "type": "album",
         "title": al.title, "parentTitle": al.artist.title, "parentRatingKey": al.artist.ratingKey,
         "addedAt": _ts(al.addedAt), "librarySectionID": 1}
    tags = "".join(f"<Genre tag={quoteattr(g.tag)} />" for g in al.genres)
    tags += "".join(f"<Style tag={quoteattr(s.tag)} />" for s in al.styles)
    return f"<Directory {_attrs(a)}>{tags}</Directory>"


def artist_xml(ar):
    a = {"ratingKey": ar.ratingKey, "key": f"/library/metadata/{ar.ratingKey}/children", "type": "artist",
         "title": ar.title, "librarySectionID": 1}
    tags = "".join(f"<Similar tag={quoteattr(s.tag)} />" for s in ar.similar)
    return f"<Directory {_attrs(a)}>{tags}</Directory>"


def playlist_xml(p):
    a = {"ratingKey": p.ratingKey, "key": f"/playlists/{p.ratingKey}/items", "type": "playlist",
         "title": p.title, "playlistType": "audio", "smart": 0, "leafCount": len(p._items),
         "summary": p.summary}
    return f"<Playlist {_attrs(a)} />"


def container(body, **attrs):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer {_attrs(attrs)}>{body}</MediaContainer>'


class Handler(BaseHTTPRequestHandler):
    plex = None
    history = None
    log_requests = False

    def log_message(self, fmt, *args):
        if self.log_requests:
            sys.stderr.write("fake: " + (fmt % args) + "\n")

    def _send(self, body, ctype="text/xml", code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _page(self, items, render, **attrs):
        q = self.query
        start = int(self.headers.get("X-Plex-Container-Start") or q.get("X-Plex-Container-Start", ["0"])[0])
        size = self.headers.get("X-Plex-Container-Size") or q.get("X-Plex-Container-Size", [None])[0]
        page = items[start:start + int(size)] if size is not None else items[start:]
        self._send(container("".join(render(i) for i in page), size=len(page), totalSize=len(items),
                             offset=start, **attrs))

    def _keys_from_uri(self):
        uri = self.query.get("uri", [""])[0]
        return [int(k) for k in uri.rsplit("/", 1)[-1].split(",") if k]

    def handle_any(self, method):
        url = urlparse(self.path)
        self.query = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        plex = self.plex
        with LOCK:
            if path == "/api/v2":
                return self.tautulli()
            if path == "/debug/playlists":
                return self._send(json.dumps({p.title: {"ratingKey": p.ratingKey, "summary": p.summary,
                                                        "items": [i.ratingKey for i in p._items]}
                                              for p in plex._playlists}), "application/json")
            if path == "/debug/play":
                # simulate listening: full=1 -> a completed play (Plex viewCount + Tautulli row), else a skip
                t = plex._tracks[int(self.query["rk"][0])]
                full = self.query.get("full", ["1"])[0] == "1"
                now = int(time.time()) - 60
                secs = t.duration // 1000 if full else 20
                self.history.insert(0, {"rating_key": t.ratingKey, "date": now, "started": now,
                                        "stopped": now + secs, "paused_counter": 0,
                                        "watched_status": 1 if full else 0, "title": t.title,
                                        "grandparent_title": t.grandparentTitle})
                if full:
                    t.viewCount = (t.viewCount or 0) + 1
                    t.lastViewedAt = datetime.fromtimestamp(now + secs)
                else:
                    t.skipCount = (t.skipCount or 0) + 1
                return self._send(json.dumps({"ok": True}), "application/json")
            if path == "/":
                return self._send(container("", machineIdentifier=MACHINE_ID, version="1.41.0.0",
                                            friendlyName="fake", myPlex=0))
            if path == "/library":
                return self._send(container('<Directory key="sections" title="Library Sections" />'))
            if path == "/library/sections":
                return self._send(container(
                    '<Directory key="1" type="artist" title="Music" agent="tv.plex.agents.music" '
                    'scanner="Plex Music" language="en" uuid="fake-section" />', size=1))
            if path == "/library/sections/1/all":
                kind = self.query.get("type", ["10"])[0]
                if kind == "8":
                    return self._page(list(plex._artists.values()), artist_xml)
                if kind == "9":
                    return self._page(list(plex._albums.values()), album_xml)
                return self._page(list(plex._tracks.values()), track_xml)
            parts = path.strip("/").split("/")
            if parts[:2] == ["library", "metadata"] and len(parts) >= 3:
                key = int(parts[2].split(",")[0])
                if len(parts) == 4 and parts[3] == "nearest":
                    t = plex._tracks.get(key)
                    if t is None:
                        return self._send("not found", code=404)
                    limit = int(self.query.get("limit", ["50"])[0])
                    max_d = float(self.query.get("maxDistance", ["0.25"])[0])
                    res = t.sonicallySimilar(limit=limit, maxDistance=max_d)
                    return self._send(container("".join(track_xml(r, {"distance": r.distance}) for r in res),
                                                size=len(res)))
                if key in plex._tracks:
                    return self._send(container(track_xml(plex._tracks[key]), size=1))
                pl = next((p for p in plex._playlists if p.ratingKey == key), None)
                if pl:
                    return self._send(container(playlist_xml(pl), size=1))
                return self._send("not found", code=404)
            if parts[0] == "playlists":
                return self.playlists(method, parts)
        self._send("not implemented", code=404)

    def playlists(self, method, parts):
        plex = self.plex
        if len(parts) == 1:
            if method == "POST":
                items = [plex._tracks[k] for k in self._keys_from_uri()]
                p = plex.createPlaylist(self.query.get("title", [""])[0], items=items)
                return self._send(container(playlist_xml(p), size=1))
            return self._send(container("".join(playlist_xml(p) for p in plex._playlists),
                                        size=len(plex._playlists)))
        pl = next((p for p in plex._playlists if p.ratingKey == int(parts[1])), None)
        if pl is None:
            return self._send("not found", code=404)
        if len(parts) == 2:
            if method == "PUT":
                if "summary.value" in self.query:
                    pl.summary = self.query["summary.value"][0]
                if "title.value" in self.query:
                    pl.title = self.query["title.value"][0]
                return self._send(container(""))
            if method == "DELETE":
                pl.delete()
                return self._send(container(""))
            return self._send(container(playlist_xml(pl), size=1))
        if parts[2] == "items":
            if len(parts) == 4 and method == "DELETE":
                idx = int(parts[3]) - 1
                if 0 <= idx < len(pl._items) and pl._items[idx] is not None:
                    pl._items[idx] = None          # keep the playlistItemIDs of the others stable
                return self._send(container(""))
            if method == "PUT":
                pl._items = [i for i in pl._items if i is not None]
                pl._items.extend(plex._tracks[k] for k in self._keys_from_uri())
                return self._send(container(playlist_xml(pl), size=1))
            pl._items = [i for i in pl._items if i is not None]
            return self._page(pl._items, lambda t: track_xml(t, {"playlistItemID": pl._items.index(t) + 1}))
        return self._send("not found", code=404)

    def tautulli(self):
        q = {k: v[0] for k, v in self.query.items()}
        if q.get("cmd") != "get_history":
            return self._send(json.dumps({"response": {"result": "error"}}), "application/json")
        rows = list(self.history)
        if q.get("after"):
            after = datetime.strptime(q["after"], "%Y-%m-%d").timestamp()
            rows = [r for r in rows if r["date"] >= after]
        start, length = int(q.get("start", 0)), int(q.get("length", 25))
        body = {"response": {"result": "success", "data": {"data": rows[start:start + length]}}}
        self._send(json.dumps(body), "application/json")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")


def serve(port, seed, taste_shift=False, log_requests=False):
    plex, history = fake_plex.build_library(seed=seed, now_ts=int(time.time()), taste_shift=taste_shift)
    Handler.plex, Handler.history, Handler.log_requests = plex, history, log_requests
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"fake Plex + Tautulli on :{port} — {len(plex._tracks)} tracks, {len(history)} plays", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=32499)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--taste-shift", action="store_true")
    ap.add_argument("--log", action="store_true")
    a = ap.parse_args()
    serve(a.port, a.seed, a.taste_shift, a.log)
