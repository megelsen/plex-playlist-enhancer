"""Library Galaxy tab: renders an interactive 3D star map of your artists
using Plex's "Similar Artist" links, colored by genre cluster when
available. Inspired by Music-Manager-for-Plex's "Galaxy Explorer" feature."""

import streamlit as st

from galaxy import build_similarity_graph, render_galaxy_figure


def _cluster_signature(tag_mapping, artist_cluster_map):
    """Cheap content-based fingerprint of whatever's currently driving
    galaxy node coloring, so the tab can detect 'the cluster build changed
    since I last rendered this graph' and auto-recolor — a plain identity
    check wouldn't work here since clusters_tab.py rebuilds these dicts
    fresh on every render even when their contents are unchanged."""
    mapping_part = tuple(sorted(tag_mapping.items())) if tag_mapping else None
    artist_part = tuple(sorted(artist_cluster_map.items())) if artist_cluster_map else None
    return (mapping_part, artist_part)


def render(plex, debug_box):
    st.title("🌌 Library Galaxy")
    st.caption(
        "A 3D star map of your artists, connected by Plex's 'Similar Artist' data — "
        "artists with more/stronger connections drift closer together. Build clusters "
        "in the 🗂️ Library Clusters tab first (any mode) to color nodes by the clusters "
        "that build actually produced."
    )

    try:
        section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        st.error("No music library section found on this Plex server.")
        st.stop()

    max_artists = st.slider("Max artists to include", min_value=30, max_value=600, value=400, step=10)
    st.caption(
        "Fetching similarity data for every artist can be slow on large libraries — "
        "this caps it to your most-played artists."
    )

    group_by_cluster = st.checkbox(
        "Pull same-cluster artists together",
        value=True,
        help="Node position normally comes only from Plex's 'Similar Artist' data, which "
             "is independent from cluster color — so a cluster-dominant library can "
             "look like one color scattered everywhere, correctly. This adds a gentle pull "
             "between same-cluster artists so the coloring is also visually legible, "
             "without discarding the real similarity structure. Requires clusters built in "
             "🗂️ Library Clusters first (any clustering mode — Hybrid, Tags, or Sonic).",
    )
    show_legend = st.checkbox(
        "Show legend",
        value=True,
        help="The cluster legend now floats on top of the chart instead of pushing it "
             "into a separate column, but on small screens it can still cover part of "
             "the view — toggle it off to give the whole screen to the galaxy itself.",
    )

    if st.button("🌟 Build Galaxy"):
        with st.spinner("Fetching similarity links and laying out the galaxy..."):
            graph = build_similarity_graph(section, max_artists=int(max_artists), debug=debug_box)
            st.session_state['galaxy_graph'] = graph
            tag_mapping = st.session_state.get('cluster_tag_mapping')
            artist_cluster_map = st.session_state.get('cluster_artist_map')
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                graph, tag_mapping=tag_mapping, artist_cluster_map=artist_cluster_map,
                group_by_cluster=group_by_cluster, show_legend=show_legend,
                camera_zoom=st.session_state.get('galaxy_camera_zoom', 1.0)
            )
            st.session_state['galaxy_node_count'] = graph.number_of_nodes()
            st.session_state['galaxy_edge_count'] = graph.number_of_edges()
            st.session_state['galaxy_cluster_signature'] = _cluster_signature(tag_mapping, artist_cluster_map)

    # Re-laying out with the toggle flipped doesn't need a fresh Plex fetch —
    # reuse the already-built graph if we have one.
    if st.session_state.get('galaxy_graph') is not None and st.button("🔄 Re-layout with current toggle"):
        with st.spinner("Re-laying out the galaxy..."):
            tag_mapping = st.session_state.get('cluster_tag_mapping')
            artist_cluster_map = st.session_state.get('cluster_artist_map')
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                st.session_state['galaxy_graph'], tag_mapping=tag_mapping,
                artist_cluster_map=artist_cluster_map, group_by_cluster=group_by_cluster,
                show_legend=show_legend, camera_zoom=st.session_state.get('galaxy_camera_zoom', 1.0)
            )
            st.session_state['galaxy_cluster_signature'] = _cluster_signature(tag_mapping, artist_cluster_map)

    # Auto-refresh coloring when the underlying cluster build has changed
    # since this galaxy was last rendered (e.g. you rebuilt clusters in
    # 🗂️ Library Clusters after already building the galaxy once) — without
    # this, the legend/bucket colors silently keep showing whatever cluster
    # set existed at the last Build/Re-layout click, which looks like a bug
    # (old cluster count/names) even though the underlying clusters have
    # moved on. This is a pure local recoloring — same graph, same layout,
    # no Plex re-fetch — so it's cheap enough to just do automatically
    # rather than asking the user to notice and click Re-layout themselves.
    if st.session_state.get('galaxy_graph') is not None:
        tag_mapping = st.session_state.get('cluster_tag_mapping')
        artist_cluster_map = st.session_state.get('cluster_artist_map')
        current_signature = _cluster_signature(tag_mapping, artist_cluster_map)
        cluster_changed = st.session_state.get('galaxy_cluster_signature') != current_signature
        # The "Show legend" checkbox and the zoom buttons have no dedicated
        # rebuild step — each should take effect on the very next rerun
        # (which Streamlit already triggers on checkbox/button interaction),
        # so both are tracked the same way as the cluster signature rather
        # than requiring a manual Re-layout click.
        legend_changed = st.session_state.get('galaxy_show_legend') != show_legend
        camera_zoom = st.session_state.get('galaxy_camera_zoom', 1.0)
        zoom_changed = st.session_state.get('galaxy_camera_zoom_rendered') != camera_zoom
        if cluster_changed or legend_changed or zoom_changed:
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                st.session_state['galaxy_graph'], tag_mapping=tag_mapping,
                artist_cluster_map=artist_cluster_map, group_by_cluster=group_by_cluster,
                show_legend=show_legend, camera_zoom=camera_zoom
            )
            st.session_state['galaxy_cluster_signature'] = current_signature
            st.session_state['galaxy_show_legend'] = show_legend
            st.session_state['galaxy_camera_zoom_rendered'] = camera_zoom
            if cluster_changed:
                st.caption("🔄 Recolored automatically to match the latest cluster build.")

    figure = st.session_state.get('galaxy_figure')
    if figure is not None:
        st.caption(
            f"{st.session_state.get('galaxy_node_count', 0)} artists, "
            f"{st.session_state.get('galaxy_edge_count', 0)} similarity links."
        )
        # Streamlit's default block width can leave the chart narrower than
        # the viewport on mobile — this forces the chart's own container to
        # fill all available width and centers it, on top of
        # use_container_width doing the same at the Streamlit-widget level.
        st.markdown(
            """<style>
            div[data-testid="stPlotlyChart"] {
                width: 100% !important;
                margin-left: auto !important;
                margin-right: auto !important;
            }
            /* On narrow (mobile) viewports, Streamlit's default page
            padding still eats noticeable width off both sides of the
            chart on top of anything stPlotlyChart itself does — shrinking
            that padding is what lets the 3D scene actually reach the
            edges of the screen instead of sitting in a letterboxed strip. */
            @media (max-width: 640px) {
                .block-container {
                    padding-left: 0.5rem !important;
                    padding-right: 0.5rem !important;
                }
                div[data-testid="stPlotlyChart"] > div {
                    height: 75vh !important;
                }
            }
            </style>""",
            unsafe_allow_html=True,
        )
        # Plotly's 3D scenes don't support pinch-to-zoom on touch devices
        # (the scrollZoom config option only wires up desktop mouse-wheel
        # zoom), so these buttons drive the camera distance directly —
        # each click nudges galaxy_camera_zoom, which the render calls
        # above pick up and re-apply to the figure's camera.eye.
        zoom_in, zoom_out, zoom_reset = st.columns(3)
        with zoom_in:
            if st.button("🔍➕ Zoom in", use_container_width=True):
                st.session_state['galaxy_camera_zoom'] = max(
                    0.3, st.session_state.get('galaxy_camera_zoom', 1.0) * 0.8
                )
                st.rerun()
        with zoom_out:
            if st.button("🔍➖ Zoom out", use_container_width=True):
                st.session_state['galaxy_camera_zoom'] = min(
                    3.0, st.session_state.get('galaxy_camera_zoom', 1.0) * 1.25
                )
                st.rerun()
        with zoom_reset:
            if st.button("↺ Reset zoom", use_container_width=True):
                st.session_state['galaxy_camera_zoom'] = 1.0
                st.rerun()

        st.plotly_chart(
            figure,
            use_container_width=True,
            config={
                "scrollZoom": True,  # desktop mouse-wheel zoom; touch pinch-zoom isn't
                                     # supported by Plotly's gl3d camera, hence the buttons above
                "displaylogo": False,
                # Clicking a dragmode button (Pan / Orbit / Turntable) in
                # Plotly's 3D modebar forces the camera back to its default
                # position/zoom, even when re-selecting the mode that's
                # already active — that's what was resetting zoom on mobile
                # whenever pan or orbit got tapped. The scene's dragmode is
                # already pinned to 'orbit' in the layout, so these buttons
                # are pure liability here and are removed rather than
                # worked around. Desktop wheel-zoom (scrollZoom above) and
                # the mobile zoom buttons below are independent of this and
                # unaffected by removing them.
                "modeBarButtonsToRemove": [
                    "pan3d", "orbitRotation", "tableRotation", "zoom3d",
                ],
            },
        )
        if not st.session_state.get('cluster_tag_mapping') and not st.session_state.get('cluster_artist_map'):
            st.info("All nodes are 'Uncategorized' right now — build clusters in 🗂️ Library Clusters to color them.")
    else:
        st.info("Click 'Build Galaxy' to render your library's artist similarity map.")