"""Library Galaxy tab: renders an interactive 3D star map of your artists
using Plex's "Similar Artist" links, colored by genre cluster when
available. Inspired by Music-Manager-for-Plex's "Galaxy Explorer" feature."""

import streamlit as st

from galaxy import build_similarity_graph, render_galaxy_figure


def render(plex, debug_box):
    st.title("🌌 Library Galaxy")
    st.caption(
        "A 3D star map of your artists, connected by Plex's 'Similar Artist' data — "
        "artists with more/stronger connections drift closer together. Build clusters "
        "in the 🗂️ Library Clusters tab first to color nodes by genre cluster."
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
             "is independent from genre-cluster color — so a genre-dominant library can "
             "look like one color scattered everywhere, correctly. This adds a gentle pull "
             "between same-cluster artists so the coloring is also visually legible, "
             "without discarding the real similarity structure. Requires clusters built in "
             "🗂️ Library Clusters first.",
    )

    if st.button("🌟 Build Galaxy"):
        with st.spinner("Fetching similarity links and laying out the galaxy..."):
            graph = build_similarity_graph(section, max_artists=int(max_artists), debug=debug_box)
            st.session_state['galaxy_graph'] = graph
            tag_mapping = st.session_state.get('cluster_tag_mapping')
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                graph, tag_mapping=tag_mapping, group_by_cluster=group_by_cluster
            )
            st.session_state['galaxy_node_count'] = graph.number_of_nodes()
            st.session_state['galaxy_edge_count'] = graph.number_of_edges()

    # Re-laying out with the toggle flipped doesn't need a fresh Plex fetch —
    # reuse the already-built graph if we have one.
    if st.session_state.get('galaxy_graph') is not None and st.button("🔄 Re-layout with current toggle"):
        with st.spinner("Re-laying out the galaxy..."):
            tag_mapping = st.session_state.get('cluster_tag_mapping')
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                st.session_state['galaxy_graph'], tag_mapping=tag_mapping, group_by_cluster=group_by_cluster
            )

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
            </style>""",
            unsafe_allow_html=True,
        )
        st.plotly_chart(
            figure,
            use_container_width=True,
            config={
                "scrollZoom": True,  # pinch/scroll to zoom, including on mobile
                "displaylogo": False,
            },
        )
        if not st.session_state.get('cluster_tag_mapping'):
            st.info("All nodes are 'Uncategorized' right now — build clusters in 🗂️ Library Clusters to color them by genre.")
    else:
        st.info("Click 'Build Galaxy' to render your library's artist similarity map.")
