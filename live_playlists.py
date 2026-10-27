"""Live Playlists: daily-refreshing Artist Mixes and Cluster Mixes.

Two subscription types, persisted to disk (LIVE_PLAYLISTS_PATH) so they
survive container restarts:

  - "artist_mix": re-runs build_artist_mix() with the same settings every
    day. Different songs come out simply because the builder already
    shuffles internally — no special rotation logic needed here.

  - "cluster_mix": re-generating from scratch (re-running Gemini/Louvain)
    would let the vibe drift day to day, so instead this draws from the
    cluster's CURRENT album membership in the album cluster registry
    (cluster_registry.py) — every track of every album in the cluster.
    The playlist is composed of two rotating parts:
      - ~cluster_ratio (default 67%) drawn from that whole-cluster pool.
      - the remainder expanded via Plex's sonic-similarity hub, seeded
        from `sonic_seed_count` (default 10) tracks drawn from the
        cluster-seeded part just picked that run.
    Each part rotates independently: `rotation_percent` (default 20%) of
    its tracks — the ones that have been in the mix longest first — get
    swapped each day for new picks from its own pool, with a one-rotation
    cooldown per part. New picks use the same picker as static cluster
    playlists (_pick_tracks: popular/random split, per-artist cap across
    the whole mix). The day's tracks are then put in DJ order (dj_order):
    a random opener, then always the most similar-sounding next track.

The daily job itself runs via APScheduler's BackgroundScheduler, started
once per container process (guarded by a module-level singleton) so it
keeps firing regardless of whether a browser tab is open. It authenticates
using the PLEX_URL / PLEX_TOKEN environment variables (NOT whatever is
typed into the sidebar in any given browser session), since a 4am job has
no session to read from.

Cluster mixes follow the album-level cluster registry (cluster_registry.py)
by stable `cluster_id` once one exists, so a weekly re-curation that
renames, merges or splits a cluster doesn't break them. A mix whose
cluster was dissolved (or merged into a cluster that already has its own
mix) is marked `stale` — see apply_cluster_events — and its Plex playlist
is deleted at the next weekly curation (delete_stale_subscriptions), only
ever by the playlist ratingKey this module created it under.

Both jobs (daily refresh, weekly curation) run on SCHEDULER_TZ
(default Europe/Paris) wall-clock time.
"""

import json
import logging
import math
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger("live_playlists")

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer

import cluster_registry as cr
from artist_mix import build_artist_mix
from plex_helpers import get_sonic_match_percent
from clustering import (
    CLUSTER_RESULTS_CACHE_PATH, apply_merge_plan, build_artist_cluster_map, load_build_cache,
    load_cluster_results_cache,
)

LIVE_PLAYLISTS_PATH = os.environ.get("LIVE_PLAYLISTS_PATH", "/app/data/live_playlists.json")

# When the daily job fires. Configurable via env var rather than hardcoded
# so this doesn't need a code change if you want a different hour later.
LIVE_PLAYLISTS_HOUR = int(os.environ.get("LIVE_PLAYLISTS_HOUR", 4))
LIVE_PLAYLISTS_MINUTE = int(os.environ.get("LIVE_PLAYLISTS_MINUTE", 0))

# Weekly cluster curation (curation.py). Same timezone as the daily job, and
# deliberately an hour earlier so the 4am refresh already uses new clusters.
SCHEDULER_TZ = os.environ.get("SCHEDULER_TZ", "Europe/Paris")
CURATION_DAY_OF_WEEK = os.environ.get("CURATION_DAY_OF_WEEK", "mon")
CURATION_HOUR = int(os.environ.get("CURATION_HOUR", 3))
CURATION_MINUTE = int(os.environ.get("CURATION_MINUTE", 0))
CURATION_CATCH_UP_DAYS = 8

# Cluster Mix / cluster playlist defaults (percent = share of the playlist).
DEFAULT_ROTATION_PERCENT = 20
DEFAULT_MAX_ARTIST_SHARE = 0.10
DEFAULT_POPULAR_SHARE = 0.5
DJ_NEIGHBOR_LIMIT = 50

# Fields a run_*_subscription call may change on `sub`, persisted by the runners.
_RUN_STATE_FIELDS = (
    "cluster_id", "cluster_name", "playlist_name", "playlist_rating_key",
    "current_cluster_keys", "recent_retired_cluster_keys",
    "current_sonic_keys", "recent_retired_sonic_keys",
    "track_view_counts", "fit_stats", "baseline_album_keys", "drift", "track_ages",
)


class PlaylistDeletedError(Exception):
    """The playlist this subscription created no longer exists in Plex."""


class StaleMixError(Exception):
    """The subscription's cluster no longer exists; it awaits weekly cleanup."""

_scheduler = None
_scheduler_lock = threading.Lock()


# --- Persistence -----------------------------------------------------------

def _default_config():
    return {"artist_mixes": [], "cluster_mixes": []}


def _load_config():
    try:
        with open(LIVE_PLAYLISTS_PATH, "r") as f:
            cfg = json.load(f)
        cfg.setdefault("artist_mixes", [])
        cfg.setdefault("cluster_mixes", [])
        return cfg
    except Exception:
        return _default_config()


def _save_config(cfg):
    try:
        os.makedirs(os.path.dirname(LIVE_PLAYLISTS_PATH), exist_ok=True)
        with open(LIVE_PLAYLISTS_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _new_id():
    return f"live_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"


# --- CRUD --------------------------------------------------------------

def list_artist_mix_subscriptions():
    return _load_config()["artist_mixes"]


def list_cluster_mix_subscriptions():
    return _load_config()["cluster_mixes"]


def add_artist_mix_subscription(artist_name, playlist_name, max_total=30, max_artist=10,
                                 max_related=2, max_sonic=2):
    cfg = _load_config()
    cfg["artist_mixes"].append({
        "id": _new_id(),
        "artist_name": artist_name,
        "playlist_name": playlist_name,
        "max_total": int(max_total),
        "max_artist": int(max_artist),
        "max_related": int(max_related),
        "max_sonic": int(max_sonic),
        "enabled": True,
        "last_run": None,
        "last_track_count": None,
        "last_error": None,
    })
    _save_config(cfg)


def add_cluster_mix_subscription(cluster_name, playlist_name, target_size=30,
                                  rotation_percent=DEFAULT_ROTATION_PERCENT, cluster_ratio=0.67,
                                  sonic_seed_count=10, cluster_id=None,
                                  max_artist_share=DEFAULT_MAX_ARTIST_SHARE,
                                  popular_share=DEFAULT_POPULAR_SHARE, dj_order=True):
    cfg = _load_config()
    cfg["cluster_mixes"].append({
        "id": _new_id(),
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "playlist_name": playlist_name,
        "target_size": int(target_size),
        "rotation_percent": int(rotation_percent),
        "cluster_ratio": float(cluster_ratio),
        "sonic_seed_count": int(sonic_seed_count),
        "max_artist_share": float(max_artist_share),
        "popular_share": float(popular_share),
        "dj_order": bool(dj_order),
        "current_cluster_keys": [],
        "recent_retired_cluster_keys": [],
        "current_sonic_keys": [],
        "recent_retired_sonic_keys": [],
        "playlist_rating_key": None,
        "track_view_counts": {},
        "track_ages": {},
        "fit_stats": {},
        "stale": None,
        "enabled": True,
        "last_run": None,
        "last_track_count": None,
        "last_error": None,
    })
    _save_config(cfg)


def remove_subscription(kind, sub_id):
    cfg = _load_config()
    key = "artist_mixes" if kind == "artist_mix" else "cluster_mixes"
    cfg[key] = [s for s in cfg[key] if s["id"] != sub_id]
    _save_config(cfg)


def set_subscription_enabled(kind, sub_id, enabled):
    cfg = _load_config()
    key = "artist_mixes" if kind == "artist_mix" else "cluster_mixes"
    for s in cfg[key]:
        if s["id"] == sub_id:
            s["enabled"] = enabled
            # Switched back on after its playlist was deleted in Plex:
            # forget the old ratingKey so the next run creates a fresh one.
            if enabled and s.pop("playlist_missing", False):
                s["playlist_rating_key"] = None
    _save_config(cfg)


def update_subscription_settings(kind, sub_id, **fields):
    """Edits one existing subscription's tunable settings in place (target
    size, rotation %, cluster/sonic ratio for cluster mixes; max_total etc.
    for artist mixes) without touching its id, current rotation-tracking
    keys, or run history. Takes effect on the NEXT run (scheduled or
    manual) — it doesn't retroactively rebuild the playlist. Only the keys
    passed in `fields` are changed, so callers can pass just what they
    edited."""
    cfg = _load_config()
    key = "artist_mixes" if kind == "artist_mix" else "cluster_mixes"
    for s in cfg[key]:
        if s["id"] == sub_id:
            s.update(fields)
            _save_config(cfg)
            return True
    return False  # subscription no longer exists


# --- Playlist upsert -----------------------------------------------------

def _rename_playlist(playlist, title):
    try:
        playlist.editTitle(title)
    except Exception:
        logger.exception(f"Couldn't rename playlist '{playlist.title}' to '{title}'")


def _upsert_playlist(plex, title, tracks, rating_key=None):
    """Replaces the playlist's contents (creating it if needed) and returns
    the playlist. With `rating_key`, the playlist is addressed by the key it
    was created under (and renamed if `title` changed); raises
    PlaylistDeletedError if that playlist no longer exists. Without one,
    falls back to the legacy match by title."""
    if not tracks:
        return None
    existing = None
    if rating_key:
        try:
            existing = plex.fetchItem(int(rating_key))
        except NotFound:
            raise PlaylistDeletedError(title)
    else:
        existing = next((p for p in plex.playlists() if p.title == title), None)
    if existing:
        if existing.title != title:
            _rename_playlist(existing, title)
        existing.removeItems(existing.items())
        existing.addItems(tracks)
        return existing
    return plex.createPlaylist(title=title, items=tracks)


# Public alias — used by mix_tab.py / clusters_tab.py to instantly save a
# Live Mix's first build without waiting for the next scheduled run.
def upsert_playlist(plex, title, tracks):
    _upsert_playlist(plex, title, tracks)


# --- Single shared "save as live mix" entry points -------------------------
#
# clusters_tab.py, live_tab.py, and any future caller all go through these
# two functions instead of each re-implementing "add_*_subscription + run it
# once immediately" themselves. One code path for creating a live mix, so
# both tabs' UIs stay in sync automatically.

def save_artist_mix_as_live(plex, plex_url, plex_token, artist_name, playlist_name,
                             max_total=30, max_artist=10, max_related=2, max_sonic=2,
                             debug=None):
    """Registers a new artist-mix subscription and runs it once immediately
    so the playlist is populated right away instead of waiting for the next
    scheduled run. Returns (playlist_name, track_count)."""
    add_artist_mix_subscription(
        artist_name, playlist_name,
        max_total=max_total, max_artist=max_artist, max_related=max_related, max_sonic=max_sonic,
    )
    new_sub = next(
        (s for s in list_artist_mix_subscriptions()
         if s["artist_name"] == artist_name and s["playlist_name"] == playlist_name),
        None
    )
    count = None
    if new_sub:
        count = run_single_subscription("artist_mix", new_sub["id"], plex_url, plex_token, debug=debug)
    return playlist_name, count


def save_cluster_as_live_mix(plex, plex_url, plex_token, cluster_name, playlist_name,
                              target_size=30, rotation_percent=DEFAULT_ROTATION_PERCENT, cluster_ratio=0.67,
                              sonic_seed_count=10, cluster_id=None,
                              max_artist_share=DEFAULT_MAX_ARTIST_SHARE, popular_share=DEFAULT_POPULAR_SHARE,
                              dj_order=True, debug=None):
    """Registers a new cluster-mix subscription and runs it once immediately.
    `cluster_id` (a registry cluster) is optional — without it the cluster
    is resolved by name on each run. Returns (playlist_name, track_count)."""
    add_cluster_mix_subscription(
        cluster_name, playlist_name,
        target_size=target_size, rotation_percent=rotation_percent,
        cluster_ratio=cluster_ratio, sonic_seed_count=sonic_seed_count,
        cluster_id=cluster_id, max_artist_share=max_artist_share,
        popular_share=popular_share, dj_order=dj_order,
    )
    new_sub = next(
        (s for s in list_cluster_mix_subscriptions()
         if s["cluster_name"] == cluster_name and s["playlist_name"] == playlist_name),
        None
    )
    count = None
    if new_sub:
        count = run_single_subscription("cluster_mix", new_sub["id"], plex_url, plex_token, debug=debug)
    return playlist_name, count


def list_all_subscriptions():
    """Combined view for the Live Playlists overview: every subscription,
    tagged with its kind, so the tab can render one unified list instead of
    two separate ones."""
    cfg = _load_config()
    out = []
    for s in cfg["artist_mixes"]:
        out.append({**s, "kind": "artist_mix"})
    for s in cfg["cluster_mixes"]:
        out.append({**s, "kind": "cluster_mix"})
    return out


# --- Artist Mix rotation ----------------------------------------------

def run_artist_mix_subscription(sub, plex, debug=None):
    """Re-runs build_artist_mix with the subscription's saved settings and
    upserts the target playlist. Raises on failure (caller records
    last_error) rather than swallowing it, so problems are visible."""
    music_section = next(
        (s for s in plex.library.sections() if s.type in ("artist", "music")), None
    )
    if music_section is None:
        raise ValueError("No music library section found on this Plex server.")

    matches = music_section.searchArtists(title=sub["artist_name"])
    if not matches:
        raise ValueError(f"Artist '{sub['artist_name']}' not found in the music library.")
    artist = next((a for a in matches if a.title == sub["artist_name"]), matches[0])

    tracks = build_artist_mix(
        artist, plex,
        max_total=sub["max_total"], max_artist=sub["max_artist"],
        max_related=sub["max_related"], max_sonic=sub["max_sonic"],
        debug=debug,
    )
    if not tracks:
        raise ValueError(f"build_artist_mix returned zero tracks for '{sub['artist_name']}'.")

    playlist = _upsert_playlist(plex, sub["playlist_name"], tracks, sub.get("playlist_rating_key"))
    if playlist is not None:
        sub["playlist_rating_key"] = playlist.ratingKey
    return len(tracks)


# --- Cluster Mix rotation ----------------------------------------------

def _album_tracks_pool(plex, album_keys):
    pool = {}
    for ak in album_keys:
        try:
            for t in plex.fetchItem(int(ak)).tracks():
                pool[t.ratingKey] = t
        except Exception:
            continue
    return pool


def _gather_cluster_pool(plex, cluster_name, debug=None):
    """
    Fallback for mixes on a build that was never committed to the album
    cluster registry: every track of the cluster's albums in the last saved
    build (v2), or — for a legacy v1 cache — the full catalogs of the
    artists in the old track sample. Returns {ratingKey: track}. Raises
    ValueError if the cluster can't be found.
    """
    build, merge_plan = load_build_cache()
    if build:
        build = apply_merge_plan(build, merge_plan)
        if cluster_name not in build["clusters"]:
            raise ValueError(
                f"Cluster '{cluster_name}' not found in the last saved cluster build — "
                "open Library Clusters, build and commit clusters first."
            )
        pool = _album_tracks_pool(plex, build["clusters"][cluster_name])
        if not pool:
            raise ValueError(f"Resolved zero tracks for cluster '{cluster_name}'.")
        return pool

    results, _tag_mapping, _saved = load_cluster_results_cache(plex, debug=debug)
    if not results or cluster_name not in results:
        raise ValueError(
            f"Cluster '{cluster_name}' not found in the last saved cluster build — "
            "open Library Clusters and build/save clusters first."
        )

    artist_map = build_artist_cluster_map(results)
    artist_keys = [k for k, v in artist_map.items() if v == cluster_name]
    if not artist_keys:
        raise ValueError(f"No artists resolved for cluster '{cluster_name}'.")

    pool = {}
    for ak in artist_keys:
        try:
            artist_obj = plex.fetchItem(ak)
            for t in artist_obj.tracks():
                pool[t.ratingKey] = t
        except Exception:
            continue

    if not pool:
        raise ValueError(f"Resolved zero tracks for cluster '{cluster_name}''s artists.")
    return pool


def _registry_cluster_pool(plex, registry, cluster_id):
    """Every track of every album the registry currently assigns to
    `cluster_id`. Returns ({ratingKey: track}, [album keys])."""
    album_keys = cr.cluster_album_keys(registry, cluster_id)
    pool = _album_tracks_pool(plex, album_keys)
    if not pool:
        raise ValueError(f"Resolved zero tracks for cluster '{registry['clusters'][cluster_id]['name']}'.")
    return pool, album_keys


def _artist_cap(size, max_artist_share):
    """Max tracks per artist for a playlist of `size` (None = no cap)."""
    return max(1, math.ceil(size * max_artist_share)) if max_artist_share else None


def _pick_tracks(candidates, n, popular_share, cap=None, artist_counts=None, label=None):
    """Picks up to n tracks from `candidates`, the same way for static
    cluster playlists and Live Mixes:
      - round(n * popular_share) by weighted random sampling without
        replacement, weight = plays + 1 (viewCount; +1 so unplayed tracks
        keep a chance) — Efraimidis–Spirakis: order by u ** (1 / weight);
      - the rest uniformly at random from what's left;
      - never more than `cap` tracks per artist, counted in `artist_counts`
        (shared by the caller across parts, updated in place); tracks with
        no artist key aren't capped.
    Returns the picks (fewer than n only if the cap or pool runs out)."""
    artist_counts = artist_counts if artist_counts is not None else defaultdict(int)
    candidates = list(candidates)
    picked, picked_keys = [], set()

    def _take(ordered, target, kind):
        for t in ordered:
            if len(picked) >= target:
                return
            artist = getattr(t, "grandparentRatingKey", None)
            if t.ratingKey in picked_keys or (cap and artist is not None and artist_counts[artist] >= cap):
                continue
            if label:
                setattr(t, "recommendation_type", f"{label} ({kind})")
            picked.append(t)
            picked_keys.add(t.ratingKey)
            artist_counts[artist] += 1

    weighted = sorted(candidates, key=lambda t: random.random() ** (1.0 / ((getattr(t, "viewCount", 0) or 0) + 1)),
                      reverse=True)
    _take(weighted, round(n * popular_share), "Popular")
    rest = [t for t in candidates if t.ratingKey not in picked_keys]
    random.shuffle(rest)
    _take(rest, n, "Discovery")
    return picked


def _album_similarity(fa, fb, album_a, album_b):
    """0..1 similarity of two albums from the cached album features: their
    sonic-neighbour weight (Plex match % / 100), else tag overlap (Jaccard)
    scaled by 0.5 so a tag match never beats a real sonic match."""
    if not fa or not fb:
        return 0.0
    sonic = max((fa.get("sonic") or {}).get(album_b, 0.0), (fb.get("sonic") or {}).get(album_a, 0.0))
    ta = set(fa.get("genres", [])) | set(fa.get("moods", []))
    tb = set(fb.get("genres", [])) | set(fb.get("moods", []))
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(sonic, 0.5 * jaccard)


def dj_order(tracks, start=None, debug=None):
    """DJ-style running order: a random opener (or `start`), then always the
    remaining track that sounds most like the one before it.

    Transition score (higher = smoother), all 0..1-based:
      1 + Plex sonic match (match % / 100) if one track is in the other's
          sonicallySimilar list (DJ_NEIGHBOR_LIMIT neighbours each),
      else album-level similarity from the cached album features (sonic
          neighbour albums, else tag overlap × 0.5), 0.6 for the same album;
      − 0.5 if it's the same artist as the previous track (avoids
          back-to-back artists unless nothing else fits),
      + a tiny random jitter so ties don't always resolve the same way.
    Costs one sonicallySimilar call per track."""
    tracks = list(tracks)
    if len(tracks) < 3:
        return tracks
    keys = {t.ratingKey for t in tracks}
    direct = defaultdict(dict)
    for t in tracks:
        try:
            matches = t.sonicallySimilar(limit=DJ_NEIGHBOR_LIMIT)
        except Exception as e:
            if debug:
                debug.write(f"└ ⚠️ Sonic lookup failed while ordering: `{e}`")
            continue
        for m in matches:
            mk = getattr(m, "ratingKey", None)
            if mk in keys and mk != t.ratingKey:
                pct = get_sonic_match_percent(m)
                w = (pct / 100.0) if pct is not None else 0.5
                direct[t.ratingKey][mk] = max(direct[t.ratingKey].get(mk, 0.0), w)
                direct[mk][t.ratingKey] = max(direct[mk].get(t.ratingKey, 0.0), w)

    feats = cr.load_album_features()["albums"]

    def _score(a, b):
        album_a, album_b = getattr(a, "parentRatingKey", None), getattr(b, "parentRatingKey", None)
        if b.ratingKey in direct[a.ratingKey]:
            score = 1.0 + direct[a.ratingKey][b.ratingKey]
        elif album_a is not None and album_a == album_b:
            score = 0.6
        else:
            score = _album_similarity(feats.get(str(album_a)), feats.get(str(album_b)), str(album_a), str(album_b))
        artist_a = getattr(a, "grandparentRatingKey", None)
        if artist_a is not None and artist_a == getattr(b, "grandparentRatingKey", None):
            score -= 0.5
        return score + random.random() * 1e-3

    current = start if start is not None else random.choice(tracks)
    ordered = [current]
    remaining = [t for t in tracks if t.ratingKey != current.ratingKey]
    while remaining:
        nxt = max(remaining, key=lambda t: _score(current, t))
        ordered.append(nxt)
        remaining.remove(nxt)
        current = nxt
    return ordered


def build_cluster_playlist(plex, cluster_id, size=50, max_artist_share=DEFAULT_MAX_ARTIST_SHARE,
                           popular_share=DEFAULT_POPULAR_SHARE, dj=True, registry=None, debug=None):
    """A playlist drawn from EVERY track of every album in the cluster,
    picked by _pick_tracks (popular/random split, per-artist cap of
    max(1, ceil(size * max_artist_share)); 0 = no cap). Returned in DJ order
    (dj_order) when `dj`, else shuffled. Shorter than `size` only if the
    cluster can't fill it under the artist cap."""
    registry = registry or cr.load_registry()
    if not registry or cluster_id not in registry["clusters"]:
        raise ValueError("Cluster not found in the album cluster registry.")
    name = registry["clusters"][cluster_id]["name"]
    pool, _album_keys = _registry_cluster_pool(plex, registry, cluster_id)
    picked = _pick_tracks(pool.values(), size, popular_share, cap=_artist_cap(size, max_artist_share), label=name)
    if dj:
        return dj_order(picked, debug=debug)
    random.shuffle(picked)
    return picked


def _resolve_cluster_pool(plex, sub, debug=None):
    """Registry first (by cluster_id, else by name — persisting the id it
    found), legacy Library Clusters build cache as a fallback for mixes
    saved from a build that was never committed to the registry. Also keeps
    the mix's cluster name / '[Mix] <name>' playlist name in sync with a
    renamed cluster, and tracks drift vs. the album set at subscription."""
    if sub.get("stale"):
        raise StaleMixError(
            f"Stale: {sub['stale'].get('reason')} — will be deleted at the next weekly curation."
        )
    registry = cr.load_registry()
    if registry:
        cid = sub.get("cluster_id") or cr.find_cluster_id_by_name(registry, sub["cluster_name"])
        if cid and cid in registry["clusters"]:
            sub["cluster_id"] = cid
            _rename_sub(sub, registry["clusters"][cid]["name"])
            pool, album_keys = _registry_cluster_pool(plex, registry, cid)
            current = set(album_keys)
            if not sub.get("baseline_album_keys"):
                sub["baseline_album_keys"] = sorted(current)
            baseline = set(sub["baseline_album_keys"])
            union = baseline | current
            sub["drift"] = round(1 - len(baseline & current) / len(union), 3) if union else 0.0
            return pool
        if sub.get("cluster_id"):
            raise ValueError(
                f"Cluster '{sub['cluster_name']}' no longer exists in the cluster registry — "
                "it will be cleaned up at the next weekly curation."
            )
    return _gather_cluster_pool(plex, sub["cluster_name"], debug=debug)


def _update_fit_stats(sub, core_pool):
    """Per-album "did this mix's picks get played" counters, used by
    curation as a weak cluster-fit signal. For each cluster-seeded track
    that was in the playlist since the last run: exposures += 1 for its
    album, plays += the track's viewCount increase (Plex only counts a play
    at ~90% completion, so skipped tracks add nothing)."""
    fit = sub.get("fit_stats") or {}
    prev_counts = sub.get("track_view_counts") or {}
    for rk in sub.get("current_cluster_keys") or []:
        track = core_pool.get(rk)
        if track is None or str(rk) not in prev_counts:
            continue
        album = str(getattr(track, "parentRatingKey", "") or "")
        if not album:
            continue
        delta = (getattr(track, "viewCount", 0) or 0) - prev_counts[str(rk)]
        entry = fit.setdefault(album, {"exposures": 0, "plays": 0})
        entry["exposures"] += 1
        entry["plays"] += max(0, delta)
    sub["fit_stats"] = fit


def run_cluster_mix_subscription(sub, plex, debug=None):
    """
    Builds the cluster mix from two rotating parts and upserts the target
    playlist:
      - a cluster part (~cluster_ratio of target_size) from every track of
        the cluster's albums,
      - a sonic-expansion part (the remainder): Plex sonicallySimilar
        matches from OUTSIDE the cluster, seeded from sonic_seed_count
        tracks of today's cluster part (yesterday's sonic tracks stay
        available, so rotation applies to this part too).
    Both parts rotate via _rotate_pool and share one per-artist cap
    (max_artist_share of target_size) and the popular_share split. The
    result is put in DJ order unless dj_order is off. Mutates sub in place —
    the caller persists it.
    """
    core_pool = _resolve_cluster_pool(plex, sub, debug=debug)
    _update_fit_stats(sub, core_pool)

    target_size = sub.get("target_size", 30)
    rotation_percent = sub.get("rotation_percent", DEFAULT_ROTATION_PERCENT)
    cluster_ratio = sub.get("cluster_ratio", 0.67)
    sonic_seed_count = sub.get("sonic_seed_count", 10)
    popular_share = sub.get("popular_share", DEFAULT_POPULAR_SHARE)
    cap = _artist_cap(target_size, sub.get("max_artist_share", DEFAULT_MAX_ARTIST_SHARE))
    ages = sub.get("track_ages") or {}
    artist_counts = defaultdict(int)

    cluster_target = max(1, round(target_size * cluster_ratio))
    sonic_target = max(0, target_size - cluster_target)

    # --- Cluster part ---
    cluster_tracks, cluster_removed = _rotate_pool(
        core_pool,
        sub.get("current_cluster_keys") or [],
        set(sub.get("recent_retired_cluster_keys") or []),
        cluster_target, rotation_percent, popular_share, cap, artist_counts, ages,
    )

    # --- Sonic-expansion part, seeded from what the cluster part just picked ---
    seed_pool = cluster_tracks or list(core_pool.values())
    seeds = random.sample(seed_pool, min(sonic_seed_count, len(seed_pool))) if seed_pool else []

    sonic_pool = {}
    for seed in seeds:
        try:
            matches = seed.sonicallySimilar(limit=15)
        except Exception as e:
            if debug:
                debug.write(f"└ ❌ Sonic lookup failed for a cluster seed: `{e}`")
            continue
        for m in matches:
            rk = getattr(m, "ratingKey", None)
            if rk and rk not in core_pool and rk not in sonic_pool:
                sonic_pool[rk] = m
    # Today's seeds rarely re-surface yesterday's matches; keep those tracks
    # available so they rotate out gradually instead of all at once.
    for rk in sub.get("current_sonic_keys") or []:
        if rk not in sonic_pool and rk not in core_pool:
            try:
                sonic_pool[rk] = plex.fetchItem(int(rk))
            except Exception:
                continue

    sonic_tracks, sonic_removed = _rotate_pool(
        sonic_pool,
        sub.get("current_sonic_keys") or [],
        set(sub.get("recent_retired_sonic_keys") or []),
        sonic_target, rotation_percent, popular_share, cap, artist_counts, ages,
    )

    # Combine and dedup (keep the cluster copy if both parts hold a track).
    final_tracks = []
    seen = set()
    for t in cluster_tracks + sonic_tracks:
        if t.ratingKey not in seen:
            final_tracks.append(t)
            seen.add(t.ratingKey)

    if sub.get("dj_order", True):
        final_tracks = dj_order(final_tracks, debug=debug)
    else:
        random.shuffle(final_tracks)
    playlist = _upsert_playlist(plex, sub["playlist_name"], final_tracks, sub.get("playlist_rating_key"))
    if playlist is not None:
        sub["playlist_rating_key"] = playlist.ratingKey
    sub["track_view_counts"] = {str(t.ratingKey): getattr(t, "viewCount", 0) or 0 for t in cluster_tracks}
    sub["track_ages"] = {str(t.ratingKey): ages.get(str(t.ratingKey), 0) + 1 for t in final_tracks}

    sub["current_cluster_keys"] = [t.ratingKey for t in cluster_tracks]
    sub["recent_retired_cluster_keys"] = [t.ratingKey for t in cluster_removed]
    sub["current_sonic_keys"] = [t.ratingKey for t in sonic_tracks]
    sub["recent_retired_sonic_keys"] = [t.ratingKey for t in sonic_removed]

    return len(final_tracks)


def _rotate_pool(pool, current_keys, recent_retired, target_size, rotation_percent,
                 popular_share=DEFAULT_POPULAR_SHARE, cap=None, artist_counts=None, ages=None):
    """
    Rotation for one part of a Live Mix. First run (or nothing carries
    over): picks target_size tracks with _pick_tracks. Later runs:
      - removes rotation_percent of the current tracks (of what's actually
        in the mix, not of target_size), the ones that have
        been in the mix the longest first (`ages` = runs in the mix), then
        the least played — so every track stays roughly 100/rotation_percent
        days and the popular/random balance holds over time;
      - drops kept tracks that break the artist cap (e.g. after the cap was
        tightened);
      - refills with _pick_tracks from the pool, skipping what was just
        removed (one-rotation cooldown) unless the pool is too small.
    `artist_counts` is shared across parts and updated in place.
    Returns (final_tracks, removed_tracks).
    """
    artist_counts = artist_counts if artist_counts is not None else defaultdict(int)
    ages = ages or {}
    if target_size <= 0 or not pool:
        return [], []

    current_tracks = [pool[k] for k in current_keys if k in pool]
    if not current_tracks:
        return _pick_tracks(pool.values(), target_size, popular_share, cap, artist_counts), []

    # Share of what's actually in the mix (it can be shorter than target_size
    # when the artist cap or a small pool limits it).
    rotation_count = max(1, round(len(current_tracks) * rotation_percent / 100))
    by_age = sorted(
        current_tracks,
        key=lambda t: (-ages.get(str(t.ratingKey), 0), getattr(t, "viewCount", 0) or 0, random.random()),
    )
    removed = by_age[:rotation_count]
    kept = []
    for t in by_age[rotation_count:]:
        artist = getattr(t, "grandparentRatingKey", None)
        if cap and artist is not None and artist_counts[artist] >= cap:
            removed.append(t)
            continue
        artist_counts[artist] += 1
        kept.append(t)

    need = target_size - len(kept)
    kept_keys = {t.ratingKey for t in kept}
    removed_keys = {t.ratingKey for t in removed}
    candidates = [t for t in pool.values()
                  if t.ratingKey not in kept_keys and t.ratingKey not in removed_keys
                  and t.ratingKey not in recent_retired]
    added = _pick_tracks(candidates, need, popular_share, cap, artist_counts)
    if len(added) < need:  # pool too small for the cooldown — allow just-removed tracks back
        added_keys = {t.ratingKey for t in added}
        backup = [t for t in pool.values() if t.ratingKey not in kept_keys and t.ratingKey not in added_keys]
        added += _pick_tracks(backup, need - len(added), popular_share, cap, artist_counts)
    return kept + added, removed


# --- Batch runner --------------------------------------------------------

def _update_sub_fields(kind, sub_id, updates):
    """Reloads the config fresh from disk, applies `updates` to just the one
    subscription (if it still exists), and saves — used instead of mutating
    a long-held in-memory `cfg` so a batch run doesn't clobber a removal or
    toggle that happened on disk while the batch was still in progress (see
    run_all_subscriptions). Silently no-ops if the subscription was removed
    in the meantime — that's the correct outcome, not an error."""
    cfg = _load_config()
    key = "artist_mixes" if kind == "artist_mix" else "cluster_mixes"
    for s in cfg[key]:
        if s["id"] == sub_id:
            s.update(updates)
            _save_config(cfg)
            return
    # sub_id no longer exists (removed concurrently) — nothing to update.


def run_all_subscriptions(plex_url=None, plex_token=None, debug=None, only=None):
    """
    Runs every enabled subscription once, connecting to Plex using env vars
    by default (used by the scheduled job) but overridable for manual
    "Run now" calls from the UI, which can pass the sidebar's current
    values instead. `only` can restrict this to just "artist_mixes" or
    just "cluster_mixes" (defaults to both). Returns a summary dict for
    display.

    Deliberately does NOT hold one `cfg` snapshot in memory for the whole
    batch and write it back wholesale — a batch run over many subscriptions
    can take real time (per-track sonic lookups etc.), and if a subscription
    is removed or toggled via the UI while the batch is still running, a
    stale in-memory `cfg` would silently resurrect it on the next periodic
    save. The list of *which* subscriptions to run is snapshotted once at
    the start (so mid-batch additions don't unexpectedly join a run already
    in progress), but each result is persisted via _update_sub_fields, which
    reloads fresh from disk and only ever touches that one subscription.
    """
    plex_url = plex_url or os.environ.get("PLEX_URL")
    plex_token = plex_token or os.environ.get("PLEX_TOKEN")
    if not plex_url or not plex_token:
        raise ValueError("PLEX_URL/PLEX_TOKEN not available (checked env vars and arguments).")

    plex = PlexServer(plex_url, plex_token)
    cfg = _load_config()
    summary = {"artist_mixes": [], "cluster_mixes": []}

    run_artist = only is None or only == "artist_mixes"
    run_cluster = only is None or only == "cluster_mixes"

    if run_artist:
        for sub in list(cfg["artist_mixes"]):
            if not sub.get("enabled", True):
                continue
            count, err = _run_and_record("artist_mix", sub, plex, debug=debug)
            summary["artist_mixes"].append((sub["artist_name"], count, err))

    if run_cluster:
        for sub in list(cfg["cluster_mixes"]):
            if not sub.get("enabled", True) or sub.get("stale"):
                continue
            count, err = _run_and_record("cluster_mix", sub, plex, debug=debug)
            summary["cluster_mixes"].append((sub["cluster_name"], count, err))

    return summary


def _run_and_record(kind, sub, plex, debug=None):
    """Runs one subscription and persists its outcome (via
    _update_sub_fields, so concurrent UI edits aren't clobbered). Returns
    (track_count, error_message)."""
    now = datetime.now().isoformat(timespec="seconds")
    label = sub["artist_name"] if kind == "artist_mix" else sub["cluster_name"]
    try:
        if kind == "artist_mix":
            count = run_artist_mix_subscription(sub, plex, debug=debug)
        else:
            count = run_cluster_mix_subscription(sub, plex, debug=debug)
    except PlaylistDeletedError:
        # Deleting the playlist in Plex is taken as the user's intent: stop
        # instead of silently recreating it.
        msg = ("Playlist was deleted in Plex — subscription switched off. Switch it back on to "
               "recreate the playlist, or remove the subscription.")
        _update_sub_fields(kind, sub["id"], {"last_run": now, "last_error": msg,
                                             "enabled": False, "playlist_missing": True})
        return None, msg
    except Exception as e:
        logger.exception(f"Live playlist subscription failed: {label}")
        _update_sub_fields(kind, sub["id"], {"last_run": now, "last_error": str(e)})
        return None, str(e)

    updates = {"last_run": now, "last_track_count": count, "last_error": None}
    updates.update({k: sub[k] for k in _RUN_STATE_FIELDS if k in sub})
    _update_sub_fields(kind, sub["id"], updates)
    return count, None


def run_single_subscription(kind, sub_id, plex_url=None, plex_token=None, debug=None):
    """Manual 'Run now' for one subscription, used by the UI's per-row button."""
    plex_url = plex_url or os.environ.get("PLEX_URL")
    plex_token = plex_token or os.environ.get("PLEX_TOKEN")
    plex = PlexServer(plex_url, plex_token)

    cfg = _load_config()
    key = "artist_mixes" if kind == "artist_mix" else "cluster_mixes"
    sub = next((s for s in cfg[key] if s["id"] == sub_id), None)
    if sub is None:
        raise ValueError("Subscription not found (may have been removed).")

    count, err = _run_and_record(kind, sub, plex, debug=debug)
    if err:
        raise RuntimeError(err)
    return count


# --- Cluster registry reconciliation ---------------------------------------

def _rename_sub(sub, new_cluster_name):
    """Follows a cluster rename. The playlist is renamed too, but only if it
    still uses the default '[Mix] <cluster>' name (custom names are kept);
    the Plex rename itself happens on the next run via its ratingKey."""
    old = sub.get("cluster_name")
    if not new_cluster_name or old == new_cluster_name:
        return
    sub["cluster_name"] = new_cluster_name
    if sub.get("playlist_name") == f"[Mix] {old}":
        sub["playlist_name"] = f"[Mix] {new_cluster_name}"


def _mark_stale(sub, reason, generation):
    if not sub.get("stale"):
        sub["stale"] = {"reason": reason, "generation": generation,
                        "marked_at": datetime.now().isoformat(timespec="seconds")}


def apply_cluster_events(events, registry):
    """Brings cluster-mix subscriptions in line with a newly committed
    registry generation. Per subscription:
      - continued / renamed / split (kept its id) -> keeps running, renamed if needed
      - merged into a cluster with no mix yet     -> re-pointed to the survivor
      - merged into a cluster that has a mix      -> stale (duplicate)
      - retired (dissolved)                       -> stale
      - id no longer in the registry              -> stale (orphaned)
    Legacy subscriptions without a cluster_id are matched by name first.
    Stale playlists are NOT deleted here — that happens at the weekly
    curation (delete_stale_subscriptions). Returns the number of subs marked stale."""
    cfg = _load_config()
    clusters = registry["clusters"]
    gen = registry.get("generation")
    subs = cfg["cluster_mixes"]
    newly_stale = 0

    for s in subs:
        if not s.get("cluster_id"):
            cid = cr.find_cluster_id_by_name(registry, s.get("cluster_name"))
            if cid:
                s["cluster_id"] = cid

    def _active_on(cid):
        return [s for s in subs if s.get("cluster_id") == cid and not s.get("stale")]

    for ev in events:
        affected = _active_on(ev["cluster_id"])
        if ev["type"] == "retired":
            for s in affected:
                _mark_stale(s, f"cluster '{ev.get('name')}' was dissolved ({ev.get('albums', 0)} albums left)", gen)
                newly_stale += 1
        elif ev["type"] == "merged":
            for s in affected:
                if _active_on(ev["into"]):
                    _mark_stale(s, f"cluster '{ev.get('name')}' was merged into '{ev.get('into_name')}', "
                                   "which already has its own Live Mix", gen)
                    newly_stale += 1
                else:
                    s["cluster_id"] = ev["into"]
                    s["baseline_album_keys"] = []
                    _rename_sub(s, clusters[ev["into"]]["name"])

    for s in subs:
        cid = s.get("cluster_id")
        if not cid or s.get("stale"):
            continue
        if cid in clusters:
            _rename_sub(s, clusters[cid]["name"])
        else:
            _mark_stale(s, f"cluster '{s.get('cluster_name')}' no longer exists", gen)
            newly_stale += 1

    _save_config(cfg)
    return newly_stale


def delete_stale_subscriptions(plex):
    """Weekly cleanup: deletes each stale mix's Plex playlist — only by the
    ratingKey this module created it under, and only if that item really is
    a playlist — then removes the subscription. A sub with no stored
    ratingKey (created before ratingKeys were tracked and never run since)
    is removed without touching Plex. On a transient Plex error the sub is
    kept so the next run retries. Returns one record per removed sub
    (including the playlist's track keys, so a rollback can recreate it)."""
    cfg = _load_config()
    removed = []
    keep = []
    for s in cfg["cluster_mixes"]:
        if not s.get("stale"):
            keep.append(s)
            continue
        record = {"sub": dict(s), "track_keys": [], "playlist_deleted": False, "note": None}
        rk = s.get("playlist_rating_key")
        if rk:
            try:
                playlist = plex.fetchItem(int(rk))
                if getattr(playlist, "TYPE", None) != "playlist":
                    record["note"] = f"ratingKey {rk} is not a playlist — left untouched"
                else:
                    record["track_keys"] = [t.ratingKey for t in playlist.items()]
                    playlist.delete()
                    record["playlist_deleted"] = True
            except NotFound:
                record["note"] = "playlist was already gone from Plex"
            except Exception as e:
                logger.exception(f"Couldn't delete stale playlist '{s.get('playlist_name')}'")
                record["note"] = f"delete failed ({e}) — will retry next week"
                keep.append(s)
                continue
        else:
            record["note"] = "no stored playlist ratingKey — Plex playlist left untouched"
        removed.append(record)
    cfg["cluster_mixes"] = keep
    _save_config(cfg)
    return removed


def restore_deleted_subscription(plex, record):
    """Undo for delete_stale_subscriptions (used by curation rollback):
    recreates the playlist from its saved track keys and re-adds the sub."""
    sub = dict(record["sub"])
    sub["stale"] = None
    sub["baseline_album_keys"] = []
    tracks = []
    for key in record.get("track_keys") or []:
        try:
            tracks.append(plex.fetchItem(int(key)))
        except Exception:
            continue
    sub["playlist_rating_key"] = None
    if tracks:
        playlist = plex.createPlaylist(title=sub["playlist_name"], items=tracks)
        sub["playlist_rating_key"] = playlist.ratingKey
    cfg = _load_config()
    if not any(s["id"] == sub["id"] for s in cfg["cluster_mixes"]):
        cfg["cluster_mixes"].append(sub)
        _save_config(cfg)


def collect_mix_fit_penalties(min_exposures, penalty):
    """{(album_key, cluster_id): penalty} for albums whose tracks sat in a
    cluster's Live Mix for >= min_exposures rotations without a single play
    — but only for mixes you did play from (otherwise "unplayed" says
    nothing about fit)."""
    out = {}
    for s in _load_config()["cluster_mixes"]:
        cid = s.get("cluster_id")
        fit = s.get("fit_stats") or {}
        if not cid or s.get("stale") or not any(v.get("plays") for v in fit.values()):
            continue
        for album, v in fit.items():
            if v.get("exposures", 0) >= min_exposures and not v.get("plays"):
                out[(album, cid)] = penalty
    return out


# --- Scheduler singleton ---------------------------------------------

def _job_wrapper():
    """Wraps run_all_subscriptions so a scheduler-triggered run's exceptions
    are actually logged instead of silently vanishing into APScheduler's
    executor thread (BackgroundScheduler does NOT re-raise or print job
    exceptions anywhere by default — a bug here would otherwise look
    identical to "the job just didn't fire"). Also stamps a last-attempt
    timestamp file independent of individual subscription state, so
    get_last_scheduled_attempt() can tell "the job fired but everything
    failed" apart from "the job never fired at all".

    Holds cluster_registry.JOB_LOCK so it never overlaps the weekly
    curation (or a UI-triggered registry job); first assigns any albums
    added since yesterday so they're in today's mixes."""
    try:
        with cr.JOB_LOCK:
            try:
                if cr.load_registry():
                    plex = PlexServer(os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN"))
                    logger.info(f"New-album sync: {cr.sync_new_albums(plex)}")
            except Exception:
                logger.exception("New-album sync failed (live playlists still refresh)")
            run_all_subscriptions()
    except Exception:
        logger.exception("Scheduled live-playlist run failed")
    finally:
        _record_scheduler_heartbeat()


def _curation_job_wrapper():
    import curation  # lazy: curation imports this module
    try:
        with cr.JOB_LOCK:
            curation.run_weekly_curation(trigger="schedule")
    except Exception:
        logger.exception("Scheduled weekly cluster curation failed")
    finally:
        curation.record_heartbeat()


def _curation_due():
    import curation
    last = curation.get_last_heartbeat()
    if last is not None and (datetime.now() - last) <= timedelta(days=CURATION_CATCH_UP_DAYS):
        return False
    return cr.load_registry() is not None or os.path.exists(CLUSTER_RESULTS_CACHE_PATH)


def _startup_catch_up(run_curation, run_daily):
    if run_curation:
        _curation_job_wrapper()
    if run_daily:
        _job_wrapper()


def _record_scheduler_heartbeat():
    try:
        os.makedirs(os.path.dirname(LIVE_PLAYLISTS_PATH), exist_ok=True)
        with open(LIVE_PLAYLISTS_PATH + ".heartbeat", "w") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))
    except Exception:
        pass


def get_last_scheduler_heartbeat():
    """Last time the scheduled job actually fired (success or failure),
    independent of per-subscription last_run. Used to detect "the process
    restarted and the scheduler has been silently dead" rather than just
    "one subscription happens to be failing"."""
    try:
        with open(LIVE_PLAYLISTS_PATH + ".heartbeat", "r") as f:
            return datetime.fromisoformat(f.read().strip())
    except Exception:
        return None


def ensure_scheduler_started():
    """
    Starts the daily APScheduler job exactly once per container process,
    regardless of how many times Streamlit re-executes app.py (module-level
    state survives Streamlit reruns since modules are only imported once
    per process). Safe to call on every app.py render.

    Self-healing catch-up: if the container was restarted (crash, redeploy,
    host reboot) and more than ~25h have passed since the job last actually
    fired, a whole day's sync would otherwise silently vanish — the cron
    trigger only fires at the next scheduled wall-clock time, it never
    "catches up" a missed run on its own. So on startup, if the gap since
    the last heartbeat is too large, run once immediately in a background
    thread (non-blocking, so Streamlit doesn't hang on connect).
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return

        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)
        scheduler.add_job(
            _job_wrapper,
            trigger="cron",
            hour=LIVE_PLAYLISTS_HOUR,
            minute=LIVE_PLAYLISTS_MINUTE,
            id="live_playlists_daily",
            replace_existing=True,
            # If the process was asleep/restarting exactly at the scheduled
            # minute, still fire rather than silently skipping the day —
            # up to a 1 hour grace window.
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            _curation_job_wrapper,
            trigger="cron",
            day_of_week=CURATION_DAY_OF_WEEK,
            hour=CURATION_HOUR,
            minute=CURATION_MINUTE,
            id="cluster_curation_weekly",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        scheduler.start()
        _scheduler = scheduler

        last_heartbeat = get_last_scheduler_heartbeat()
        daily_gap = (
            last_heartbeat is None
            or (datetime.now() - last_heartbeat) > timedelta(hours=25)
        )
        curation_gap = _curation_due()
        if daily_gap or curation_gap:
            logger.info(f"Catch-up on startup: curation={curation_gap}, daily refresh={daily_gap}.")
            threading.Thread(target=_startup_catch_up, args=(curation_gap, daily_gap), daemon=True).start()


def get_next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("live_playlists_daily")
    return job.next_run_time if job else None


def get_next_curation_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("cluster_curation_weekly")
    return job.next_run_time if job else None
