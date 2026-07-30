"""Library Clusters tab: groups the whole library into N genre/mood
clusters (some locked by name, the rest invented by Gemini — or all
suggested via the Suggest Clusters step), ranks each cluster's top tracks
as a blend of popular/sonic/related picks, and lets the user save any
cluster as a playlist."""

import streamlit as st

from clustering import build_genre_clusters, get_all_genre_and_mood_tags, suggest_cluster_names, apply_cluster_merge_plan
from ui_components import render_track_row


def render(plex, plex_url, plex_token, debug_box, gemini_api_key):
    st.title("🗂️ Library Clusters")
    st.caption(
        "Groups your whole library into a handful of sections by genre and mood — "
        "lock in any clusters you already know you want (or let Gemini suggest a "
        "full set first), and Gemini sorts every genre/mood tag into one of them."
    )

    if 'cluster_results' not in st.session_state:
        st.session_state['cluster_results'] = None
    if 'cluster_results_raw' not in st.session_state:
        st.session_state['cluster_results_raw'] = None
    if 'cluster_tag_mapping_raw' not in st.session_state:
        st.session_state['cluster_tag_mapping_raw'] = None
    if 'cluster_merge_plan' not in st.session_state:
        st.session_state['cluster_merge_plan'] = []  # [{"members": [...], "new_name": str}, ...]
    if 'cluster_removed_keys' not in st.session_state:
        st.session_state['cluster_removed_keys'] = {}  # {cluster_name: {ratingKey, ...}}
    if 'cluster_names_used' not in st.session_state:
        st.session_state['cluster_names_used'] = []
    if 'cluster_locked' not in st.session_state:
        st.session_state['cluster_locked'] = ""
    if 'suggested_snapshot' not in st.session_state:
        st.session_state['suggested_snapshot'] = None  # {tags, total, clusters, mapping}
    if 'cluster_tag_counts' not in st.session_state:
        st.session_state['cluster_tag_counts'] = None  # {"genre": n, "mood": n} from last Preview Tags

    try:
        cluster_music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        cluster_music_section = None
        st.error("No music library section found on this Plex server.")
        st.stop()

    # --- Step 1: scan the library's tags first. Zero-cost, no API call —
    # doing this before the settings below means the cluster-count guidance
    # has real numbers to work with immediately, instead of asking the user
    # to configure things blind and find the tag scan buried further down.
    st.subheader("1. Scan your library")
    st.caption("Zero-cost — no Gemini call. Run this first so the cluster count below has real numbers to work from.")
    if st.button("👀 Scan genre/mood tags"):
        with st.spinner("Scanning library tags..."):
            genre_tags, mood_tags = get_all_genre_and_mood_tags(cluster_music_section)
        st.session_state['cluster_tag_counts'] = {"genre": len(genre_tags), "mood": len(mood_tags)}
        st.session_state['cluster_tag_preview'] = {"genre_tags": genre_tags, "mood_tags": mood_tags}
        total_tags = len(genre_tags) + len(mood_tags)
        suggested_low = max(2, total_tags // 25)
        suggested_high = max(suggested_low, total_tags // 15)
        st.session_state['cluster_total'] = min(40, max(2, round((suggested_low + suggested_high) / 2)))

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

    st.write("---")

    # --- Step 2: configure how clusters should be built.
    st.subheader("2. Configure clusters")
    with st.expander("⚙️ Cluster settings", expanded=st.session_state['cluster_results'] is None):
        clustering_mode_label = st.radio(
            "Clustering method",
            options=[
                "Hybrid: sonic + tags, weighted (recommended)",
                "Genre/mood tags only (Gemini)",
                "Sonic similarity only (track-level)",
            ],
            index=0,
            horizontal=False,
        )
        clustering_mode = "tags" if clustering_mode_label.startswith("Genre") else "sonic"
        sonic_group_by = "track" if clustering_mode_label.startswith("Sonic") else "artist"
        st.caption(
            "**Hybrid (artist-level):** this is the real combination \u2014 every artist gets "
            "one profile blending actual audio similarity, raw shared tags, and agreement with "
            "your Gemini-defined clusters, and Louvain groups artists using all three signals "
            "together in a single weighted graph (not tags-then-sonic or sonic-then-tags as two "
            "separate passes). **Tags only:** Gemini maps tags to clusters and every track is "
            "assigned purely by its tags (sonic can optionally recover/correct leftovers "
            "afterward \u2014 see below). **Sonic only (track-level):** membership comes purely "
            "from Plex's audio analysis, one node per track, no tag signal at all until naming."
        )

        if clustering_mode == "sonic" and sonic_group_by == "artist":
            st.write("**Hybrid signal weights**")
            st.caption(
                "How much each signal counts toward pulling two artists into the same cluster. "
                "Values are normalized automatically, so only the ratio between them matters "
                "\u2014 set one to 0 to exclude that signal entirely."
            )
            hybrid_sonic_weight = st.slider(
                "🎧 Sonic similarity (actual audio analysis)", min_value=0.0, max_value=1.0, value=0.5, step=0.05,
                key="hybrid_sonic_weight",
            )
            hybrid_cluster_agreement_weight = st.slider(
                "🎯 Gemini cluster agreement (curated, narrow)", min_value=0.0, max_value=1.0, value=0.35, step=0.05,
                key="hybrid_cluster_agreement_weight",
            )
            hybrid_tag_overlap_weight = st.slider(
                "🏷️ Raw tag overlap (broad, noisy)", min_value=0.0, max_value=1.0, value=0.15, step=0.05,
                key="hybrid_tag_overlap_weight",
            )
            st.caption(
                "Sonic = what Plex's audio fingerprinting actually hears. Cluster agreement = "
                "both artists' tags vote for the same cluster you asked Gemini to build \u2014 "
                "narrow but meaningful, since it's filtered through real category definitions "
                "rather than any incidental shared tag. Tag overlap = any shared genre/mood tag "
                "at all \u2014 broad, catches stragglers the other two miss, but noisiest on its "
                "own. For crisp, sharply-defined clusters, favor sonic + cluster agreement over "
                "raw tag overlap; for looser, more inclusive clusters, raise tag overlap."
            )

            sonic_artist_sample_size = st.slider(
                "Sampled tracks per artist", min_value=2, max_value=3, value=3, step=1,
                key="sonic_artist_sample_size",
            )
            st.caption(
                "How many of an artist's most-played tracks (or Plex-popular, as a fallback "
                "for artists with no play history) get sonically analyzed to represent that "
                "artist."
            )
            sonic_use_cache = st.checkbox(
                "Reuse cached artist profiles", value=True, key="sonic_use_cache",
            )
            st.caption(
                "Unchecking forces every artist's sample tracks to be re-analyzed against Plex "
                "this build, ignoring any cached profile \u2014 useful after changing sample "
                "size, or if you suspect a stale profile."
            )
            sonic_max_tracks = 1500
            sonic_resolution = st.slider(
                "Community granularity", min_value=0.5, max_value=2.0, value=1.0, step=0.1,
                key="sonic_resolution",
            )
            st.caption(
                "Louvain's resolution parameter: lower values merge artists into fewer, "
                "broader communities; higher values split them into more, narrower ones."
            )
        elif clustering_mode == "sonic":
            sonic_max_tracks = st.number_input(
                "Max tracks in sonic graph", min_value=100, max_value=5000, value=1500, step=100,
                key="sonic_max_tracks",
            )
            st.caption(
                "Each track in the graph costs one Plex API call (to fetch its sonic "
                "neighbors), so this caps runtime on large libraries \u2014 the most-played "
                "tracks are prioritized if your library has more than this."
            )
            sonic_resolution = st.slider(
                "Community granularity", min_value=0.5, max_value=2.0, value=1.0, step=0.1,
                key="sonic_resolution",
            )
            st.caption(
                "Louvain's resolution parameter: lower values merge tracks into fewer, "
                "broader communities; higher values split them into more, narrower ones. "
                "1.0 is the neutral default \u2014 adjust and rebuild to compare."
            )
            sonic_artist_sample_size = 3
            hybrid_sonic_weight, hybrid_tag_overlap_weight, hybrid_cluster_agreement_weight = 1.0, 0.0, 0.0
            sonic_use_cache = True
        else:
            sonic_max_tracks = 1500
            sonic_resolution = 1.0
            sonic_artist_sample_size = 3
            hybrid_sonic_weight, hybrid_tag_overlap_weight, hybrid_cluster_agreement_weight = 0.5, 0.15, 0.35
            sonic_use_cache = True

        total_clusters_input = st.number_input(
            "Maximum number of clusters", min_value=2, max_value=40, value=10, key="cluster_total"
        )
        total_clusters = int(total_clusters_input)
        st.caption(
            "This is a ceiling, not a forced target — Gemini uses fewer if the tags don't "
            "naturally support that many distinct groups, and skips vague/ambiguous tags "
            "into 'Unsorted' instead of stretching them to fill out every bucket."
        )
        tag_counts = st.session_state['cluster_tag_counts']
        if tag_counts:
            total_tags = tag_counts["genre"] + tag_counts["mood"]
            suggested_low = max(2, total_tags // 25)
            suggested_high = max(suggested_low, total_tags // 15)
            st.caption(
                f"Rule of thumb: ~1 cluster per 15\u201325 genre/mood tags. Your library has "
                f"{total_tags} distinct tags (from step 1's scan), suggesting roughly "
                f"{suggested_low}\u2013{suggested_high} clusters \u2014 the field above defaults "
                f"to the midpoint of that range; adjust as you like."
            )
        else:
            st.caption(
                "Tip: run the tag scan in step 1 above to get a suggested cluster range here."
            )

        if gemini_api_key:
            if st.button("🔍 Suggest Clusters"):
                with st.spinner("Analyzing your library's genres/moods..."):
                    try:
                        genre_tags, mood_tags = get_all_genre_and_mood_tags(cluster_music_section)
                        clusters, mapping = suggest_cluster_names(
                            genre_tags, mood_tags, total_clusters, gemini_api_key
                        )
                        st.session_state['suggested_snapshot'] = {
                            "genre_tags": genre_tags,
                            "mood_tags": mood_tags,
                            "total_clusters": total_clusters,
                            "clusters": clusters,
                            "mapping": mapping,
                        }
                        st.session_state['suggested_review_selection'] = list(clusters)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to suggest clusters: {e}")
            st.caption(
                "Suggest Clusters proposes a full set of names from scratch (one Gemini "
                "call). Review the list below, uncheck any you don't want, then apply — "
                "accepting them as-is skips a second Gemini call."
            )

            snap = st.session_state.get('suggested_snapshot')
            if snap:
                st.write(f"**Suggested {len(snap['clusters'])} clusters** — untick any to drop them:")
                selected = []
                cols = st.columns(2)
                for i, cluster_name in enumerate(snap['clusters']):
                    with cols[i % 2]:
                        checked = st.checkbox(
                            cluster_name, value=True, key=f"suggest_review_{cluster_name}"
                        )
                        if checked:
                            selected.append(cluster_name)

                col_apply, col_dismiss = st.columns([1, 1])
                with col_apply:
                    if st.button("✅ Apply selected as locked clusters", disabled=not selected):
                        st.session_state['cluster_locked'] = ", ".join(selected)
                        st.rerun()
                with col_dismiss:
                    if st.button("✕ Dismiss suggestions"):
                        st.session_state['suggested_snapshot'] = None
                        st.rerun()

        locked_input = st.text_input(
            "Locked cluster names (comma-separated, optional)",
            placeholder="e.g. Metal, Punk Rock, Classic Rock",
            key="cluster_locked",
        )
        st.caption(
            "These are kept exactly as typed and always exist if relevant tags are found. "
            "Gemini determines any remaining clusters and decides which genre/mood tags "
            "belong in every bucket, locked or not."
        )

        top_n = st.number_input(
            "Total tracks per cluster", min_value=3, max_value=90, value=30, key="cluster_top_n",
        )
        st.caption(
            "Split roughly into thirds: popular plays, sonically similar tracks, "
            "and tracks from related artists — same blend style as Artist Mix."
        )

        if clustering_mode == "tags":
            refine_unsorted = st.checkbox(
                "Refine 'Unsorted' via sonic similarity", value=False,
            )
            st.caption(
                "Extrapolates genre/mood for 'Unsorted' tracks from Plex's own sonic-similarity "
                "analysis instead of tags. No extra Gemini calls, just local Plex lookups."
            )

            sonic_weight_pct = st.slider(
                "Sonic influence", min_value=0, max_value=100, value=0, step=10,
                disabled=not refine_unsorted,
            )
            sonic_weight = sonic_weight_pct / 100.0
            st.caption(
                "How much weight sonic-similarity evidence gets vs. genre/mood tags, for both "
                "the 'Unsorted' refinement above and (if enabled below) already-tagged tracks. "
                "0 = original strict thresholds (6+ neighbors agreeing, a clear margin, a real "
                "majority share of votes). Higher values relax those thresholds so more tracks "
                "get moved on weaker sonic consensus \u2014 sonic similarity is a noisier signal "
                "than genre tags, so higher settings trade some accuracy for more reassignments."
            )

            reassign_tagged_via_sonic = st.checkbox(
                "Also let sonic evidence reassign already-tagged tracks", value=False,
                disabled=not refine_unsorted,
            )
            st.caption(
                "Off by default: normally sonic similarity only fills in 'Unsorted' tracks. "
                "Turn this on to let a strong sonic-neighbor consensus move a track OUT of "
                "the cluster its genre/mood tags gave it (e.g. correcting a mistagged track) "
                "\u2014 gated by a stricter bar than Unsorted recovery, since these tracks "
                "already have real tag evidence behind their current cluster."
            )

            sonic_propagation_rounds = st.slider(
                "Sonic propagation rounds", min_value=1, max_value=4, value=2,
                disabled=not refine_unsorted,
            )
            st.caption(
                "How many passes the sonic-neighbor voting runs. Each round lets tracks that "
                "flipped clusters in the previous round pass that influence on to their own "
                "neighbors, so sonic agreement can propagate through a chain of tracks rather "
                "than only ever comparing to the original tag-based labels once. Each track's "
                "sonic neighbors are still only fetched from Plex once, no matter how many "
                "rounds run \u2014 higher values are free in API calls, just slightly slower "
                "to compute locally."
            )
        else:
            # Sonic mode: membership already comes from the sonic graph itself,
            # so the tag-refinement knobs above don't apply here.
            refine_unsorted = False
            sonic_weight = 0.0
            reassign_tagged_via_sonic = False
            sonic_propagation_rounds = 2

    locked_clusters = [c.strip() for c in locked_input.split(",") if c.strip()]

    st.write("---")

    # --- Step 3: build.
    st.subheader("3. Build")
    dry_run = st.checkbox(
        "🧪 Dry run (no Gemini call — test the pipeline/UI for free)", disabled=(clustering_mode == "sonic"),
    )
    st.caption(
        "Uses a simple offline keyword mapper instead of Gemini. Clusters won't be "
        "accurate, but this lets you test track assignment, ranking, and playlist "
        "saving without spending any tokens. Not available in Sonic mode — clustering "
        "there doesn't depend on the tag mapping, only naming does."
    )

    if not gemini_api_key and not dry_run:
        st.warning("Enter a Gemini API key in the sidebar to build clusters, or check 'Dry run' to test for free.")
    elif len(locked_clusters) > total_clusters:
        st.error("You've locked in more cluster names than the total cluster count allows.")
    else:
        build_clicked = st.button("🧩 Build / Refresh Clusters", use_container_width=True)
        st.caption(
            "Re-scans tracks and play counts. Reuses the cached genre mapping (disk + "
            "memory) if nothing's changed — no Gemini call, no cost. In dry run mode, "
            "never calls Gemini at all."
        )
        remap_clicked = st.button("🔁 Force Re-map Genres", disabled=dry_run, use_container_width=True)
        st.caption(
            "Forces a fresh Gemini call even if a mapping is cached on disk or in "
            "memory. Disabled in dry run mode (there's nothing to remap)."
        )

        if build_clicked or remap_clicked:
            # If the user accepted a Suggest Clusters result unchanged (same tags,
            # same total, same cluster names), reuse that mapping directly instead
            # of paying for a second Gemini call.
            snap = st.session_state.get('suggested_snapshot')
            preloaded = None
            if snap and not remap_clicked and not dry_run:
                current_genre_tags, current_mood_tags = get_all_genre_and_mood_tags(cluster_music_section)
                same_tags = (sorted(current_genre_tags) == sorted(snap["genre_tags"]) and
                             sorted(current_mood_tags) == sorted(snap["mood_tags"]))
                same_total = total_clusters == snap["total_clusters"]
                same_clusters = sorted(locked_clusters, key=str.lower) == sorted(snap["clusters"], key=str.lower)
                if same_tags and same_total and same_clusters:
                    preloaded = (snap["clusters"], snap["mapping"])
                    debug_box.write("**Using Suggest Clusters mapping directly — no extra Gemini call.**")

            with st.spinner("Analyzing genres and building clusters..."):
                try:
                    results, tag_mapping = build_genre_clusters(
                        cluster_music_section,
                        plex,
                        locked_clusters=locked_clusters,
                        total_clusters=total_clusters,
                        api_key=gemini_api_key,
                        top_n_per_cluster=int(top_n),
                        debug=debug_box,
                        force_remap=remap_clicked,
                        dry_run=dry_run,
                        preloaded_mapping=preloaded,
                        refine_unsorted=refine_unsorted,
                        sonic_weight=sonic_weight,
                        reassign_tagged_via_sonic=reassign_tagged_via_sonic,
                        sonic_propagation_rounds=sonic_propagation_rounds,
                        clustering_mode=clustering_mode,
                        sonic_max_tracks=int(sonic_max_tracks),
                        sonic_neighbor_limit=15,
                        sonic_resolution=float(sonic_resolution),
                        sonic_group_by=sonic_group_by,
                        sonic_artist_sample_size=int(sonic_artist_sample_size),
                        hybrid_sonic_weight=float(hybrid_sonic_weight),
                        hybrid_tag_overlap_weight=float(hybrid_tag_overlap_weight),
                        hybrid_cluster_agreement_weight=float(hybrid_cluster_agreement_weight),
                        sonic_use_cache=bool(sonic_use_cache),
                    )
                    st.session_state['cluster_results_raw'] = results
                    st.session_state['cluster_tag_mapping_raw'] = tag_mapping
                    st.session_state['cluster_merge_plan'] = []  # fresh build invalidates any prior grouping
                    st.session_state['cluster_removed_keys'] = {}
                    st.session_state['cluster_names_used'] = list(results.keys())
                except Exception as e:
                    st.error(f"Failed to build clusters: {e}")
                    st.session_state['cluster_results_raw'] = None

    st.write("---")
    st.subheader("4. Results")

    raw_results = st.session_state['cluster_results_raw']
    raw_tag_mapping = st.session_state['cluster_tag_mapping_raw']

    if not raw_results:
        st.info("Complete steps 1\u20133 above, then build clusters to see them here.")
        return

    # --- Cluster size summary, sorted largest-first, so imbalance is
    # visible at a glance without digging through the debug log. Small
    # clusters are the natural candidates to fold into a bigger one via
    # the "Combine clusters" step below.
    size_order = sorted(raw_results.items(), key=lambda kv: len(kv[1]), reverse=True)
    with st.expander(f"📊 Cluster sizes ({len(raw_results)} clusters)", expanded=False):
        max_size = max((len(tracks) for _, tracks in size_order), default=0)
        for cluster_name, tracks in size_order:
            count = len(tracks)
            bar_frac = (count / max_size) if max_size else 0
            col1, col2 = st.columns([3, 1])
            with col1:
                st.progress(bar_frac, text=cluster_name)
            with col2:
                st.write(f"{count}")

    # --- Phase 2: combine fine-grained clusters into broader buckets ---
    # Pure local operation (no Gemini call) — build narrow clusters first
    # (phase 1, above), then decide here which of those to merge into a
    # broader final bucket. The merge plan is stored separately from the
    # raw build so it can be freely changed/reset without re-running
    # anything against Plex or Gemini.
    with st.expander(f"🔗 Combine clusters ({len(raw_results)} fine-grained clusters currently)"):
        st.caption(
            "Pick two or more of the clusters above to combine into one broader bucket. "
            "Free and instant — no Gemini call, just relabeling and merging the track lists "
            "you already have."
        )
        available_names = sorted(raw_results.keys())
        already_grouped = {m for g in st.session_state['cluster_merge_plan'] for m in g["members"]}
        selectable = [n for n in available_names if n not in already_grouped]

        merge_members = st.multiselect(
            "Clusters to combine", selectable, key="merge_members_select"
        )
        merge_name = st.text_input(
            "Name for the combined cluster", key="merge_name_input",
            placeholder="e.g. Heavy & Punk",
        )
        if st.button("➕ Add this merge group", disabled=len(merge_members) < 2 or not merge_name.strip()):
            st.session_state['cluster_merge_plan'].append({
                "members": merge_members, "new_name": merge_name.strip()
            })
            st.rerun()

        if st.session_state['cluster_merge_plan']:
            st.write("**Current merge groups:**")
            for i, group in enumerate(st.session_state['cluster_merge_plan']):
                col1, col2 = st.columns([5, 1])
                with col1:
                    st.write(f"- **{group['new_name']}** ← {', '.join(group['members'])}")
                with col2:
                    if st.button("✕", key=f"remove_merge_group_{i}"):
                        st.session_state['cluster_merge_plan'].pop(i)
                        st.rerun()
            if st.button("↩️ Reset all merges"):
                st.session_state['cluster_merge_plan'] = []
                st.rerun()

    if st.session_state['cluster_merge_plan']:
        merged_results, merged_tag_mapping = apply_cluster_merge_plan(
            raw_results, raw_tag_mapping, st.session_state['cluster_merge_plan']
        )
    else:
        merged_results, merged_tag_mapping = raw_results, raw_tag_mapping

    # Apply any manual per-track removals on top of the current merged view.
    removed_keys = st.session_state['cluster_removed_keys']
    cluster_results = {
        name: [t for t in tracks if getattr(t, 'ratingKey', None) not in removed_keys.get(name, set())]
        for name, tracks in merged_results.items()
    }
    st.session_state['cluster_results'] = cluster_results
    st.session_state['cluster_tag_mapping'] = merged_tag_mapping

    st.write("---")

    cluster_tabs = st.tabs(list(cluster_results.keys()))
    for cluster_name, sub_tab in zip(cluster_results.keys(), cluster_tabs):
        with sub_tab:
            tracks = cluster_results[cluster_name]
            if not tracks:
                st.info("No tracks left in this cluster.")
                continue
            st.caption(f"{len(tracks)} tracks — a blend of popular plays, sonically similar picks, and related artists. Tap ✕ to drop one before saving.")
            for idx, track in enumerate(tracks):
                render_track_row(
                    track, idx, key_prefix=f"cluster_{cluster_name}", mode="cluster",
                    plex_url=plex_url, plex_token=plex_token, cluster_name=cluster_name
                )

            save_name = st.text_input(
                "Playlist name:", value=f"{cluster_name} Mix",
                key=f"cluster_playlist_name_{cluster_name}"
            )
            if st.button(f"💾 Save '{cluster_name}' as Plex Playlist", key=f"save_cluster_{cluster_name}"):
                try:
                    plex.createPlaylist(title=save_name, items=tracks)
                    st.success(f"Created playlist '{save_name}' with {len(tracks)} tracks.")
                except Exception as e:
                    st.error(f"Failed to create playlist: {e}")