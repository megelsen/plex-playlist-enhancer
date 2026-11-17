"""Bulk library snapshot and small helpers shared by the playlist scripts.

Track dict fields and their units (converted here, once):
  views     Plex viewCount, lifetime plays counted at ~90% completion (int)
  skips     Plex skipCount (int)
  last      Plex lastViewedAt as unix seconds (0 = never)
  rating    Plex userRating, 0-10 (= 0-5 stars), None if unrated
  pop       Plex ratingCount = Last.fm listeners of the track (int), None if unknown
  duration  seconds (Plex reports milliseconds; divided by 1000 here)
"""

import math
import re
from collections import defaultdict

VERSION_WORDS = (r"live|demo|remaster\w*|remix|version|edit|mono|stereo|acoustic|"
                 r"bonus|instrumental|deluxe|mix|single|take|session")
BRACKET_RE = re.compile(rf"\s*[\(\[][^)\]]*\b({VERSION_WORDS})\b[^)\]]*[\)\]]", re.I)
DASH_RE = re.compile(rf"\s+-\s+.*\b({VERSION_WORDS})\b.*$", re.I)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def norm(text):
    return NON_ALNUM_RE.sub("", (text or "").lower())


def song_key(artist, title):
    """Same song across live/remaster/compilation copies -> same key."""
    title = BRACKET_RE.sub("", title or "")
    title = DASH_RE.sub("", title)
    return f"{norm(artist)}|{norm(title)}"


def to_ts(dt):
    """datetime -> unix seconds (0 for None)."""
    return dt.timestamp() if dt else 0


def normalize(d, flatten=False):
    """Scale values to 0..1 by the maximum (sqrt first when flatten)."""
    if not d:
        return {}
    top = max(d.values()) or 1
    return {k: (math.sqrt(v / top) if flatten else v / top) for k, v in d.items()}


def load_library(plex, section_id):
    """(tracks, albums, similar) for one music section, from three bulk
    searches (no per-item requests)."""
    section = plex.library.sectionByID(int(section_id))

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
            "duration": (t.duration or 0) / 1000,     # ms -> s
        }
    return tracks, albums, similar


def aggregate_songs(tracks):
    """Song-level view of the library: copies of one song (live, remaster,
    compilation) summed. {song_key: {"views", "skips", "last", "best",
    "copies"}} where best is the most-played copy (None if never played)."""
    songs = defaultdict(lambda: {"views": 0, "skips": 0, "last": 0, "best": None, "copies": []})
    for t in tracks.values():
        s = songs[t["song"]]
        s["views"] += t["views"]
        s["skips"] += t["skips"]
        s["last"] = max(s["last"], t["last"])
        s["copies"].append(t)
        if t["views"] and (s["best"] is None or t["views"] > s["best"]["views"]):
            s["best"] = t
    return songs


def artist_song_percentiles(tracks):
    """Within-artist popularity percentile (0..1) per (artist_key, song),
    from Plex ratingCount (Last.fm listeners). Songs without a count are
    left out."""
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
    return pct


def album_style_scores(albums, style):
    """0..1 fit of each album's style/genre tags to the taste style profile."""
    style_norm = normalize(style)
    album_style = {k: sum(style_norm.get(tag, 0) for tag in a["tags"]) / math.sqrt(len(a["tags"]))
                   for k, a in albums.items() if a["tags"]}
    return normalize(album_style)


def percentile(values, q):
    """q-th percentile (0..100) of a list, nearest-rank; None if empty."""
    values = sorted(values)
    if not values:
        return None
    idx = min(len(values) - 1, max(0, math.ceil(q / 100 * len(values)) - 1))
    return values[idx]
