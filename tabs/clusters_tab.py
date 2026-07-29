"""Library Clusters tab: groups the whole library into N genre/mood
clusters (some locked by name, the rest invented by Gemini — or all
suggested via the Suggest Clusters step), ranks each cluster's top tracks
as a blend of popular/sonic/related picks, and lets the user save any
cluster as a playlist."""

import streamlit as st

from clustering import build_genre_clusters, get_all_genre_and_mood_tags, suggest_cluster_names
from file_tags import diagnose_unsorted_tracks
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
    if 'cluster_names_used' not in st.session_state:
        st.session_state['cluster_names_used'] = []
    if 'cluster_locked' not in st.session_state:
        st.session_state['cluster_locked'] = ""
    if 'suggested_snapshot' not in st.session_state:
        st.session_state['suggested_snapshot'] = None  # {tags, total, clusters, mapping}

    try:
        cluster_music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        cluster_music_section = None
        st.error("No music library section found on this Plex server.")
        st.stop()

    with st.expander("⚙️ Cluster settings", expanded=st.session_state['cluster_results'] is None):
        total_clusters = st.number_input(
            "Total number of clusters", min_value=2, max_value=15, value=5, key="cluster_total"
        )

        if gemini_api_key:
            if st.button("🔍 Suggest Clusters"):
                with st.spinner("Analyzing your library's genres/moods..."):
                    try:
                        genre_tags, mood_tags = get_all_genre_and_mood_tags(cluster_music_section)
                        clusters, mapping = suggest_cluster_names(
                            genre_tags, mood_tags, int(total_clusters), gemini_api_key
                        )
                        st.session_state['suggested_snapshot'] = {
                            "genre_tags": genre_tags,
                            "mood_tags": mood_tags,
                            "total_clusters": int(total_clusters),
                            "clusters": clusters,
                            "mapping": mapping,
                        }
                        st.session_state['cluster_locked'] = ", ".join(clusters)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to suggest clusters: {e}")
            st.caption(
                "Suggest Clusters proposes a full set of names from scratch (one Gemini "
                "call) and fills in the field below — edit, remove, or add your own "
                "before building. Accepting them as-is skips a second Gemini call."
            )

        locked_input = st.text_input(
            "Locked cluster names (comma-separated, optional)",
            placeholder="e.g. Metal, Fast Paced",
            key="cluster_locked",
        )
        st.caption(
            "These are kept exactly as typed. Gemini invents any remaining clusters "
            "and decides which genre tags belong in every bucket, locked or not."
        )

        top_n = st.number_input(
            "Total tracks per cluster", min_value=3, max_value=90, value=30, key="cluster_top_n",
        )
        st.caption(
            "Split roughly into thirds: popular plays, sonically similar tracks, "
            "and tracks from related artists — same blend style as Artist Mix."
        )

        refine_unsorted = st.checkbox(
            "Refine 'Unsorted' via sonic similarity", value=True,
        )
        st.caption(
            "Extrapolates genre/mood for 'Unsorted' tracks from Plex's own sonic-similarity "
            "analysis instead of tags. Works per-track (not per-artist), and only reassigns a "
            "track when at least 3 sonic neighbors agree with a clear margin \u2014 stricter than "
            "before, since one noisy 'similar artist' match was previously enough to pull an "
            "unrelated track (e.g. a hip-hop track landing in a Metal cluster) into the wrong "
            "place. No extra Gemini calls, just local Plex lookups."
        )

    locked_clusters = [c.strip() for c in locked_input.split(",") if c.strip()]

    # --- Zero-cost testing helpers ---
    if st.button("👀 Preview Tags (no API call)"):
        with st.spinner("Scanning library tags..."):
            genre_tags, mood_tags = get_all_genre_and_mood_tags(cluster_music_section)
        st.write(f"**{len(genre_tags)} genre tags:**")
        st.code(", ".join(genre_tags) or "(none found)")
        st.write(f"**{len(mood_tags)} mood tags:**")
        st.code(", ".join(mood_tags) or "(none found)")

    dry_run = st.checkbox("🧪 Dry run (no Gemini call — test the pipeline/UI for free)")
    st.caption(
        "Uses a simple offline keyword mapper instead of Gemini. Clusters won't be "
        "accurate, but this lets you test track assignment, ranking, and playlist "
        "saving without spending any tokens."
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
                same_total = int(total_clusters) == snap["total_clusters"]
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
                        total_clusters=int(total_clusters),
                        api_key=gemini_api_key,
                        top_n_per_cluster=int(top_n),
                        debug=debug_box,
                        force_remap=remap_clicked,
                        dry_run=dry_run,
                        preloaded_mapping=preloaded,
                        refine_unsorted=refine_unsorted,
                    )
                    st.session_state['cluster_results'] = results
                    st.session_state['cluster_tag_mapping'] = tag_mapping
                    st.session_state['cluster_names_used'] = list(results.keys())
                except Exception as e:
                    st.error(f"Failed to build clusters: {e}")
                    st.session_state['cluster_results'] = None

    st.write("---")

    cluster_results = st.session_state['cluster_results']
    if not cluster_results:
        st.info("Set your locked clusters and total count above, then build clusters to see them here.")
    else:
        cluster_tabs = st.tabs(list(cluster_results.keys()))
        for cluster_name, sub_tab in zip(cluster_results.keys(), cluster_tabs):
            with sub_tab:
                tracks = cluster_results[cluster_name]
                if not tracks:
                    st.info("No tracks landed in this cluster.")
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

                if cluster_name == "Unsorted":
                    st.write("---")
                    st.caption(
                        "🔍 Diagnose why these tracks landed here — checks each file's own "
                        "embedded genre/mood tags directly, bypassing Plex's database. "
                        "Requires this app to have filesystem access to your music files "
                        "(mount your library path into the container to enable this)."
                    )
                    if st.button("🔍 Inspect embedded file tags", key="inspect_unsorted_tags"):
                        with st.spinner("Reading embedded tags from audio files..."):
                            diagnostics = diagnose_unsorted_tracks(tracks, max_tracks=50)

                        has_tags = [d for d in diagnostics if d["diagnosis"] == "has_embedded_tags"]
                        no_tags = [d for d in diagnostics if d["diagnosis"] == "no_embedded_tags"]
                        unreadable = [d for d in diagnostics if d["diagnosis"] == "unreadable"]

                        if unreadable and len(unreadable) == len(diagnostics):
                            st.error(
                                f"Couldn't read any of these {len(diagnostics)} files — this app likely "
                                f"doesn't have filesystem access to your music library. Example error: "
                                f"`{unreadable[0]['error']}`"
                            )
                        else:
                            if has_tags:
                                st.warning(
                                    f"**{len(has_tags)} tracks have embedded genre/mood Plex isn't showing** "
                                    f"— try a metadata refresh in Plex for these instead of re-tagging:"
                                )
                                for d in has_tags:
                                    st.write(f"- **{d['title']}** — {d['artist']} → genre: `{d['embedded_genre'] or '—'}`, mood: `{d['embedded_mood'] or '—'}`")
                            if no_tags:
                                st.info(
                                    f"**{len(no_tags)} tracks genuinely have no genre/mood embedded** "
                                    f"— these need tagging via Picard or manually in Plex:"
                                )
                                for d in no_tags:
                                    st.write(f"- **{d['title']}** — {d['artist']}")
                            if unreadable:
                                st.caption(f"{len(unreadable)} file(s) couldn't be opened (see errors above/mount issue).")
