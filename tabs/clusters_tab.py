"""Library Clusters tab, two layers:

  1. 🧬 Album clusters (tabs/registry_panel.py) — the committed, album-level
     clusters that cover the whole library and are re-curated every week.
     Playlists and Live Mixes are made here, from every track in a cluster.
  2. 🛠️ Build clusters — generate a fresh set from scratch (Gemini tag
     mapping or Relational/Louvain), review the FULL membership (albums,
     artists, top artists per cluster), optionally combine clusters, then
     commit. Committing seeds the album clusters with the build's full album
     membership and caps them at the build's cluster count.
"""

import streamlit as st

import cluster_registry
import curation
from clustering import (
    apply_merge_plan, artist_cluster_map_from_albums, build_clusters, clear_all_caches,
    clear_artist_scan_cache, get_all_genre_and_mood_tags, load_build_cache, save_build_cache,
    suggest_cluster_names,
)
from tabs import registry_panel

_SESSION_DEFAULTS = {
    'cluster_build': None,          # raw build dict from clustering.build_clusters
    'cluster_merge_plan': [],       # [{"members": [...], "new_name": str}, ...]
    'cluster_locked': "",
    'suggested_snapshot': None,     # {genre_tags, mood_tags, total_clusters, clusters, mapping}
    'cluster_tag_counts': None,     # {"genre": n, "mood": n} from the tag scan
    'cluster_tag_preview': None,
    'cluster_tag_mapping': None,    # for Library Galaxy
    'cluster_artist_map': None,     # {artist ratingKey: cluster name}, for Library Galaxy
}


def _init_state():
    for key, value in _SESSION_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value if not isinstance(value, list) else list(value)
    if 'cluster_cache_checked' not in st.session_state:
        # Once per session: restore the last build (and its pending merges)
        # from disk so a restart doesn't lose an uncommitted review.
        st.session_state['cluster_cache_checked'] = True
        build, merge_plan = load_build_cache()
        if build and st.session_state['cluster_build'] is None:
            st.session_state['cluster_build'] = build
            st.session_state['cluster_merge_plan'] = merge_plan


def _sync_galaxy_state(build):
    """Library Galaxy colours artists by cluster: the committed album
    clusters when they exist, else the current build."""
    reg = cluster_registry.load_registry()
    if reg:
        st.session_state['cluster_artist_map'] = cluster_registry.artist_cluster_map(reg)
        st.session_state['cluster_tag_mapping'] = (build or {}).get('tag_mapping')
    elif build:
        st.session_state['cluster_artist_map'] = artist_cluster_map_from_albums(build['clusters'], build['albums'])
        st.session_state['cluster_tag_mapping'] = build.get('tag_mapping')


def render(plex, plex_url, plex_token, debug_box, gemini_api_key):
    st.title("🗂️ Library Clusters")
    st.caption(
        "Your whole library, grouped into album clusters. Make playlists and Live Mixes from any "
        "cluster below; the clusters themselves are kept up to date automatically (new albums daily, "
        "re-curation every week). Use 🛠️ Build clusters further down to generate a new set from scratch."
    )
    _init_state()

    try:
        music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        st.error("No music library section found on this Plex server.")
        st.stop()

    raw_build = st.session_state['cluster_build']
    build = apply_merge_plan(raw_build, st.session_state['cluster_merge_plan']) if raw_build else None
    _sync_galaxy_state(build)

    registry_panel.render(plex, plex_url, plex_token, gemini_api_key)

    st.write("---")
    st.header("🛠️ Build clusters")
    st.caption(
        "Generates a new set of clusters over the whole library. Nothing changes until you review the "
        "result and press **📌 Commit as album clusters** — that replaces the 🧬 album clusters above "
        "(Live Mixes follow their cluster by album overlap)."
    )
    _render_clean_slate(debug_box)
    settings = _render_build_settings(music_section, gemini_api_key)
    _render_build_step(plex, music_section, gemini_api_key, debug_box, settings)

    raw_build = st.session_state['cluster_build']
    if raw_build:
        _render_review(plex, raw_build, gemini_api_key)


# --- Clean slate ----------------------------------------------------------

def _render_clean_slate(debug_box):
    with st.expander("🧹 Clean slate", expanded=False):
        st.caption(
            "Clears the build caches — the tag→cluster mapping, per-artist sonic profiles, the saved "
            "build and the in-process artist scan — so the next build starts from scratch. Keeps the "
            "🧬 album clusters and anything already saved as a Plex playlist."
        )
        if st.button("🧹 Clear build caches"):
            summary = clear_all_caches(debug=debug_box)
            for key, value in _SESSION_DEFAULTS.items():
                st.session_state[key] = value if not isinstance(value, list) else list(value)
            # Shown after the rerun — a message right before st.rerun() is never painted.
            st.session_state['clean_slate_confirmation'] = summary
            st.rerun()

    confirmation = st.session_state.pop('clean_slate_confirmation', None)
    if confirmation is not None:
        if confirmation['errors']:
            st.warning(
                f"Clean slate mostly finished, but {len(confirmation['errors'])} file(s) couldn't be removed: "
                f"{', '.join(p for p, _ in confirmation['errors'])}."
            )
        else:
            st.success(f"✅ Build caches cleared ({len(confirmation['cleared'])} file(s) deleted).")


# --- Settings ---------------------------------------------------------------

def _render_build_settings(music_section, gemini_api_key):
    st.subheader("1. Scan your library")
    st.caption("Free — no Gemini call. Gives the cluster count below real numbers to work from.")
    if st.button("👀 Scan genre/mood tags"):
        if st.session_state.get('force_rescan_artists', False):
            clear_artist_scan_cache()
        with st.spinner("Scanning library tags..."):
            genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
        st.session_state['cluster_tag_counts'] = {"genre": len(genre_tags), "mood": len(mood_tags)}
        st.session_state['cluster_tag_preview'] = {"genre_tags": genre_tags, "mood_tags": mood_tags}
        low, high = _suggested_range(len(genre_tags) + len(mood_tags))
        st.session_state['cluster_total'] = min(40, max(2, round((low + high) / 2)))

    tag_preview = st.session_state.get('cluster_tag_preview')
    if tag_preview:
        with st.expander(
            f"{len(tag_preview['genre_tags'])} genre tags, {len(tag_preview['mood_tags'])} mood tags found",
            expanded=False,
        ):
            st.write("**Genre tags:**")
            st.code(", ".join(tag_preview['genre_tags']) or "(none found)")
            st.write("**Mood tags:**")
            st.code(", ".join(tag_preview['mood_tags']) or "(none found)")

    st.subheader("2. Cluster count & vibes")
    total_clusters = int(st.number_input(
        "Maximum number of clusters", min_value=2, max_value=40, value=10, key="cluster_total"
    ))
    st.caption(
        "A real cap: Gemini may use fewer if the library doesn't support this many distinct groups "
        "(Tags mode), and Relational mode keeps the strongest this-many communities. Committing also "
        "makes it the album clusters' cap, so weekly curation never grows past it."
    )
    tag_counts = st.session_state['cluster_tag_counts']
    if tag_counts:
        total_tags = tag_counts["genre"] + tag_counts["mood"]
        low, high = _suggested_range(total_tags)
        st.caption(f"Rule of thumb: ~1 cluster per 15–25 genre/mood tags. {total_tags} tags → roughly "
                   f"{low}–{high} clusters.")

    if gemini_api_key:
        _render_suggest(music_section, total_clusters, gemini_api_key)

    locked_input = st.text_input(
        "Cluster vibes / names you want (comma-separated, optional)",
        placeholder="e.g. Metal, Punk Rock, Classic Rock", key="cluster_locked",
    )
    st.caption("Kept exactly as typed and always exist if relevant tags/artists are found. Committed "
               "clusters with these names are locked against weekly curation.")
    locked_clusters = [c.strip() for c in locked_input.split(",") if c.strip()]

    with st.expander("⚙️ Advanced settings", expanded=False):
        mode_label = st.radio(
            "Clustering method",
            options=["Relational: Plex Similar Artist + sonic (recommended)", "Genre/mood tags only (Gemini)"],
            index=0,
        )
        clustering_mode = "tags" if mode_label.startswith("Genre") else "sonic"
        st.caption(
            "**Relational:** artists are grouped by Plex's 'Similar Artist' links (what 🌌 Library Galaxy "
            "shows), optionally strengthened by audio matching; each group's artists bring all their "
            "albums. Tags only name the result. **Tags only:** every track is sorted by its genre/mood "
            "tags; each album joins the cluster holding most of its tracks."
        )
        settings = {
            "clustering_mode": clustering_mode, "total_clusters": total_clusters,
            "locked_clusters": locked_clusters,
            "sonic_boost": 0.5, "sonic_artist_sample_size": 6, "sonic_use_cache": True,
            "refine_unsorted": False, "sonic_weight": 0.0, "reassign_tagged_via_sonic": False,
            "sonic_propagation_rounds": 2,
        }
        if clustering_mode == "sonic":
            settings["sonic_boost"] = st.slider("Sonic boost", 0.0, 1.0, 0.5, 0.05, key="sonic_boost")
            st.caption("How much audio matching can strengthen an existing Similar-Artist link. It never "
                       "creates a link on its own — 0 = pure Similar-Artist topology.")
            settings["sonic_artist_sample_size"] = st.slider("Sampled tracks per artist", 3, 10, 6, 1,
                                                             key="sonic_artist_sample_size")
            settings["sonic_use_cache"] = st.checkbox("Reuse cached artist profiles", value=True,
                                                      key="sonic_use_cache")
        else:
            refine = st.checkbox("Refine 'Unsorted' via sonic similarity", value=False)
            st.caption("Infers a cluster for untagged/unmatched tracks from Plex's sonic neighbours. "
                       "No Gemini calls, just local Plex lookups.")
            settings["refine_unsorted"] = refine
            settings["sonic_weight"] = st.slider("Sonic influence", 0, 100, 0, 10, disabled=not refine) / 100.0
            settings["reassign_tagged_via_sonic"] = st.checkbox(
                "Also let sonic evidence reassign already-tagged tracks", value=False, disabled=not refine,
            )
            settings["sonic_propagation_rounds"] = st.slider("Sonic propagation rounds", 1, 4, 2,
                                                             disabled=not refine)
        st.checkbox(
            "Force rescan library (ignore cached artist scan)", value=False, key="force_rescan_artists",
            help="The artist list is cached in memory for a while. Check this if you've just added music.",
        )
    settings["force_rescan"] = st.session_state.get("force_rescan_artists", False)
    return settings


def _suggested_range(total_tags):
    low = max(2, total_tags // 25)
    return low, max(low, total_tags // 15)


def _render_suggest(music_section, total_clusters, gemini_api_key):
    if st.button("🔍 Suggest Clusters"):
        with st.spinner("Analyzing your library's genres/moods..."):
            try:
                genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
                clusters, mapping = suggest_cluster_names(genre_tags, mood_tags, total_clusters, gemini_api_key)
                st.session_state['suggested_snapshot'] = {
                    "genre_tags": genre_tags, "mood_tags": mood_tags, "total_clusters": total_clusters,
                    "clusters": clusters, "mapping": mapping,
                }
                st.rerun()
            except Exception as e:
                st.error(f"Failed to suggest clusters: {e}")
    st.caption("Proposes a full set of names (one Gemini call). Untick any you don't want, then apply — "
               "accepting them unchanged skips a second Gemini call at build time.")

    snap = st.session_state.get('suggested_snapshot')
    if not snap:
        return
    st.write(f"**Suggested {len(snap['clusters'])} clusters** — untick any to drop them:")
    selected = []
    cols = st.columns(2)
    for i, name in enumerate(snap['clusters']):
        with cols[i % 2]:
            if st.checkbox(name, value=True, key=f"suggest_review_{name}"):
                selected.append(name)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ Apply selected as locked clusters", disabled=not selected):
            st.session_state['cluster_locked'] = ", ".join(selected)
            st.rerun()
    with c2:
        if st.button("✕ Dismiss suggestions"):
            st.session_state['suggested_snapshot'] = None
            st.rerun()


# --- Build ------------------------------------------------------------------

def _render_build_step(plex, music_section, gemini_api_key, debug_box, settings):
    st.subheader("3. Build")
    dry_run = st.checkbox(
        "🧪 Dry run (no Gemini call — test the pipeline for free)",
        disabled=(settings["clustering_mode"] == "sonic"),
    )
    st.caption("Uses an offline keyword mapper instead of Gemini — clusters won't be accurate, but the whole "
               "flow (review, commit, playlists) can be tested without spending tokens. Not needed in "
               "Relational mode, where tags only name the clusters.")

    if not gemini_api_key and not dry_run:
        st.warning("Enter a Gemini API key in the sidebar to build clusters, or check 'Dry run' to test for free.")
        return
    if len(settings["locked_clusters"]) > settings["total_clusters"]:
        st.error("You've locked in more cluster names than the total cluster count allows.")
        return

    c1, c2 = st.columns(2)
    build_clicked = c1.button("🧩 Build clusters", use_container_width=True,
                              help="Reuses the cached genre mapping if nothing changed — no Gemini cost.")
    remap_clicked = c2.button("🔁 Force re-map genres", disabled=dry_run, use_container_width=True,
                              help="Forces a fresh Gemini call even if a mapping is cached.")
    if not (build_clicked or remap_clicked):
        return

    preloaded = None
    snap = st.session_state.get('suggested_snapshot')
    if snap and not remap_clicked and not dry_run:
        genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
        same_tags = sorted(genre_tags) == sorted(snap["genre_tags"]) and sorted(mood_tags) == sorted(snap["mood_tags"])
        same_total = settings["total_clusters"] == snap["total_clusters"]
        same_clusters = (sorted(settings["locked_clusters"], key=str.lower)
                         == sorted(snap["clusters"], key=str.lower))
        if same_tags and same_total and same_clusters:
            preloaded = (snap["clusters"], snap["mapping"])
            debug_box.write("**Using Suggest Clusters mapping directly — no extra Gemini call.**")

    with st.spinner("Building clusters over the whole library..."):
        try:
            if settings["force_rescan"]:
                clear_artist_scan_cache()
            build = build_clusters(
                music_section, plex,
                locked_clusters=settings["locked_clusters"], total_clusters=settings["total_clusters"],
                api_key=gemini_api_key, debug=debug_box, force_remap=remap_clicked, dry_run=dry_run,
                preloaded_mapping=preloaded, refine_unsorted=settings["refine_unsorted"],
                sonic_weight=settings["sonic_weight"],
                reassign_tagged_via_sonic=settings["reassign_tagged_via_sonic"],
                sonic_propagation_rounds=settings["sonic_propagation_rounds"],
                clustering_mode=settings["clustering_mode"],
                sonic_artist_sample_size=int(settings["sonic_artist_sample_size"]),
                sonic_boost=float(settings["sonic_boost"]), sonic_use_cache=bool(settings["sonic_use_cache"]),
            )
            st.session_state['cluster_build'] = build
            st.session_state['cluster_merge_plan'] = []
            save_build_cache(build, [])
            st.rerun()
        except Exception as e:
            st.error(f"Failed to build clusters: {e}")


# --- Review & commit ----------------------------------------------------------

def _cluster_summary(build, name):
    keys = build["clusters"].get(name, [])
    albums = build["albums"]
    by_artist = {}
    plays = 0
    for k in keys:
        info = albums.get(k) or {}
        artist = info.get("artist") or "?"
        by_artist[artist] = by_artist.get(artist, 0) + 1 + (info.get("plays") or 0) / 1000.0
        plays += info.get("plays") or 0
    top = [a for a, _ in sorted(by_artist.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    return len(keys), len(by_artist), plays, top


def _render_review(plex, raw_build, gemini_api_key):
    st.subheader("4. Review & commit")
    plan = st.session_state['cluster_merge_plan']
    build = apply_merge_plan(raw_build, plan)
    total_albums = len(build["albums"])
    claimed = sum(len(v) for v in build["clusters"].values())
    mode = "Relational" if build.get("mode") == "sonic" else "Tags"
    st.caption(
        f"{mode} build from {build.get('built_at')}: **{len(build['clusters'])} clusters** covering "
        f"{claimed} of {total_albums} albums ({len(build.get('unsorted', []))} unsorted). Unsorted albums "
        "aren't lost — on commit every album is scored, and the ones that fit nowhere go to Unclustered."
    )

    rows = sorted(build["clusters"], key=lambda n: len(build["clusters"][n]), reverse=True)
    max_albums = max((len(build["clusters"][n]) for n in rows), default=0) or 1
    for name in rows:
        n_albums, n_artists, plays, top = _cluster_summary(build, name)
        col1, col2 = st.columns([3, 2])
        with col1:
            st.progress(n_albums / max_albums, text=f"**{name}** — {n_albums} albums · {n_artists} artists · "
                                                     f"{plays} plays")
        with col2:
            st.caption(", ".join(top))

    with st.expander(f"🔗 Combine clusters ({len(raw_build['clusters'])} in the build)"):
        st.caption("Pick two or more clusters to combine into one broader cluster before committing. "
                   "Free and instant — no Gemini call.")
        grouped = {m for g in plan for m in g["members"]}
        selectable = sorted(n for n in raw_build["clusters"] if n not in grouped)
        members = st.multiselect("Clusters to combine", selectable, key="merge_members_select")
        new_name = st.text_input("Name for the combined cluster", key="merge_name_input",
                                 placeholder="e.g. Heavy & Punk")
        if st.button("➕ Add this merge group", disabled=len(members) < 2 or not new_name.strip()):
            plan.append({"members": members, "new_name": new_name.strip()})
            save_build_cache(raw_build, plan)
            st.rerun()
        for i, group in enumerate(plan):
            c1, c2 = st.columns([5, 1])
            c1.write(f"- **{group['new_name']}** ← {', '.join(group['members'])}")
            if c2.button("✕", key=f"remove_merge_group_{i}"):
                plan.pop(i)
                save_build_cache(raw_build, plan)
                st.rerun()
        if plan and st.button("↩️ Reset all merges"):
            plan.clear()
            save_build_cache(raw_build, plan)
            st.rerun()

    busy = cluster_registry.get_job_status()["running"]
    has_registry = cluster_registry.load_registry() is not None
    if st.button("📌 Commit as album clusters", type="primary", use_container_width=True, disabled=busy,
                 help=f"Seeds the 🧬 album clusters with this build's full album membership (merges included) "
                      f"and caps them at {build.get('total_clusters')} clusters. Existing Live Mixes follow "
                      f"their cluster by album overlap; mixes whose cluster disappears are marked stale and "
                      f"deleted at the next weekly curation."):
        if not cluster_registry.run_background_job("Commit build", curation.commit_build, plex, build,
                                                   api_key=gemini_api_key):
            st.warning("Another cluster job is already running.")
        st.rerun()
    st.caption(
        ("Replaces the current 🧬 album clusters. " if has_registry else "")
        + "Runs in the background; the first commit analyses every album once (sonic neighbours + tags), "
          "later commits only new or changed albums."
    )
