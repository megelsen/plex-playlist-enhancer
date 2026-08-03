"""Library-wide genre+mood clustering pipeline: collects distinct genre and
mood tags, uses Gemini to map them into a fixed number of clusters (some of
which the user can lock in by name), assigns every track to a cluster based
on both its genre and mood tags, and ranks each cluster's tracks by
popularity.

The only network/LLM cost in this whole pipeline is the single Gemini
call in build_tag_cluster_mapping — everything else is local plexapi
work. That call is wrapped in a Streamlit cache (see
get_cached_tag_cluster_mapping) so it only re-runs when the underlying
tag list, locked clusters, total count, or prompt logic actually change.
"""

import hashlib
import json
import os
import random
import time
from collections import defaultdict
from math import ceil

import requests
import streamlit as st

try:
    import networkx as nx
    import community as community_louvain  # python-louvain
    HAS_SONIC_GRAPH_DEPS = True
except ImportError:
    HAS_SONIC_GRAPH_DEPS = False

from plex_helpers import get_sonic_match_percent, get_top_tracks_for_artist

# In-process cache of music_section.searchArtists() results. A full-library
# artist scan is the single most repeated Plex round-trip in this file —
# get_all_genre_and_mood_tags and build_artist_sonic_clusters each used to
# call searchArtists() separately, so a single "Build Clusters" click could
# scan the whole library 2-3 times over. Keyed by section (not by anything
# about the caller), so all three share one fetch per TTL window regardless
# of which functions are called in what order. This is intentionally NOT
# disk-backed — artists are live plexapi objects (can't be JSON-serialized),
# so this only helps within one running process/session, not across
# restarts (that's what ARTIST_PROFILE_CACHE_PATH and
# CLUSTER_RESULTS_CACHE_PATH are for).
_ARTIST_SCAN_CACHE = {}
ARTIST_SCAN_TTL_SECONDS = 900  # long enough to cover one scan->configure->build session

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

SONIC_GRAPH_NEIGHBOR_LIMIT = 15

# Bumped whenever build_tag_cluster_mapping's prompt logic changes in a way
# that would produce different results for the same tag list. Included in
# the cache key so a prompt fix takes effect immediately without requiring
# the user to manually click "Force Re-map Genres".
PROMPT_VERSION = 3

# Gemini can take a while on larger tag lists (bigger prompt = longer
# generation), so give it real headroom rather than the default requests
# timeout. Retries use exponential backoff for transient slowness/hiccups.
GEMINI_TIMEOUT_SECONDS = 180
GEMINI_MAX_RETRIES = 3

# Cap on how many fully-untagged ("Unsorted") tracks get a sonic-similarity
# lookup per build — each candidate costs one Plex API call, so this bounds
# worst-case runtime on very large libraries. Candidates are prioritized by
# play count, so the most-listened untagged tracks get refined first if the
# library has more Unsorted tracks than this.
REFINE_MAX_TRACKS = 300

# Minimum number of sonic neighbors that must agree on a cluster before an
# Unsorted track gets reassigned, AND the minimum margin the winning
# cluster's vote count must have over the runner-up. Both bars exist because
# a single matching neighbor (or a narrow plurality) was too weak a signal
# and was itself contributing to clusters drifting broad/mixed — e.g. a
# folk-rock track with a loud/driving arrangement getting pulled into an
# unrelated "aggressive" cluster on a handful of superficial sonic matches.
# These are intentionally strict: sonic similarity is a much noisier signal
# than genre tags, so extrapolation from it should be the exception, not
# the default outcome, for any given Unsorted track.
REFINE_MIN_NEIGHBOR_VOTES = 6
REFINE_MIN_VOTE_MARGIN = 4
# Winning cluster must also account for a real majority of ALL neighbor
# votes cast (not just beat the runner-up), so a track with scattered,
# inconclusive sonic matches doesn't get forced into whichever cluster
# happened to get slightly more hits.
REFINE_MIN_VOTE_SHARE = 0.6

# Mechanical safety net for cluster balance: even when the Gemini prompt
# tells the model to avoid thin/fragile clusters (see build_tag_cluster_mapping
# rule 7), the library's actual track distribution can't be known until
# after tag->track pooling happens locally. Any real (non-locked) cluster
# that ends up with fewer than this many pooled tracks gets folded into
# "Unsorted" rather than surfaced as a near-empty bucket — see
# _fold_thin_clusters. Locked clusters are exempt since the user explicitly
# asked for them by name.
MIN_CLUSTER_TRACKS = 5

# Disk-based cache for the tag->cluster mapping, so it survives container
# restarts/rebuilds (unlike st.cache_data, which is in-memory only and
# resets whenever the process restarts). Point CLUSTER_CACHE_PATH at a
# mounted volume in Docker so it actually persists across rebuilds.
DISK_CACHE_PATH = os.environ.get("CLUSTER_CACHE_PATH", "/app/data/cluster_mapping.json")

# Disk-based cache for per-artist sonic profiles (used by the artist-level
# relational clustering mode — see build_relational_graph). Each artist's
# profile costs `sample_size` sonicallySimilar() Plex calls to compute, so
# caching this is what makes rebuilds with the same library fast: unlike the
# tag mapping cache above (one blob, invalidated as a whole), this is a
# per-artist dict so a single new/changed artist doesn't force recomputing
# everyone else's profile.
ARTIST_PROFILE_CACHE_PATH = os.environ.get("ARTIST_PROFILE_CACHE_PATH", "/app/data/artist_sonic_profiles.json")

# Bumped whenever build_artist_sonic_profile's sampling/scoring logic changes
# in a way that would produce different profiles for the same artist+params
# — included in each entry's cache key so old entries are ignored (and
# naturally overwritten) after a logic change, without needing to wipe the
# whole cache file.
ARTIST_PROFILE_VERSION = 1

# How many of an artist's tracks get sonically sampled to build its profile.
# Kept small on purpose: this is a per-ARTIST cost (not per-track), so the
# whole library is affordable even at library scale, and 2-3 well-chosen
# tracks (most-played, or Plex's own popularity ranking as a fallback for
# artists with no play history) are a reasonable stand-in for "what this
# artist sounds like" without sampling every track.
ARTIST_SONIC_SAMPLE_SIZE = 3


def _disk_cache_key(genre_tags, mood_tags, locked_clusters, total_clusters):
    """
    Deterministic fingerprint of everything that should invalidate the
    cached mapping: the exact tag sets, locked cluster names, total count,
    and prompt version. Two runs with identical inputs get the same key
    regardless of tag ordering (sorted before hashing).
    """
    payload = json.dumps({
        "genre_tags": sorted(genre_tags),
        "mood_tags": sorted(mood_tags),
        "locked_clusters": sorted(locked_clusters),
        "total_clusters": total_clusters,
        "prompt_version": PROMPT_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_disk_cache():
    """Returns the cached {"key": ..., "clusters": ..., "mapping": ...} dict,
    or None if the file doesn't exist / can't be read / is malformed."""
    try:
        with open(DISK_CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _save_disk_cache(key, clusters, mapping):
    """Best-effort write — if the directory isn't mounted/writable, this
    silently no-ops rather than breaking the build (disk persistence is a
    nice-to-have, not a requirement for the app to function)."""
    try:
        os.makedirs(os.path.dirname(DISK_CACHE_PATH), exist_ok=True)
        with open(DISK_CACHE_PATH, "w") as f:
            json.dump({"key": key, "clusters": clusters, "mapping": mapping}, f)
    except Exception:
        pass


def get_cached_artists(music_section, debug=None, force_refresh=False):
    """
    Shared front door for the full-library artist scan (see
    _ARTIST_SCAN_CACHE above) — every function in this file that needs
    "every artist in the library" should call this instead of
    music_section.searchArtists() directly, so repeated calls within one
    build (or across the Scan Tags -> Configure -> Build steps in the UI)
    reuse the same fetch instead of re-hitting Plex each time.
    force_refresh=True bypasses the cache and re-scans regardless of TTL —
    wired to the UI's "Force rescan library" option for when the user knows
    their library changed mid-session.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    section_key = str(getattr(music_section, 'key', None) or getattr(music_section, 'title', 'default'))
    cached = _ARTIST_SCAN_CACHE.get(section_key)
    now = time.time()
    if not force_refresh and cached and (now - cached["fetched_at"]) < ARTIST_SCAN_TTL_SECONDS:
        d(f"Reusing cached artist scan for `{section_key}` — {len(cached['artists'])} artists, "
          f"{int(now - cached['fetched_at'])}s old.")
        return cached["artists"]
    try:
        artists = music_section.searchArtists()
    except Exception as e:
        d(f"❌ Artist scan failed: `{e}` — {'falling back to stale cache' if cached else 'no prior cache to fall back on'}.")
        artists = cached["artists"] if cached else []
    _ARTIST_SCAN_CACHE[section_key] = {"artists": artists, "fetched_at": now}
    d(f"Scanned `{section_key}`: {len(artists)} artists (fresh Plex fetch).")
    return artists


def clear_artist_scan_cache():
    """Drops the in-process artist scan cache (see _ARTIST_SCAN_CACHE) so
    the next call to get_cached_artists does a fresh Plex fetch regardless
    of TTL. Wired to the UI's 'Force rescan library' checkbox — simpler
    than threading a force_refresh flag through every function that
    ultimately calls get_cached_artists."""
    _ARTIST_SCAN_CACHE.clear()


def clear_all_caches(debug=None):
    """
    Full reset: drops every cache this file keeps, so the next build starts
    completely from scratch — the in-process artist scan (clear_artist_scan_cache),
    the per-artist sonic-profile disk cache (ARTIST_PROFILE_CACHE_PATH), the
    tag->cluster mapping disk cache (DISK_CACHE_PATH), and the last saved
    cluster-results disk cache (CLUSTER_RESULTS_CACHE_PATH). Wired to the
    UI's 'Clean slate' button.

    Useful any time you want to be certain nothing stale is being reused —
    e.g. after a weighting/logic change, or just to sanity-check a result
    by rebuilding with zero assumptions carried over. Deleting these files
    does NOT undo anything already saved as a Plex playlist; it only clears
    this app's own intermediate caches. Best-effort: any individual
    deletion failing (e.g. file already gone) is silently skipped, same as
    the rest of this file's caching.

    Returns {"cleared": [...], "missing": [...], "errors": [(path, error), ...]}
    so the caller can show an explicit, self-contained confirmation in the
    UI rather than relying on the debug log being open.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    clear_artist_scan_cache()
    cleared, missing, errors = [], [], []
    for path in (ARTIST_PROFILE_CACHE_PATH, DISK_CACHE_PATH, CLUSTER_RESULTS_CACHE_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
                cleared.append(path)
            else:
                missing.append(path)
        except Exception as e:
            errors.append((path, str(e)))
            d(f"\u26a0\ufe0f Couldn't remove `{path}`: `{e}`")
    d(f"**Clean slate:** in-process artist scan cleared; disk caches removed: "
      f"{', '.join(cleared) if cleared else '(none existed)'}.")
    return {"cleared": cleared, "missing": missing, "errors": errors}


def get_all_genre_and_mood_tags(music_section):
    """
    Collects every distinct genre AND mood tag in the library at album
    level, falling back to artist-level genre tags for albums that have
    none (moods live on albums/tracks, not artists, so there's no artist
    fallback for those). Returns two sorted lists: (genre_tags, mood_tags).

    Album-level is more precise than artist-level alone (an artist can span
    sub-genres across different albums), so genre prefers album tags and
    only falls back to artist tags when an album has none.
    """
    genre_tags = set()
    mood_tags = set()
    artists = get_cached_artists(music_section)
    for artist in artists:
        artist_genres = {g.tag for g in getattr(artist, 'genres', [])}
        try:
            albums = artist.albums()
        except Exception:
            albums = []
        had_album_genres = False
        for album in albums:
            album_genres = {g.tag for g in getattr(album, 'genres', [])}
            album_moods = {m.tag for m in getattr(album, 'moods', [])}
            if album_genres:
                had_album_genres = True
                genre_tags |= album_genres
            mood_tags |= album_moods
        if not had_album_genres:
            # No album carried genre tags at all — fall back to the artist's.
            genre_tags |= artist_genres
    return sorted(genre_tags), sorted(mood_tags)


def build_tag_cluster_mapping(genre_tags, mood_tags, locked_clusters, total_clusters, api_key, model=None,
                               timeout=GEMINI_TIMEOUT_SECONDS, max_retries=GEMINI_MAX_RETRIES):
    """
    One Gemini call that both (a) determines the cluster names (locked ones
    kept as-is, others invented/derived), and (b) maps every genre AND mood
    tag to exactly one final cluster name. Returns (cluster_names,
    tag_to_cluster dict) — the dict has both genre and mood tag strings as
    keys, so callers don't need to know which kind a tag was.

    locked_clusters: list of cluster names the user wants kept as-is, e.g.
        ["Metal", "Fast Paced"] — these are NOT genre tags themselves, they're
        target buckets that must exist if relevant tags are found for them.

    total_clusters: int — the MAXIMUM number of clusters to produce (a
        ceiling, not a target Gemini is forced to hit exactly). Gemini may
        return fewer if the tag list doesn't naturally support that many
        distinct, coherent groups. Tags that don't clearly belong anywhere
        are left unmapped rather than stretched to fill out unused
        cluster slots — they fall through to "Unsorted" downstream.

    timeout: seconds to wait for a response before retrying. Defaults to
        GEMINI_TIMEOUT_SECONDS (180s) since a large genre+mood tag list can
        take a while for the model to fully classify in one pass.
    max_retries: number of additional attempts after the first, with a
        short backoff between them, to smooth over transient timeouts or
        API hiccups without the user needing to manually click "Build"
        again.
    """
    model = model or DEFAULT_GEMINI_MODEL

    remaining = max(total_clusters - len(locked_clusters), 0)
    count_instruction = f"""You are organizing a music library into AT MOST {total_clusters} clusters
total. This is a ceiling, not a target — use fewer if the tags below don't
naturally support that many distinct, coherent groups. Do not invent vague
or overlapping clusters just to use up the full count.

These {len(locked_clusters)} cluster names are FIXED and must be used exactly
as given, if any tags genuinely belong under them:
{locked_clusters}

Beyond those, invent UP TO {remaining} additional cluster name(s) that best
cover the remaining tags below (genre families or moods not covered by the
fixed clusters)."""

    prompt = f"""You are organizing a music library's genre and mood tags into clusters.
Clusters can represent genre families, moods/vibes, or both — whatever best
groups the tags below.

{count_instruction}

CRITICAL RULES for assigning tags to clusters:
1. Group a genre with ALL of its subgenres, styles, and variants under one
   umbrella ONLY when they are truly the same family. For example, if a
   cluster is "Metal", every metal-family tag — metalcore, black metal,
   death metal, doom metal, nu-metal, thrash, classic metal, etc. — belongs
   in "Metal". But do NOT extend this to merging genuinely distinct
   families (Metal ≠ Punk ≠ Classic Rock ≠ Indie Rock) just to reduce the
   cluster count — keep genuinely distinct genre families in their own
   clusters.
2. Do NOT cluster or split tags by nationality, region, or language
   (e.g. "German", "Turkish", "Korean", "French") unless one of the FIXED
   cluster names explicitly asks for that. A tag like "German Rock" belongs
   with other Rock tags in the Rock cluster, not grouped with unrelated
   genres just because they share a nationality modifier.
3. Mood tags (e.g. "Aggressive", "Chill", "Melancholic", "Upbeat") describe
   vibe, not genre — only group two mood tags together, or a mood with a
   genre, if they are actually thematically related. Do not merge unrelated
   items just because you're running short on distinct clusters.
4. Only assign a tag to a cluster if it CLEARLY and confidently belongs
   there. If a tag is vague, ambiguous, or doesn't fit any cluster well,
   LEAVE IT OUT of the mapping entirely rather than forcing it into the
   closest-ish bucket — it's far better to skip an uncertain tag than to
   stretch a cluster's definition to absorb it. Omitted tags are handled
   separately downstream, so do not invent a catch-all cluster for them.
5. Be very conservative with invented mood/vibe cluster names (e.g. "Power
   & Edge", "High Energy"). Only put a genre tag in such a cluster if that
   ENTIRE genre family is unambiguously that vibe (e.g. "Death Metal" or
   "Grindcore" really is inherently aggressive/high-energy). Do NOT put a
   genre tag there just because SOME artists or songs in that genre can
   sound powerful/energetic — genres like Folk Rock, Pop Rock, Anthemic
   Indie, Electropop, or Singer-Songwriter have plenty of loud/driving
   moments but are NOT inherently "power/edge" as a genre family, and
   belong with their own genre cluster instead. When genuinely unsure
   whether a tag belongs in a vague vibe cluster vs. its own genre family,
   choose the genre family — a clear, narrow cluster beats a vague one
   that quietly absorbs unrelated music.
6. Avoid inventing two-word abstract-adjective cluster names (e.g. "Power &
   Edge", "Deep & Dark") unless the tag list genuinely has no better,
   clearer organizing concept — these vague names are the most likely to
   accidentally sweep in mismatched genres. Prefer a specific genre-family
   name where one fits.
7. Don't create a cluster for a thin sliver of the tag list. Each invented
   (non-locked) cluster should be backed by a real, substantial group of
   tags — not just one or two obscure/rare ones. If only a handful of tags
   would go into a cluster, fold them into the closest genuinely-related
   existing cluster instead of standing up a fragile one-off bucket. It is
   completely fine — and expected — to end up with fewer clusters than the
   ceiling for this reason.

Genre tags to classify:
{genre_tags}

Mood tags to classify:
{mood_tags}

Respond with ONLY a JSON object in this exact shape, nothing else, no markdown
fences, no commentary:
{{
  "clusters": ["Cluster1", "Cluster2", ...],   // final cluster names actually used, in any order
  "mapping": {{"tag1": "Cluster1", "tag2": "Cluster2", ...}}  // only tags with a clear, confident cluster — omit uncertain ones
}}"""

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=timeout,
            )
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()
            parsed = json.loads(text)
            return parsed["clusters"], parsed["mapping"]
        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))  # brief backoff: 2s, 4s, ...
                continue
        except (requests.exceptions.RequestException, KeyError, json.JSONDecodeError) as e:
            # Non-timeout failures (bad key, malformed response, etc.) aren't
            # helped by retrying with the same request — fail fast instead.
            raise

    total_tags = len(genre_tags) + len(mood_tags)
    raise TimeoutError(
        f"Gemini didn't respond within {timeout}s after {max_retries + 1} attempt(s), "
        f"classifying {total_tags} tags. If this keeps happening, try a smaller "
        f"library section, fewer total_clusters, or check aistudio.google.com for "
        f"an API outage."
    ) from last_error


def assign_artist_cluster(artist_genre_tags, tag_mapping):
    """
    Same voting logic as assign_track_cluster, but for an artist's own
    genre tags directly (no album/mood involved) — used by the Library
    Galaxy tab to color each artist node by cluster without re-deriving a
    per-track assignment for every one of their tracks.
    """
    votes = defaultdict(int)
    for tag in artist_genre_tags:
        cluster = tag_mapping.get(tag)
        if cluster:
            votes[cluster] += 1
    if not votes:
        return "Unsorted"
    return max(votes, key=votes.get)


def assign_track_cluster(track, tag_mapping):
    """
    Assigns a single track to a cluster using BOTH its album's genre tags
    and its album's mood tags, falling back to the artist's genre tags when
    the album has no genre tags (moods have no artist-level fallback, since
    Plex only tags moods at album/track level). Ties are broken by whichever
    cluster gets the most combined tag "votes"; tracks with no identifiable
    tags land in "Unsorted" rather than being forced into one of the real
    clusters.
    """
    votes = defaultdict(int)
    for tag in _track_raw_tags(track):
        cluster = tag_mapping.get(tag)
        if cluster:
            votes[cluster] += 1

    if not votes:
        return "Unsorted"
    return max(votes, key=votes.get)


@st.cache_data(show_spinner=False)
def _cached_tag_cluster_mapping(genre_tags_tuple, mood_tags_tuple, locked_clusters_tuple,
                                 total_clusters, api_key, prompt_version):
    """
    In-memory cached wrapper around build_tag_cluster_mapping. Cache key
    includes prompt_version so a prompt-logic fix invalidates old cached
    mappings automatically. This only lives for the process's lifetime —
    see get_tag_cluster_mapping() for the disk-backed layer in front of it
    that survives restarts.
    """
    return build_tag_cluster_mapping(
        list(genre_tags_tuple), list(mood_tags_tuple), list(locked_clusters_tuple),
        total_clusters, api_key
    )


def get_tag_cluster_mapping(genre_tags, mood_tags, locked_clusters, total_clusters, api_key, force_remap=False):
    """
    Two-layer cache in front of the actual Gemini call:
      1. In-memory (st.cache_data, via _cached_tag_cluster_mapping) — fast,
         but wiped on every process restart.
      2. Disk (DISK_CACHE_PATH) — slower, but survives restarts/rebuilds as
         long as the path is on a mounted volume.

    On force_remap, both layers are bypassed and the disk file is
    overwritten with the fresh result. Otherwise: try disk first (covers
    the "just restarted the container" case), then fall through to the
    in-memory cached call (which itself calls Gemini only if nothing
    matches).
    """
    key = _disk_cache_key(genre_tags, mood_tags, locked_clusters, total_clusters)

    if force_remap:
        _cached_tag_cluster_mapping.clear()
    else:
        cached = _load_disk_cache()
        if cached and cached.get("key") == key:
            return cached["clusters"], cached["mapping"]

    clusters, mapping = _cached_tag_cluster_mapping(
        tuple(sorted(genre_tags)), tuple(sorted(mood_tags)), tuple(sorted(locked_clusters)),
        total_clusters, api_key, PROMPT_VERSION
    )
    _save_disk_cache(key, clusters, mapping)
    return clusters, mapping


def suggest_cluster_names(genre_tags, mood_tags, total_clusters, api_key):
    """
    Zero-commitment preview: asks Gemini to propose all total_clusters names
    from scratch (locked_clusters=[]) and also returns the tag mapping it
    produced along the way. Callers typically only show the cluster names
    for editing, but the mapping is returned too so that if the user
    accepts the suggestions unchanged, build_genre_clusters can reuse it
    directly instead of paying for a second Gemini call.
    """
    return build_tag_cluster_mapping(genre_tags, mood_tags, [], total_clusters, api_key)


def build_dry_run_mapping(genre_tags, mood_tags, locked_clusters, total_clusters):
    """
    Zero-cost, offline stand-in for build_tag_cluster_mapping — no network
    call, no API key needed. Uses simple keyword matching against the
    locked cluster names (e.g. "Metal" matches any tag containing "metal")
    and dumps everything else round-robin into filler clusters named
    "Unmapped 1", "Unmapped 2", etc. up to total_clusters.

    This is NOT meant to produce good clusters — it exists purely so you
    can test the rest of the pipeline (track assignment, ranking, the UI,
    saving playlists) without spending any Gemini tokens. Swap back to the
    real build_tag_cluster_mapping (via the normal Build/Refresh flow) once
    you're ready to see real results.
    """
    remaining = max(total_clusters - len(locked_clusters), 0)
    filler_clusters = [f"Unmapped {i + 1}" for i in range(remaining)]
    clusters = list(locked_clusters) + filler_clusters

    mapping = {}
    all_tags = list(genre_tags) + list(mood_tags)
    for i, tag in enumerate(all_tags):
        tag_lower = tag.lower()
        matched = next((lc for lc in locked_clusters if lc.lower() in tag_lower), None)
        if matched:
            mapping[tag] = matched
        elif filler_clusters:
            mapping[tag] = filler_clusters[i % len(filler_clusters)]
        else:
            mapping[tag] = locked_clusters[0] if locked_clusters else "Unsorted"

    return clusters, mapping


def _cap_per_artist(tracks, max_per_artist):
    """
    Filters a track list, preserving its existing order/priority, so no
    more than max_per_artist tracks from the same artist survive. None or
    0 disables the cap (returns the list unchanged). This is a hard cap —
    if capping leaves a list shorter than what was wanted, the caller gets
    fewer tracks rather than the cap being relaxed to hit a target count;
    that's the whole point of "max N per artist" as a diversity control.
    """
    if not max_per_artist:
        return list(tracks)
    result = []
    counts = defaultdict(int)
    for t in tracks:
        artist_key = getattr(t, 'grandparentRatingKey', None)
        if artist_key is not None and counts[artist_key] >= max_per_artist:
            continue
        result.append(t)
        if artist_key is not None:
            counts[artist_key] += 1
    return result


def _select_popular(tracks, n, max_per_artist=None):
    """Top-N by viewCount, falling back to a random sample if nothing in
    the pool has ever been played (same pattern as get_top_tracks_for_artist).
    max_per_artist (None or 0 = no cap) limits how many tracks from the
    same artist can land in the result — enforced as a hard cap via
    _cap_per_artist, applied to the popularity-ranked (or shuffled, if
    unplayed) order so the kept track per artist is still whichever was
    most popular."""
    if not tracks or n <= 0:
        return []
    ranked = sorted(tracks, key=lambda t: getattr(t, 'viewCount', 0) or 0, reverse=True)
    top_view_count = getattr(ranked[0], 'viewCount', 0) or 0
    if top_view_count <= 0:
        pool = list(tracks)
        random.shuffle(pool)
        ranked = pool
    if max_per_artist:
        ranked = _cap_per_artist(ranked, max_per_artist)
    return ranked[:n]


def _select_sonic(seed_tracks, exclude_keys, n, cluster_name, tag_mapping, debug=None):
    """
    Seeds sonicallySimilar() calls from up to 6 of the cluster's popular
    tracks. Candidates are FILTERED to only those that themselves tag-map
    into the same cluster_name (via assign_track_cluster) — sonic
    similarity alone often crosses genre lines (e.g. an acoustic cover, a
    shared production style), which is what was diluting cluster purity
    before this filter existed. Anything that doesn't independently belong
    to this cluster is discarded rather than included "for variety".
    """
    d = debug.write if debug else (lambda *a, **k: None)
    if not seed_tracks or n <= 0:
        return []

    candidates = []
    seeds = random.sample(seed_tracks, min(len(seed_tracks), 6))
    for seed in seeds:
        seed_name = f"{getattr(seed, 'grandparentTitle', 'Unknown')} - {seed.title}"
        try:
            matches = seed.sonicallySimilar(limit=15)
        except Exception as e:
            d(f"└ ❌ Sonic lookup failed for `{seed_name}`: `{e}`")
            continue
        for m in matches:
            rk = getattr(m, 'ratingKey', None)
            if not rk or rk in exclude_keys:
                continue
            if assign_track_cluster(m, tag_mapping) != cluster_name:
                continue
            setattr(m, 'recommendation_type', 'Sonic Match')
            setattr(m, 'match_percent', get_sonic_match_percent(m))
            setattr(m, 'match_seed', seed_name)
            candidates.append(m)
            exclude_keys.add(rk)

    random.shuffle(candidates)
    return candidates[:n]


def _select_related(seed_tracks, music_section, plex, exclude_keys, n, cluster_name, tag_mapping, debug=None):
    """
    Finds related artists for up to 6 of the cluster's popular tracks (via
    each seed's artist.similar()), pulls each related artist's top tracks.
    Same genre-purity filter as _select_sonic: a candidate track only
    counts if it independently tag-maps into cluster_name — "related artist"
    per Plex doesn't mean "same genre", so without this filter a Metal
    cluster could easily pull in a related artist's non-metal tracks.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    if not seed_tracks or n <= 0:
        return []

    candidates = []
    seen_artist_keys = set()
    seeds = random.sample(seed_tracks, min(len(seed_tracks), 6))

    for seed in seeds:
        artist_key = getattr(seed, 'grandparentRatingKey', None)
        if not artist_key:
            continue
        try:
            artist = plex.fetchItem(artist_key)
            similar_artists = artist.similar() if hasattr(artist, 'similar') else []
            if callable(similar_artists):
                similar_artists = similar_artists()
        except Exception as e:
            d(f"└ ❌ Similar-artist fetch failed for artist key {artist_key}: `{e}`")
            continue

        for sim in (similar_artists or []):
            name = getattr(sim, 'tag', None)
            if not name:
                continue
            try:
                found = music_section.searchArtists(title=name)
                if not found:
                    continue
                real_artist = found[0]
                if real_artist.ratingKey in seen_artist_keys:
                    continue
                seen_artist_keys.add(real_artist.ratingKey)
                top_tracks = get_top_tracks_for_artist(real_artist, limit=4, per_album_sample=2)
                for t in top_tracks:
                    rk = getattr(t, 'ratingKey', None)
                    if not rk or rk in exclude_keys:
                        continue
                    if assign_track_cluster(t, tag_mapping) != cluster_name:
                        continue
                    setattr(t, 'recommendation_type', f'Related Artist ({real_artist.title})')
                    candidates.append(t)
                    exclude_keys.add(rk)
            except Exception as e:
                d(f"└ ❌ Related artist `{name}` failed: `{e}`")

    random.shuffle(candidates)
    return candidates[:n]


def _blend_cluster_tracks(cluster_name, tracks_pool, music_section, plex, total_n, tag_mapping,
                           max_per_artist=None, debug=None):
    """
    Builds a cluster's final track list as a blend of three sources, split
    roughly into thirds of total_n:
      1. Popular   — top plays from the cluster's own pooled tracks.
      2. Sonic     — sonically similar tracks seeded from the popular picks,
                      filtered to only matches that also tag-map into this
                      same cluster (genre-purity filter — see _select_sonic).
      3. Related   — top tracks from related artists of the popular picks,
                      same genre-purity filter (see _select_related).

    If the genre-purity filter leaves the sonic/related buckets short (which
    is expected — most sonic/related candidates for a narrow genre will get
    filtered out), backfills from whatever's left in the cluster's own pool
    rather than pulling in off-genre tracks just to hit total_n.

    max_per_artist (None or 0 = no cap) caps how many tracks from the same
    artist can appear in the FINAL blended list — applied once across all
    three sources combined (plus backfill), since an artist could otherwise
    show up via popular AND sonic AND related independently. A hard cap:
    if a cluster doesn't have enough distinct artists to hit total_n under
    the limit, the result is shorter than total_n rather than the cap
    being relaxed.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    popular_n = total_n // 3
    sonic_n = total_n // 3
    related_n = total_n - popular_n - sonic_n

    popular = _select_popular(tracks_pool, popular_n)
    for t in popular:
        setattr(t, 'recommendation_type', f'{cluster_name} (Popular)')
    selected_keys = {getattr(t, 'ratingKey', None) for t in popular}

    sonic = _select_sonic(popular, selected_keys, sonic_n, cluster_name, tag_mapping, debug=debug)
    related = _select_related(popular, music_section, plex, selected_keys, related_n, cluster_name, tag_mapping, debug=debug)

    combined = popular + sonic + related
    d(f"└ `{cluster_name}` blend: {len(popular)} popular, {len(sonic)} sonic (genre-filtered), "
      f"{len(related)} related (genre-filtered).")

    if len(combined) < total_n:
        remaining_pool = [t for t in tracks_pool if getattr(t, 'ratingKey', None) not in selected_keys]
        random.shuffle(remaining_pool)
        needed = total_n - len(combined)
        backfill = remaining_pool[:needed]
        for t in backfill:
            if not getattr(t, 'recommendation_type', None):
                setattr(t, 'recommendation_type', f'{cluster_name} (More)')
        combined += backfill
        d(f"└ `{cluster_name}` backfilled {len(backfill)} more from the pool to reach {total_n}.")

    if max_per_artist:
        before = len(combined)
        combined = _cap_per_artist(combined, max_per_artist)
        if len(combined) < before:
            d(f"└ `{cluster_name}` capped to \u2264{max_per_artist} tracks/artist: "
              f"{before} \u2192 {len(combined)} tracks.")

    random.shuffle(combined)
    return combined[:total_n]


def _sonic_thresholds_for_weight(sonic_weight):
    """
    Maps a 0.0-1.0 sonic_weight dial to neighbor-vote thresholds for
    RECOVERING Unsorted tracks. 0.0 keeps the original conservative
    defaults; 1.0 relaxes them to the most permissive floor still
    considered safe (never fully disabled).

    Returns (min_votes, min_margin, min_share).
    """
    w = max(0.0, min(1.0, sonic_weight))
    min_votes = round(REFINE_MIN_NEIGHBOR_VOTES - w * 3)     # 6  -> 3
    min_margin = round(REFINE_MIN_VOTE_MARGIN - w * 2)       # 4  -> 2
    min_share = REFINE_MIN_VOTE_SHARE - w * 0.15             # .6 -> .45
    return max(min_votes, 2), max(min_margin, 1), max(min_share, 0.4)


def _sonic_thresholds_for_reassignment(sonic_weight):
    """
    Same idea, but for PULLING a track OUT of a cluster it already tag-
    matched into — deliberately a stricter floor than recovery thresholds
    at every weight level, since a tagged track already has real evidence
    behind its current cluster and should only move on genuinely lopsided
    sonic consensus, not just a mild lean.
    """
    w = max(0.0, min(1.0, sonic_weight))
    min_votes = round(REFINE_MIN_NEIGHBOR_VOTES - w * 2)     # 6  -> 4
    min_margin = round((REFINE_MIN_VOTE_MARGIN + 2) - w * 2) # 6  -> 4
    min_share = (REFINE_MIN_VOTE_SHARE + 0.15) - w * 0.15    # .75 -> .6
    return max(min_votes, 3), max(min_margin, 2), max(min_share, 0.55)


def refine_unsorted_via_sonic_neighbors(pools, sonic_weight=0.0, reassign_tagged=False,
                                         max_tracks_to_check=REFINE_MAX_TRACKS,
                                         propagation_rounds=2, debug=None):
    """
    Uses Plex's own sonic-similarity graph (audio fingerprint matching, not
    artist metadata) as a second signal alongside genre/mood tags.

    This runs WEIGHTED, MULTI-ROUND label propagation over a cached
    similarity graph, rather than a single one-shot vote against a frozen
    tag-based snapshot:

    - WEIGHTED: each neighbor's vote is scaled by its actual sonic match
      score (via get_sonic_match_percent), not counted as a flat "1 vote"
      regardless of how close the match actually is.
    - MULTI-ROUND: candidate tracks' labels are re-evaluated over
      `propagation_rounds` passes, using each other's UPDATED labels from
      the previous round. This lets sonic influence propagate transitively
      through the candidate pool (track A can pull track B, which in turn
      helps pull track C, across rounds) instead of only ever comparing a
      track to the original tag-based labels once. Each track's own sonic
      neighbor list is fetched only once total (cached) no matter how many
      rounds run, so this doesn't multiply the Plex API cost.

    Two things happen, both driven by `sonic_weight`, and gated per-track
    by its ORIGINAL tag-based cluster (frozen for the whole run, so the
    bar a track has to clear doesn't shift as other tracks flip around
    it):

    1. RECOVERY: tracks that started as "Unsorted" get the more permissive,
       weight-scaled bar (see _sonic_thresholds_for_weight).
    2. REASSIGNMENT (opt-in via reassign_tagged=True): tracks that started
       with a real tag-based cluster get the stricter bar
       (_sonic_thresholds_for_reassignment) before sonic consensus is
       allowed to move them elsewhere.

    Deliberately operates per TRACK, not per artist — an artist's whole
    discography moving off a handful of "Similar Artist" crossover links
    is how things like an Eminem track ending up in a Metal cluster
    happen. Per-track voting (even with propagation) means influence has
    to actually accumulate through real neighbor agreement, not a single
    noisy link.

    Candidates are capped at max_tracks_to_check total (Unsorted tracks
    prioritized first, then tagged tracks by play count if reassignment is
    on) since each candidate costs one Plex API call (once, cached).

    Mutates and returns `pools` in place.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    has_unsorted = bool(pools.get("Unsorted"))
    if not has_unsorted and not reassign_tagged:
        return pools

    # Frozen baseline: every track's tag-based cluster before any sonic
    # influence. Used both as the initial label snapshot AND to decide,
    # per track, which threshold tier (recovery vs reassignment) applies —
    # that decision must not shift mid-run just because a track's label
    # happens to change in an earlier round.
    baseline_cluster_by_key = {}
    track_by_key = {}
    for cluster_name, tracks in pools.items():
        for t in tracks:
            key = getattr(t, 'ratingKey', None)
            baseline_cluster_by_key[key] = cluster_name
            track_by_key[key] = t

    unsorted_candidates = sorted(
        pools.get("Unsorted", []), key=lambda t: getattr(t, 'viewCount', 0) or 0, reverse=True
    )
    candidates = list(unsorted_candidates)
    if reassign_tagged:
        tagged_candidates = [
            t for cluster_name, tracks in pools.items() if cluster_name != "Unsorted" for t in tracks
        ]
        tagged_candidates.sort(key=lambda t: getattr(t, 'viewCount', 0) or 0, reverse=True)
        candidates += tagged_candidates

    if len(candidates) > max_tracks_to_check:
        d(f"**Sonic-neighbor pass (weight={sonic_weight:.2f}, {propagation_rounds} round(s)):** "
          f"capping to the {max_tracks_to_check} highest-priority of {len(candidates)} candidate tracks.")
        candidates = candidates[:max_tracks_to_check]
    else:
        d(f"**Sonic-neighbor pass (weight={sonic_weight:.2f}, {propagation_rounds} round(s)):** "
          f"checking sonic neighbors for {len(candidates)} candidate tracks"
          f"{' (recovery + tagged reassignment)' if reassign_tagged else ' (recovery only)'}.")

    candidate_keys = [getattr(t, 'ratingKey', None) for t in candidates]

    # Fetch + cache each candidate's sonic neighbors ONCE regardless of how
    # many propagation rounds run. Weight each neighbor by its actual match
    # score rather than counting every match as a flat "1 vote".
    neighbor_cache = {}
    for track, key in zip(candidates, candidate_keys):
        try:
            matches = track.sonicallySimilar(limit=20)
        except Exception:
            neighbor_cache[key] = []
            continue
        weighted_neighbors = []
        for m in matches:
            mk = getattr(m, 'ratingKey', None)
            if not mk:
                continue
            pct = get_sonic_match_percent(m)
            weight = (pct / 100.0) if pct is not None else 0.5
            weighted_neighbors.append((mk, weight))
        neighbor_cache[key] = weighted_neighbors

    recover_min_votes, recover_min_margin, recover_min_share = _sonic_thresholds_for_weight(sonic_weight)
    reassign_min_votes, reassign_min_margin, reassign_min_share = _sonic_thresholds_for_reassignment(sonic_weight)

    # current_labels starts as the tag-based baseline and evolves across
    # rounds ONLY for candidate tracks — everything else stays fixed, so
    # non-candidate neighbors always vote with their real tag-based label.
    current_labels = dict(baseline_cluster_by_key)

    for round_num in range(propagation_rounds):
        next_labels = dict(current_labels)
        changed_this_round = 0
        for key in candidate_keys:
            original_cluster = baseline_cluster_by_key.get(key, "Unsorted")
            neighbors = neighbor_cache.get(key, [])
            if not neighbors:
                continue

            weighted_votes = defaultdict(float)
            for nk, w in neighbors:
                ncluster = current_labels.get(nk)
                if ncluster and ncluster != "Unsorted":
                    weighted_votes[ncluster] += w
            if not weighted_votes:
                continue

            sorted_votes = sorted(weighted_votes.values(), reverse=True)
            top_votes = sorted_votes[0]
            runner_up_votes = sorted_votes[1] if len(sorted_votes) > 1 else 0
            total_votes = sum(weighted_votes.values())
            vote_share = top_votes / total_votes if total_votes else 0
            new_cluster = max(weighted_votes, key=weighted_votes.get)

            if original_cluster == "Unsorted":
                if (top_votes < recover_min_votes
                        or top_votes < runner_up_votes + recover_min_margin
                        or vote_share < recover_min_share):
                    continue
            else:
                if not reassign_tagged or new_cluster == original_cluster:
                    continue
                if (top_votes < reassign_min_votes
                        or top_votes < runner_up_votes + reassign_min_margin
                        or vote_share < reassign_min_share):
                    continue

            if next_labels.get(key) != new_cluster:
                next_labels[key] = new_cluster
                changed_this_round += 1

        current_labels = next_labels
        d(f"└ Round {round_num + 1}/{propagation_rounds}: {changed_this_round} label change(s).")
        if changed_this_round == 0:
            break

    # Apply the final labels: move any candidate whose label differs from
    # its tag-based baseline into its new cluster.
    recovered_count = 0
    moved_count = 0
    moved_from = defaultdict(set)
    moved_to = defaultdict(list)
    for key in candidate_keys:
        original_cluster = baseline_cluster_by_key.get(key, "Unsorted")
        final_cluster = current_labels.get(key, original_cluster)
        if final_cluster == original_cluster:
            continue
        moved_from[original_cluster].add(key)
        moved_to[final_cluster].append(track_by_key[key])
        if original_cluster == "Unsorted":
            recovered_count += 1
        else:
            moved_count += 1

    for from_cluster, keys in moved_from.items():
        pools[from_cluster] = [t for t in pools.get(from_cluster, []) if getattr(t, 'ratingKey', None) not in keys]
    for to_cluster, tracks in moved_to.items():
        pools.setdefault(to_cluster, []).extend(tracks)

    d(f"└ Recovered {recovered_count} Unsorted track(s); reassigned {moved_count} already-tagged "
      f"track(s) via sonic-neighbor propagation (weight={sonic_weight:.2f}).")
    return pools


def apply_cluster_merge_plan(raw_results, raw_tag_mapping, merge_plan):
    """
    Combines fine-grained clusters (e.g. from Auto mode) into user-chosen
    groups — the "phase 2" of the two-step workflow: build natural, narrow
    clusters first, then let the user decide which of THOSE to combine into
    broader buckets, rather than forcing Gemini to guess the right
    granularity upfront.

    Pure local operation — no Plex or Gemini calls, so it's free and
    instant, and can be re-applied with a different plan at any time
    without rebuilding from scratch.

    raw_results: {cluster_name: [tracks]} — the untouched fine-grained
        build output (always kept around separately so merges are
        non-destructive and re-pickable).
    raw_tag_mapping: the tag->cluster dict from the fine-grained build —
        remapped here too, so anything downstream that reads it (e.g. the
        Library Galaxy tab's coloring) reflects the merged groups.
    merge_plan: list of {"members": [cluster_name, ...], "new_name": str}
        dicts. Any raw cluster name NOT listed in any group's "members"
        passes through unchanged under its original name.

    Returns (merged_results, merged_tag_mapping). Tracks are deduplicated
    by ratingKey within a merged group (the same track could theoretically
    appear in two narrow clusters via the sonic/related blend, though the
    genre-purity filter makes that rare) and shuffled so the merged list
    isn't just "all of cluster A then all of cluster B" in a visible block.
    """
    member_to_new_name = {}
    for group in merge_plan:
        for member in group["members"]:
            member_to_new_name[member] = group["new_name"]

    merged_results = defaultdict(list)
    seen_keys_per_cluster = defaultdict(set)
    for cluster_name, tracks in raw_results.items():
        final_name = member_to_new_name.get(cluster_name, cluster_name)
        for t in tracks:
            rk = getattr(t, 'ratingKey', None)
            if rk in seen_keys_per_cluster[final_name]:
                continue
            seen_keys_per_cluster[final_name].add(rk)
            merged_results[final_name].append(t)

    for tracks in merged_results.values():
        random.shuffle(tracks)

    merged_tag_mapping = {
        tag: member_to_new_name.get(cluster_name, cluster_name)
        for tag, cluster_name in raw_tag_mapping.items()
    }

    return dict(merged_results), merged_tag_mapping


def build_artist_cluster_map(results):
    """
    Given a final {cluster_name: [tracks]} build output (any clustering_mode
    — tags, hybrid, or sonic), derives a {artist_ratingKey: cluster_name}
    map by majority vote of each artist's OWN tracks across whichever
    cluster(s) they ended up in.

    This is what the build actually decided, as opposed to independently
    re-deriving membership from tags via assign_artist_cluster — the two
    agree in Tags mode (where tag IS the membership decision) but can
    genuinely differ in Hybrid/Sonic mode, where membership comes from the
    similarity graph and an artist's tags might disagree with where their
    sonic profile actually landed them. Library Galaxy uses this so its
    node coloring reflects the real build, not a separate tag-only guess.
    """
    votes = defaultdict(lambda: defaultdict(int))
    for cluster_name, tracks in results.items():
        for t in tracks:
            artist_key = getattr(t, 'grandparentRatingKey', None)
            if artist_key is not None:
                votes[artist_key][cluster_name] += 1
    return {artist_key: max(counts, key=counts.get) for artist_key, counts in votes.items()}


# Disk path for the last successful cluster BUILD (as track ratingKeys, not
# live plexapi objects — those can't be JSON-serialized). Distinct from
# CLUSTER_CACHE_PATH (which only caches the tag->cluster mapping, i.e. the
# Gemini call) — this caches the actual final track lists, so re-opening the
# app after a restart can show last time's results immediately instead of
# an empty Results section, without re-running Gemini OR re-scanning Plex
# OR re-analyzing any sonic profiles.
CLUSTER_RESULTS_CACHE_PATH = os.environ.get("CLUSTER_RESULTS_CACHE_PATH", "/app/data/cluster_results.json")


def save_cluster_results_cache(results, tag_mapping, saved_playlists=None):
    """Persists the current cluster build to disk as track ratingKeys, for
    load_cluster_results_cache to rehydrate on a future app start. Call
    this any time `results` changes (fresh build, merge, or manual track
    removal) — best-effort, silently does nothing on failure since this is
    a convenience cache, not a source of truth.

    `saved_playlists` is an optional {cluster_name: playlist_name} map
    recording which clusters have already been saved out as a real Plex
    playlist (and under what name) — persisted alongside the tracks so
    "already saved" status survives an app/container restart too, instead
    of being forgotten the moment the in-memory session ends."""
    try:
        os.makedirs(os.path.dirname(CLUSTER_RESULTS_CACHE_PATH), exist_ok=True)
        payload = {
            "tag_mapping": tag_mapping,
            "clusters": {name: [getattr(t, 'ratingKey', None) for t in tracks] for name, tracks in results.items()},
            "saved_playlists": saved_playlists or {},
        }
        with open(CLUSTER_RESULTS_CACHE_PATH, "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


def load_cluster_results_cache(plex, debug=None):
    """
    Rehydrates the last cached cluster build (see save_cluster_results_cache)
    back into real Plex track objects via plex.fetchItem, so a saved build
    survives an app/container restart instead of forcing a full rebuild.

    Returns (results, tag_mapping, saved_playlists), or (None, None, {}) if
    there's no cache file, it's unreadable, or every cached ratingKey has
    since vanished from the library (tracks removed/library changed) — any
    individual missing ratingKey is just skipped rather than failing the
    whole load, since a few stale entries shouldn't discard an otherwise-
    good cache. `saved_playlists` is the {cluster_name: playlist_name} map
    of clusters already saved to Plex as of the last save, restored here
    too so that status isn't silently forgotten on reload.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    try:
        with open(CLUSTER_RESULTS_CACHE_PATH, "r") as f:
            payload = json.load(f)
    except Exception:
        return None, None, {}

    results = {}
    missing = 0
    for name, keys in payload.get("clusters", {}).items():
        tracks = []
        for key in keys:
            try:
                tracks.append(plex.fetchItem(key))
            except Exception:
                missing += 1
                continue
        if tracks:
            results[name] = tracks
    if not results:
        return None, None, {}
    total = sum(len(t) for t in results.values())
    d(f"**Loaded {total} tracks across {len(results)} clusters from disk cache** "
      f"({missing} stale ratingKey(s) skipped) — no rebuild needed.")
    return results, payload.get("tag_mapping"), payload.get("saved_playlists", {})


def _fold_thin_clusters(pools, clusters, tag_mapping, locked_clusters, debug=None):
    """
    Mechanical balance safety net that runs after real track pooling, since
    only then is it known how many tracks a cluster actually ended up with
    — the Gemini prompt can avoid inventing thin clusters from the tag list
    alone, but a cluster that looked reasonable tag-wise can still turn out
    to cover almost no tracks in this particular library.

    Any non-locked cluster with fewer than MIN_CLUSTER_TRACKS pooled tracks
    is folded into "Unsorted": its tracks move over, its name is dropped
    from the cluster list, and any tag that mapped to it is repointed to
    "Unsorted" too (so Library Galaxy coloring and any other tag_mapping
    consumer stay consistent). Locked clusters are exempt — the user asked
    for them by name, so they're kept even if sparsely populated.

    Runs BEFORE refine_unsorted_via_sonic_neighbors so folded tracks get a
    fair shot at sonic reassignment into a real cluster rather than being
    permanently stuck, and so sonic refinement never reinforces a cluster
    that's about to be folded anyway.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    locked_set = set(locked_clusters)
    thin = [c for c in clusters if c not in locked_set and len(pools.get(c, [])) < MIN_CLUSTER_TRACKS]
    if not thin:
        return pools, clusters, tag_mapping

    thin_set = set(thin)
    for cluster_name in thin:
        tracks = pools.pop(cluster_name, [])
        pools["Unsorted"].extend(tracks)
        d(f"└ Folded thin cluster `{cluster_name}` ({len(tracks)} track(s)) into Unsorted "
          f"— below the {MIN_CLUSTER_TRACKS}-track balance floor.")

    remaining_clusters = [c for c in clusters if c not in thin_set]
    folded_tag_mapping = {
        tag: ("Unsorted" if cluster_name in thin_set else cluster_name)
        for tag, cluster_name in tag_mapping.items()
    }
    return pools, remaining_clusters, folded_tag_mapping


def _load_artist_profile_cache():
    """Returns the {"artist_ratingKey": {...profile...}} disk cache dict, or
    an empty dict if the file doesn't exist / can't be read / is malformed.
    Same best-effort pattern as _load_disk_cache."""
    try:
        with open(ARTIST_PROFILE_CACHE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_artist_profile_cache(cache):
    """Best-effort full-file write of the artist profile cache. Called once
    per build (not once per artist) — build_relational_graph mutates
    the in-memory dict as it goes and this persists the final result."""
    try:
        os.makedirs(os.path.dirname(ARTIST_PROFILE_CACHE_PATH), exist_ok=True)
        with open(ARTIST_PROFILE_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _artist_fingerprint(artist, sample_size, neighbor_limit):
    """Cache-invalidation fingerprint for one artist's profile: changes if
    the artist's own Plex data changes (updatedAt), if the sampling/neighbor
    params change, or if ARTIST_PROFILE_VERSION is bumped. viewCount isn't
    included — a play-count bump alone doesn't necessarily change which
    tracks would be sampled, and re-sonic-analyzing on every single play
    would defeat the point of caching."""
    updated_at = getattr(artist, 'updatedAt', None)
    updated_at = updated_at.isoformat() if hasattr(updated_at, 'isoformat') else str(updated_at)
    payload = json.dumps({
        "updated_at": updated_at,
        "sample_size": sample_size,
        "neighbor_limit": neighbor_limit,
        "version": ARTIST_PROFILE_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _track_genre_and_mood_tags(track):
    """Like _track_raw_tags, but keeps genre and mood as separate sets —
    see get_artist_genre_and_mood_tags for why this split matters for
    naming."""
    try:
        album = track.album()
    except Exception:
        album = None

    genre_tags = {g.tag for g in getattr(album, 'genres', [])} if album else set()
    if not genre_tags:
        try:
            artist = track.artist()
        except Exception:
            artist = None
        genre_tags = {g.tag for g in getattr(artist, 'genres', [])} if artist else set()

    mood_tags = {m.tag for m in getattr(album, 'moods', [])} if album else set()
    return genre_tags, mood_tags


def _track_raw_tags(track):
    """Same tag lookup assign_track_cluster uses (album genre/mood, falling
    back to artist genre when the album has none) — factored out so
    community-naming code can use the identical raw tag set without
    duplicating the fallback logic."""
    genre_tags, mood_tags = _track_genre_and_mood_tags(track)
    return genre_tags | mood_tags


def _most_common_tag(tag_iterable):
    """Plain most-common-tag count across an iterable of individual tags
    (not tag sets — flatten before calling). Returns None if empty."""
    counts = defaultdict(int)
    for t in tag_iterable:
        counts[t] += 1
    if not counts:
        return None
    return max(counts, key=counts.get)


# Tags that are uselessly generic as a CLUSTER NAME no matter how rare they
# happen to be in a given library — unlike the frequency-based genericity
# check in _name_communities (which flags a tag only if it's the dominant
# tag for a large fraction of communities), these carry no descriptive
# content at all even when only one community happens to have them as its
# top tag (e.g. a handful of poorly-tagged artists whose only genre is the
# literal catch-all "Music"). Matched case-insensitively.
ALWAYS_GENERIC_NAME_TAGS = {"music", "other", "unknown", "misc", "miscellaneous", "various", "general"}


def _is_always_generic_tag(tag):
    return tag is not None and tag.strip().lower() in ALWAYS_GENERIC_NAME_TAGS


def _representative_names(items, is_track, max_names=2):
    """
    Picks up to max_names representative ARTIST names for a community,
    ordered by play count (most-played first) so the name reflects its
    most recognizable members. Works for artist-level communities (items
    are Artist objects) or track-level ones (items are Track objects —
    each track's artist() is resolved and de-duplicated first).

    This is the fallback naming signal used whenever tags prove too
    generic to be useful (see _name_communities) — representative artists
    are always specific by construction, since different communities
    necessarily have different members, unlike a genre/mood tag which can
    be applied near-uniformly across a whole library regardless of how
    musically different the artists actually are.
    """
    if is_track:
        seen = {}
        for t in items:
            try:
                artist = t.artist()
            except Exception:
                continue
            key = getattr(artist, 'ratingKey', None)
            if key is not None and key not in seen:
                seen[key] = artist
        artists = list(seen.values())
    else:
        artists = list(items)
    artists.sort(key=lambda a: getattr(a, 'viewCount', 0) or 0, reverse=True)
    return [a.title for a in artists[:max_names] if getattr(a, 'title', None)]


def _llm_name_fallback_communities(fallback_info, api_key, model=None, timeout=60, debug=None):
    """
    One batched Gemini call to name communities whose tag data was too
    generic in this library to produce a good name on its own (see
    _name_communities) — instead of falling back to bare
    "Artist1 & Artist2 Mix" naming, asks for a short, evocative genre/vibe
    name per community based on its representative artists (Gemini's own
    knowledge of them), with whatever weak/generic tags exist offered only
    as a hint, not a constraint.

    Best-effort and silent on failure — no API key, a network error, a
    malformed response, or Gemini simply not recognizing enough of a
    group's artists all just mean "no name for that group," never a
    raised exception. Naming quality is a nice-to-have; it must never be
    able to break a build.

    fallback_info: {community_id: {"artists": [name, ...], "tags": [tag, ...]}}
    Returns {community_id: name} — only for communities Gemini actually
    named confidently; missing keys mean "use the artist-name fallback."
    """
    d = debug.write if debug else (lambda *a, **k: None)
    if not api_key or not fallback_info:
        return {}

    model = model or DEFAULT_GEMINI_MODEL
    order = list(fallback_info.keys())
    items = []
    for cid in order:
        info = fallback_info[cid]
        artists = ", ".join(info["artists"]) or "(unknown artists)"
        tags = ", ".join(info["tags"][:8]) if info["tags"] else "(no clear tags)"
        items.append(f'{{"id": "{cid}", "artists": "{artists}", "hint_tags": "{tags}"}}')

    prompt = f"""You are naming groups of musical artists that were clustered together by
real listening/similarity data. Each group's genre tags were too generic (or
absent) in this library to produce a good name on their own — some groups do
have a mood hint (e.g. "Aggressive", "Dreamy"), but a bare mood by itself is a
weak, repetitive name that doesn't say what the music actually IS. Trust your
own knowledge of these artists' actual sound more than the hint tags, which
may be vague, misleading, or (for mood) just one dimension of a fuller vibe.

For each group below, invent a short, evocative 2-4 word name that captures
what these artists actually sound like — genre-forward when there's a clear
one (e.g. "Epic Folk Metal", "Balkan Gypsy Punk", "Melancholic Post-Rock",
"90s Alt Rock"), or a regional/vibe-forward name when that fits better (e.g.
"Anatolian Vibes", "Iberian Ska-Punk", "Sun-Bleached Surf Rock"). Be specific
and confident — avoid vague filler like "Great Music", "Mixed Bag", or
reusing the mood hint verbatim as the whole name. If you don't recognize
enough of a group's artists to name it well, OMIT that group's id from your
response rather than guessing badly.

Groups:
{chr(10).join(items)}

Respond with ONLY a JSON object mapping id -> name, nothing else, no markdown
fences, no commentary:
{{"0": "Epic Folk Metal", "2": "Anatolian Vibes"}}"""

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
        result = {}
        for cid in order:
            name = parsed.get(str(cid))
            if isinstance(name, str) and name.strip():
                result[cid] = name.strip()
        d(f"**LLM named {len(result)}/{len(fallback_info)} fallback communities** "
          "(replacing bare artist-name labels with real genre/vibe names).")
        return result
    except Exception as e:
        d(f"⚠️ LLM community naming failed ({e}) — using artist-name fallback instead.")
        return {}


def _name_communities(community_members, tag_mapping, is_track, generic_tag_threshold=0.25,
                       api_key=None, debug=None):
    """
    Names every community from a build in ONE pass with real awareness of
    how the library's tags actually behave, instead of naming each
    community in isolation. This directly targets what kept going wrong
    with per-community naming: a genre tag like "Pop/Rock" (or a mood like
    "Aggressive") can be the single most common tag in a very large
    fraction of a library's artists regardless of how different they
    actually sound — some libraries just have coarse or heavily-defaulted
    Plex metadata. Naming each community independently by "its own most
    common tag" then produces a wall of near-identical names, because
    "most common in this community" and "most common practically
    everywhere" end up being the same tag over and over.

    Two passes decide whether a tag can name a community at all (as
    before); a third pass tries an LLM name for anything without a usable
    GENRE label (mood alone is too weak/repetitive a descriptor to use as
    a first choice — see below), and only the bare artist-name format is
    used if that's unavailable too:
      1. For every community, find its dominant genre tag and dominant
         mood tag, AND tally how many DIFFERENT communities share that
         same dominant tag.
      2. A tag is only used as the label if fewer than
         `generic_tag_threshold` (default 25%) of all communities share it
         as their dominant tag — i.e. it's somewhat specific to this
         community, not just the library's overall default — AND it isn't
         on the hardcoded always-generic list (ALWAYS_GENERIC_NAME_TAGS,
         e.g. "Music", "Other") regardless of frequency.
      3. Whatever's left with no usable GENRE (whether or not it has a
         mood) is sent to Gemini in ONE batched call
         (see _llm_name_fallback_communities) — a short, evocative
         genre/vibe/regional name based on the community's representative
         artists (with the mood, if any, offered only as a hint), since
         Gemini's own knowledge of those artists is a better signal than a
         single mood word that's often shared across otherwise very
         different communities. Priority order for the final name is
         genre > LLM name > bare mood > representative artist names —
         mood is a last resort, not a first choice, and is only used when
         Gemini couldn't confidently improve on it (no api key, dry run,
         or it didn't recognize enough of the group).

    community_members: dict {community_id: [artist_or_track, ...]}
    api_key: Gemini API key for pass 3; pass None to skip it entirely
    (bare artist-name fallback used directly, no extra call/cost).
    Returns {community_id: (name, tag_vote_counts)} — tag_vote_counts is
    still the full Gemini tag-cluster vote tally per community, kept for
    the debug log / transparency even when it isn't the naming signal.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    # Pass 1: per-community dominant tags + Gemini vote tally.
    per_community = {}
    genre_users = defaultdict(set)   # dominant_genre -> {community_id, ...}
    mood_users = defaultdict(set)    # dominant_mood -> {community_id, ...}
    for community_id, members in community_members.items():
        if is_track:
            pairs = [_track_genre_and_mood_tags(m) for m in members]
        else:
            pairs = [get_artist_genre_and_mood_tags(m) for m in members]
        all_genres = [g for gs, _ in pairs for g in gs]
        all_moods = [m for _, ms in pairs for m in ms]

        vote_counts = {}
        if tag_mapping:
            votes = defaultdict(int)
            for tag in all_genres + all_moods:
                cluster = tag_mapping.get(tag)
                if cluster:
                    votes[cluster] += 1
            vote_counts = dict(votes)

        dominant_genre = _most_common_tag(all_genres)
        dominant_mood = _most_common_tag(all_moods)
        if dominant_genre:
            genre_users[dominant_genre].add(community_id)
        if dominant_mood:
            mood_users[dominant_mood].add(community_id)

        per_community[community_id] = {
            "dominant_genre": dominant_genre,
            "dominant_mood": dominant_mood,
            "vote_counts": vote_counts,
        }

    n_communities = max(len(community_members), 1)
    max_users = max(1, round(generic_tag_threshold * n_communities))

    def _is_generic(tag, users_by_tag):
        return tag is not None and (len(users_by_tag.get(tag, ())) > max_users or _is_always_generic_tag(tag))

    generic_genres = {t for t in genre_users if _is_generic(t, genre_users)}
    generic_moods = {t for t in mood_users if _is_generic(t, mood_users)}
    if generic_genres or generic_moods:
        d(f"**Tag genericity check:** treating "
          f"{', '.join(sorted(generic_genres)) or '(none)'} as too common to name a genre by, "
          f"and {', '.join(sorted(generic_moods)) or '(none)'} as too common to name a mood by "
          f"(shared by more than {generic_tag_threshold:.0%} of {n_communities} communities) — "
          "affected communities are named from representative artists instead.")

    # Pass 3: try an LLM name from representative artists for anything
    # without a usable GENRE label — not just anything without ANY label.
    # A bare mood ("Aggressive", "Rousing", "Dreamy") describes a vibe but
    # says almost nothing about what the music actually IS, and several
    # communities can easily share the same dominant mood without either
    # crossing the generic-frequency threshold — so leaning on mood alone
    # produces flat, repetitive, low-information names. Handing the mood
    # to Gemini as a hint (alongside representative artists) instead lets
    # it invent something like "Anatolian Vibes" or "Iberian Ska-Punk" —
    # specific, evocative, and still mood-aware. Gathered up front and
    # sent as ONE batched call, not one call per community.
    fallback_info = {}
    for community_id, members in community_members.items():
        info = per_community[community_id]
        genre = info["dominant_genre"] if info["dominant_genre"] not in generic_genres else None
        mood = info["dominant_mood"] if info["dominant_mood"] not in generic_moods else None
        if genre:
            continue
        names = _representative_names(members, is_track, max_names=5)
        hint_tags = sorted({info["dominant_genre"], info["dominant_mood"]} - {None})
        fallback_info[community_id] = {"artists": names, "tags": hint_tags, "mood_only": mood is not None}

    llm_names = _llm_name_fallback_communities(fallback_info, api_key, debug=debug) if fallback_info else {}

    # Pass 2 (final assembly): genre label -> LLM name -> mood label -> bare artist names.
    # Mood drops from a first-choice label to a last-resort one, used only
    # when Gemini couldn't (or wasn't able to, e.g. dry run/no api key)
    # turn the mood + representative artists into something more specific.
    result = {}
    for community_id, members in community_members.items():
        info = per_community[community_id]
        genre = info["dominant_genre"] if info["dominant_genre"] not in generic_genres else None
        mood = info["dominant_mood"] if info["dominant_mood"] not in generic_moods else None

        if genre:
            name = genre
            if mood and mood.lower() not in genre.lower():
                name = f"{genre} — {mood}"
        elif community_id in llm_names:
            name = llm_names[community_id]
        elif mood:
            name = mood
        else:
            names = _representative_names(members, is_track)
            if names:
                name = f"{' & '.join(names)} Mix"
            else:
                named_votes = {k: v for k, v in info["vote_counts"].items() if k != "Unsorted"}
                name = max(named_votes, key=named_votes.get) if named_votes else f"Sonic Cluster {community_id}"

        result[community_id] = (name, info["vote_counts"])

    # Pass 4: resolve NAME COLLISIONS with real differentiation instead of
    # leaving it to _finalize_community_name's "(2)", "(3)" numbering. Tag-
    # based names are especially prone to this: a mood like "Aggressive" or
    # "Rousing" can be the top mood for several DIFFERENT communities
    # without any single one crossing the generic_tag_threshold (e.g. 4
    # communities out of 17 each "own" a different mood, each under 25%
    # individually) — so the tag itself passes the genericity check, yet
    # several communities still end up proposing the exact same word. Left
    # alone, that produces a wall of "Aggressive", "Aggressive (2)",
    # "Aggressive (3)"... which numbers cosmetically rather than actually
    # telling clusters apart. Since only genre/mood-named communities can
    # collide this way (LLM-named and artist-named communities are already
    # distinct by construction — different membership, different prompt
    # context), only those are considered for re-naming here.
    name_to_ids = defaultdict(list)
    for community_id, (name, _) in result.items():
        name_to_ids[name].append(community_id)
    duplicate_ids = {cid for ids in name_to_ids.values() if len(ids) > 1 for cid in ids}

    if duplicate_ids:
        dup_fallback_info = {}
        for community_id in duplicate_ids:
            members = community_members[community_id]
            names = _representative_names(members, is_track, max_names=5)
            dup_fallback_info[community_id] = {"artists": names, "tags": [result[community_id][0]]}
        dup_llm_names = _llm_name_fallback_communities(dup_fallback_info, api_key, debug=debug)

        for community_id in duplicate_ids:
            base_name, vote_counts = result[community_id]
            if community_id in dup_llm_names:
                result[community_id] = (dup_llm_names[community_id], vote_counts)
                continue
            # No LLM name available (no api key, or Gemini didn't recognize
            # this group confidently) — fold in representative artists
            # rather than a bare number, so the name still says something
            # real about what's actually in this cluster.
            names = _representative_names(community_members[community_id], is_track, max_names=2)
            if names:
                result[community_id] = (f"{base_name} ({' & '.join(names)})", vote_counts)

    return result


def _finalize_community_name(base_name, used_names):
    """
    Turns a community's proposed name into a final, GUARANTEED-UNIQUE
    output key. This is what actually prevents communities from silently
    disappearing into each other: every caller must use the returned name
    as a fresh dict key (never merge/extend into an existing one) —
    collisions (two communities that land on the exact same name even
    after the genericity-aware naming above) are resolved here with
    numbering rather than by accidentally overwriting/merging elsewhere.
    """
    final = base_name
    n = 2
    while final in used_names:
        final = f"{base_name} ({n})"
        n += 1
    used_names.add(final)
    return final



def get_artist_genre_and_mood_tags(artist):
    """Like get_artist_combined_tags, but keeps genre and mood as SEPARATE
    sets instead of merging them — needed for naming communities by their
    own dominant genre specifically (see _name_community), since merging
    genre and mood together made it impossible to tell "this community's
    defining trait is a specific genre" from "...a specific mood"."""
    genre_tags = {g.tag for g in getattr(artist, 'genres', [])}
    mood_tags = set()
    try:
        albums = artist.albums()
    except Exception:
        albums = []
    for album in albums:
        genre_tags |= {g.tag for g in getattr(album, 'genres', [])}
        mood_tags |= {m.tag for m in getattr(album, 'moods', [])}
    return genre_tags, mood_tags


def get_artist_combined_tags(artist):
    """Combined genre+mood tag set for one artist: the artist's own genre
    tags, plus every genre+mood tag on any of its albums. Same
    album-is-more-precise idea as get_all_genre_and_mood_tags, but rolled up
    to a single artist instead of scanned across the whole library — this is
    the tag signal that feeds an artist's sonic profile (see
    build_artist_sonic_profile) rather than the tag-cluster pipeline."""
    genre_tags, mood_tags = get_artist_genre_and_mood_tags(artist)
    return genre_tags | mood_tags


def build_artist_sonic_profile(artist, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                                neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT, cache=None, debug=None):
    """
    Builds one artist's combined sonic+tag profile:
      1. Sample `sample_size` tracks via get_top_tracks_for_artist — most-
         played first, falling back to Plex's own popularity ranking for
         artists with no play history (that fallback already lives in
         get_top_tracks_for_artist; this just reuses it).
      2. Run sonicallySimilar() on each sampled track and roll the matches
         up from TRACK to ARTIST: each match's own artist (grandparentTitle
         / grandparentRatingKey) gets a vote weighted by the match score, so
         an artist whose sampled tracks sonically resemble three different
         Artist X tracks accumulates a stronger link to X than one with a
         single marginal match.
      3. Attach the artist's combined genre+mood tags (see
         get_artist_combined_tags) alongside the sonic votes, so the tag
         signal travels with the profile instead of being a separate lookup.

    Returns {"tags": [...], "neighbors": {artist_ratingKey: weight, ...}}.
    Cached to disk per-artist (keyed on _artist_fingerprint) — pass the same
    `cache` dict across all artists in one build (load once via
    _load_artist_profile_cache, save once via _save_artist_profile_cache)
    so repeat builds against an unchanged library do zero Plex sonic calls.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    artist_key = str(getattr(artist, 'ratingKey', ''))
    fingerprint = _artist_fingerprint(artist, sample_size, neighbor_limit)

    if cache is not None:
        cached = cache.get(artist_key)
        if cached and cached.get("fingerprint") == fingerprint:
            return {"tags": cached["tags"], "neighbors": {k: v for k, v in cached["neighbors"].items()}}

    tags = sorted(get_artist_combined_tags(artist))

    try:
        sample_tracks = get_top_tracks_for_artist(artist, limit=sample_size, per_album_sample=1)
    except Exception as e:
        d(f"└ ❌ Couldn't sample tracks for artist `{getattr(artist, 'title', '?')}`: `{e}`")
        sample_tracks = []

    neighbor_weights = defaultdict(float)
    for track in sample_tracks:
        try:
            matches = track.sonicallySimilar(limit=neighbor_limit)
        except Exception:
            continue
        for m in matches:
            m_artist_key = str(getattr(m, 'grandparentRatingKey', '') or '')
            if not m_artist_key or m_artist_key == artist_key:
                continue
            pct = get_sonic_match_percent(m)
            weight = (pct / 100.0) if pct is not None else 0.5
            neighbor_weights[m_artist_key] = max(neighbor_weights[m_artist_key], weight)

    profile = {"tags": tags, "neighbors": dict(neighbor_weights)}
    if cache is not None:
        cache[artist_key] = {"fingerprint": fingerprint, "tags": tags, "neighbors": profile["neighbors"]}
    return profile


def _artist_majority_cluster(tags, tag_mapping):
    """Given an artist's combined tag set and the Gemini tag->cluster
    mapping, returns whichever cluster the artist's own tags vote for most
    (or None if no tag maps to anything, or tag_mapping isn't supplied).
    Used to let two artists' shared CLUSTER membership pull them together
    in the similarity graph — a stronger, more curated signal than raw tag
    overlap, since it's filtered through the clusters you actually defined
    rather than any incidental shared tag."""
    if not tag_mapping:
        return None
    votes = defaultdict(int)
    for tag in tags:
        if tag in tag_mapping:
            votes[tag_mapping[tag]] += 1
    if not votes:
        return None
    return max(votes, key=votes.get)


def _build_plex_similar_edges(all_artists, debug=None):
    """
    Builds {artist_key: set(neighbor_key, ...)} from Plex's own "Similar
    Artist" hub — the exact same data source and title-matching approach as
    galaxy.build_similarity_graph. This is the piece that lets clustering
    actually follow the same branches you SEE in the Library Galaxy tab:
    previously the clustering graph only knew about sonic-sample matches
    and tag overlap, a different (and much sparser) signal than what draws
    the galaxy's visible structure, so the two could look unrelated even
    though they're describing the "same" library. Costs one Plex call per
    artist — comparable to what the galaxy tab already pays for the same
    data, and covered by the same get_cached_artists scan reuse elsewhere.
    Both directions of a link (A says B is similar; B may not say A back)
    are folded into one undirected adjacency, same as galaxy.py.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    key_by_title = {}
    for a in all_artists:
        title = getattr(a, 'title', None)
        if title:
            key_by_title[title.lower()] = str(getattr(a, 'ratingKey', ''))

    edges = defaultdict(set)
    failures = 0
    for a in all_artists:
        key = str(getattr(a, 'ratingKey', ''))
        try:
            similar_attr = getattr(a, 'similar', None)
            similar = similar_attr() if callable(similar_attr) else (similar_attr or [])
        except Exception:
            failures += 1
            continue
        for sim in (similar or []):
            name = getattr(sim, 'tag', None)
            if not name:
                continue
            match_key = key_by_title.get(name.lower())
            if match_key and match_key != key:
                edges[key].add(match_key)
                edges[match_key].add(key)

    total_links = sum(len(v) for v in edges.values()) // 2
    d(f"└ Plex 'Similar Artist' signal: {total_links} links across {len(edges)} artists "
      f"({failures} lookup failures).")
    return edges


def build_relational_graph(all_artists, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                            neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                            sonic_boost=0.5, use_cache=True, debug=None):
    """
    Builds the artist-level graph clustering actually runs Louvain on — and
    this is deliberately simpler than the four-signal blend it replaces.

    Topology comes from EXACTLY ONE place: Plex's own "Similar Artist" hub
    (see _build_plex_similar_edges) — the SAME data Library Galaxy already
    visualizes. That's the whole point: whatever branches you can see in
    the galaxy are, by construction, the same graph clustering groups here.
    No tag signal of any kind touches this graph, at any weight — that
    was the recurring source of every collapse/fragmentation bug earlier
    (tags are either too coarse to discriminate, in which case they merge
    everything, or too specific, in which case they fragment everything;
    a library's tag quality is uncontrollable and shouldn't be load-bearing
    for WHICH artists end up together).

    Sonic similarity (build_artist_sonic_profile — a handful of sampled
    top tracks per artist, compared via Plex's own audio analysis) plays a
    supporting role only: it BOOSTS the weight of an edge that Plex's
    Similar Artist hub already created, it never creates a new edge on its
    own. `sonic_boost` controls how much — 0 ignores sonic entirely and
    uses pure Similar-Artist topology; higher values let a strong sonic
    match noticeably strengthen (and therefore help keep together during
    the size-rebalancing pass) an already-real relationship.

    Artists with no Similar Artist links at all end up as isolated nodes —
    that's honest: if Plex has no relational signal for an artist, this
    graph shouldn't invent one from noisier data. Isolated/tiny communities
    are handled afterward by _rebalance_communities, not by the graph
    itself.

    Returns (graph, artist_by_key, profile_cache) — profile_cache is the
    (possibly updated) sonic-profile disk cache; save it with
    _save_artist_profile_cache once the caller is done.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    if not HAS_SONIC_GRAPH_DEPS:
        raise ImportError(
            "Community-detection clustering requires 'networkx' and 'python-louvain' "
            "(pip install networkx python-louvain)."
        )

    artist_by_key = {str(getattr(a, 'ratingKey', '')): a for a in all_artists if getattr(a, 'ratingKey', None)}
    plex_similar_edges = _build_plex_similar_edges(all_artists, debug=debug)

    cache = _load_artist_profile_cache() if use_cache else {}
    sonic_neighbors = {}
    if sonic_boost > 0:
        for key, artist in artist_by_key.items():
            profile = build_artist_sonic_profile(
                artist, sample_size=sample_size, neighbor_limit=neighbor_limit, cache=cache, debug=debug
            )
            sonic_neighbors[key] = profile["neighbors"]

    graph = nx.Graph()
    graph.add_nodes_from(artist_by_key.keys())

    edges_added = 0
    for key, neighbors in plex_similar_edges.items():
        for neighbor_key in neighbors:
            if neighbor_key not in artist_by_key or neighbor_key == key or graph.has_edge(key, neighbor_key):
                continue
            weight = 1.0
            if sonic_boost > 0:
                sonic = max(
                    sonic_neighbors.get(key, {}).get(neighbor_key, 0.0),
                    sonic_neighbors.get(neighbor_key, {}).get(key, 0.0),
                )
                weight += sonic_boost * sonic
            graph.add_edge(key, neighbor_key, weight=weight)
            edges_added += 1

    isolated = sum(1 for n in graph.nodes() if graph.degree(n) == 0)
    d(f"**Relational graph built:** {len(artist_by_key)} artists, {edges_added} Similar-Artist links "
      f"({'sonic boost applied where available' if sonic_boost > 0 else 'sonic boost off'}), "
      f"{isolated} artists with no Similar-Artist data (isolated).")

    return graph, artist_by_key, cache


def _rebalance_communities(graph, partition, min_size=2, max_size=60, debug=None):
    """
    Cleanup pass over Louvain's raw output, BEFORE ranking/selection (see
    _score_and_select_communities): merges communities too small to be
    meaningful (< min_size) into their best-connected neighbor, and splits
    ones too large to be coherent (> max_size) via recursive Louvain on
    just that subgraph. This is not what controls the final cluster COUNT
    — it just prevents true garbage (singleton artists, one giant blob)
    from skewing the scoring step that follows. min_size/max_size are
    fixed, not user-facing; the number of mixes you actually see is
    controlled by `target_count` in _score_and_select_communities.

    Returns {community_id: [artist_key, ...]}.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    communities = defaultdict(list)
    for key, cid in partition.items():
        communities[cid].append(key)

    # Merge undersized communities into whichever neighbor they share the
    # most connection weight with; anything left with no cross-community
    # edges at all (fully isolated) gets pooled into one shared bucket.
    changed = True
    while changed:
        changed = False
        for cid, members in list(communities.items()):
            if len(members) >= min_size or len(communities) <= 1:
                continue
            scores = defaultdict(float)
            for m in members:
                for neighbor in graph.neighbors(m):
                    other_cid = next((c for c, mem in communities.items() if neighbor in mem and c != cid), None)
                    if other_cid is not None:
                        scores[other_cid] += graph[m][neighbor].get('weight', 1.0)
            if scores:
                best = max(scores, key=scores.get)
                communities[best].extend(members)
                del communities[cid]
                changed = True
                break

    leftover_key = None
    for cid, members in list(communities.items()):
        if len(members) < min_size:
            if leftover_key is None:
                leftover_key = cid
                continue
            communities[leftover_key].extend(members)
            del communities[cid]

    # Split oversized communities via recursive Louvain on their own subgraph.
    final = {}
    next_id = 0
    for cid, members in communities.items():
        if len(members) <= max_size:
            final[next_id] = members
            next_id += 1
            continue
        subgraph = graph.subgraph(members)
        try:
            sub_partition = community_louvain.best_partition(subgraph, weight='weight', random_state=42)
        except Exception:
            final[next_id] = members
            next_id += 1
            continue
        sub_groups = defaultdict(list)
        for key, sub_cid in sub_partition.items():
            sub_groups[sub_cid].append(key)
        if len(sub_groups) <= 1:
            final[next_id] = members
            next_id += 1
        else:
            for group in sub_groups.values():
                final[next_id] = group
                next_id += 1

    d(f"**Rebalanced to {len(final)} candidate communities** before ranking/selection.")
    return final


def _score_and_select_communities(graph, communities, target_count, min_artists_needed, debug=None):
    """
    THIS is what controls how many mixes you actually see — not size
    bounds. Every candidate community (post-_rebalance_communities) gets a
    score = connection_strength \u00d7 completion, and only the top
    `target_count` survive:
      - connection_strength: mean weight of edges WITHIN the community
        (its own induced subgraph) — how tightly these artists actually
        connect to each other, not just "were grouped together." A
        community with no internal edges (can happen after a forced merge
        of otherwise-unconnected leftovers) scores 0 here and sinks to the
        bottom automatically.
      - completion: min(1.0, artist_count / min_artists_needed) — how much
        content the community can actually supply, relative to what's
        needed to fill a mix (see the top_n / max_tracks_per_artist math
        in build_genre_clusters). A small-but-tight community (say 4
        well-connected artists making a 12-track mix) still scores
        reasonably via a high connection_strength even with completion
        < 1.0 — it's a real mix, just a shorter one. This is deliberate:
        completion penalizes but doesn't disqualify.

    Communities that don't make the cut are simply dropped, not merged
    into survivors — merging them back in would just recreate the
    "everything blurs together" problem this whole approach exists to
    avoid. If target_count exceeds how many candidates exist, all of them
    are kept (nothing to trim).

    Returns {community_id: [artist_key, ...]} — only the selected ones.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    scored = []
    for cid, members in communities.items():
        subgraph = graph.subgraph(members)
        weights = [w for _, _, w in subgraph.edges(data='weight', default=1.0)]
        connection_strength = (sum(weights) / len(weights)) if weights else 0.0
        completion = min(1.0, len(members) / min_artists_needed) if min_artists_needed else 1.0
        score = connection_strength * completion
        scored.append((cid, score, connection_strength, completion))

    scored.sort(key=lambda x: x[1], reverse=True)
    kept = scored[:target_count]
    dropped = scored[target_count:]

    d(f"**Ranked {len(scored)} candidate communities, keeping the top {len(kept)}** "
      f"(target {target_count}). Kept scores: "
      f"{[round(s, 2) for _, s, _, _ in kept]}." +
      (f" Dropped {len(dropped)} weaker/thinner communities." if dropped else ""))

    return {cid: communities[cid] for cid, _, _, _ in kept}


def build_artist_sonic_clusters(music_section, tag_mapping=None, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                                 neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                                 sonic_boost=0.5, target_count=10, max_tracks_per_artist=3,
                                 top_n_per_cluster=30, use_cache=True, api_key=None, debug=None):
    """
    Artist-level community detection: Louvain over build_relational_graph
    (pure Plex Similar-Artist topology, optionally boosted by real sonic
    matches), cleaned up via _rebalance_communities, then RANKED and cut
    down to the top `target_count` via _score_and_select_communities —
    score = connection strength \u00d7 completion (see that function). This
    is what makes "Maximum number of clusters" mean what it says: the
    output is always at most target_count mixes, the strongest/most
    complete ones, not every fragment Louvain happened to find. A smaller,
    tightly-connected community can still outrank a larger, loosely-
    connected one — size alone isn't the deciding factor.

    Naming is genericity-aware (see _name_communities), tries an LLM name
    from representative artists as a further fallback (api_key; None skips
    that call entirely), and never influences MEMBERSHIP either way, only
    the label shown afterward.

    Returns (results, community_tag_votes) — results is
    {cluster_name: [tracks]}, community_tag_votes is
    {cluster_name: {tag_cluster: count}}.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    all_artists = get_cached_artists(music_section, debug=debug)

    graph, artist_by_key, cache = build_relational_graph(
        all_artists, sample_size=sample_size, neighbor_limit=neighbor_limit,
        sonic_boost=sonic_boost, use_cache=use_cache, debug=debug
    )
    if use_cache:
        _save_artist_profile_cache(cache)

    if graph.number_of_nodes() == 0:
        return {}, {}

    partition = community_louvain.best_partition(graph, weight='weight', random_state=42)
    d(f"**Louvain found {len(set(partition.values()))} raw artist communities** at natural resolution.")

    communities_by_key = _rebalance_communities(graph, partition, debug=debug)

    min_artists_needed = ceil(top_n_per_cluster / max_tracks_per_artist) if max_tracks_per_artist else 1
    selected_by_key = _score_and_select_communities(
        graph, communities_by_key, target_count, min_artists_needed, debug=debug
    )
    communities = {cid: [artist_by_key[k] for k in keys] for cid, keys in selected_by_key.items()}

    results = {}
    community_tag_votes = {}
    used_names = set()
    names_by_community = _name_communities(
        communities, tag_mapping, is_track=False, api_key=api_key, debug=debug
    )
    for community_id, artists in communities.items():
        base_name, vote_counts = names_by_community[community_id]
        name = _finalize_community_name(base_name, used_names)

        tracks = []
        for artist in artists:
            try:
                tracks.extend(artist.tracks())
            except Exception:
                continue

        community_tag_votes[name] = vote_counts
        results[name] = tracks
        d(f"\u2514 Community {community_id}: {len(artists)} artists / {len(tracks)} tracks -> named `{name}` "
          f"(tag votes: {vote_counts or 'n/a'}).")

    return results, community_tag_votes



def build_genre_clusters(music_section, plex, locked_clusters, total_clusters, api_key,
                          top_n_per_cluster=30, debug=None, force_remap=False, dry_run=False,
                          preloaded_mapping=None, refine_unsorted=True, sonic_weight=0.0,
                          reassign_tagged_via_sonic=False, sonic_propagation_rounds=2,
                          clustering_mode="tags", sonic_neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                          sonic_artist_sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                          sonic_boost=0.5, sonic_use_cache=True, max_tracks_per_artist=3):
    """
    Full pipeline. clustering_mode options:

    - "tags" (default): collect genre+mood tags -> tag/cluster mapping
      (disk + memory cached, or reused from a prior Suggest step) -> assign
      every track in the library to a cluster BY TAG -> optionally let
      sonic-neighbor consensus recover Unsorted tracks and/or reassign
      mistagged ones (see refine_unsorted_via_sonic_neighbors) -> blend each
      cluster's final track list from popular + sonically-similar +
      related-artist picks (_blend_cluster_tracks). Tags decide membership;
      sonic analysis only corrects afterward.

    - "sonic" ("Relational" in the UI): build_artist_sonic_clusters.
      Membership comes from Louvain over Plex's own "Similar Artist" hub —
      the SAME data Library Galaxy visualizes — optionally boosted by real
      sonic audio matching (`sonic_boost`, 0-1; only strengthens an edge
      that already exists, never creates one). No tag signal is involved
      in deciding who belongs together, only in naming the result
      afterward (see _name_communities).

      `total_clusters` is a REAL cap on this mode's output too: raw
      communities are cleaned up (tiny ones merged, huge ones re-split —
      see _rebalance_communities) and then ranked by
      connection-strength \u00d7 completion, keeping only the top
      `total_clusters` (see _score_and_select_communities). A small,
      tightly-connected community can outrank a larger, loosely-connected
      one — completion softly rewards mixes that can actually reach
      top_n_per_cluster, it doesn't require it (a 4-artist mix that only
      makes 12 tracks is still a fine mix, just not a full one).

    max_tracks_per_artist (default 3, 0 = no cap): hard cap on how many
    tracks from the same artist can appear in one cluster's final list, in
    every clustering_mode — the last step of assembling each cluster, so
    one prolific artist can't quietly fill an entire mix alone. Also feeds
    the "how many artists does a full mix need" math that drives
    completion scoring above.

    Returns (results, tag_mapping) — results is {cluster_name: [tracks]},
    tag_mapping is the raw tag->cluster dict (handy for coloring the
    Library Galaxy tab by cluster without re-deriving it, and used in
    "sonic" mode purely for naming, never for membership).

    Set dry_run=True (tags mode only) to skip Gemini entirely and use
    build_dry_run_mapping instead — useful for testing the rest of the
    pipeline at zero cost while iterating.

    preloaded_mapping: optional (clusters, tag_mapping) tuple from a prior
    suggest_cluster_names() call — if the user accepted the suggestions
    unchanged, this skips a second Gemini call entirely.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
    d(f"**Found {len(genre_tags)} genre tags and {len(mood_tags)} mood tags in library.**")

    if dry_run:
        d("**Dry run mode — using offline keyword mapper, no Gemini call.**")
        clusters, tag_mapping = build_dry_run_mapping(genre_tags, mood_tags, locked_clusters, total_clusters)
    elif preloaded_mapping is not None:
        d("**Reusing mapping from Suggest Clusters — no additional Gemini call.**")
        clusters, tag_mapping = preloaded_mapping
    else:
        clusters, tag_mapping = get_tag_cluster_mapping(
            genre_tags, mood_tags, locked_clusters, total_clusters, api_key, force_remap=force_remap
        )
    d(f"**Final clusters:** {clusters}")

    if clustering_mode == "sonic":
        d(f"**Relational mode:** cluster membership comes from Louvain community detection over "
          f"Plex's own Similar-Artist graph (sonic_boost={sonic_boost:g}); tags only name the "
          f"result, never decide membership. Keeping the top {total_clusters} mixes by "
          "connection strength \u00d7 completion.")
        sonic_pools, community_tag_votes = build_artist_sonic_clusters(
            music_section, tag_mapping=tag_mapping, sample_size=sonic_artist_sample_size,
            neighbor_limit=sonic_neighbor_limit, sonic_boost=sonic_boost,
            target_count=total_clusters, max_tracks_per_artist=max_tracks_per_artist,
            top_n_per_cluster=top_n_per_cluster, use_cache=sonic_use_cache,
            api_key=(None if dry_run else api_key), debug=debug
        )

        results = {}
        for cluster_name, tracks in sonic_pools.items():
            results[cluster_name] = _select_popular(
                tracks, min(top_n_per_cluster, len(tracks)), max_per_artist=max_tracks_per_artist
            )
            # Artist-fallback names already end in "Mix" (see _name_communities) —
            # avoid a redundant "X & Y Mix (Mix)" label in that case.
            already_says_mix = cluster_name.endswith("Mix") or " Mix (" in cluster_name
            label = cluster_name if already_says_mix else f"{cluster_name} (Mix)"
            for t in results[cluster_name]:
                setattr(t, 'recommendation_type', label)
        return results, tag_mapping

    pools = defaultdict(list)
    all_artists = get_cached_artists(music_section, debug=debug)

    for artist in all_artists:
        try:
            tracks = artist.tracks()
        except Exception:
            continue
        for t in tracks:
            cluster = assign_track_cluster(t, tag_mapping)
            pools[cluster].append(t)

    for cluster, tracks in pools.items():
        d(f"└ `{cluster}`: {len(tracks)} tracks pooled.")

    if not dry_run:
        pools, clusters, tag_mapping = _fold_thin_clusters(pools, clusters, tag_mapping, locked_clusters, debug=debug)

    if (refine_unsorted or reassign_tagged_via_sonic) and not dry_run:
        pools = refine_unsorted_via_sonic_neighbors(
            pools, sonic_weight=sonic_weight, reassign_tagged=reassign_tagged_via_sonic,
            propagation_rounds=sonic_propagation_rounds, debug=debug
        )

    results = {}
    for cluster_name in clusters + (["Unsorted"] if "Unsorted" in pools else []):
        tracks = pools.get(cluster_name, [])
        if not tracks:
            results[cluster_name] = []
            continue
        results[cluster_name] = _blend_cluster_tracks(
            cluster_name, tracks, music_section, plex, top_n_per_cluster, tag_mapping,
            max_per_artist=max_tracks_per_artist, debug=debug
        )

    return results, tag_mapping