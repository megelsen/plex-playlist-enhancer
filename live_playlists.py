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
    Each part rotates independently: `rotation_percent` of its least-
    played current tracks get swapped for new picks from its own pool
    each day, with a one-rotation cooldown per part. Because the artist
    set (and therefore the sonic seeds it can produce) never fundamentally
    changes, the vibe stays intact while specific songs cycle.

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

# Fields a run_*_subscription call may change on `sub`, persisted by the runners.
_RUN_STATE_FIELDS = (
    "cluster_id", "cluster_name", "playlist_name", "playlist_rating_key",
    "current_cluster_keys", "recent_retired_cluster_keys",
    "current_sonic_keys", "recent_retired_sonic_keys",
    "track_view_counts", "fit_stats", "baseline_album_keys", "drift",
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


def add_cluster_mix_subscription(cluster_name, playlist_name, target_size=30, rotation_percent=85,
                                  cluster_ratio=0.67, sonic_seed_count=10, cluster_id=None):
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
        "current_cluster_keys": [],
        "recent_retired_cluster_keys": [],
        "current_sonic_keys": [],
        "recent_retired_sonic_keys": [],
        "playlist_rating_key": None,
        "track_view_counts": {},
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
                              target_size=30, rotation_percent=85, cluster_ratio=0.67,
                              sonic_seed_count=10, cluster_id=None, debug=None):
    """Registers a new cluster-mix subscription and runs it once immediately.
    `cluster_id` (a registry cluster) is optional — without it the cluster
    is resolved by name on each run. Returns (playlist_name, track_count)."""
    add_cluster_mix_subscription(
        cluster_name, playlist_name,
        target_size=target_size, rotation_percent=rotation_percent,
        cluster_ratio=cluster_ratio, sonic_seed_count=sonic_seed_count,
        cluster_id=cluster_id,
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


def build_cluster_playlist(plex, cluster_id, size=50, max_artist_share=0.10, popular_share=0.5, registry=None):
    """A playlist drawn from EVERY track of every album in the cluster:
      - popular_share of `size` picked by weighted random sampling, weight =
        plays + 1 (viewCount; the +1 gives unplayed tracks a chance),
      - the rest picked uniformly from the remaining tracks (discovery),
      - at most max(1, ceil(size * max_artist_share)) tracks per artist
        (0 = no cap).
    Returns a shuffled track list; shorter than `size` only if the cluster
    can't fill it under the artist cap. Tracks get a recommendation_type of
    "<cluster> (Popular)" / "(Discovery)" for display."""
    registry = registry or cr.load_registry()
    if not registry or cluster_id not in registry["clusters"]:
        raise ValueError("Cluster not found in the album cluster registry.")
    name = registry["clusters"][cluster_id]["name"]
    pool, _album_keys = _registry_cluster_pool(plex, registry, cluster_id)
    tracks = list(pool.values())

    cap = max(1, math.ceil(size * max_artist_share)) if max_artist_share else None
    per_artist = defaultdict(int)
    picked, picked_keys = [], set()

    def _take(candidates, target, label):
        for t in candidates:
            if len(picked) >= target:
                return
            artist = getattr(t, "grandparentRatingKey", None)
            if t.ratingKey in picked_keys or (cap and per_artist[artist] >= cap):
                continue
            setattr(t, "recommendation_type", f"{name} ({label})")
            picked.append(t)
            picked_keys.add(t.ratingKey)
            per_artist[artist] += 1

    # Weighted sampling without replacement (Efraimidis–Spirakis): order by
    # u ** (1 / weight), u ~ U(0, 1) — O(n log n) on large clusters.
    weighted = sorted(tracks, key=lambda t: random.random() ** (1.0 / ((getattr(t, "viewCount", 0) or 0) + 1)),
                      reverse=True)
    _take(weighted, round(size * popular_share), "Popular")
    rest = [t for t in tracks if t.ratingKey not in picked_keys]
    random.shuffle(rest)
    _take(rest, size, "Discovery")

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
      - a cluster-seeded part (~cluster_ratio of target_size), pulled from
        the cluster's assigned artists' full catalogs.
      - a sonic-expansion part (the remainder), pulled from Plex's
        sonic-similarity hub, seeded from sonic_seed_count tracks drawn
        from the cluster-seeded part just picked this run.
    Each part rotates independently via _rotate_pool. Mutates sub in place
    with the new state — caller is responsible for persisting cfg.
    """
    core_pool = _resolve_cluster_pool(plex, sub, debug=debug)
    _update_fit_stats(sub, core_pool)

    target_size = sub.get("target_size", 30)
    rotation_percent = sub.get("rotation_percent", 30)
    cluster_ratio = sub.get("cluster_ratio", 0.67)
    sonic_seed_count = sub.get("sonic_seed_count", 10)

    cluster_target = max(1, round(target_size * cluster_ratio))
    sonic_target = max(0, target_size - cluster_target)

    # --- Cluster-seeded part ---
    cluster_tracks, cluster_removed = _rotate_pool(
        core_pool,
        sub.get("current_cluster_keys") or [],
        set(sub.get("recent_retired_cluster_keys") or []),
        cluster_target, rotation_percent,
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

    sonic_tracks, sonic_removed = _rotate_pool(
        sonic_pool,
        sub.get("current_sonic_keys") or [],
        set(sub.get("recent_retired_sonic_keys") or []),
        sonic_target, rotation_percent,
    )

    # Combine and dedup (a sonic match could in rare cases coincide with a
    # cluster pick if Plex's hub surfaces a same-cluster-artist track that
    # somehow wasn't in core_pool — keep the cluster copy, drop the dupe).
    final_tracks = []
    seen = set()
    for t in cluster_tracks + sonic_tracks:
        if t.ratingKey not in seen:
            final_tracks.append(t)
            seen.add(t.ratingKey)

    random.shuffle(final_tracks)
    playlist = _upsert_playlist(plex, sub["playlist_name"], final_tracks, sub.get("playlist_rating_key"))
    if playlist is not None:
        sub["playlist_rating_key"] = playlist.ratingKey
    sub["track_view_counts"] = {str(t.ratingKey): getattr(t, "viewCount", 0) or 0 for t in cluster_tracks}

    sub["current_cluster_keys"] = [t.ratingKey for t in cluster_tracks]
    sub["recent_retired_cluster_keys"] = [t.ratingKey for t in cluster_removed]
    sub["current_sonic_keys"] = [t.ratingKey for t in sonic_tracks]
    sub["recent_retired_sonic_keys"] = [t.ratingKey for t in sonic_removed]

    return len(final_tracks)


def _rotate_pool(pool, current_keys, recent_retired, target_size, rotation_percent):
    """
    Shared rotation logic for one sub-pool: on first run (or once nothing
    carries over), picks target_size tracks leaning toward popular but
    shuffled from a wider shortlist. On subsequent runs, swaps out
    rotation_percent of the current tracks (least-played first) for fresh
    picks from the pool, respecting a one-rotation cooldown on what was
    just removed. Returns (final_tracks, removed_tracks).
    """
    if target_size <= 0 or not pool:
        return [], []

    current_tracks = [pool[k] for k in current_keys if k in pool]

    if not current_tracks:
        ranked = sorted(pool.values(), key=lambda t: getattr(t, "viewCount", 0) or 0, reverse=True)
        shortlist = ranked[: max(target_size * 3, target_size)]
        random.shuffle(shortlist)
        return shortlist[:target_size], []

    rotation_count = max(1, round(target_size * rotation_percent / 100))
    rotation_count = min(rotation_count, len(current_tracks))

    sorted_current = sorted(
        current_tracks, key=lambda t: (getattr(t, "viewCount", 0) or 0, random.random())
    )
    removed = sorted_current[:rotation_count]
    kept = sorted_current[rotation_count:]
    kept_keys = {t.ratingKey for t in kept}

    candidates = [t for t in pool.values() if t.ratingKey not in kept_keys and t.ratingKey not in recent_retired]
    if len(candidates) < rotation_count:
        candidates = [t for t in pool.values() if t.ratingKey not in kept_keys]
    random.shuffle(candidates)
    added = candidates[:rotation_count]

    final = kept + added

    # If the pool is smaller than target_size, pad out from whatever's left.
    if len(final) < target_size:
        final_keys = {t.ratingKey for t in final}
        extra = [t for t in pool.values() if t.ratingKey not in final_keys]
        random.shuffle(extra)
        final += extra[: target_size - len(final)]

    return final, removed


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
