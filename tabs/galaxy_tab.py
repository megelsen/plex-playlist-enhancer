"""Library Galaxy tab: renders an interactive 3D star map of your artists
using Plex's "Similar Artist" links, colored by genre cluster when
available. Inspired by Music-Manager-for-Plex's "Galaxy Explorer" feature."""

import json

import plotly.utils
import streamlit as st
import streamlit.components.v1 as components

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


def _render_galaxy_html(figure, height=650):
    """Embeds the figure via raw plotly.js instead of st.plotly_chart, and
    wires zoom/legend buttons to Plotly.relayout() calls that run entirely
    in the browser. Doing this through Streamlit (session_state + a
    Python-side re-render + a full rerun) meant every zoom click or legend
    toggle re-shipped the whole figure JSON over the Streamlit websocket
    and re-ran the tab's Python — slow, and increasingly so as the graph
    grows. A relayout of just the camera or showlegend property is a tiny,
    instant browser-side patch that never leaves the page, so this bypasses
    Streamlit entirely for anything that doesn't actually change the
    underlying node data or layout (that part — Build/Re-layout/cluster
    recoloring — still goes through Python, since it genuinely needs it).
    """
    fig_json = json.dumps(figure, cls=plotly.utils.PlotlyJSONEncoder)
    html = f"""
    <div id="galaxy-plot" style="width:100%; height:{height}px;"></div>
    <div style="display:flex; flex-direction:column; gap:8px; margin-top:10px;">
        <button onclick="galaxyZoomBy(0.8)"
            style="width:100%; padding:12px; font-size:15px; border-radius:8px;
                   border:1px solid rgba(128,128,128,0.4); cursor:pointer;">
            🔍➕ Zoom in
        </button>
        <button onclick="galaxyZoomBy(1.25)"
            style="width:100%; padding:12px; font-size:15px; border-radius:8px;
                   border:1px solid rgba(128,128,128,0.4); cursor:pointer;">
            🔍➖ Zoom out
        </button>
        <button onclick="galaxyResetZoom()"
            style="width:100%; padding:12px; font-size:15px; border-radius:8px;
                   border:1px solid rgba(128,128,128,0.4); cursor:pointer;">
            ↺ Reset zoom
        </button>
        <button onclick="galaxyToggleLegend()"
            style="width:100%; padding:12px; font-size:15px; border-radius:8px;
                   border:1px solid rgba(128,128,128,0.4); cursor:pointer;">
            🗂️ Toggle legend
        </button>
    </div>
    <script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
    <script>
        var galaxyFig = {fig_json};
        var galaxyDiv = document.getElementById('galaxy-plot');
        Plotly.newPlot(galaxyDiv, galaxyFig.data, galaxyFig.layout, {{
            scrollZoom: true,
            displaylogo: false,
            // Same rationale as before: 3D dragmode-switch buttons reset
            // the camera on click, including on mobile, so they're
            // stripped from the modebar rather than worked around.
            modeBarButtonsToRemove: ['pan3d', 'orbitRotation', 'tableRotation', 'zoom3d'],
            responsive: true
        }});

        var galaxyZoom = 1.0;
        function galaxyZoomBy(factor) {{
            galaxyZoom = Math.max(0.3, Math.min(3.0, galaxyZoom * factor));
            Plotly.relayout(galaxyDiv, {{
                'scene.camera.eye.x': 1.25 * galaxyZoom,
                'scene.camera.eye.y': 1.25 * galaxyZoom,
                'scene.camera.eye.z': 1.25 * galaxyZoom
            }});
        }}
        function galaxyResetZoom() {{
            galaxyZoom = 1.0;
            Plotly.relayout(galaxyDiv, {{
                'scene.camera.eye.x': 1.25, 'scene.camera.eye.y': 1.25, 'scene.camera.eye.z': 1.25
            }});
        }}
        var galaxyLegendOn = true;
        function galaxyToggleLegend() {{
            galaxyLegendOn = !galaxyLegendOn;
            Plotly.relayout(galaxyDiv, {{showlegend: galaxyLegendOn}});
        }}
    </script>
    """
    # height padding accounts for the 4 stacked buttons below the plot
    components.html(html, height=height + 230, scrolling=False)


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

    if st.button("🌟 Build Galaxy"):
        with st.spinner("Fetching similarity links and laying out the galaxy..."):
            graph = build_similarity_graph(section, max_artists=int(max_artists), debug=debug_box)
            st.session_state['galaxy_graph'] = graph
            tag_mapping = st.session_state.get('cluster_tag_mapping')
            artist_cluster_map = st.session_state.get('cluster_artist_map')
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                graph, tag_mapping=tag_mapping, artist_cluster_map=artist_cluster_map,
                group_by_cluster=group_by_cluster
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
                artist_cluster_map=artist_cluster_map, group_by_cluster=group_by_cluster
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
    # (Zoom and legend-visibility no longer live here at all — see
    # _render_galaxy_html — since neither changes node data or coloring.)
    if st.session_state.get('galaxy_graph') is not None:
        tag_mapping = st.session_state.get('cluster_tag_mapping')
        artist_cluster_map = st.session_state.get('cluster_artist_map')
        current_signature = _cluster_signature(tag_mapping, artist_cluster_map)
        if st.session_state.get('galaxy_cluster_signature') != current_signature:
            st.session_state['galaxy_figure'] = render_galaxy_figure(
                st.session_state['galaxy_graph'], tag_mapping=tag_mapping,
                artist_cluster_map=artist_cluster_map, group_by_cluster=group_by_cluster
            )
            st.session_state['galaxy_cluster_signature'] = current_signature
            st.caption("🔄 Recolored automatically to match the latest cluster build.")

    figure = st.session_state.get('galaxy_figure')
    if figure is not None:
        st.caption(
            f"{st.session_state.get('galaxy_node_count', 0)} artists, "
            f"{st.session_state.get('galaxy_edge_count', 0)} similarity links."
        )
        _render_galaxy_html(figure)
        if not st.session_state.get('cluster_tag_mapping') and not st.session_state.get('cluster_artist_map'):
            st.info("All nodes are 'Uncategorized' right now — build clusters in 🗂️ Library Clusters to color them.")
    else:
        st.info("Click 'Build Galaxy' to render your library's artist similarity map.")