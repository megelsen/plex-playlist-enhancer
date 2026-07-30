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
# get_all_genre_and_mood_tags, build_sonic_clusters, and
# build_artist_sonic_clusters each used to call searchArtists() separately,
# so a single "Build Clusters" click could scan the whole library 2-3 times
# over. Keyed by section (not by anything about the caller), so all three
# share one fetch per TTL window regardless of which functions are called
# in what order. This is intentionally NOT disk-backed — artists are live
# plexapi objects (can't be JSON-serialized), so this only helps within one
# running process/session, not across restarts (that's what
# ARTIST_PROFILE_CACHE_PATH and CLUSTER_RESULTS_CACHE_PATH are for).
_ARTIST_SCAN_CACHE = {}
ARTIST_SCAN_TTL_SECONDS = 900  # long enough to cover one scan->configure->build session

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Cap on how many tracks get pulled into the sonic-similarity graph for
# community-detection clustering (build_sonic_communities). Each node costs
# one Plex sonicallySimilar() call, so this bounds worst-case runtime the
# same way REFINE_MAX_TRACKS does for the tag-refinement pass. Prioritized
# by play count, so the graph is built from the library's most-listened
# tracks first on very large libraries.
SONIC_GRAPH_MAX_TRACKS = 1500
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
# sonic clustering mode — see build_artist_similarity_graph). Each artist's
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

    votes = defaultdict(int)
    for tag in genre_tags | mood_tags:
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


def _select_popular(tracks, n):
    """Top-N by viewCount, falling back to a random sample if nothing in
    the pool has ever been played (same pattern as get_top_tracks_for_artist)."""
    if not tracks or n <= 0:
        return []
    ranked = sorted(tracks, key=lambda t: getattr(t, 'viewCount', 0) or 0, reverse=True)
    top_view_count = getattr(ranked[0], 'viewCount', 0) or 0
    if top_view_count > 0:
        return ranked[:n]
    pool = list(tracks)
    random.shuffle(pool)
    return pool[:n]


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


def _blend_cluster_tracks(cluster_name, tracks_pool, music_section, plex, total_n, tag_mapping, debug=None):
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


def save_cluster_results_cache(results, tag_mapping):
    """Persists the current cluster build to disk as track ratingKeys, for
    load_cluster_results_cache to rehydrate on a future app start. Call
    this any time `results` changes (fresh build, merge, or manual track
    removal) — best-effort, silently does nothing on failure since this is
    a convenience cache, not a source of truth."""
    try:
        os.makedirs(os.path.dirname(CLUSTER_RESULTS_CACHE_PATH), exist_ok=True)
        payload = {
            "tag_mapping": tag_mapping,
            "clusters": {name: [getattr(t, 'ratingKey', None) for t in tracks] for name, tracks in results.items()},
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

    Returns (results, tag_mapping), or (None, None) if there's no cache
    file, it's unreadable, or every cached ratingKey has since vanished
    from the library (tracks removed/library changed) — any individual
    missing ratingKey is just skipped rather than failing the whole load,
    since a few stale entries shouldn't discard an otherwise-good cache.
    """
    d = debug.write if debug else (lambda *a, **k: None)
    try:
        with open(CLUSTER_RESULTS_CACHE_PATH, "r") as f:
            payload = json.load(f)
    except Exception:
        return None, None

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
        return None, None
    total = sum(len(t) for t in results.values())
    d(f"**Loaded {total} tracks across {len(results)} clusters from disk cache** "
      f"({missing} stale ratingKey(s) skipped) — no rebuild needed.")
    return results, payload.get("tag_mapping")


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
    per build (not once per artist) — build_artist_similarity_graph mutates
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


def get_artist_combined_tags(artist):
    """Combined genre+mood tag set for one artist: the artist's own genre
    tags, plus every genre+mood tag on any of its albums. Same
    album-is-more-precise idea as get_all_genre_and_mood_tags, but rolled up
    to a single artist instead of scanned across the whole library — this is
    the tag signal that feeds an artist's sonic profile (see
    build_artist_sonic_profile) rather than the tag-cluster pipeline."""
    tags = {g.tag for g in getattr(artist, 'genres', [])}
    try:
        albums = artist.albums()
    except Exception:
        albums = []
    for album in albums:
        tags |= {g.tag for g in getattr(album, 'genres', [])}
        tags |= {m.tag for m in getattr(album, 'moods', [])}
    return tags


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


def build_artist_similarity_graph(all_artists, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                                   neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                                   sonic_weight=0.5, tag_overlap_weight=0.15, cluster_agreement_weight=0.35,
                                   tag_mapping=None, use_cache=True, debug=None):
    """
    Artist-level counterpart to build_sonic_similarity_graph: one node per
    ARTIST rather than per track, so Louvain groups artists directly instead
    of grouping individual tracks that then need re-rolling-up into artists
    afterward. Each artist's profile is built from a small sample of its
    most-played (or Plex-popular, if unplayed) tracks — see
    build_artist_sonic_profile — so the whole library is affordable in Plex
    API calls even when it wouldn't be at full track-level (this is why
    there's no max_tracks cap here, unlike the track-level graph).

    This is the real hybrid: edge weight between two artists blends THREE
    independent signals, each contributing in proportion to its weight
    (normalized to sum to 1 across whichever of the three are non-zero):
      - sonic: the stronger of either direction's neighbor-vote weight from
        build_artist_sonic_profile (max, not sum — two artists sampling into
        each other isn't "twice as similar", just confirmed from both sides).
        This is the only signal that comes from actual audio analysis.
      - tag_overlap: Jaccard similarity of their combined genre/mood tag
        sets (see get_artist_combined_tags) — a broad, noisy signal (any
        shared tag counts a little), but catches real similarity that
        neither Plex's sonic analysis nor a shared Gemini cluster caught.
      - cluster_agreement: 1.0 if the two artists' tags vote for the SAME
        Gemini-defined cluster (via _artist_majority_cluster), else 0 — a
        narrow but curated signal, since it's filtered through the actual
        cluster definitions you asked Gemini to build rather than any
        incidental tag overlap. Requires tag_mapping; silently contributes
        0 if it's not supplied (weight is still counted so callers can pass
        it deliberately without tag_mapping to mean "off").
    An edge is only added if the blended weight is > 0 (i.e. at least one
    signal exists between the pair) — most artist pairs in a library have
    none and simply aren't connected.

    Returns (graph, artist_by_key, profile_cache) — profile_cache is the
    (possibly updated) disk-cache dict; save it with
    _save_artist_profile_cache once the caller is done, so successive builds
    within the same process share one write instead of one per artist.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    if not HAS_SONIC_GRAPH_DEPS:
        raise ImportError(
            "Sonic community-detection clustering requires 'networkx' and 'python-louvain' "
            "(pip install networkx python-louvain)."
        )

    total_weight = sonic_weight + tag_overlap_weight + cluster_agreement_weight
    if total_weight <= 0:
        sonic_weight, tag_overlap_weight, cluster_agreement_weight, total_weight = 1.0, 0.0, 0.0, 1.0
    w_sonic = sonic_weight / total_weight
    w_tag = tag_overlap_weight / total_weight
    w_cluster = cluster_agreement_weight / total_weight

    cache = _load_artist_profile_cache() if use_cache else {}
    cache_hits = 0

    artist_by_key = {}
    profiles = {}
    majority_cluster = {}
    for artist in all_artists:
        key = str(getattr(artist, 'ratingKey', ''))
        if not key:
            continue
        artist_by_key[key] = artist
        before = cache.get(key, {}).get("fingerprint")
        profiles[key] = build_artist_sonic_profile(
            artist, sample_size=sample_size, neighbor_limit=neighbor_limit, cache=cache, debug=debug
        )
        if before and cache.get(key, {}).get("fingerprint") == before:
            cache_hits += 1
        majority_cluster[key] = _artist_majority_cluster(profiles[key]["tags"], tag_mapping)

    d(f"**Artist similarity graph:** {len(artist_by_key)} artists profiled "
      f"({cache_hits} reused from cache, {len(artist_by_key) - cache_hits} freshly sampled at "
      f"{sample_size} track(s) each). Blend: {w_sonic:.0%} sonic / {w_tag:.0%} tag overlap / "
      f"{w_cluster:.0%} cluster agreement.")

    graph = nx.Graph()
    graph.add_nodes_from(artist_by_key.keys())

    # Sonic signal only exists between pairs with an actual neighbor vote —
    # iterate those. Tag/cluster signal can exist between ANY pair, but
    # checking every pair is O(n^2); since an edge with zero sonic weight
    # still needs tag/cluster weight to justify existing, we only add those
    # for pairs that share at least one tag (cheap to detect) — two artists
    # with zero tag overlap AND zero sonic link have no real evidence of
    # similarity anyway, so skipping them is a fidelity/runtime tradeoff
    # worth taking at library scale.
    tags_by_key = {k: set(p["tags"]) for k, p in profiles.items()}
    tag_to_artists = defaultdict(set)
    for key, tags in tags_by_key.items():
        for tag in tags:
            tag_to_artists[tag].add(key)

    candidate_pairs = set()
    for key, profile in profiles.items():
        for neighbor_key in profile["neighbors"]:
            if neighbor_key in artist_by_key and neighbor_key != key:
                candidate_pairs.add(tuple(sorted((key, neighbor_key))))
    for tag, keys in tag_to_artists.items():
        keys = list(keys)
        if len(keys) > 1 and len(keys) <= 200:  # skip near-universal tags — no discriminative value
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    candidate_pairs.add(tuple(sorted((keys[i], keys[j]))))

    edges_added = 0
    for key, neighbor_key in candidate_pairs:
        sonic = max(
            profiles.get(key, {}).get("neighbors", {}).get(neighbor_key, 0.0),
            profiles.get(neighbor_key, {}).get("neighbors", {}).get(key, 0.0),
        )
        tags_a, tags_b = tags_by_key.get(key, set()), tags_by_key.get(neighbor_key, set())
        union = tags_a | tags_b
        tag_sim = (len(tags_a & tags_b) / len(union)) if union else 0.0
        same_cluster = (
            majority_cluster.get(key) is not None
            and majority_cluster.get(key) == majority_cluster.get(neighbor_key)
        )
        cluster_sim = 1.0 if same_cluster else 0.0

        weight = w_sonic * sonic + w_tag * tag_sim + w_cluster * cluster_sim
        if weight <= 0:
            continue
        graph.add_edge(key, neighbor_key, weight=weight)
        edges_added += 1

    d(f"└ Artist graph built: {graph.number_of_nodes()} artists, {edges_added} similarity edges.")

    return graph, artist_by_key, cache


def _auto_tune_resolution(graph, target_communities, resolution_bounds=(0.2, 4.0), max_iter=10, debug=None):
    """
    Louvain's `resolution` parameter trades off community count (higher ->
    more, smaller communities; lower -> fewer, larger ones) but isn't
    directly interpretable as "give me N communities" — a fixed resolution
    of 1.0 can land anywhere from 4 to 40 communities depending on how the
    similarity graph happens to be shaped for a given library, and how HIGH
    a resolution is actually needed to reach a given count varies wildly
    with graph density (a sparse artist-similarity graph, e.g. from a
    heavily tag-driven blend with few sonic edges, can need a resolution
    well above the old fixed upper bound of 4.0 to split into 15+ groups).

    Two phases:
      1. EXPANDING SEARCH: starting from the middle of resolution_bounds,
         repeatedly double (if still below target) or halve (if already
         above) the resolution being tried, so the search isn't capped by
         whatever resolution_bounds happened to be — it escapes upward or
         downward until it brackets the target or hits a hard sanity limit
         (0.01 / 64.0). This is what actually fixes "settles for way fewer
         clusters than asked for" — a fixed bounds search can only ever
         report the best IT COULD REACH within those bounds, silently
         falling short if the true answer needed a higher resolution.
      2. BINARY SEARCH within whatever bracket phase 1 found, using the
         rest of the iteration budget to refine within it.

    Not guaranteed to land exactly on target (Louvain's community count
    isn't perfectly monotonic in resolution, and some libraries just don't
    have a natural partition at every possible count) — returns whichever
    of the tried candidates landed closest, along with the resolution that
    produced it, so the caller can report what was actually used.

    Returns (partition, resolution_used, community_count).
    """
    d = debug.write if debug else (lambda *a, **k: None)
    HARD_LO, HARD_HI = 0.01, 64.0

    best = None  # (resolution, count, partition)

    def _try(resolution):
        nonlocal best
        partition = community_louvain.best_partition(graph, weight='weight', resolution=resolution, random_state=42)
        count = len(set(partition.values()))
        if best is None or abs(count - target_communities) < abs(best[1] - target_communities):
            best = (resolution, count, partition)
        return count

    lo, hi = resolution_bounds
    mid = (lo + hi) / 2
    count = _try(mid)
    d(f"└ Auto-tuning granularity (1/{max_iter}): resolution={mid:.3f} -> {count} clusters "
      f"(target {target_communities}).")
    iters_used = 1

    # Phase 1: expand outward until the target is bracketed, or we hit the
    # hard sanity limits — this is what lets the search reach far past the
    # original resolution_bounds when the graph needs it.
    while iters_used < max_iter and count != target_communities:
        if count < target_communities:
            if hi >= HARD_HI:
                break
            lo, hi = hi, min(HARD_HI, hi * 2)
        else:
            if lo <= HARD_LO:
                break
            lo, hi = max(HARD_LO, lo / 2), lo
        mid = (lo + hi) / 2
        count = _try(mid)
        iters_used += 1
        d(f"└ Auto-tuning granularity ({iters_used}/{max_iter}): resolution={mid:.3f} -> {count} clusters "
          f"(target {target_communities}).")

    # Phase 2: binary search the remaining budget within [lo, hi] — by now
    # either bracketed (one side <= target, other >= target) or we hit a
    # hard limit and lo==hi effectively, in which case this just confirms it.
    while iters_used < max_iter and count != target_communities and hi > lo:
        mid = (lo + hi) / 2
        count = _try(mid)
        iters_used += 1
        d(f"└ Auto-tuning granularity ({iters_used}/{max_iter}): resolution={mid:.3f} -> {count} clusters "
          f"(target {target_communities}).")
        if count < target_communities:
            lo = mid
        else:
            hi = mid

    resolution, count, partition = best
    if count < target_communities:
        d(f"⚠️ Auto-tune couldn't reach {target_communities} clusters even at resolution={resolution:.3f} "
          f"(hit {count}) — the similarity graph may simply not support finer splits at this edge "
          "density; consider raising sonic/tag signal weights or lowering the target.")
    d(f"**Granularity auto-tune settled on resolution={resolution:.3f} -> {count} clusters** "
      f"(target was {target_communities}).")
    return partition, resolution, count


def build_artist_sonic_clusters(music_section, tag_mapping=None, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                                 neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                                 sonic_weight=0.5, tag_overlap_weight=0.15, cluster_agreement_weight=0.35,
                                 resolution=1.0, target_clusters=None, use_cache=True, debug=None):
    """
    Artist-level counterpart to build_sonic_clusters: Louvain community
    detection over build_artist_similarity_graph — a genuine hybrid blend of
    sonic profile, raw tag overlap, and Gemini cluster agreement (see that
    function's docstring for the three-way weighting) — so membership is
    decided per ARTIST using all three signals together, not tags-then-sonic
    or sonic-then-tags as two separate passes. Every track from an artist
    lands in the same community as the rest of that artist's catalog.

    Naming works the same way as build_sonic_clusters: each community is
    named after whichever tag_mapping cluster its member artists' tags vote
    for most, for a readable label — this reuses the same majority-cluster
    signal that also fed the graph weighting above, so a community's name
    should usually agree with what pulled its members together in the first
    place (though membership can still be swayed by sonic/tag-overlap signal
    even when cluster_agreement_weight isn't the dominant one).

    If target_clusters is given, `resolution` is ignored and
    _auto_tune_resolution searches for whichever resolution produces
    closest to that many communities instead of using a fixed one — this is
    what lets "Maximum number of clusters" in the UI actually steer Hybrid
    mode's community count the same way it steers Gemini's tag-cluster
    count, rather than resolution being a separate, unrelated dial.

    Returns (results, community_tag_votes) — results is
    {cluster_name: [tracks]} (every track from every artist in that
    community), community_tag_votes is {cluster_name: {tag_cluster: count}}.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    all_artists = get_cached_artists(music_section, debug=debug)

    graph, artist_by_key, cache = build_artist_similarity_graph(
        all_artists, sample_size=sample_size, neighbor_limit=neighbor_limit,
        sonic_weight=sonic_weight, tag_overlap_weight=tag_overlap_weight,
        cluster_agreement_weight=cluster_agreement_weight, tag_mapping=tag_mapping,
        use_cache=use_cache, debug=debug
    )
    if use_cache:
        _save_artist_profile_cache(cache)

    if graph.number_of_nodes() == 0:
        return {}, {}

    if target_clusters:
        partition, resolution, _ = _auto_tune_resolution(graph, target_clusters, debug=debug)
    else:
        partition = community_louvain.best_partition(graph, weight='weight', resolution=resolution, random_state=42)
    communities = defaultdict(list)
    for key, community_id in partition.items():
        communities[community_id].append(artist_by_key[key])

    d(f"**Louvain found {len(communities)} raw artist communities** (resolution={resolution}).")

    results = defaultdict(list)
    community_tag_votes = {}
    for community_id, artists in communities.items():
        name = f"Sonic Cluster {community_id}"
        vote_counts = {}
        if tag_mapping:
            votes = defaultdict(int)
            for artist in artists:
                for tag in get_artist_combined_tags(artist):
                    if tag in tag_mapping:
                        votes[tag_mapping[tag]] += 1
            vote_counts = dict(votes)
            named_votes = {k: v for k, v in votes.items() if k != "Unsorted"}
            if named_votes:
                name = max(named_votes, key=named_votes.get)
            elif votes:
                name = f"Sonic Cluster {community_id} (Unsorted)"

        tracks = []
        for artist in artists:
            try:
                tracks.extend(artist.tracks())
            except Exception:
                continue

        community_tag_votes[name] = vote_counts
        results[name].extend(tracks)
        d(f"└ Community {community_id}: {len(artists)} artists / {len(tracks)} tracks -> named `{name}` "
          f"(tag votes: {vote_counts or 'n/a'}).")

    return dict(results), community_tag_votes


def build_sonic_similarity_graph(all_artists, max_tracks=SONIC_GRAPH_MAX_TRACKS,
                                  neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT, debug=None):
    """
    Builds an undirected, weighted graph of the library's own tracks using
    Plex's sonicallySimilar() as the edge source — the same "similarity
    graph -> community detection" approach as Music-Manager-for-Plex's
    Galaxy tab (see their process_galaxy_data), but at TRACK level using
    real acoustic similarity, rather than at artist level using the
    "Similar Artist" metadata hub.

    One node per track (up to max_tracks, capped by play count so the most-
    listened tracks are prioritized on large libraries — each node costs one
    Plex API call). One edge per sonicallySimilar() match between two
    tracks that are both in the graph, weighted by the actual match score
    (get_sonic_match_percent) so Louvain gives closer sonic matches more
    pull than marginal ones, instead of treating every link as equally
    strong.

    Returns (graph, track_by_key) where track_by_key maps ratingKey -> the
    actual plexapi Track object (graph nodes are bare ratingKeys, since
    networkx/Louvain need hashable, comparison-friendly node IDs).
    """
    d = debug.write if debug else (lambda *a, **k: None)

    if not HAS_SONIC_GRAPH_DEPS:
        raise ImportError(
            "Sonic community-detection clustering requires 'networkx' and 'python-louvain' "
            "(pip install networkx python-louvain)."
        )

    all_tracks = []
    for artist in all_artists:
        try:
            all_tracks.extend(artist.tracks())
        except Exception:
            continue

    if len(all_tracks) > max_tracks:
        all_tracks = sorted(all_tracks, key=lambda t: getattr(t, 'viewCount', 0) or 0, reverse=True)
        all_tracks = all_tracks[:max_tracks]
        d(f"**Sonic graph:** library has more tracks than the {max_tracks}-track cap — "
          f"using the {max_tracks} most-played.")
    else:
        d(f"**Sonic graph:** building from all {len(all_tracks)} tracks.")

    track_by_key = {getattr(t, 'ratingKey', None): t for t in all_tracks}
    graph = nx.Graph()
    graph.add_nodes_from(track_by_key.keys())

    edges_added = 0
    for track in all_tracks:
        key = getattr(track, 'ratingKey', None)
        try:
            matches = track.sonicallySimilar(limit=neighbor_limit)
        except Exception:
            continue
        for m in matches:
            mk = getattr(m, 'ratingKey', None)
            if not mk or mk not in track_by_key or mk == key:
                continue
            pct = get_sonic_match_percent(m)
            weight = (pct / 100.0) if pct is not None else 0.5
            # Two tracks can each nominate the other; keep the stronger of
            # the two match scores if the edge already exists rather than
            # overwriting with whichever direction happened to run last.
            if graph.has_edge(key, mk):
                existing = graph[key][mk].get('weight', 0)
                graph[key][mk]['weight'] = max(existing, weight)
            else:
                graph.add_edge(key, mk, weight=weight)
                edges_added += 1

    d(f"└ Graph built: {graph.number_of_nodes()} tracks, {edges_added} sonic similarity edges.")
    return graph, track_by_key


def build_sonic_clusters(music_section, tag_mapping=None, max_tracks=SONIC_GRAPH_MAX_TRACKS,
                          neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT, resolution=1.0,
                          target_clusters=None, debug=None):
    """
    Clusters the library FROM the sonic-similarity graph itself, via Louvain
    community detection — this is the "sonic analysis actually building the
    clusters" mode, as opposed to tag-based clustering with sonic evidence
    only nudging leftover/uncertain tracks afterward.

    Pipeline:
      1. build_sonic_similarity_graph — one node per track, edges from
         sonicallySimilar(), weighted by match score.
      2. community_louvain.best_partition — groups tracks into communities
         purely by acoustic-similarity structure (modularity optimization).
         `resolution` controls granularity: >1.0 yields more, smaller
         communities; <1.0 yields fewer, larger ones (same knob as
         Music-Manager-for-Plex uses on their artist graph). If
         target_clusters is given, resolution is ignored and
         _auto_tune_resolution searches for whichever resolution produces
         closest to that many communities instead.
      3. NAMING (only place tags/Gemini are involved in this mode): each
         community is named after whichever tag-based cluster is most
         common among its member tracks (via assign_track_cluster against
         the supplied tag_mapping) — tags describe the community after the
         fact, they don't decide its membership. Communities with no clear
         tag majority (or if tag_mapping is None) get a generic "Sonic
         Cluster N" name instead. Two communities that land on the same
         tag-majority name are merged together in the result.

    Returns (results, community_tag_votes) where results is
    {cluster_name: [tracks]} and community_tag_votes is
    {cluster_name: {tag_cluster: count, ...}} for transparency/debugging
    (e.g. showing how "pure" a community's naming vote actually was).
    """
    d = debug.write if debug else (lambda *a, **k: None)

    all_artists = get_cached_artists(music_section, debug=debug)

    graph, track_by_key = build_sonic_similarity_graph(
        all_artists, max_tracks=max_tracks, neighbor_limit=neighbor_limit, debug=debug
    )

    if graph.number_of_nodes() == 0:
        return {}, {}

    if target_clusters:
        partition, resolution, _ = _auto_tune_resolution(graph, target_clusters, debug=debug)
    else:
        partition = community_louvain.best_partition(graph, weight='weight', resolution=resolution, random_state=42)
    communities = defaultdict(list)
    for key, community_id in partition.items():
        communities[community_id].append(track_by_key[key])

    d(f"**Louvain found {len(communities)} raw sonic communities** (resolution={resolution}).")

    results = defaultdict(list)
    community_tag_votes = {}
    for community_id, tracks in communities.items():
        name = f"Sonic Cluster {community_id}"
        vote_counts = {}
        if tag_mapping:
            votes = defaultdict(int)
            for t in tracks:
                votes[assign_track_cluster(t, tag_mapping)] += 1
            vote_counts = dict(votes)
            # "Unsorted" only wins the naming vote if it's the ONLY thing
            # present — a community that's mostly one real genre with a
            # few tag-less stragglers should still be named for the genre.
            named_votes = {k: v for k, v in votes.items() if k != "Unsorted"}
            if named_votes:
                name = max(named_votes, key=named_votes.get)
            elif votes:
                name = f"Sonic Cluster {community_id} (Unsorted)"
        community_tag_votes[name] = vote_counts
        results[name].extend(tracks)
        d(f"└ Community {community_id}: {len(tracks)} tracks -> named `{name}` "
          f"(tag votes: {vote_counts or 'n/a'}).")

    return dict(results), community_tag_votes


def build_genre_clusters(music_section, plex, locked_clusters, total_clusters, api_key,
                          top_n_per_cluster=30, debug=None, force_remap=False, dry_run=False,
                          preloaded_mapping=None, refine_unsorted=True, sonic_weight=0.0,
                          reassign_tagged_via_sonic=False, sonic_propagation_rounds=2,
                          clustering_mode="tags", sonic_max_tracks=SONIC_GRAPH_MAX_TRACKS,
                          sonic_neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT, sonic_resolution=1.0,
                          sonic_group_by="artist", sonic_artist_sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                          hybrid_sonic_weight=0.5, hybrid_tag_overlap_weight=0.15,
                          hybrid_cluster_agreement_weight=0.35, sonic_use_cache=True,
                          sonic_auto_tune_clusters=True):
    """
    Full pipeline. clustering_mode options:

    - "tags" (default): collect genre+mood tags -> tag/cluster mapping
      (disk + memory cached, or reused from a prior Suggest step) -> assign
      every track in the library to a cluster BY TAG -> optionally let
      sonic-neighbor consensus recover Unsorted tracks and/or reassign
      mistagged ones (see refine_unsorted_via_sonic_neighbors) -> blend each
      cluster's final track list from popular + sonically-similar +
      related-artist picks (_blend_cluster_tracks). Tags decide membership;
      sonic analysis only corrects afterward. `sonic_weight` here controls
      that correction pass only (see refine_unsorted_via_sonic_neighbors) —
      it is unrelated to the hybrid_* weights below.

    - "sonic": membership comes from Louvain community detection over a
      similarity graph that genuinely BLENDS sonic and tag signal together
      at the graph-edge level (not tags-then-sonic or sonic-then-tags as two
      separate passes). `sonic_group_by` picks which graph:
        - "artist" (default, recommended): build_artist_sonic_clusters —
          one node per artist, profile built from `sonic_artist_sample_size`
          sampled top tracks (most-played, falling back to Plex-popular).
          Each edge weight is a 3-way blend, each independently weighted:
            - hybrid_sonic_weight: real audio-fingerprint similarity
              between artists' sampled tracks (get_sonic_match_percent).
            - hybrid_tag_overlap_weight: raw Jaccard similarity of the
              artists' own + album genre/mood tags — broad, catches
              similarity the other two signals miss.
            - hybrid_cluster_agreement_weight: 1.0 if both artists' tags
              vote into the SAME Gemini-defined cluster, else 0 — narrow
              but curated, since it's filtered through the actual cluster
              names you asked Gemini to build. Requires a real tag_mapping;
              contributes nothing if dry_run built a throwaway one.
          Weights are normalized to sum to 1 automatically, so e.g.
          (0.5, 0.15, 0.35) and (1.0, 0.3, 0.7) produce the same blend.
          Set any one to 0 to exclude that signal entirely (e.g. sonic=0,
          cluster=1 reproduces pure tag-cluster grouping at artist level).
          Profiles are disk-cached per artist (sonic_use_cache=False to
          force fresh sampling), so this is affordable at full-library
          scale and keeps an artist's whole catalog in one community.
        - "track": build_sonic_clusters — one node per track, capped at
          sonic_max_tracks, sonic-only (no tag signal in membership, tags
          only name the result afterward), no caching. Kept for comparison
          against the hybrid artist mode above.
      `sonic_auto_tune_clusters` (default True): when set, `total_clusters`
      also acts as a target community count for sonic/hybrid mode — a
      small search over Louvain's resolution parameter (see
      _auto_tune_resolution) looks for whichever resolution lands closest
      to that many communities, instead of using `sonic_resolution` as a
      fixed value. This is what makes "Maximum number of clusters" actually
      steer Hybrid mode's output count the way it already steers Gemini's
      tag-cluster count — without it, resolution=1.0 can land anywhere from
      a handful to dozens of communities depending on the library. Set to
      False to use `sonic_resolution` directly instead (manual control).

    Returns (results, tag_mapping) — results is {cluster_name: [tracks]},
    tag_mapping is the raw tag->cluster dict (handy for coloring the
    Library Galaxy tab by cluster without re-deriving it, and used in
    "sonic" mode for community naming and, in "artist" grouping, for the
    cluster_agreement signal too).

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
        if sonic_group_by == "artist":
            d("**Hybrid mode (artist-level):** cluster membership comes from Louvain community "
              f"detection over a blended graph — {hybrid_sonic_weight:g} sonic / "
              f"{hybrid_tag_overlap_weight:g} tag overlap / {hybrid_cluster_agreement_weight:g} "
              "Gemini cluster agreement (normalized). All three signals decide membership together.")
            sonic_pools, community_tag_votes = build_artist_sonic_clusters(
                music_section, tag_mapping=tag_mapping, sample_size=sonic_artist_sample_size,
                neighbor_limit=sonic_neighbor_limit, sonic_weight=hybrid_sonic_weight,
                tag_overlap_weight=hybrid_tag_overlap_weight,
                cluster_agreement_weight=hybrid_cluster_agreement_weight,
                resolution=sonic_resolution,
                target_clusters=total_clusters if sonic_auto_tune_clusters else None,
                use_cache=sonic_use_cache, debug=debug
            )
            tag_suffix = "Hybrid Community"
        else:
            d("**Sonic-only mode (track-level):** cluster membership comes from Louvain "
              "community detection on the sonic-similarity graph; tags are only used to "
              "name the resulting communities.")
            sonic_pools, community_tag_votes = build_sonic_clusters(
                music_section, tag_mapping=tag_mapping, max_tracks=sonic_max_tracks,
                neighbor_limit=sonic_neighbor_limit, resolution=sonic_resolution,
                target_clusters=total_clusters if sonic_auto_tune_clusters else None, debug=debug
            )
            tag_suffix = "Sonic Community"

        results = {}
        for cluster_name, tracks in sonic_pools.items():
            results[cluster_name] = _select_popular(tracks, min(top_n_per_cluster, len(tracks)))
            for t in results[cluster_name]:
                setattr(t, 'recommendation_type', f'{cluster_name} ({tag_suffix})')
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
            cluster_name, tracks, music_section, plex, top_n_per_cluster, tag_mapping, debug=debug
        )

    return results, tag_mapping