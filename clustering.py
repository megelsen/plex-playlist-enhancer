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

from plex_helpers import get_sonic_match_percent, get_top_tracks_for_artist

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Bumped whenever build_tag_cluster_mapping's prompt logic changes in a way
# that would produce different results for the same tag list. Included in
# the cache key so a prompt fix takes effect immediately without requiring
# the user to manually click "Force Re-map Genres".
PROMPT_VERSION = 2

# Gemini can take a while on larger tag lists (bigger prompt = longer
# generation), so give it real headroom rather than the default requests
# timeout. Retries use exponential backoff for transient slowness/hiccups.
GEMINI_TIMEOUT_SECONDS = 180
GEMINI_MAX_RETRIES = 3

# Cap on how many fully-untagged ("core-to-perimeter" refinement candidate)
# artists get a similar-artist lookup per build — each candidate costs one
# Plex API call, so this bounds worst-case runtime on very large libraries.
# Candidates are prioritized by play count, so the most-listened untagged
# artists get refined first if the library has more than this many.
REFINE_MAX_ARTISTS = 200

# Minimum number of similar-artist neighbors that must agree on a cluster
# before a fully-untagged artist gets reassigned out of "Unsorted". A single
# matching neighbor was too weak a signal and was itself contributing to
# clusters drifting broad/mixed — this (plus requiring a clear margin over
# the runner-up cluster, enforced in refine_unsorted_via_similarity) makes
# the refinement pass much more conservative.
REFINE_MIN_NEIGHBOR_VOTES = 2

# Disk-based cache for the tag->cluster mapping, so it survives container
# restarts/rebuilds (unlike st.cache_data, which is in-memory only and
# resets whenever the process restarts). Point CLUSTER_CACHE_PATH at a
# mounted volume in Docker so it actually persists across rebuilds.
DISK_CACHE_PATH = os.environ.get("CLUSTER_CACHE_PATH", "/app/data/cluster_mapping.json")


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
    try:
        artists = music_section.searchArtists()
    except Exception:
        artists = []
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
    One Gemini call that both (a) invents the remaining cluster names beyond
    the ones the user locked in, and (b) maps every genre AND mood tag to
    exactly one of the final cluster names. Returns (cluster_names,
    tag_to_cluster dict) — the dict has both genre and mood tag strings as
    keys, so callers don't need to know which kind a tag was.

    locked_clusters: list of cluster names the user wants kept as-is, e.g.
        ["Metal", "Fast Paced"] — these are NOT genre tags themselves, they're
        the target buckets; Gemini must still decide which raw tags fall
        under them.

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

    prompt = f"""You are organizing a music library into exactly {total_clusters} clusters.
Clusters can represent genre families, moods/vibes, or both — whatever best
groups the tags below.

These {len(locked_clusters)} cluster names are FIXED and must be used exactly as given:
{locked_clusters}

Invent {remaining} additional cluster name(s) that best cover the remaining
tags below (genre families or moods not covered by the fixed clusters).

CRITICAL RULES for assigning tags to clusters:
1. Group a genre with ALL of its subgenres, styles, and variants under one
   umbrella. For example, if a cluster is "Metal", every metal-family tag —
   metalcore, black metal, death metal, doom metal, nu-metal, thrash,
   classic metal, mainstream/commercial metal, etc. — belongs in "Metal",
   not spread across other clusters. The same applies to any other broad
   genre family (Rock, Indie, Folk, Electronic, etc.): sweep in every
   stylistic variant of that family.
2. Do NOT cluster or split tags by nationality, region, or language
   (e.g. "German", "Turkish", "Korean", "French") unless one of the FIXED
   cluster names explicitly asks for that. A tag like "German Rock" belongs
   with other Rock tags in the Rock/umbrella cluster, not grouped with
   unrelated genres just because they share a nationality modifier or a
   vaguely similar-sounding name.
3. Mood tags (e.g. "Aggressive", "Chill", "Melancholic", "Upbeat") describe
   vibe, not genre — only group two mood tags together, or a mood with a
   genre, if they are actually thematically related. Do not merge unrelated
   items just because you're running short on distinct clusters.
4. Every single tag below (genre and mood) must be assigned to exactly one
   of the {total_clusters} final cluster names. No tag left unassigned.

Genre tags to classify:
{genre_tags}

Mood tags to classify:
{mood_tags}

Respond with ONLY a JSON object in this exact shape, nothing else, no markdown
fences, no commentary:
{{
  "clusters": ["Cluster1", "Cluster2", ...],   // all {total_clusters} final names
  "mapping": {{"tag1": "Cluster1", "tag2": "Cluster2", ...}}  // every genre AND mood tag
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


def refine_unsorted_via_similarity(pools, all_artists, debug=None):
    """
    "Core-to-perimeter" refinement pass: tag-based assignment alone leaves
    some artists with zero usable genre/mood signal, and every one of their
    tracks lands in "Unsorted" regardless of how well they'd actually fit a
    real cluster. This pass fixes that using a signal tag-voting can't see —
    Plex's "Similar Artist" links — without any extra Gemini calls.

    1. "Core": any artist with at least one track that landed in a real
       (non-Unsorted) cluster gets a confident label — the majority cluster
       among that artist's own already-assigned tracks.
    2. "Periphery": artists with NO tracks in any real cluster (fully
       Unsorted) are the refinement candidates.
    3. For each periphery artist (capped at REFINE_MAX_ARTISTS, prioritized
       by play count), look at their Plex similar-artist neighbors. If any
       neighbors are core artists, move this artist's tracks out of
       "Unsorted" into the majority cluster among those neighbors.

    Mutates and returns `pools` in place. Artists with no core neighbors at
    all remain in "Unsorted" — this only recovers cases where the
    similarity graph actually has something to go on.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    if "Unsorted" not in pools or not pools["Unsorted"]:
        return pools

    track_cluster_by_key = {}
    for cluster_name, tracks in pools.items():
        for t in tracks:
            track_cluster_by_key[getattr(t, 'ratingKey', None)] = cluster_name

    artist_known_cluster = {}
    artist_all_unsorted_keys = set()
    artist_tracks_by_key = {}
    for artist in all_artists:
        rk = getattr(artist, 'ratingKey', None)
        if rk is None:
            continue
        try:
            a_tracks = artist.tracks()
        except Exception:
            continue
        artist_tracks_by_key[rk] = a_tracks
        votes = defaultdict(int)
        for t in a_tracks:
            c = track_cluster_by_key.get(getattr(t, 'ratingKey', None))
            if c and c != "Unsorted":
                votes[c] += 1
        if votes:
            artist_known_cluster[rk] = max(votes, key=votes.get)
        else:
            artist_all_unsorted_keys.add(rk)

    if not artist_all_unsorted_keys:
        return pools

    # O(1) neighbor lookup by title instead of re-scanning all_artists per
    # candidate — built once up front.
    title_to_known_cluster = {
        a.title.lower(): artist_known_cluster[a.ratingKey]
        for a in all_artists
        if getattr(a, 'ratingKey', None) in artist_known_cluster
    }

    d(f"**Core-to-perimeter refinement:** {len(artist_all_unsorted_keys)} artists have no direct "
      f"genre/mood signal at all — checking similar artists for a confident neighbor label.")

    candidates = [a for a in all_artists if getattr(a, 'ratingKey', None) in artist_all_unsorted_keys]
    candidates = sorted(candidates, key=lambda a: getattr(a, 'viewCount', 0) or 0, reverse=True)
    if len(candidates) > REFINE_MAX_ARTISTS:
        d(f"└ Capping refinement to the {REFINE_MAX_ARTISTS} most-played of these artists.")
        candidates = candidates[:REFINE_MAX_ARTISTS]

    refined_count = 0
    for artist in candidates:
        rk = artist.ratingKey
        try:
            similar_attr = getattr(artist, 'similar', None)
            similar = similar_attr() if callable(similar_attr) else (similar_attr or [])
        except Exception:
            continue

        neighbor_votes = defaultdict(int)
        for sim in (similar or []):
            name = getattr(sim, 'tag', None)
            if not name:
                continue
            known_cluster = title_to_known_cluster.get(name.lower())
            if known_cluster:
                neighbor_votes[known_cluster] += 1

        if not neighbor_votes:
            continue

        sorted_votes = sorted(neighbor_votes.values(), reverse=True)
        top_votes = sorted_votes[0]
        runner_up_votes = sorted_votes[1] if len(sorted_votes) > 1 else 0

        # Require real consensus, not a single lucky match: at least 2
        # neighbors voting for the same cluster, AND a clear margin over
        # whatever the second-place cluster got. A single matching neighbor
        # was too weak a signal and was itself contributing to clusters
        # drifting broad/mixed.
        if top_votes < REFINE_MIN_NEIGHBOR_VOTES or top_votes <= runner_up_votes:
            continue

        new_cluster = max(neighbor_votes, key=neighbor_votes.get)
        moved_tracks = artist_tracks_by_key.get(rk, [])
        moved_keys = {getattr(t, 'ratingKey', None) for t in moved_tracks}
        pools["Unsorted"] = [t for t in pools["Unsorted"] if getattr(t, 'ratingKey', None) not in moved_keys]
        pools.setdefault(new_cluster, []).extend(moved_tracks)
        refined_count += 1

    d(f"└ Reassigned {refined_count} of {len(candidates)} checked artists via similar-artist neighbor signal.")
    return pools


def build_genre_clusters(music_section, plex, locked_clusters, total_clusters, api_key,
                          top_n_per_cluster=30, debug=None, force_remap=False, dry_run=False,
                          preloaded_mapping=None, refine_unsorted=True):
    """
    Full pipeline: collect genre+mood tags -> tag/cluster mapping (disk +
    memory cached, or reused from a prior Suggest step) -> assign every
    track in the library to a cluster -> blend each cluster's final track
    list from popular + sonically-similar + related-artist picks (see
    _blend_cluster_tracks), same spirit as Artist Mix.

    Returns (results, tag_mapping) — results is {cluster_name: [tracks]},
    tag_mapping is the raw tag->cluster dict (handy for coloring the
    Library Galaxy tab by cluster without re-deriving it).

    Set dry_run=True to skip Gemini entirely and use build_dry_run_mapping
    instead — useful for testing the rest of the pipeline (track
    assignment, blending, UI, playlist saving) at zero cost while iterating.
    Clusters won't be meaningful in dry_run mode, just structurally present.

    preloaded_mapping: optional (clusters, tag_mapping) tuple from a prior
    suggest_cluster_names() call — if the user accepted the suggestions
    unchanged, this skips a second Gemini call entirely.

    The tag/cluster mapping is cached on two layers (see
    get_tag_cluster_mapping): in-memory for the process's lifetime, and on
    disk so it survives container restarts/rebuilds. It only re-runs when
    the tag lists, locked clusters, total count, or prompt version change,
    or when force_remap=True. Track assignment and the popular/sonic/related
    blend are pure-local plexapi/Python work with no LLM cost, so those
    always run fresh to pick up newly added tracks, updated play counts, or
    new sonic/related matches.
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

    pools = defaultdict(list)
    try:
        all_artists = music_section.searchArtists()
    except Exception:
        all_artists = []

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

    if refine_unsorted and not dry_run:
        pools = refine_unsorted_via_similarity(pools, all_artists, debug=debug)

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
