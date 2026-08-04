"""Library Galaxy: visualizes artists and Plex's "Similar Artist" links as
an interactive 3D star map, in the spirit of Music-Manager-for-Plex's
"Galaxy Explorer" feature. Every artist is a node; an edge exists wherever
Plex's similarity hub connects two artists in the library. Nodes are
colored by genre/mood cluster when a cluster mapping is available (built in
the Library Clusters tab), so the visual clustering and the tag clustering
can be cross-checked against each other.
"""

import networkx as nx
import plotly.express as px
import plotly.graph_objects as go

from clustering import assign_artist_cluster


def build_similarity_graph(music_section, max_artists=150, debug=None):
    """
    Builds an undirected graph: one node per artist (up to max_artists,
    capped by play count so the most-listened artists are kept if the
    library is large), one edge per Plex "Similar Artist" link between two
    artists that are both in the graph.

    Similarity lookups only work in one direction per artist (A says B is
    similar; B may or may not say A is similar back) — both directions are
    added as a single undirected edge, so the graph reflects "these two are
    connected" rather than "A points at B".
    """
    d = debug.write if debug else (lambda *a, **k: None)

    try:
        artists = music_section.searchArtists()
    except Exception:
        artists = []

    if len(artists) > max_artists:
        artists = sorted(artists, key=lambda a: getattr(a, 'viewCount', 0) or 0, reverse=True)[:max_artists]
        d(f"Library has more than {max_artists} artists — showing the {max_artists} most-played.")

    key_to_title = {a.ratingKey: a.title for a in artists}
    title_to_key = {t.lower(): k for k, t in key_to_title.items()}

    graph = nx.Graph()
    for a in artists:
        genre_tags = {g.tag for g in getattr(a, 'genres', [])}
        mood_tags = set()
        try:
            albums = a.albums()
        except Exception:
            albums = []
        if not genre_tags:
            # Plex usually tags genre at album level, not artist level, so an
            # artist with no direct genre tags is common, not necessarily
            # "no genre info" — sample its albums the same way playlist
            # clustering does (assign_track_cluster's album-first fallback),
            # or every artist without album-level genre metadata would
            # incorrectly get dumped into "Unsorted".
            for album in albums:
                genre_tags |= {g.tag for g in getattr(album, 'genres', [])}
        # Moods only ever live at album/track level in Plex (no artist-level
        # fallback exists, matching clustering.py's assign_track_cluster) —
        # collected unconditionally here so an artist whose cluster
        # membership in the Clusters tab comes mainly from mood tags (e.g. a
        # "Chill" or "Upbeat" cluster) still gets picked up here instead of
        # only ever showing as Uncategorized in the galaxy.
        for album in albums:
            mood_tags |= {m.tag for m in getattr(album, 'moods', [])}
        graph.add_node(
            a.ratingKey,
            title=a.title,
            genres=list(genre_tags),
            moods=list(mood_tags),
        )

    for a in artists:
        try:
            similar_attr = getattr(a, 'similar', None)
            similar = similar_attr() if callable(similar_attr) else (similar_attr or [])
        except Exception as e:
            d(f"└ ❌ Similar-artist fetch failed for `{a.title}`: `{e}`")
            continue
        for sim in (similar or []):
            name = getattr(sim, 'tag', None)
            if not name:
                continue
            match_key = title_to_key.get(name.lower())
            if match_key and match_key != a.ratingKey:
                graph.add_edge(a.ratingKey, match_key)

    d(f"**Graph built:** {graph.number_of_nodes()} artists, {graph.number_of_edges()} similarity links.")
    return graph


def render_galaxy_figure(graph, tag_mapping=None, artist_cluster_map=None, group_by_cluster=True,
                          show_legend=True, camera_zoom=1.0):
    """
    Lays the graph out in 3D using a spring (force-directed) layout and
    renders it as an interactive Plotly figure: drag to rotate (works at
    any zoom level), scroll to zoom, hover a node to see its title and
    cluster.

    Node position and node color come from two independent signals — Plex's
    "Similar Artist" web (position) and cluster membership (color) — so by
    default they won't visually align; a library that's genuinely dominated
    by one cluster will look like one color scattered everywhere, which is
    accurate, not a clustering bug.

    Coloring prefers `artist_cluster_map` — {artist_ratingKey: cluster_name}
    from clustering.build_artist_cluster_map, i.e. what Library Clusters'
    last build ACTUALLY decided for each artist — over re-deriving
    membership from tags via tag_mapping. The two agree in Tags mode (tag
    IS the membership decision there), but can genuinely differ in
    Hybrid/Sonic mode, where an artist's cluster comes from the similarity
    graph and may disagree with what its tags alone would suggest — using
    artist_cluster_map is what makes the galaxy reflect the SAME clusters
    you see in the Library Clusters tab, in whichever mode built them,
    rather than silently re-running a tags-only guess in the background.
    tag_mapping is still accepted as a fallback for artists missing from
    artist_cluster_map (e.g. an artist with zero tracks in any cluster's
    final track list) and for backward compatibility when no cluster build
    has happened yet at all.

    group_by_cluster=True adds a gentle attractive pull between all members
    of the same cluster (via temporary hub nodes connected to every member,
    removed again before rendering) so clusters also tend to clump spatially
    — a middle ground between "pure similarity layout" and "pure genre
    grouping" that makes the color-coding visually legible without
    discarding the real similarity-graph structure.
    """
    if graph.number_of_nodes() == 0:
        return None

    have_cluster_signal = bool(artist_cluster_map) or bool(tag_mapping)

    clusters_to_nodes = {}
    for n, data in graph.nodes(data=True):
        cluster = None
        if artist_cluster_map:
            cluster = artist_cluster_map.get(n) or artist_cluster_map.get(str(n))
        if cluster is None and tag_mapping:
            artist_tags = set(data.get('genres', [])) | set(data.get('moods', []))
            cluster = assign_artist_cluster(artist_tags, tag_mapping)
        clusters_to_nodes.setdefault(cluster or "Uncategorized", []).append(n)

    if group_by_cluster and have_cluster_signal:
        layout_graph = graph.copy()
        for cluster_name, node_ids in clusters_to_nodes.items():
            hub_id = f"__hub__{cluster_name}"
            layout_graph.add_node(hub_id)
            for n in node_ids:
                # Higher weight = networkx treats this as a stronger pull,
                # drawing cluster members toward their shared hub without
                # fully overriding the real similarity edges (which get the
                # default weight of 1).
                layout_graph.add_edge(hub_id, n, weight=6.0)
        raw_pos = nx.spring_layout(layout_graph, dim=3, weight='weight', seed=42)
        pos = {n: raw_pos[n] for n in graph.nodes()}  # drop hub-only positions
    else:
        pos = nx.spring_layout(graph, dim=3, seed=42)

    edge_x, edge_y, edge_z = [], [], []
    for u, v in graph.edges():
        x0, y0, z0 = pos[u]
        x1, y1, z1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
        edge_z += [z0, z1, None]

    edge_trace = go.Scatter3d(
        x=edge_x, y=edge_y, z=edge_z,
        mode='lines',
        line=dict(color='rgba(150,150,150,0.25)', width=1),
        hoverinfo='none',
        showlegend=False,
    )

    # Explicit qualitative palette (not Plotly's default trace-cycling colors,
    # which can look washed out / too similar for adjacent clusters) so each
    # cluster is clearly distinguishable at a glance.
    palette = px.colors.qualitative.Bold + px.colors.qualitative.Set3
    node_traces = []
    for i, (cluster_name, node_ids) in enumerate(clusters_to_nodes.items()):
        xs = [pos[n][0] for n in node_ids]
        ys = [pos[n][1] for n in node_ids]
        zs = [pos[n][2] for n in node_ids]
        titles = [graph.nodes[n]['title'] for n in node_ids]
        color = palette[i % len(palette)]
        node_traces.append(go.Scatter3d(
            x=xs, y=ys, z=zs,
            mode='markers',
            marker=dict(size=5, opacity=0.9, color=color, line=dict(width=0)),
            text=titles,
            hovertext=[f"{t} — {cluster_name}" for t in titles],
            hoverinfo='text',
            name=cluster_name,
        ))

    fig = go.Figure(data=[edge_trace] + node_traces)
    fig.update_layout(
        margin=dict(l=0, r=0, b=0, t=0),
        showlegend=show_legend,
        # Legend is positioned as a floating overlay INSIDE the plot area
        # (x/y anchored near the top-left corner, semi-transparent
        # background) rather than Plotly's default of a separate column
        # outside the plot. The default column is what strangles the 3D
        # scene down to a sliver on narrow/mobile viewports — an overlay
        # costs zero horizontal layout space, so the scene fills the full
        # container width whether or not the legend is currently shown.
        legend=dict(
            bgcolor='rgba(0,0,0,0.4)',
            bordercolor='rgba(255,255,255,0.15)',
            borderwidth=1,
            font=dict(color='white', size=11),
            x=0.01,
            y=0.99,
            xanchor='left',
            yanchor='top',
        ),
        scene=dict(
            # Axes have no inherent meaning (spring-layout positions are
            # just whatever minimizes edge crossing/tension), so they stay
            # hidden — but 'orbit' dragmode is what makes click-drag rotate
            # the view smoothly, including while zoomed in, rather than
            # panning or doing nothing.
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            zaxis=dict(visible=False),
            dragmode='orbit',
            # Plotly's gl3d camera has no built-in pinch-to-zoom on touch
            # devices (scrollZoom in the chart config only covers desktop
            # mouse-wheel zoom) — so zoom here is driven explicitly by
            # scaling the camera's eye vector, controlled by dedicated
            # +/- buttons in the tab rather than a gesture.
            camera=dict(eye=dict(x=1.25 * camera_zoom, y=1.25 * camera_zoom, z=1.25 * camera_zoom)),
        ),
        autosize=True,
        width=None,   # let the container (see use_container_width in the tab) drive width
        height=650,
        # uirevision is tied to camera_zoom (rounded to avoid float-noise
        # churn) rather than a plain constant: a fixed string would make
        # Plotly ignore the explicit camera eye above on every rerun after
        # the first (that's the whole point of uirevision — preserve
        # whatever the user last set), which would silently swallow the
        # zoom-button clicks. Bumping the revision only when zoom actually
        # changes still preserves rotation/orbit position across unrelated
        # reruns (e.g. toggling the legend), since the revision string is
        # unchanged in that case.
        uirevision=f'galaxy-{round(camera_zoom, 3)}',
    )
    return fig