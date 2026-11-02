"""Last.fm artist data: community genre tags and similar artists, cached on
disk. Optional — everything works without a key, just with Plex data only.

  - artist.getTopTags → {tag: weight 0–100}, filtered to genre-like tags
    (no "seen live", "favorites", years, decades, nationalities…). Used to
    name clusters (clustering.genre_first_names), to make Relational
    grouping genre-aware (clustering.build_relational_graph) and as an
    extra tag source in album fit scoring (cluster_registry._Scorer).
  - artist.getSimilar → {similar artist (lower-cased name): match 0–1}. Adds
    similarity links Plex doesn't have.

Artists are looked up by name with Last.fm's autocorrect. Each artist costs
two calls; Last.fm asks for at most 5 requests per second, so calls are
spaced at least LASTFM_MIN_INTERVAL apart. Entries are refreshed after
LASTFM_MAX_AGE_DAYS. The key comes from LASTFM_API_KEY (background jobs) or
is passed in (the sidebar field).
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger("lastfm")

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_CACHE_PATH = os.environ.get("LASTFM_CACHE_PATH", "/app/data/lastfm_artists.json")
LASTFM_MAX_AGE_DAYS = 30
LASTFM_MIN_INTERVAL = 0.22          # seconds between calls (< 5 per second)
LASTFM_TIMEOUT = 15                 # seconds per request
LASTFM_RETRIES = 3
LASTFM_MIN_TAG_WEIGHT = 10          # Last.fm tag weights run 0–100 (relative to the artist's top tag)
LASTFM_MAX_TAGS = 15
LASTFM_SIMILAR_LIMIT = 50
LASTFM_SAVE_EVERY = 25              # artists fetched between cache checkpoints

ERROR_NOT_FOUND = 6
ERROR_RATE_LIMITED = 29

_NON_GENRE_TAGS = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "favorite artists", "favourite artists",
    "my favorite", "albums i own", "love", "loved", "awesome", "amazing", "beautiful", "cool", "good",
    "great", "best", "epic", "fun", "sexy", "female vocalists", "male vocalists", "female vocalist",
    "male vocalist", "female fronted", "singer-songwriter-ish", "spotify", "under 2000 listeners",
    "all", "music", "other", "misc", "various", "check out", "to listen", "wishlist", "live", "cover",
    "covers", "soundtrack-ish", "british", "american", "german", "finnish", "swedish", "norwegian",
    "danish", "icelandic", "french", "spanish", "catalan", "basque", "italian", "irish", "scottish",
    "english", "welsh", "canadian", "australian", "japanese", "russian", "polish", "dutch", "swiss",
    "austrian", "belgian", "brazilian", "mexican", "argentinian", "turkish", "greek", "portuguese",
    "czech", "ukrainian", "hungarian", "nigerian", "malian", "african", "uk", "usa", "us", "germany",
    "finland", "sweden", "norway", "france", "spain", "italy", "ireland", "canada", "australia", "japan",
    "deutsch", "german lyrics", "english lyrics",
}
_NON_GENRE_PATTERNS = [
    re.compile(r"^\d{4}s?$"),            # 1999, 2010s
    re.compile(r"^\d0s$"),               # 80s, 90s
    re.compile(r"^(19|20)\d\d\b"),       # "2012 albums"
    re.compile(r"\bseen\b|\bfavou?rite|\bmy\b|\bi \w+"),
]

_lock = threading.Lock()
_last_call = [0.0]


def available(api_key=None):
    return bool(api_key or os.environ.get("LASTFM_API_KEY"))


def _key(api_key=None):
    return api_key or os.environ.get("LASTFM_API_KEY")


def is_genre_tag(tag):
    t = tag.strip().lower()
    if not t or t in _NON_GENRE_TAGS:
        return False
    return not any(p.search(t) for p in _NON_GENRE_PATTERNS)


def normalize_name(name):
    return (name or "").strip().lower()


# --- HTTP -------------------------------------------------------------------

def _http_get(params):
    """One GET to the Last.fm API (separate so tests can replace it)."""
    resp = requests.get(LASTFM_API_URL, params=params, timeout=LASTFM_TIMEOUT)
    try:
        return resp.json()
    except ValueError:
        resp.raise_for_status()
        raise


def _call(method, api_key, **params):
    """Rate-limited call; returns the parsed JSON, or None if Last.fm doesn't
    know the artist. Retries timeouts and rate-limit errors."""
    params = {"method": method, "api_key": api_key, "format": "json", "autocorrect": 1, **params}
    for attempt in range(LASTFM_RETRIES + 1):
        with _lock:
            wait = LASTFM_MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        try:
            data = _http_get(params)
        except requests.exceptions.RequestException as e:
            if attempt < LASTFM_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Last.fm request failed: {e}") from e
        error = data.get("error") if isinstance(data, dict) else None
        if error is None:
            return data
        if error == ERROR_NOT_FOUND:
            return None
        if error == ERROR_RATE_LIMITED and attempt < LASTFM_RETRIES:
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"Last.fm error {error}: {data.get('message')}")
    return None


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_artist(name, api_key=None):
    """{"tags": {tag: weight 0–100}, "similar": {name: match 0–1}, "found": bool,
    "fetched_at": iso} for one artist."""
    key = _key(api_key)
    tags_json = _call("artist.gettoptags", key, artist=name)
    similar_json = _call("artist.getsimilar", key, artist=name, limit=LASTFM_SIMILAR_LIMIT)

    tags = {}
    for t in _as_list(((tags_json or {}).get("toptags") or {}).get("tag")):
        tag = normalize_name(t.get("name"))
        weight = _num(t.get("count"))
        if tag and weight >= LASTFM_MIN_TAG_WEIGHT and is_genre_tag(tag):
            tags[tag] = weight
    tags = dict(sorted(tags.items(), key=lambda kv: kv[1], reverse=True)[:LASTFM_MAX_TAGS])

    similar = {}
    for a in _as_list(((similar_json or {}).get("similarartists") or {}).get("artist")):
        other = normalize_name(a.get("name"))
        if other:
            similar[other] = round(_num(a.get("match")), 4)

    return {"tags": tags, "similar": similar, "found": tags_json is not None or similar_json is not None,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}


# --- Cache --------------------------------------------------------------------

def load_cache():
    try:
        with open(LASTFM_CACHE_PATH, "r") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("artists", {})
    return data


def save_cache(cache):
    os.makedirs(os.path.dirname(LASTFM_CACHE_PATH) or ".", exist_ok=True)
    tmp = LASTFM_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, LASTFM_CACHE_PATH)


def _stale(entry, max_age_days):
    try:
        return datetime.now() - datetime.fromisoformat(entry["fetched_at"]) > timedelta(days=max_age_days)
    except Exception:
        return True


def ensure_artists(artist_names, api_key=None, max_age_days=LASTFM_MAX_AGE_DAYS, debug=None):
    """Fetches Last.fm data for every artist not cached (or older than
    max_age_days) and returns the cache's artist dict {lower name: entry}.
    Without a key it just returns what's cached. A failing artist is logged
    and skipped; the run continues."""
    d = debug.write if debug else (lambda *a, **k: None)
    cache = load_cache()
    artists = cache["artists"]
    if not available(api_key):
        return artists
    todo = []
    seen = set()
    for name in artist_names:
        key = normalize_name(name)
        if key and key not in seen and (key not in artists or _stale(artists[key], max_age_days)):
            todo.append(name)
        seen.add(key)
    if not todo:
        return artists
    d(f"**Last.fm:** fetching tags + similar artists for {len(todo)} artist(s) "
      f"(~{len(todo) * 2 * LASTFM_MIN_INTERVAL / 60:.1f} min)…")
    failures = 0
    for i, name in enumerate(todo, 1):
        try:
            artists[normalize_name(name)] = fetch_artist(name, api_key)
        except Exception as e:
            failures += 1
            logger.warning(f"Last.fm lookup failed for {name!r}: {e}")
            if failures >= 10 and failures == i:
                d(f"⚠️ Last.fm isn't answering ({e}) — continuing without it.")
                break
        if i % LASTFM_SAVE_EVERY == 0:
            save_cache(cache)
            d(f"└ Last.fm: {i}/{len(todo)} artists done.")
    save_cache(cache)
    found = sum(1 for n in todo if artists.get(normalize_name(n), {}).get("found"))
    d(f"└ Last.fm: {found} of {len(todo)} artist(s) found{f', {failures} failed' if failures else ''}.")
    return artists


# --- Helpers for clustering -------------------------------------------------

def artist_tags(artists, name):
    return (artists.get(normalize_name(name)) or {}).get("tags") or {}


def artist_similar(artists, name):
    return (artists.get(normalize_name(name)) or {}).get("similar") or {}


def tag_similarity(tags_a, tags_b):
    """Cosine similarity of two {tag: weight} dicts, 0..1 (0 if either is empty)."""
    if not tags_a or not tags_b:
        return 0.0
    dot = sum(w * tags_b.get(t, 0.0) for t, w in tags_a.items())
    na = sum(w * w for w in tags_a.values()) ** 0.5
    nb = sum(w * w for w in tags_b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
