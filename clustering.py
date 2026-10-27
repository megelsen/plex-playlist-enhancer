"""Library-wide cluster BUILD: groups the whole library into a set of
clusters and returns their full album membership (build_clusters). Two
methods:

  - "tags": Gemini maps every genre/mood tag to a cluster (some locked in by
    name), every track is assigned by its tags (optionally corrected via
    sonic neighbours), and each album goes to the cluster holding most of
    its tracks.
  - "sonic" (Relational): Louvain communities over Plex's Similar-Artist
    graph (optionally sonic-boosted); each community's artists contribute
    all their albums.

The build is a starting point: committing it seeds the album cluster
registry (cluster_registry.py), which owns the live clusters, playlists
and Live Mixes from then on. Nothing here picks playlist tracks.

The only network/LLM cost in this whole pipeline is the single Gemini
call in build_tag_cluster_mapping — everything else is local plexapi
work. That call is wrapped in a Streamlit cache (see
get_cached_tag_cluster_mapping) so it only re-runs when the underlying
tag list, locked clusters, total count, or prompt logic actually change.
"""

import hashlib
import json
import os
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

_ARTIST_SCAN_CACHE = {}
ARTIST_SCAN_TTL_SECONDS = 900

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

SONIC_GRAPH_NEIGHBOR_LIMIT = 15

PROMPT_VERSION = 3

GEMINI_TIMEOUT_SECONDS = 180
GEMINI_MAX_RETRIES = 3

REFINE_MAX_TRACKS = 300

REFINE_MIN_NEIGHBOR_VOTES = 6
REFINE_MIN_VOTE_MARGIN = 4
REFINE_MIN_VOTE_SHARE = 0.6

MIN_CLUSTER_TRACKS = 5

DISK_CACHE_PATH = os.environ.get("CLUSTER_CACHE_PATH", "/app/data/cluster_mapping.json")

ARTIST_PROFILE_CACHE_PATH = os.environ.get("ARTIST_PROFILE_CACHE_PATH", "/app/data/artist_sonic_profiles.json")

ARTIST_PROFILE_VERSION = 1

ARTIST_SONIC_SAMPLE_SIZE = 3


def _disk_cache_key(genre_tags, mood_tags, locked_clusters, total_clusters):
    payload = json.dumps({
        "genre_tags": sorted(genre_tags),
        "mood_tags": sorted(mood_tags),
        "locked_clusters": sorted(locked_clusters),
        "total_clusters": total_clusters,
        "prompt_version": PROMPT_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_disk_cache():
    try:
        with open(DISK_CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _save_disk_cache(key, clusters, mapping):
    try:
        os.makedirs(os.path.dirname(DISK_CACHE_PATH), exist_ok=True)
        with open(DISK_CACHE_PATH, "w") as f:
            json.dump({"key": key, "clusters": clusters, "mapping": mapping}, f)
    except Exception:
        pass


def get_cached_artists(music_section, debug=None, force_refresh=False):
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
    _ARTIST_SCAN_CACHE.clear()


def clear_all_caches(debug=None):
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
            genre_tags |= artist_genres
    return sorted(genre_tags), sorted(mood_tags)


def build_tag_cluster_mapping(genre_tags, mood_tags, locked_clusters, total_clusters, api_key, model=None,
                               timeout=GEMINI_TIMEOUT_SECONDS, max_retries=GEMINI_MAX_RETRIES):
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
                time.sleep(2 * (attempt + 1))
                continue
        except (requests.exceptions.RequestException, KeyError, json.JSONDecodeError) as e:
            raise

    total_tags = len(genre_tags) + len(mood_tags)
    raise TimeoutError(
        f"Gemini didn't respond within {timeout}s after {max_retries + 1} attempt(s), "
        f"classifying {total_tags} tags. If this keeps happening, try a smaller "
        f"library section, fewer total_clusters, or check aistudio.google.com for "
        f"an API outage."
    ) from last_error


def assign_artist_cluster(artist_genre_tags, tag_mapping):
    votes = defaultdict(int)
    for tag in artist_genre_tags:
        cluster = tag_mapping.get(tag)
        if cluster:
            votes[cluster] += 1
    if not votes:
        return "Unsorted"
    return max(votes, key=votes.get)


def assign_track_cluster(track, tag_mapping):
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
    return build_tag_cluster_mapping(
        list(genre_tags_tuple), list(mood_tags_tuple), list(locked_clusters_tuple),
        total_clusters, api_key
    )


def get_tag_cluster_mapping(genre_tags, mood_tags, locked_clusters, total_clusters, api_key, force_remap=False):
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
    return build_tag_cluster_mapping(genre_tags, mood_tags, [], total_clusters, api_key)


def build_dry_run_mapping(genre_tags, mood_tags, locked_clusters, total_clusters):
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


def _sonic_thresholds_for_weight(sonic_weight):
    w = max(0.0, min(1.0, sonic_weight))
    min_votes = round(REFINE_MIN_NEIGHBOR_VOTES - w * 3)
    min_margin = round(REFINE_MIN_VOTE_MARGIN - w * 2)
    min_share = REFINE_MIN_VOTE_SHARE - w * 0.15
    return max(min_votes, 2), max(min_margin, 1), max(min_share, 0.4)


def _sonic_thresholds_for_reassignment(sonic_weight):
    w = max(0.0, min(1.0, sonic_weight))
    min_votes = round(REFINE_MIN_NEIGHBOR_VOTES - w * 2)
    min_margin = round((REFINE_MIN_VOTE_MARGIN + 2) - w * 2)
    min_share = (REFINE_MIN_VOTE_SHARE + 0.15) - w * 0.15
    return max(min_votes, 3), max(min_margin, 2), max(min_share, 0.55)


def refine_unsorted_via_sonic_neighbors(pools, sonic_weight=0.0, reassign_tagged=False,
                                         max_tracks_to_check=REFINE_MAX_TRACKS,
                                         propagation_rounds=2, debug=None):
    d = debug.write if debug else (lambda *a, **k: None)

    has_unsorted = bool(pools.get("Unsorted"))
    if not has_unsorted and not reassign_tagged:
        return pools

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


def apply_merge_plan(build, merge_plan):
    """Returns a copy of `build` with each merge group's member clusters
    combined under the group's new name (album lists unioned, tag mapping
    relabelled). Pure local operation."""
    if not build or not merge_plan:
        return build
    rename = {m: g["new_name"] for g in merge_plan for m in g["members"]}
    clusters = defaultdict(set)
    for name, keys in build["clusters"].items():
        clusters[rename.get(name, name)].update(keys)
    merged = dict(build)
    merged["clusters"] = {name: sorted(keys) for name, keys in clusters.items()}
    merged["tag_mapping"] = {t: rename.get(c, c) for t, c in (build.get("tag_mapping") or {}).items()}
    merged["locked"] = sorted({rename.get(n, n) for n in build.get("locked", [])})
    return merged


def artist_cluster_map_from_albums(clusters, albums):
    """{artist ratingKey (str): cluster name} by majority of the artist's
    albums — what Library Galaxy colours nodes by."""
    votes = defaultdict(lambda: defaultdict(int))
    for name, keys in clusters.items():
        for k in keys:
            artist_key = (albums.get(k) or {}).get("artist_key")
            if artist_key:
                votes[artist_key][name] += 1
    return {a: max(c, key=c.get) for a, c in votes.items()}


def build_artist_cluster_map(results):
    votes = defaultdict(lambda: defaultdict(int))
    for cluster_name, tracks in results.items():
        for t in tracks:
            artist_key = getattr(t, 'grandparentRatingKey', None)
            if artist_key is not None:
                votes[artist_key][cluster_name] += 1
    return {artist_key: max(counts, key=counts.get) for artist_key, counts in votes.items()}


CLUSTER_RESULTS_CACHE_PATH = os.environ.get("CLUSTER_RESULTS_CACHE_PATH", "/app/data/cluster_results.json")


BUILD_VERSION = 2


def save_build_cache(build, merge_plan=None):
    """Persists the latest build (full album membership, v2 format) plus the
    pending merge plan, so the review survives a restart and the first
    weekly curation can bootstrap from it."""
    try:
        os.makedirs(os.path.dirname(CLUSTER_RESULTS_CACHE_PATH), exist_ok=True)
        tmp = CLUSTER_RESULTS_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({**build, "merge_plan": merge_plan or []}, f)
        os.replace(tmp, CLUSTER_RESULTS_CACHE_PATH)
    except Exception:
        pass


def load_build_cache():
    """(build, merge_plan) from the v2 cache, or (None, []) if there is none
    (or only a legacy v1 track-sample cache)."""
    try:
        with open(CLUSTER_RESULTS_CACHE_PATH, "r") as f:
            payload = json.load(f)
    except Exception:
        return None, []
    if payload.get("version") != BUILD_VERSION:
        return None, []
    merge_plan = payload.pop("merge_plan", [])
    return payload, merge_plan


def load_cluster_results_cache(plex, debug=None):
    """LEGACY: reads a v1 cache ({cluster_name: [track ratingKeys]} — the
    old ~30-track display sample). Returns (None, None, {}) for v2 files."""
    d = debug.write if debug else (lambda *a, **k: None)
    try:
        with open(CLUSTER_RESULTS_CACHE_PATH, "r") as f:
            payload = json.load(f)
    except Exception:
        return None, None, {}
    if payload.get("version") == BUILD_VERSION:
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
    try:
        with open(ARTIST_PROFILE_CACHE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_artist_profile_cache(cache):
    try:
        os.makedirs(os.path.dirname(ARTIST_PROFILE_CACHE_PATH), exist_ok=True)
        with open(ARTIST_PROFILE_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _artist_fingerprint(artist, sample_size, neighbor_limit):
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
    genre_tags, mood_tags = _track_genre_and_mood_tags(track)
    return genre_tags | mood_tags


def _most_common_tag(tag_iterable):
    counts = defaultdict(int)
    for t in tag_iterable:
        counts[t] += 1
    if not counts:
        return None
    return max(counts, key=counts.get)


ALWAYS_GENERIC_NAME_TAGS = {"music", "other", "unknown", "misc", "miscellaneous", "various", "general"}


def _is_always_generic_tag(tag):
    return tag is not None and tag.strip().lower() in ALWAYS_GENERIC_NAME_TAGS


def _representative_names(items, is_track, max_names=2):
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
    d = debug.write if debug else (lambda *a, **k: None)

    per_community = {}
    genre_users = defaultdict(set)
    mood_users = defaultdict(set)
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
            names = _representative_names(community_members[community_id], is_track, max_names=2)
            if names:
                result[community_id] = (f"{base_name} ({' & '.join(names)})", vote_counts)

    return result


def _finalize_community_name(base_name, used_names):
    final = base_name
    n = 2
    while final in used_names:
        final = f"{base_name} ({n})"
        n += 1
    used_names.add(final)
    return final



def get_artist_genre_and_mood_tags(artist):
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
    genre_tags, mood_tags = get_artist_genre_and_mood_tags(artist)
    return genre_tags | mood_tags


def build_artist_sonic_profile(artist, sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                                neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT, cache=None, debug=None):
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
    d = debug.write if debug else (lambda *a, **k: None)

    communities = defaultdict(list)
    for key, cid in partition.items():
        communities[cid].append(key)

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


# Relational mode ranks communities by connection strength x "completion".
# Completion used to be derived from the playlist settings (30 tracks / 3 per
# artist = 10 artists); it's now measured in albums, independent of playlists.
FULL_COMMUNITY_ALBUMS = 20


def _score_and_select_communities(graph, communities, target_count, album_counts, debug=None):
    d = debug.write if debug else (lambda *a, **k: None)

    scored = []
    for cid, members in communities.items():
        subgraph = graph.subgraph(members)
        weights = [w for _, _, w in subgraph.edges(data='weight', default=1.0)]
        connection_strength = (sum(weights) / len(weights)) if weights else 0.0
        albums = sum(album_counts.get(m, 0) for m in members)
        completion = min(1.0, albums / FULL_COMMUNITY_ALBUMS)
        score = connection_strength * completion
        scored.append((cid, score, connection_strength, completion))

    scored.sort(key=lambda x: x[1], reverse=True)
    kept = scored[:target_count]
    dropped = scored[target_count:]

    d(f"**Ranked {len(scored)} candidate communities, keeping the top {len(kept)}** "
      f"(target {target_count}). Kept scores: "
      f"{[round(s, 2) for _, s, _, _ in kept]}." +
      (f" Dropped {len(dropped)} weaker/thinner communities — their albums are scored "
       "individually when you commit." if dropped else ""))

    return {cid: communities[cid] for cid, _, _, _ in kept}


def build_artist_sonic_clusters(music_section, albums_by_artist, tag_mapping=None,
                                 sample_size=ARTIST_SONIC_SAMPLE_SIZE, neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                                 sonic_boost=0.5, target_count=10, use_cache=True, api_key=None, debug=None):
    """Relational mode: Louvain over the Similar-Artist graph. Returns
    ({cluster_name: [artist objects]}, {cluster_name: tag vote counts})."""
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
    album_counts = {k: len(v) for k, v in albums_by_artist.items()}
    selected_by_key = _score_and_select_communities(
        graph, communities_by_key, target_count, album_counts, debug=debug
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
        community_tag_votes[name] = vote_counts
        results[name] = artists
        n_albums = sum(album_counts.get(str(a.ratingKey), 0) for a in artists)
        d(f"\u2514 Community {community_id}: {len(artists)} artists / {n_albums} albums -> named `{name}` "
          f"(tag votes: {vote_counts or 'n/a'}).")

    return results, community_tag_votes


def _albums_by_artist(music_section, debug=None):
    """{artist ratingKey (str): [album objects]} from one library-wide listing."""
    d = debug.write if debug else (lambda *a, **k: None)
    try:
        albums = music_section.albums()
    except Exception as e:
        d(f"\u274c Album listing failed: `{e}`")
        return {}
    out = defaultdict(list)
    for album in albums:
        out[str(getattr(album, 'parentRatingKey', '') or '')].append(album)
    return out


def _album_info(album):
    return {
        "title": getattr(album, 'title', None),
        "artist": getattr(album, 'parentTitle', None),
        "artist_key": str(getattr(album, 'parentRatingKey', '') or ''),
        "plays": getattr(album, 'viewCount', 0) or 0,
    }


def build_clusters(music_section, plex, locked_clusters, total_clusters, api_key,
                   debug=None, force_remap=False, dry_run=False, preloaded_mapping=None,
                   refine_unsorted=True, sonic_weight=0.0, reassign_tagged_via_sonic=False,
                   sonic_propagation_rounds=2, clustering_mode="tags",
                   sonic_neighbor_limit=SONIC_GRAPH_NEIGHBOR_LIMIT,
                   sonic_artist_sample_size=ARTIST_SONIC_SAMPLE_SIZE,
                   sonic_boost=0.5, sonic_use_cache=True):
    """Builds clusters over the WHOLE library and returns their full album
    membership:

      {"version": 2, "mode", "built_at", "total_clusters", "locked",
       "tag_mapping": {tag: cluster},
       "clusters": {cluster_name: [album ratingKey (str), ...]},
       "unsorted": [album keys no cluster claimed],
       "albums": {album key: {"title", "artist", "artist_key", "plays"}}}

    "plays" is a play count (integer)."""
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

    albums_by_artist = _albums_by_artist(music_section, debug=debug)
    album_info = {str(a.ratingKey): _album_info(a) for albs in albums_by_artist.values() for a in albs}
    membership = defaultdict(set)

    if clustering_mode == "sonic":
        d(f"**Relational mode:** cluster membership comes from Louvain community detection over "
          f"Plex's own Similar-Artist graph (sonic_boost={sonic_boost:g}); tags only name the "
          f"result. Keeping the top {total_clusters} communities by connection strength \u00d7 size; "
          "each community's artists bring ALL their albums.")
        communities, _tag_votes = build_artist_sonic_clusters(
            music_section, albums_by_artist, tag_mapping=tag_mapping,
            sample_size=sonic_artist_sample_size, neighbor_limit=sonic_neighbor_limit,
            sonic_boost=sonic_boost, target_count=total_clusters, use_cache=sonic_use_cache,
            api_key=(None if dry_run else api_key), debug=debug
        )
        for name, artists in communities.items():
            for artist in artists:
                for album in albums_by_artist.get(str(artist.ratingKey), []):
                    membership[name].add(str(album.ratingKey))
    else:
        pools = defaultdict(list)
        for artist in get_cached_artists(music_section, debug=debug):
            try:
                tracks = artist.tracks()
            except Exception:
                continue
            for t in tracks:
                pools[assign_track_cluster(t, tag_mapping)].append(t)
        for cluster, tracks in pools.items():
            d(f"\u2514 `{cluster}`: {len(tracks)} tracks pooled.")

        if not dry_run:
            pools, clusters, tag_mapping = _fold_thin_clusters(pools, clusters, tag_mapping, locked_clusters, debug=debug)
        if (refine_unsorted or reassign_tagged_via_sonic) and not dry_run:
            pools = refine_unsorted_via_sonic_neighbors(
                pools, sonic_weight=sonic_weight, reassign_tagged=reassign_tagged_via_sonic,
                propagation_rounds=sonic_propagation_rounds, debug=debug
            )

        # Album = the cluster holding most of its tracks (tagged tracks win ties
        # against Unsorted), so an artist whose albums span genres stays split.
        votes = defaultdict(lambda: defaultdict(int))
        for cluster, tracks in pools.items():
            for t in tracks:
                album_key = str(getattr(t, 'parentRatingKey', '') or '')
                if not album_key:
                    continue
                votes[album_key][cluster] += 1
                if album_key not in album_info:
                    album_info[album_key] = {
                        "title": getattr(t, 'parentTitle', None), "artist": getattr(t, 'grandparentTitle', None),
                        "artist_key": str(getattr(t, 'grandparentRatingKey', '') or ''), "plays": 0,
                    }
        for album_key, counts in votes.items():
            tagged = {c: n for c, n in counts.items() if c != "Unsorted"}
            if tagged and sum(tagged.values()) >= counts.get("Unsorted", 0):
                membership[max(tagged, key=tagged.get)].add(album_key)

    claimed = set().union(*membership.values()) if membership else set()
    unsorted = sorted(k for k in album_info if k not in claimed)
    result_clusters = {name: sorted(keys) for name, keys in membership.items() if keys}
    d(f"**Build done:** {len(result_clusters)} clusters covering {len(claimed)} of {len(album_info)} albums "
      f"({len(unsorted)} unsorted).")
    return {
        "version": BUILD_VERSION,
        "mode": clustering_mode,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_clusters": int(total_clusters),
        "locked": list(locked_clusters),
        "tag_mapping": tag_mapping,
        "clusters": result_clusters,
        "unsorted": unsorted,
        "albums": album_info,
    }
