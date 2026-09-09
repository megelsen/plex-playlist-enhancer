"""Live Playlists: daily-refreshing Artist Mixes and Cluster Mixes.

Two subscription types, persisted to disk (LIVE_PLAYLISTS_PATH) so they
survive container restarts:

  - "artist_mix": re-runs build_artist_mix() with the same settings every
    day. Different songs come out simply because the builder already
    shuffles internally — no special rotation logic needed here.

  - "cluster_mix": re-generating from scratch (re-running Gemini/Louvain)
    would let the vibe drift day to day, so instead this keeps the SAME
    artist set that's already assigned to the cluster (read from the
    existing Library Clusters disk cache via build_artist_cluster_map).
    The playlist is composed of two rotating parts:
      - ~cluster_ratio (default 67%) seeded directly from those artists'
        full catalogs.
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
"""

import json
import logging
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger("live_playlists")

from plexapi.server import PlexServer

from artist_mix import build_artist_mix
from clustering import build_artist_cluster_map, load_cluster_results_cache

LIVE_PLAYLISTS_PATH = os.environ.get("LIVE_PLAYLISTS_PATH", "/app/data/live_playlists.json")

# When the daily job fires. Configurable via env var rather than hardcoded
# so this doesn't need a code change if you want a different hour later.
LIVE_PLAYLISTS_HOUR = int(os.environ.get("LIVE_PLAYLISTS_HOUR", 4))
LIVE_PLAYLISTS_MINUTE = int(os.environ.get("LIVE_PLAYLISTS_MINUTE", 0))

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
                                  cluster_ratio=0.67, sonic_seed_count=10):
    cfg = _load_config()
    cfg["cluster_mixes"].append({
        "id": _new_id(),
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

def _upsert_playlist(plex, title, tracks):
    if not tracks:
        return
    existing = next((p for p in plex.playlists() if p.title == title), None)
    if existing:
        existing.removeItems(existing.items())
        existing.addItems(tracks)
    else:
        plex.createPlaylist(title=title, items=tracks)


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
                              sonic_seed_count=10, debug=None):
    """Registers a new cluster-mix subscription and runs it once immediately.
    Returns (playlist_name, track_count)."""
    add_cluster_mix_subscription(
        cluster_name, playlist_name,
        target_size=target_size, rotation_percent=rotation_percent,
        cluster_ratio=cluster_ratio, sonic_seed_count=sonic_seed_count,
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

    _upsert_playlist(plex, sub["playlist_name"], tracks)
    return len(tracks)


# --- Cluster Mix rotation ----------------------------------------------

def _gather_cluster_pool(plex, cluster_name, debug=None):
    """
    Artists already assigned to `cluster_name` (per the last saved cluster
    build), expanded to their full track catalogs. Returns {ratingKey: track}.
    Raises ValueError if the cluster can't be found — most commonly because
    Library Clusters hasn't been built/saved yet.
    """
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
    core_pool = _gather_cluster_pool(plex, sub["cluster_name"], debug=debug)

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
    _upsert_playlist(plex, sub["playlist_name"], final_tracks)

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
    now_snapshot = datetime.now().isoformat(timespec="seconds")
    summary = {"artist_mixes": [], "cluster_mixes": []}

    run_artist = only is None or only == "artist_mixes"
    run_cluster = only is None or only == "cluster_mixes"

    if run_artist:
        for sub in list(cfg["artist_mixes"]):
            if not sub.get("enabled", True):
                continue
            now = datetime.now().isoformat(timespec="seconds")
            try:
                count = run_artist_mix_subscription(sub, plex, debug=debug)
                _update_sub_fields("artist_mix", sub["id"], {
                    "last_run": now, "last_track_count": count, "last_error": None,
                })
                summary["artist_mixes"].append((sub["artist_name"], count, None))
            except Exception as e:
                logger.exception(f"Artist mix subscription failed: {sub['artist_name']}")
                _update_sub_fields("artist_mix", sub["id"], {"last_run": now, "last_error": str(e)})
                summary["artist_mixes"].append((sub["artist_name"], None, str(e)))

    if run_cluster:
        for sub in list(cfg["cluster_mixes"]):
            if not sub.get("enabled", True):
                continue
            now = datetime.now().isoformat(timespec="seconds")
            try:
                count = run_cluster_mix_subscription(sub, plex, debug=debug)
                _update_sub_fields("cluster_mix", sub["id"], {
                    "last_run": now, "last_track_count": count, "last_error": None,
                    "current_cluster_keys": sub.get("current_cluster_keys", []),
                    "recent_retired_cluster_keys": sub.get("recent_retired_cluster_keys", []),
                    "current_sonic_keys": sub.get("current_sonic_keys", []),
                    "recent_retired_sonic_keys": sub.get("recent_retired_sonic_keys", []),
                })
                summary["cluster_mixes"].append((sub["cluster_name"], count, None))
            except Exception as e:
                logger.exception(f"Cluster mix subscription failed: {sub['cluster_name']}")
                _update_sub_fields("cluster_mix", sub["id"], {"last_run": now, "last_error": str(e)})
                summary["cluster_mixes"].append((sub["cluster_name"], None, str(e)))

    return summary


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

    now = datetime.now().isoformat(timespec="seconds")
    try:
        if kind == "artist_mix":
            count = run_artist_mix_subscription(sub, plex, debug=debug)
            _update_sub_fields(kind, sub_id, {"last_run": now, "last_track_count": count, "last_error": None})
        else:
            count = run_cluster_mix_subscription(sub, plex, debug=debug)
            _update_sub_fields(kind, sub_id, {
                "last_run": now, "last_track_count": count, "last_error": None,
                "current_cluster_keys": sub.get("current_cluster_keys", []),
                "recent_retired_cluster_keys": sub.get("recent_retired_cluster_keys", []),
                "current_sonic_keys": sub.get("current_sonic_keys", []),
                "recent_retired_sonic_keys": sub.get("recent_retired_sonic_keys", []),
            })
    except Exception as e:
        _update_sub_fields(kind, sub_id, {"last_run": now, "last_error": str(e)})
        raise

    return count


# --- Scheduler singleton ---------------------------------------------

def _job_wrapper():
    """Wraps run_all_subscriptions so a scheduler-triggered run's exceptions
    are actually logged instead of silently vanishing into APScheduler's
    executor thread (BackgroundScheduler does NOT re-raise or print job
    exceptions anywhere by default — a bug here would otherwise look
    identical to "the job just didn't fire"). Also stamps a last-attempt
    timestamp file independent of individual subscription state, so
    get_last_scheduled_attempt() can tell "the job fired but everything
    failed" apart from "the job never fired at all"."""
    try:
        run_all_subscriptions()
    except Exception:
        logger.exception("Scheduled live-playlist run failed")
    finally:
        _record_scheduler_heartbeat()


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

        scheduler = BackgroundScheduler()
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
        scheduler.start()
        _scheduler = scheduler

        last_heartbeat = get_last_scheduler_heartbeat()
        gap_too_large = (
            last_heartbeat is None
            or (datetime.now() - last_heartbeat) > timedelta(hours=25)
        )
        if gap_too_large:
            logger.info("Live playlists: no scheduled run in >25h, catching up now.")
            threading.Thread(target=_job_wrapper, daemon=True).start()


def get_next_run_time():
    if _scheduler is None:
        return None
    job = _scheduler.get_job("live_playlists_daily")
    return job.next_run_time if job else None
