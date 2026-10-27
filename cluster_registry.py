"""Album-level cluster registry: the persistent, auto-curated source of truth
for which cluster every album in the library belongs to.

Library Clusters' build (clustering.py) produces a one-off grouping plus a
~30-track display sample per cluster. This module turns that into a
registry that covers EVERY album, keeps stable cluster IDs across rebuilds
(so Live Mixes can follow a cluster even when it's renamed, merged or
split), and can be updated incrementally:

  - bootstrap_from_results(): seeds clusters from a Library Clusters build
    (each built cluster's artists -> all their albums), then scores every
    album against every cluster and only keeps albums that clear a
    threshold. Albums that don't fit anywhere try to form new clusters
    among themselves; whatever's left lands in the system "Unclustered"
    cluster.
  - sync_new_albums(): daily, cheap — scores only albums added since the
    last scan against the existing clusters. Never creates/removes clusters.
  - curate(mode="weekly"): the structural pass used by curation.py —
    hysteresis-gated moves, retire thin clusters, merge near-duplicates,
    split loose/oversized ones, spawn new ones from Unclustered.

Scores are unitless 0..1 affinities (see _Scorer). Every input is either a
0..1 similarity (Plex sonic match % / 100, tag cosine, shares of albums) or
a play count (an integer number of plays, from Plex history over
`history_days` days) — nothing here is in hours/seconds.

Per-album features (tags + sonic-neighbour albums) are cached in
ALBUM_FEATURES_PATH, keyed by album ratingKey with a fingerprint on the
album's updatedAt, so only new/changed albums ever cost Plex lookups.
"""

import json
import logging
import math
import os
import threading
import uuid
from collections import defaultdict
from datetime import datetime

from clustering import (
    HAS_SONIC_GRAPH_DEPS, _build_plex_similar_edges, _finalize_community_name, _is_always_generic_tag,
    _llm_name_fallback_communities, build_artist_cluster_map, get_cached_artists,
)
from plex_helpers import get_sonic_match_percent

if HAS_SONIC_GRAPH_DEPS:
    import networkx as nx
    import community as community_louvain

logger = logging.getLogger("cluster_registry")

REGISTRY_PATH = os.environ.get("CLUSTER_REGISTRY_PATH", "/app/data/cluster_registry.json")
ALBUM_FEATURES_PATH = os.environ.get("ALBUM_FEATURES_PATH", "/app/data/album_features.json")
SNAPSHOT_DIR = REGISTRY_PATH + ".snapshots"
SNAPSHOT_KEEP = 8

ALBUM_FEATURE_VERSION = 1
ALBUM_SONIC_SAMPLE_SIZE = 2
ALBUM_SONIC_NEIGHBOR_LIMIT = 15

# Same-artist albums are linked in the album graph (for Louvain splits /
# spawns) and count toward the "artist" affinity component — except for
# artists with a huge album count (compilations, "Various Artists"), where
# sharing an artist says nothing about sound.
SAME_ARTIST_MAX_ALBUMS = 40
SAME_ARTIST_EDGE_WEIGHT = 0.3

# Tag vector weights per tag source (unitless, relative).
TAG_WEIGHT_GENRE = 1.0
TAG_WEIGHT_MOOD = 0.6
TAG_WEIGHT_ARTIST_GENRE = 0.4

UNCLUSTERED_ID = "c_unclustered"
UNCLUSTERED_NAME = "Unclustered"

FEATURES_CHECKPOINT_EVERY = 50

DEFAULT_PARAMS = {
    # Assignment (all 0..1 affinity units)
    "assign_threshold": 0.35,      # min score to join a cluster
    "assign_margin": 0.05,         # min lead over the runner-up cluster
    "move_hysteresis": 0.08,       # min gain over the current cluster to move
    "eject_threshold": 0.15,       # below this (and nothing better) -> Unclustered
    "max_move_share": 0.10,        # max share of a cluster's albums moved out per week
    # Affinity component weights (relative)
    "w_sonic": 0.5,
    "w_tag": 0.3,
    "w_artist": 0.2,
    # Structure
    "max_clusters": 0,             # hard cap on clusters (Unclustered not counted); 0 = no cap.
                                   # Set from the build's "Maximum number of clusters" on commit.
    "max_splits_per_run": 2,
    "min_cluster_albums": 6,
    "max_cluster_albums": 150,
    "split_min_modularity": 0.30,
    "split_min_internal": 0.70,    # share of edge weight that must stay inside the split parts
    "merge_ratio": 0.70,           # inter-affinity >= ratio * mean cohesion -> merge
    "spawn_cohesion": 0.35,
    "max_new_clusters": 3,
    # Listening habits
    "history_days": 180,           # days of Plex play history read per curation
    "mixfit_min_exposures": 3,     # Live Mix rotations an album must have sat through
    "mixfit_penalty": 0.05,        # affinity subtracted for a never-played album
    # Weekly guardrails (abort the run if exceeded)
    "guard_max_moved_share": 0.15,
    "guard_max_structural_share": 0.30,
}

# Serialises every job that writes the registry (scheduled daily/weekly
# jobs and UI-triggered background jobs). Callers acquire it; the
# functions in this module never do, so they can be composed freely.
JOB_LOCK = threading.Lock()
_IO_LOCK = threading.RLock()


def _writer(debug):
    return debug.write if debug else (lambda *a, **k: None)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _assigned(cid):
    return bool(cid) and cid != UNCLUSTERED_ID


def _new_cluster_id():
    return f"c_{uuid.uuid4().hex[:10]}"


# --- Persistence ----------------------------------------------------------

def _atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def empty_registry(params=None):
    return {
        "version": 1,
        "generation": 0,
        "params": {**DEFAULT_PARAMS, **(params or {})},
        "clusters": {},
        "albums": {},
        "pending_moves": {},
        "last_scan_at": None,
        "updated_at": None,
    }


def _ensure_unclustered(reg):
    reg["clusters"].setdefault(UNCLUSTERED_ID, {
        "name": UNCLUSTERED_NAME, "locked": False, "system": True,
        "created_gen": reg.get("generation", 0), "updated_gen": reg.get("generation", 0),
        "origin": "system", "stats": {},
    })


def load_registry():
    """Returns the registry dict, or None if no registry has been committed yet."""
    with _IO_LOCK:
        reg = _read_json(REGISTRY_PATH)
    if not reg or not reg.get("clusters"):
        return None
    reg["params"] = {**DEFAULT_PARAMS, **(reg.get("params") or {})}
    reg.setdefault("albums", {})
    reg.setdefault("pending_moves", {})
    _ensure_unclustered(reg)
    return reg


def save_registry(reg):
    reg["updated_at"] = _now_iso()
    with _IO_LOCK:
        _atomic_write_json(REGISTRY_PATH, reg)


def load_album_features():
    store = _read_json(ALBUM_FEATURES_PATH) or {}
    store.setdefault("albums", {})
    store.setdefault("artist_similar", {})
    return store


def save_album_features(store):
    with _IO_LOCK:
        _atomic_write_json(ALBUM_FEATURES_PATH, store)


def snapshot_registry(reg):
    """Saves a copy of `reg` as gen{N}.json (N = its current generation) and
    prunes to the newest SNAPSHOT_KEEP. Returns the snapshot's generation."""
    gen = reg.get("generation", 0)
    _atomic_write_json(os.path.join(SNAPSHOT_DIR, f"gen{gen:04d}.json"), reg)
    snaps = list_snapshots()
    for old_gen, path in snaps[:-SNAPSHOT_KEEP]:
        try:
            os.remove(path)
        except Exception:
            pass
    return gen


def list_snapshots():
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    out = []
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname.startswith("gen") and fname.endswith(".json"):
            try:
                out.append((int(fname[3:-5]), os.path.join(SNAPSHOT_DIR, fname)))
            except ValueError:
                continue
    return sorted(out)


def load_snapshot(gen):
    for g, path in list_snapshots():
        if g == gen:
            return _read_json(path)
    return None


# --- Small UI-facing helpers ---------------------------------------------

def find_cluster_id_by_name(reg, name):
    for cid, c in reg["clusters"].items():
        if c.get("name") == name:
            return cid
    return None


def cluster_album_keys(reg, cid):
    return [a for a, e in reg["albums"].items() if e.get("cluster") == cid]


def list_clusters(reg):
    """[(cluster_id, cluster_dict, album_count)], largest first, Unclustered last."""
    counts = defaultdict(int)
    for e in reg["albums"].values():
        counts[e.get("cluster")] += 1
    rows = [(cid, c, counts.get(cid, 0)) for cid, c in reg["clusters"].items()]
    rows.sort(key=lambda r: (r[0] == UNCLUSTERED_ID, -r[2], r[1].get("name", "")))
    return rows


def artist_cluster_map(reg):
    """{artist ratingKey (str): cluster name} by majority of the artist's
    albums (Unclustered ignored) — what Library Galaxy colours nodes by."""
    feats = load_album_features()["albums"]
    votes = defaultdict(lambda: defaultdict(int))
    for a, e in reg["albums"].items():
        artist_key = (feats.get(a) or {}).get("artist_key")
        if artist_key and _assigned(e.get("cluster")):
            votes[artist_key][e["cluster"]] += 1
    names = {cid: c.get("name") for cid, c in reg["clusters"].items()}
    return {a: names.get(max(c, key=c.get)) for a, c in votes.items()}


def set_cluster_locked(cid, locked):
    reg = load_registry()
    if reg and cid in reg["clusters"] and cid != UNCLUSTERED_ID:
        reg["clusters"][cid]["locked"] = bool(locked)
        save_registry(reg)


def set_albums_pinned(album_keys, pinned):
    reg = load_registry()
    if not reg:
        return
    for a in album_keys:
        if a in reg["albums"]:
            reg["albums"][a]["pinned"] = bool(pinned)
    save_registry(reg)


def update_params(**fields):
    reg = load_registry()
    if not reg:
        return
    for k, v in fields.items():
        if k in DEFAULT_PARAMS:
            reg["params"][k] = v
    save_registry(reg)


# --- Album features -------------------------------------------------------

def _tags(objs):
    return sorted({t.tag.strip().lower() for t in (objs or []) if getattr(t, "tag", None)})


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _album_fingerprint(album):
    return (f"{ALBUM_FEATURE_VERSION}:{ALBUM_SONIC_SAMPLE_SIZE}:{ALBUM_SONIC_NEIGHBOR_LIMIT}:"
            f"{_iso(getattr(album, 'updatedAt', None))}")


def compute_album_features(album, artist_genres, fingerprint):
    """Tags + sonic-neighbour albums for one album. Sonic neighbours come
    from Plex's sonicallySimilar on the album's ALBUM_SONIC_SAMPLE_SIZE
    most-played tracks, mapped to the matches' albums; weight = match % / 100
    (0.5 when Plex reports no score)."""
    try:
        album.reload()  # list views omit moods/styles
    except Exception:
        pass
    genres = set(_tags(getattr(album, "genres", None))) | set(_tags(getattr(album, "styles", None)))
    moods = _tags(getattr(album, "moods", None))

    try:
        tracks = album.tracks()
    except Exception:
        tracks = []
    sample = sorted(tracks, key=lambda t: getattr(t, "viewCount", 0) or 0, reverse=True)[:ALBUM_SONIC_SAMPLE_SIZE]

    own_key = str(album.ratingKey)
    neighbors = {}
    for track in sample:
        try:
            matches = track.sonicallySimilar(limit=ALBUM_SONIC_NEIGHBOR_LIMIT)
        except Exception:
            continue
        for m in matches:
            ak = str(getattr(m, "parentRatingKey", "") or "")
            if not ak or ak == own_key:
                continue
            pct = get_sonic_match_percent(m)
            weight = (pct / 100.0) if pct is not None else 0.5
            neighbors[ak] = round(max(neighbors.get(ak, 0.0), weight), 4)

    return {
        "fp": fingerprint,
        "title": getattr(album, "title", None),
        "artist_key": str(getattr(album, "parentRatingKey", "") or ""),
        "artist_title": getattr(album, "parentTitle", None),
        "genres": sorted(genres),
        "moods": moods,
        "artist_genres": sorted(artist_genres or []),
        "sonic": neighbors,
        "track_count": len(tracks),
        "added_at": _iso(getattr(album, "addedAt", None)),
    }


def get_music_section(plex):
    section = next((s for s in plex.library.sections() if s.type in ("artist", "music")), None)
    if section is None:
        raise ValueError("No music library section found on this Plex server.")
    return section


def scan_library(music_section, store, debug=None):
    """Refreshes `store` (in place) for new/changed albums and drops albums
    no longer in the library. Returns {"library", "new", "changed", "removed"}
    as lists/sets of album ratingKey strings."""
    d = _writer(debug)
    artists = get_cached_artists(music_section, debug=debug, force_refresh=True)
    artist_genres = {str(a.ratingKey): _tags(getattr(a, "genres", None)) for a in artists}

    albums = music_section.albums()
    d(f"**Album scan:** {len(albums)} albums in library, {len(store['albums'])} with cached features.")

    feats = store["albums"]
    library, new, changed = set(), [], []
    for album in albums:
        key = str(album.ratingKey)
        library.add(key)
        fp = _album_fingerprint(album)
        cached = feats.get(key)
        if cached and cached.get("fp") == fp:
            continue
        (new if cached is None else changed).append(key)
        feats[key] = compute_album_features(
            album, artist_genres.get(str(getattr(album, "parentRatingKey", "")), []), fp
        )
        done = len(new) + len(changed)
        if done % FEATURES_CHECKPOINT_EVERY == 0:
            d(f"└ analysed {done} new/changed album(s) so far…")
            save_album_features(store)

    removed = [k for k in feats if k not in library]
    for k in removed:
        feats.pop(k, None)

    edges = _build_plex_similar_edges(artists, debug=debug)
    store["artist_similar"] = {k: sorted(v) for k, v in edges.items()}

    d(f"└ {len(new)} new, {len(changed)} changed, {len(removed)} removed album(s).")
    return {"library": library, "new": new, "changed": changed, "removed": removed}


# --- Scoring --------------------------------------------------------------

class _Scorer:
    """album -> {cluster_id: affinity 0..1}.

    affinity = weighted mean of the components that are available for the album:
      sonic  : share of the album's sonic-neighbour weight that sits in the cluster
               (denominator = all neighbours known to the registry, Unclustered included)
      tag    : cosine(album tag vector, cluster tag centroid), idf-weighted,
               leave-one-out for the album's own cluster
      artist : 0.7 * share of the same artist's OTHER albums in the cluster
               + 0.3 * share of Plex similar artists whose majority cluster it is
    minus an optional Live-Mix fit penalty for (album, cluster).
    Centroids are weighted by an anchor weight of 1 + ln(1 + plays).
    """

    def __init__(self, labels, cluster_ids, feats, artist_similar, params, plays=None, penalties=None):
        self.labels = labels
        self.cluster_ids = [c for c in cluster_ids if c != UNCLUSTERED_ID]
        self.feats = feats
        self.artist_similar = artist_similar or {}
        self.params = params
        self.plays = plays or {}
        self.penalties = penalties or {}

        n = max(len(feats), 1)
        df = defaultdict(int)
        for f in feats.values():
            for t in set(f["genres"]) | set(f["moods"]) | set(f["artist_genres"]):
                df[t] += 1
        idf = {t: math.log(1 + n / c) for t, c in df.items()}

        self.vectors = {}
        for a, f in feats.items():
            vec = {}
            for t in f["artist_genres"]:
                vec[t] = TAG_WEIGHT_ARTIST_GENRE
            for t in f["moods"]:
                vec[t] = max(vec.get(t, 0), TAG_WEIGHT_MOOD)
            for t in f["genres"]:
                vec[t] = TAG_WEIGHT_GENRE
            vec = {t: w * idf.get(t, 1.0) for t, w in vec.items() if not _is_always_generic_tag(t)}
            norm = math.sqrt(sum(v * v for v in vec.values()))
            self.vectors[a] = {t: v / norm for t, v in vec.items()} if norm else {}
        self.anchor = {a: 1.0 + math.log1p(self.plays.get(a, 0)) for a in feats}

        self.artist_albums = defaultdict(set)
        for a, f in feats.items():
            self.artist_albums[f["artist_key"]].add(a)
        self.rebuild()

    def rebuild(self, cluster_ids=None):
        if cluster_ids is not None:
            self.cluster_ids = [c for c in cluster_ids if c != UNCLUSTERED_ID]
        self.members = defaultdict(set)
        self.tag_sums = defaultdict(lambda: defaultdict(float))
        artist_counts = defaultdict(lambda: defaultdict(int))
        for a, f in self.feats.items():
            cid = self.labels.get(a)
            if not _assigned(cid):
                continue
            self.members[cid].add(a)
            w = self.anchor[a]
            for t, v in self.vectors[a].items():
                self.tag_sums[cid][t] += w * v
            artist_counts[f["artist_key"]][cid] += 1
        self.tag_norms = {cid: math.sqrt(sum(v * v for v in s.values())) for cid, s in self.tag_sums.items()}
        self.artist_majority = {ak: max(c, key=c.get) for ak, c in artist_counts.items() if c}

    def scores_for(self, a):
        p = self.params
        f = self.feats[a]
        own = self.labels.get(a)

        sonic_denom = 0.0
        sonic_in = defaultdict(float)
        for nk, w in (f.get("sonic") or {}).items():
            if nk not in self.feats:
                continue
            sonic_denom += w
            ncid = self.labels.get(nk)
            if _assigned(ncid):
                sonic_in[ncid] += w

        others = self.artist_albums[f["artist_key"]] - {a}
        if len(others) > SAME_ARTIST_MAX_ALBUMS:
            others = set()
        same_artist = defaultdict(int)
        for o in others:
            lc = self.labels.get(o)
            if _assigned(lc):
                same_artist[lc] += 1
        similar = [s for s in self.artist_similar.get(f["artist_key"], []) if s in self.artist_majority]
        similar_votes = defaultdict(int)
        for s in similar:
            similar_votes[self.artist_majority[s]] += 1

        vec = self.vectors[a]
        w_self = self.anchor[a]
        out = {}
        for cid in self.cluster_ids:
            num = wsum = 0.0
            if sonic_denom > 0:
                num += p["w_sonic"] * sonic_in.get(cid, 0.0) / sonic_denom
                wsum += p["w_sonic"]
            if vec and cid in self.tag_sums:
                sums = self.tag_sums[cid]
                dot = sum(v * sums.get(t, 0.0) for t, v in vec.items())
                norm_sq = self.tag_norms.get(cid, 0.0) ** 2
                if cid == own:  # leave-one-out
                    dot -= w_self
                    norm_sq = norm_sq - 2 * w_self * (dot + w_self) + w_self * w_self
                if norm_sq > 1e-12:
                    num += p["w_tag"] * max(0.0, dot) / math.sqrt(norm_sq)
                    wsum += p["w_tag"]
            parts = []
            if others:
                parts.append((0.7, same_artist.get(cid, 0) / len(others)))
            if similar:
                parts.append((0.3, similar_votes.get(cid, 0) / len(similar)))
            if parts:
                num += p["w_artist"] * sum(w * v for w, v in parts) / sum(w for w, _ in parts)
                wsum += p["w_artist"]
            score = (num / wsum) if wsum else 0.0
            score -= self.penalties.get((a, cid), 0.0)
            out[cid] = max(0.0, score)
        return out

    def score_all(self, albums=None):
        return {a: self.scores_for(a) for a in (albums if albums is not None else self.feats)}


def _cohesion(scorer, table, cid):
    members = scorer.members.get(cid, ())
    vals = [table[a].get(cid, 0.0) for a in members if a in table]
    return (sum(vals) / len(vals)) if vals else 0.0


# --- Assignment -----------------------------------------------------------

def _assignment_pass(scorer, table, params, labels, pinned, locked_ids, plays, pending, mode,
                     only_unassigned=False):
    """Applies threshold assignment / hysteresis moves to `labels` in place.
    mode "weekly" requires a move of an already-assigned album to be the
    leader two runs in a row (tracked in `pending`) and caps how many albums
    can leave one cluster per run. Returns a list of applied moves
    (album, from_cid, to_cid)."""
    T = params["assign_threshold"]
    margin = params["assign_margin"]
    proposals = []

    for a, sc in table.items():
        if a in pinned:
            continue
        cur = labels.get(a)
        cur_assigned = _assigned(cur)
        if only_unassigned and cur_assigned:
            continue
        if cur_assigned and cur in locked_ids:
            continue

        ranked = sorted(sc.items(), key=lambda kv: kv[1], reverse=True)
        best, best_score = ranked[0] if ranked else (None, 0.0)
        second = ranked[1][1] if len(ranked) > 1 else 0.0

        if not cur_assigned:
            if best and best_score >= T and best_score - second >= margin:
                proposals.append((best_score, a, cur, best))
            elif cur is None:
                labels[a] = UNCLUSTERED_ID
            continue

        cur_score = sc.get(cur, 0.0)
        required = params["move_hysteresis"] * min(2.0, 1.0 + 0.5 * math.log1p(plays.get(a, 0)))
        if best and best != cur and best_score >= T and best_score - cur_score >= required:
            target, gain = best, best_score - cur_score
        elif cur_score < params["eject_threshold"] and best_score < T:
            target, gain = UNCLUSTERED_ID, T - cur_score
        else:
            pending.pop(a, None)
            continue

        if mode == "weekly" and pending.get(a) != target:
            pending[a] = target
            continue
        pending.pop(a, None)
        proposals.append((gain, a, cur, target))

    proposals.sort(key=lambda p: p[0], reverse=True)
    left = defaultdict(int)
    applied = []
    for _gain, a, src, dst in proposals:
        if mode == "weekly" and _assigned(src):
            cap = max(1, int(len(scorer.members.get(src, ())) * params["max_move_share"]))
            if left[src] >= cap:
                continue
            left[src] += 1
        labels[a] = dst
        applied.append((a, src, dst))
    return applied


# --- Album graph / Louvain ---------------------------------------------

def _album_graph(feats, nodes):
    # Sorted so Louvain sees the same node order every run (set order of
    # str keys changes per process with hash randomisation).
    nodes = sorted(nodes)
    node_set = set(nodes)
    g = nx.Graph()
    g.add_nodes_from(nodes)
    for a in nodes:
        for nk, w in sorted((feats[a].get("sonic") or {}).items()):
            if nk in node_set and nk != a:
                prev = g[a][nk]["weight"] if g.has_edge(a, nk) else 0.0
                g.add_edge(a, nk, weight=max(prev, w))
    by_artist = defaultdict(list)
    for a in nodes:
        by_artist[feats[a]["artist_key"]].append(a)
    for albs in by_artist.values():
        if not (1 < len(albs) <= SAME_ARTIST_MAX_ALBUMS):
            continue
        for i, x in enumerate(albs):
            for y in albs[i + 1:]:
                prev = g[x][y]["weight"] if g.has_edge(x, y) else 0.0
                g.add_edge(x, y, weight=prev + SAME_ARTIST_EDGE_WEIGHT)
    return g


def _ordered(graph):
    """Copy of `graph` (e.g. a subgraph view) with nodes/edges in sorted order."""
    g = nx.Graph()
    g.add_nodes_from(sorted(graph.nodes()))
    g.add_weighted_edges_from(sorted((min(x, y), max(x, y), w) for x, y, w in graph.edges(data="weight", default=1.0)))
    return g


# Louvain (at any resolution) can cut one uniformly dense group into
# artist-sized pieces, depending on node order. _coalesce_groups glues such
# pieces back together: two groups merge when the edge density BETWEEN them
# is at least COALESCE_DENSITY_RATIO of the sparser group's internal
# density. Genuinely separate groups have few edges between them and stay
# apart. Splits additionally need split_min_internal.
SPAWN_LOUVAIN_RESOLUTION = 0.5
COALESCE_DENSITY_RATIO = 0.5


def _groups_glue(graph, a_nodes, b_nodes, ratio=COALESCE_DENSITY_RATIO):
    """True if _coalesce_groups would merge these two node sets."""
    a_set, b_set = set(a_nodes), set(b_nodes)
    internal_a = internal_b = between = 0.0
    for x, y, w in graph.subgraph(a_set | b_set).edges(data="weight", default=1.0):
        if x in a_set and y in a_set:
            internal_a += w
        elif x in b_set and y in b_set:
            internal_b += w
        else:
            between += w
    na, nb = len(a_set), len(b_set)
    da = internal_a / (na * (na - 1) / 2) if na > 1 else 0.0
    db = internal_b / (nb * (nb - 1) / 2) if nb > 1 else 0.0
    base = min(da, db) or max(da, db)
    return bool(base) and between / (na * nb) >= ratio * base


def _coalesce_groups(graph, groups, ratio=COALESCE_DENSITY_RATIO):
    groups = [sorted(g) for g in groups]
    while True:
        index = {a: i for i, g in enumerate(groups) for a in g}
        internal = defaultdict(float)
        cut = defaultdict(float)
        for x, y, w in graph.edges(data="weight", default=1.0):
            gx, gy = index.get(x), index.get(y)
            if gx is None or gy is None:
                continue
            if gx == gy:
                internal[gx] += w
            else:
                cut[(min(gx, gy), max(gx, gy))] += w

        def _density(i):
            n = len(groups[i])
            return internal[i] / (n * (n - 1) / 2) if n > 1 else 0.0

        best = None
        for (i, j), w in cut.items():
            between = w / (len(groups[i]) * len(groups[j]))
            base = min(_density(i), _density(j)) or max(_density(i), _density(j))
            if base and between >= ratio * base and (best is None or between / base > best[0]):
                best = (between / base, i, j)
        if best is None:
            return sorted(groups, key=len, reverse=True)
        _r, i, j = best
        groups[i] = sorted(groups[i] + groups[j])
        del groups[j]


def _louvain_groups(graph, resolution=1.0):
    graph = _ordered(graph)
    if graph.number_of_nodes() == 0:
        return {}, []
    if graph.number_of_edges() == 0:
        return {n: i for i, n in enumerate(graph.nodes())}, [[n] for n in graph.nodes()]
    partition = community_louvain.best_partition(graph, weight="weight", resolution=resolution,
                                                 random_state=42)
    groups = defaultdict(list)
    for node, cid in partition.items():
        groups[cid].append(node)
    return partition, sorted(groups.values(), key=len, reverse=True)


# --- Naming ---------------------------------------------------------------

def _name_clusters(reg, feats, cids, labels, plays, api_key=None, debug=None):
    """Names freshly created clusters from their albums' most distinctive
    genre (idf-weighted, must cover >= 30% of the cluster) plus a dominant
    mood; falls back to Gemini (if a key is available) or artist names."""
    if not cids:
        return
    members = defaultdict(list)
    for a, cid in labels.items():
        if cid in cids:
            members[cid].append(a)

    n_all = max(len(feats), 1)
    df = defaultdict(int)
    for f in feats.values():
        for t in set(f["genres"]):
            df[t] += 1

    used = {c.get("name") for cid, c in reg["clusters"].items() if cid not in cids and c.get("name")}
    proposed, fallback = {}, {}
    for cid in cids:
        albs = members.get(cid, [])
        size = max(len(albs), 1)
        g_counts, m_counts, artist_plays = defaultdict(int), defaultdict(int), defaultdict(int)
        for a in albs:
            f = feats[a]
            for t in f["genres"]:
                g_counts[t] += 1
            for t in f["moods"]:
                m_counts[t] += 1
            if f.get("artist_title"):
                artist_plays[f["artist_title"]] += 1 + plays.get(a, 0)

        genre_candidates = [
            (cnt / size * math.log(1 + n_all / df[t]), t) for t, cnt in g_counts.items()
            if cnt / size >= 0.3 and not _is_always_generic_tag(t)
        ]
        genre = max(genre_candidates)[1] if genre_candidates else None
        mood_candidates = [(cnt, t) for t, cnt in m_counts.items() if cnt / size >= 0.3]
        mood = max(mood_candidates)[1] if mood_candidates else None
        top_artists = [n for n, _ in sorted(artist_plays.items(), key=lambda kv: kv[1], reverse=True)[:5]]

        if genre:
            name = genre.title()
            if mood and mood.lower() not in genre.lower():
                name = f"{name} — {mood.title()}"
            proposed[cid] = name
        else:
            fallback[cid] = {"artists": top_artists, "tags": [mood] if mood else []}
            proposed[cid] = (f"{' & '.join(top_artists[:2])} Mix" if top_artists else "New Cluster")

    if fallback and api_key:
        llm = _llm_name_fallback_communities(fallback, api_key, debug=debug)
        for cid, name in llm.items():
            proposed[cid] = name

    for cid in cids:
        reg["clusters"][cid]["name"] = _finalize_community_name(proposed[cid], used)


# --- Core curation pass -----------------------------------------------

def _merge_pair_ratios(scorer, table, cluster_ids, eligible):
    """[(cross-affinity / mean cohesion, x, y)] for every pair of eligible
    clusters, highest first. cross-affinity = mean affinity of each
    cluster's albums to the other cluster; cohesion = mean affinity of a
    cluster's albums to itself (all 0..1)."""
    ids = sorted(c for c in cluster_ids if c != UNCLUSTERED_ID and eligible(c) and scorer.members.get(c))
    cohesion = {cid: _cohesion(scorer, table, cid) for cid in ids}
    cross = defaultdict(lambda: defaultdict(float))
    for cid in ids:
        for a in scorer.members[cid]:
            for other, s in table[a].items():
                if other != cid:
                    cross[cid][other] += s
    pairs = []
    for i, x in enumerate(ids):
        for y in ids[i + 1:]:
            nx_, ny_ = len(scorer.members[x]), len(scorer.members[y])
            inter = (cross[x][y] + cross[y][x]) / (nx_ + ny_)
            base = (cohesion[x] + cohesion[y]) / 2
            pairs.append((inter / base if base > 0 else 0.0, x, y))
    return sorted(pairs, reverse=True)


def _non_system_count(clusters):
    return sum(1 for cid, c in clusters.items() if cid != UNCLUSTERED_ID and not c.get("system"))


def curate(reg, store, mode, plays=None, recent_plays=None, penalties=None, only_albums=None,
           api_key=None, debug=None):
    """Runs one curation pass over `reg` IN PLACE.

    mode:
      "daily"     — assign only `only_albums` (new albums) by threshold; no moves, no structure.
      "bootstrap" — moves applied immediately, no caps; full structural pass.
      "weekly"    — two-run confirmation + per-cluster move cap; full structural pass.

    Returns {"events": [...], "moved": n, "assigned": n, "ejected": n, ...}.
    Events: {"type": "retired"|"merged"|"split"|"spawned", "cluster_id", ...}.
    """
    d = _writer(debug)
    if mode != "daily" and not HAS_SONIC_GRAPH_DEPS:
        raise ImportError("Cluster curation requires 'networkx' and 'python-louvain'.")

    _ensure_unclustered(reg)
    p = reg["params"]
    gen = reg.get("generation", 0)
    feats = store["albums"]
    plays = plays or {}
    recent_plays = recent_plays or ({}, {})
    clusters = reg["clusters"]

    labels = {}
    for a in feats:
        cid = reg["albums"].get(a, {}).get("cluster")
        labels[a] = cid if cid in clusters else None
    pinned = {a for a, e in reg["albums"].items() if e.get("pinned") and a in feats}
    locked = {cid for cid, c in clusters.items() if c.get("locked")}
    pending = reg.setdefault("pending_moves", {})
    events = []
    stats = {"moved": 0, "assigned": 0, "ejected": 0}

    def _tally(applied):
        for _a, src, dst in applied:
            if dst == UNCLUSTERED_ID:
                stats["ejected"] += 1
            elif _assigned(src):
                stats["moved"] += 1
            else:
                stats["assigned"] += 1

    scorer = _Scorer(labels, list(clusters), feats, store.get("artist_similar"), p, plays, penalties)

    # 1. Assignment / moves
    if mode == "daily":
        targets = [a for a in (only_albums or ()) if a in feats]
        table = scorer.score_all(targets)
        _tally(_assignment_pass(scorer, table, p, labels, pinned, locked, plays, pending, mode,
                                only_unassigned=True))
    else:
        table = scorer.score_all()
        _tally(_assignment_pass(scorer, table, p, labels, pinned, locked, plays, pending, mode))
        scorer.rebuild()

        def _system_or_locked(cid):
            return cid == UNCLUSTERED_ID or clusters[cid].get("system") or clusters[cid].get("locked")

        # 2. Retire thin clusters
        for cid in list(clusters):
            if _system_or_locked(cid):
                continue
            size = len(scorer.members.get(cid, ()))
            if size < p["min_cluster_albums"]:
                for a in scorer.members.get(cid, ()):
                    labels[a] = UNCLUSTERED_ID
                events.append({"type": "retired", "cluster_id": cid, "name": clusters[cid].get("name"),
                               "albums": size})
                d(f"└ Retired `{clusters[cid].get('name')}` ({size} album(s) < {p['min_cluster_albums']}).")
                del clusters[cid]
        scorer.rebuild(list(clusters))
        table = scorer.score_all()
        _tally(_assignment_pass(scorer, table, p, labels, pinned, locked, plays, pending, mode,
                                only_unassigned=True))
        scorer.rebuild()
        table = scorer.score_all()

        graph = _album_graph(feats, set(feats))

        def _merge(survivor, absorbed, reason=None):
            for a in scorer.members[absorbed]:
                labels[a] = survivor
            ev = {"type": "merged", "cluster_id": absorbed, "name": clusters[absorbed].get("name"),
                  "into": survivor, "into_name": clusters[survivor].get("name")}
            if reason:
                ev["reason"] = reason
            events.append(ev)
            d(f"└ Merged `{clusters[absorbed].get('name')}` into `{clusters[survivor].get('name')}`"
              f"{' (cluster cap)' if reason == 'cap' else ''}.")
            clusters[survivor]["updated_gen"] = gen
            del clusters[absorbed]
            scorer.members[survivor] |= scorer.members.pop(absorbed, set())

        def _bigger_first(x, y):
            return (x, y) if len(scorer.members[x]) >= len(scorer.members[y]) else (y, x)

        # 3. Merge near-duplicates: similar by affinity AND not separable in
        # the album graph (otherwise next week's split would undo it).
        touched = set()
        for ratio, x, y in _merge_pair_ratios(scorer, table, list(clusters), lambda c: not _system_or_locked(c)):
            if ratio < p["merge_ratio"]:
                break
            if x in touched or y in touched:
                continue
            if len(scorer.members[x]) + len(scorer.members[y]) > p["max_cluster_albums"]:
                continue
            if not _groups_glue(graph, scorer.members[x], scorer.members[y]):
                continue
            _merge(*_bigger_first(x, y))
            touched |= {x, y}
        scorer.rebuild(list(clusters))
        table = scorer.score_all()

        # 3b. Enforce the cluster cap: merge the most similar pair (thresholds
        # ignored) until the count fits. Locked clusters are never merged, so
        # they can keep the count above the cap.
        cap = int(p.get("max_clusters") or 0)
        while cap and _non_system_count(clusters) > cap:
            pairs = _merge_pair_ratios(scorer, table, list(clusters), lambda c: not _system_or_locked(c))
            if not pairs:
                break
            _ratio, x, y = pairs[0]
            _merge(*_bigger_first(x, y), reason="cap")
            touched |= {x, y}
            scorer.rebuild(list(clusters))
            table = scorer.score_all()

        def _room():
            return (cap - _non_system_count(clusters)) if cap else float("inf")

        # 4. Split clusters that contain clearly separate groups of albums
        # (Louvain + coalescing, gated by modularity and how much edge
        # weight stays inside the parts). Uniformly connected clusters
        # coalesce back into one group and are left alone — unless they're
        # over max_cluster_albums, which skips coalescing.
        # Skipped on bootstrap (a commit keeps exactly the clusters you built),
        # capped per run, and only while there's room under max_clusters.
        created = []
        splits_done = 0
        split_order = sorted(clusters, key=lambda c: len(scorer.members.get(c, ())), reverse=True)
        for cid in (split_order if mode == "weekly" else []):
            if splits_done >= p["max_splits_per_run"] or _room() < 1:
                break
            if _system_or_locked(cid) or cid in touched:
                continue
            members = scorer.members.get(cid, set())
            if len(members) < 2 * p["min_cluster_albums"]:
                continue
            sub = _ordered(graph.subgraph(members))
            _partition, groups = _louvain_groups(sub)
            if len(members) <= p["max_cluster_albums"]:
                groups = _coalesce_groups(sub, groups)  # oversized clusters split along their weakest seams
            partition = {a: i for i, g in enumerate(groups) for a in g}
            big = [g for g in groups if len(g) >= p["min_cluster_albums"]]
            if len(big) < 2 or sub.number_of_edges() == 0:
                continue
            modularity = community_louvain.modularity(partition, sub, weight="weight")
            if modularity < p["split_min_modularity"]:
                continue
            part_of = {a: i for i, g in enumerate(big) for a in g}
            total_w = internal_w = 0.0
            for x, y, w in sub.edges(data="weight", default=1.0):
                if x in part_of and y in part_of:
                    total_w += w
                    internal_w += w if part_of[x] == part_of[y] else 0.0
            if not total_w or internal_w / total_w < p["split_min_internal"]:
                continue
            room = _room()
            if room < len(big) - 1:  # keep the largest parts that fit; the rest stay with the original
                big = big[: 1 + int(room)]
            new_ids = []
            for part in big[1:]:
                nid = _new_cluster_id()
                clusters[nid] = {"name": None, "locked": False, "system": False, "created_gen": gen,
                                 "updated_gen": gen, "origin": "split", "stats": {}}
                for a in part:
                    labels[a] = nid
                new_ids.append(nid)
            created += new_ids
            splits_done += 1
            clusters[cid]["updated_gen"] = gen
            events.append({"type": "split", "cluster_id": cid, "name": clusters[cid].get("name"),
                           "new": new_ids, "modularity": round(modularity, 3)})
            d(f"└ Split `{clusters[cid].get('name')}` into {len(new_ids) + 1} parts "
              f"(modularity {modularity:.2f}, {len(members)} albums).")
        scorer.rebuild(list(clusters))

        # 5. Spawn new clusters from Unclustered
        residual = {a for a, c in labels.items() if not _assigned(c) and a not in pinned}
        residual_graph = _ordered(graph.subgraph(residual))
        _partition, groups = _louvain_groups(residual_graph, resolution=SPAWN_LOUVAIN_RESOLUTION)
        groups = _coalesce_groups(residual_graph, groups)
        candidates = [g for g in groups if len(g) >= p["min_cluster_albums"]]
        candidates.sort(key=lambda g: sum(plays.get(a, 0) for a in g), reverse=True)
        provisional = []
        spawn_slots = min(p["max_new_clusters"], _room())
        for group in candidates[: int(spawn_slots)]:
            nid = _new_cluster_id()
            clusters[nid] = {"name": None, "locked": False, "system": False, "created_gen": gen,
                             "updated_gen": gen, "origin": "spawned", "stats": {}}
            for a in group:
                labels[a] = nid
            provisional.append(nid)
        if provisional:
            scorer.rebuild(list(clusters))
            table = scorer.score_all()
            for nid in provisional:
                coh = _cohesion(scorer, table, nid)
                if coh >= p["spawn_cohesion"]:
                    created.append(nid)
                    events.append({"type": "spawned", "cluster_id": nid,
                                   "albums": len(scorer.members[nid]), "cohesion": round(coh, 3)})
                else:
                    for a in scorer.members[nid]:
                        labels[a] = UNCLUSTERED_ID
                    del clusters[nid]
            scorer.rebuild(list(clusters))

        # 6. Final threshold pass for whatever is still unclustered
        table = scorer.score_all()
        _tally(_assignment_pass(scorer, table, p, labels, pinned, locked, plays, pending, mode,
                                only_unassigned=True))
        scorer.rebuild()

        _name_clusters(reg, feats, set(created), labels, plays, api_key=api_key, debug=debug)
        for ev in events:
            if ev["type"] == "spawned":
                ev["name"] = clusters[ev["cluster_id"]]["name"]
            elif ev["type"] == "split":
                ev["new_names"] = [clusters[n]["name"] for n in ev["new"] if n in clusters]

    # Write back
    table = scorer.score_all() if mode != "daily" else table
    new_albums = {}
    for a in feats:
        cid = labels.get(a) or UNCLUSTERED_ID
        old = reg["albums"].get(a, {})
        entry = {"cluster": cid, "pinned": bool(old.get("pinned"))}
        sc = table.get(a)
        if sc is None and old.get("cluster") == cid:
            entry["score"] = old.get("score")
            entry["assigned_gen"] = old.get("assigned_gen", gen)
        else:
            sc = sc or {}
            entry["score"] = round(sc.get(cid, 0.0), 3) if _assigned(cid) else None
            ranked = sorted(sc.items(), key=lambda kv: kv[1], reverse=True)
            runner = next(((c, s) for c, s in ranked if c != cid), None)
            entry["runner_up"] = [runner[0], round(runner[1], 3)] if runner else None
            entry["assigned_gen"] = gen if old.get("cluster") != cid else old.get("assigned_gen", gen)
        new_albums[a] = entry
    reg["albums"] = new_albums
    for a in list(pending):
        if a not in feats:
            pending.pop(a)

    plays_28d, plays_7d = recent_plays
    counts = defaultdict(int)
    for e in new_albums.values():
        counts[e["cluster"]] += 1
    for cid, c in clusters.items():
        members = [a for a, e in new_albums.items() if e["cluster"] == cid]
        stat = {"albums": counts.get(cid, 0)}
        if mode != "daily" and cid != UNCLUSTERED_ID:
            stat["cohesion"] = round(_cohesion(scorer, table, cid), 3)
        elif "cohesion" in (c.get("stats") or {}):
            stat["cohesion"] = c["stats"]["cohesion"]
        if plays_28d or plays_7d:
            stat["plays_28d"] = sum(plays_28d.get(a, 0) for a in members)
            stat["plays_7d"] = sum(plays_7d.get(a, 0) for a in members)
        else:
            for k in ("plays_28d", "plays_7d"):
                if k in (c.get("stats") or {}):
                    stat[k] = c["stats"][k]
        c["stats"] = stat

    stats["events"] = events
    stats["unclustered"] = counts.get(UNCLUSTERED_ID, 0)
    stats["clusters"] = len([c for c in clusters if c != UNCLUSTERED_ID])
    d(f"**Curation pass ({mode}):** {stats['assigned']} assigned, {stats['moved']} moved, "
      f"{stats['ejected']} sent to Unclustered, {len(events)} structural change(s); "
      f"{stats['clusters']} clusters, {stats['unclustered']} unclustered album(s).")
    return stats


# --- Lineage --------------------------------------------------------------

def match_lineage(old_members, new_members, min_jaccard=0.5, min_containment=0.5):
    """Matches old cluster IDs to new (temporary) ones by album overlap.
    Returns (id_map {new_id: old_id}, merged {old_id: new_id}, retired [old_id])."""
    pairs = []
    for o, om in old_members.items():
        for n, nm in new_members.items():
            inter = len(om & nm)
            if inter:
                pairs.append((inter / len(om | nm), o, n))
    pairs.sort(reverse=True)
    id_map, used_old = {}, set()
    for j, o, n in pairs:
        if j < min_jaccard:
            break
        if o in used_old or n in id_map:
            continue
        id_map[n] = o
        used_old.add(o)

    merged, retired = {}, []
    for o, om in old_members.items():
        if o in used_old:
            continue
        best = max(((len(om & nm) / len(om), n) for n, nm in new_members.items() if om), default=(0, None))
        if best[0] >= min_containment:
            merged[o] = best[1]
        else:
            retired.append(o)
    return id_map, merged, retired


def seeds_from_track_results(results, feats):
    """LEGACY (v1 build cache = ~30-track display sample per cluster): each
    sampled artist seeds its cluster with all its albums."""
    artist_map = {str(k): v for k, v in build_artist_cluster_map(results).items()}
    seeds = defaultdict(list)
    for a, f in feats.items():
        name = artist_map.get(f["artist_key"])
        if name and name != "Unsorted":
            seeds[name].append(a)
    return dict(seeds)


def bootstrap_from_results(plex, results, locked_names=(), api_key=None, max_clusters=None, debug=None):
    """LEGACY wrapper for v1 track-sample builds — see bootstrap_from_seeds."""
    return bootstrap_from_seeds(plex, lambda feats: seeds_from_track_results(results, feats),
                                locked_names=locked_names, api_key=api_key,
                                max_clusters=max_clusters, debug=debug)


def bootstrap_from_seeds(plex, seeds, locked_names=(), api_key=None, max_clusters=None, debug=None):
    """Builds a new registry generation from a Library Clusters build and
    saves it. `seeds` = {cluster_name: [album keys]} — the build's FULL
    membership (or a callable feats -> seeds). Seeded albums start in their
    cluster; then a full bootstrap curation pass scores every album (seeds
    that clearly fit elsewhere move, unseeded albums join by threshold).
    If a registry already exists, new clusters inherit old IDs by album
    overlap so Live Mixes follow them.
    Returns {"events": [...], "stats": {...}, "generation": n}."""
    d = _writer(debug)
    section = get_music_section(plex)
    store = load_album_features()
    scan_library(section, store, debug=debug)
    save_album_features(store)
    feats = store["albums"]

    old = load_registry()
    gen = (old["generation"] if old else 0) + 1
    if callable(seeds):
        seeds = seeds(feats)
    seeds = {name: keys for name, keys in seeds.items() if name != "Unsorted" and keys}

    reg = empty_registry(old["params"] if old else None)
    reg["generation"] = gen
    if max_clusters is not None:
        reg["params"]["max_clusters"] = int(max_clusters)
    elif not reg["params"].get("max_clusters"):
        reg["params"]["max_clusters"] = len(seeds)
    _ensure_unclustered(reg)

    locked_set = {n.lower() for n in locked_names}
    seed_of = {}
    temp_ids = {}
    for name, keys in seeds.items():
        tid = _new_cluster_id()
        temp_ids[name] = tid
        reg["clusters"][tid] = {"name": name, "locked": name.lower() in locked_set, "system": False,
                                "created_gen": gen, "updated_gen": gen, "origin": "bootstrap", "stats": {}}
        for a in keys:
            seed_of[str(a)] = tid
    for a in feats:
        reg["albums"][a] = {"cluster": seed_of.get(a, UNCLUSTERED_ID)}
    seeded = sum(1 for e in reg["albums"].values() if _assigned(e["cluster"]))
    d(f"**Bootstrap gen {gen}:** {len(temp_ids)} clusters from the build, "
      f"{seeded} of {len(feats)} albums seeded.")

    stats = curate(reg, store, mode="bootstrap", api_key=api_key, debug=debug)

    events = [e for e in stats["events"] if e["type"] in ("spawned", "split")]
    if old:
        old_members = defaultdict(set)
        for a, e in old["albums"].items():
            if _assigned(e.get("cluster")):
                old_members[e["cluster"]].add(a)
        new_members = defaultdict(set)
        for a, e in reg["albums"].items():
            if _assigned(e["cluster"]):
                new_members[e["cluster"]].add(a)
        id_map, merged, retired = match_lineage(old_members, new_members)

        rename = {}
        for new_id, old_id in id_map.items():
            rename[new_id] = old_id
            c = reg["clusters"].pop(new_id)
            prev = old["clusters"].get(old_id, {})
            c["created_gen"] = prev.get("created_gen", gen)
            c["origin"] = prev.get("origin", "bootstrap")
            c["locked"] = c["locked"] or prev.get("locked", False)
            reg["clusters"][old_id] = c
        for e in reg["albums"].values():
            e["cluster"] = rename.get(e["cluster"], e["cluster"])
            if e.get("runner_up"):
                e["runner_up"][0] = rename.get(e["runner_up"][0], e["runner_up"][0])
        for e in events:
            e["cluster_id"] = rename.get(e["cluster_id"], e["cluster_id"])
        for a, e in old["albums"].items():
            if e.get("pinned") and a in reg["albums"]:
                reg["albums"][a]["pinned"] = True
        for old_id, new_id in merged.items():
            into = rename.get(new_id, new_id)
            events.append({"type": "merged", "cluster_id": old_id, "name": old["clusters"].get(old_id, {}).get("name"),
                           "into": into, "into_name": reg["clusters"][into]["name"]})
        for old_id in retired:
            events.append({"type": "retired", "cluster_id": old_id,
                           "name": old["clusters"].get(old_id, {}).get("name"),
                           "albums": len(old_members.get(old_id, ()))})
        d(f"└ Lineage vs gen {old['generation']}: {len(id_map)} continued, {len(merged)} merged, "
          f"{len(retired)} retired.")

    reg["last_scan_at"] = _now_iso()
    save_registry(reg)
    return {"events": events, "stats": {k: v for k, v in stats.items() if k != "events"}, "generation": gen}


def sync_new_albums(plex, debug=None):
    """Daily: picks up albums added since the last scan and assigns them to
    an existing cluster if they clear the threshold (else Unclustered).
    No-op (returns None) when no registry exists yet."""
    reg = load_registry()
    if not reg:
        return None
    store = load_album_features()
    scan = scan_library(get_music_section(plex), store, debug=debug)
    save_album_features(store)
    for k in scan["removed"]:
        reg["albums"].pop(k, None)
    unseen = [k for k in scan["library"] if k not in reg["albums"]]
    stats = curate(reg, store, mode="daily", only_albums=set(unseen), debug=debug)
    reg["last_scan_at"] = _now_iso()
    save_registry(reg)
    return {"new": len(unseen), "assigned": stats["assigned"],
            "unclustered": len(unseen) - stats["assigned"], "removed": len(scan["removed"])}


# --- Background jobs for the UI ---------------------------------------

_JOB_STATUS = {"name": None, "running": False, "started_at": None, "finished_at": None,
               "log": [], "error": None, "result": None}
_JOB_LOG_MAX = 200


class _StatusWriter:
    def write(self, msg, *args, **kwargs):
        text = str(msg)
        logger.info(text)
        _JOB_STATUS["log"].append(text)
        del _JOB_STATUS["log"][:-_JOB_LOG_MAX]


def get_job_status():
    return dict(_JOB_STATUS, log=list(_JOB_STATUS["log"]))


def run_background_job(name, fn, *args, **kwargs):
    """Runs fn(*args, debug=<status writer>, **kwargs) in a daemon thread
    while holding JOB_LOCK. Returns False (and starts nothing) if another
    registry job is already running."""
    if not JOB_LOCK.acquire(blocking=False):
        return False
    _JOB_STATUS.update({"name": name, "running": True, "started_at": _now_iso(), "finished_at": None,
                        "log": [], "error": None, "result": None})

    def _runner():
        try:
            _JOB_STATUS["result"] = fn(*args, debug=_StatusWriter(), **kwargs)
        except Exception as e:
            logger.exception(f"Background job '{name}' failed")
            _JOB_STATUS["error"] = str(e)
        finally:
            _JOB_STATUS["running"] = False
            _JOB_STATUS["finished_at"] = _now_iso()
            JOB_LOCK.release()

    threading.Thread(target=_runner, daemon=True).start()
    return True
