"""Library Galaxy: visualizes artists and Plex's "Similar Artist" links as
an interactive 3D star map, in the spirit of Music-Manager-for-Plex's
"Galaxy Explorer" feature. Every artist is a node; an edge exists wherever
Plex's similarity hub connects two artists in the library. Nodes are
colored by genre cluster when a cluster mapping is available (built in the
Library Clusters tab), so the visual clustering and the genre clustering
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
        if not genre_tags:
            # Plex usually tags genre at album level, not artist level, so an
            # artist with no direct genre tags is common, not necessarily
            # "no genre info" — sample its albums the same way playlist
            # clustering does (assign_track_cluster's album-first fallback),
            # or every artist without album-level genre metadata would
            # incorrectly get dumped into "Unsorted".
            try:
                for album in a.albums():
                    genre_tags |= {g.tag for g in getattr(album, 'genres', [])}
            except Exception:
                pass
        graph.add_node(
            a.ratingKey,
            title=a.title,
            genres=list(genre_tags),
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


def render_galaxy_figure(graph, tag_mapping=None, group_by_cluster=True):
    """
    Lays the graph out in 3D using a spring (force-directed) layout and
    renders it as an interactive Plotly figure: drag to rotate (works at
    any zoom level), scroll to zoom, hover a node to see its title and
    (if tag_mapping is supplied) its genre cluster.

    Node position and node color come from two independent signals — Plex's
    "Similar Artist" web (position) and genre-tag clustering (color) — so by
    default they won't visually align; a library that's genuinely dominated
    by one genre will look like one color scattered everywhere, which is
    accurate, not a clustering bug.

    group_by_cluster=True adds a gentle attractive pull between all members
    of the same cluster (via temporary hub nodes connected to every member,
    removed again before rendering) so clusters also tend to clump spatially
    — a middle ground between "pure similarity layout" and "pure genre
    grouping" that makes the color-coding visually legible without
    discarding the real similarity-graph structure.
    """
    if graph.number_of_nodes() == 0:
        return None

    clusters_to_nodes = {}
    for n, data in graph.nodes(data=True):
        cluster = assign_artist_cluster(data.get('genres', []), tag_mapping) if tag_mapping else "Uncategorized"
        clusters_to_nodes.setdefault(cluster, []).append(n)

    if group_by_cluster and tag_mapping:
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
        showlegend=True,
        legend=dict(bgcolor='rgba(0,0,0,0)'),
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
        ),
        autosize=True,
        width=None,   # let the container (see use_container_width in the tab) drive width
        height=650,
        uirevision='galaxy',  # preserves current rotation/zoom across reruns
    )
    return fig
