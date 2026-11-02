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

import pandas as pd
import streamlit as st

import cluster_registry
import curation
import lastfm
from clustering import (
    apply_artist_moves, apply_merge_plan, artist_cluster_map_from_albums, undo_artist_moves, build_clusters, clear_all_caches,
    clear_artist_scan_cache, get_all_genre_and_mood_tags, load_build_cache, propose_clusters, save_build_cache,
    suggest_cluster_names,
)
from tabs import registry_panel

_SESSION_DEFAULTS = {
    'cluster_build': None,          # raw build dict from clustering.build_clusters
    'cluster_merge_plan': [],       # [{"members": [...], "new_name": str}, ...]
    'cluster_locked': "",
    'cluster_total': 10,            # "Maximum number of clusters" (set by the input, the scan or a proposal)
    'suggested_snapshot': None,     # {genre_tags, mood_tags, total_clusters, clusters, mapping}
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


def render(plex, plex_url, plex_token, debug_box, gemini_api_key, lastfm_api_key=None):
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
    mode = _render_method()
    if mode == "sonic":
        settings = _render_relational_clusters(music_section, gemini_api_key, lastfm_api_key)
    else:
        settings = _render_tags_clusters(music_section, gemini_api_key)
    _render_build_step(plex, music_section, gemini_api_key, debug_box, settings, lastfm_api_key)

    raw_build = st.session_state['cluster_build']
    if raw_build:
        _render_review(plex, raw_build, gemini_api_key)
    _render_clean_slate(debug_box)


# --- 1. Method ----------------------------------------------------------------

_METHODS = {
    "sonic": "🔗 Relational — similar artists (recommended)",
    "tags": "🏷️ Tags — your genre/mood tags (uses Gemini)",
}


def _render_method():
    st.subheader("1. Method")
    label = st.radio("How should albums be grouped?", list(_METHODS.values()), key="cluster_method",
                     label_visibility="collapsed")
    mode = next(m for m, text in _METHODS.items() if text == label)
    if mode == "sonic":
        st.caption("Artists are grouped by similarity — Plex's 'Similar Artist' links (what 🌌 Library Galaxy "
                   "shows), Last.fm's similar artists and genre tags if you have a key, optionally audio matching; "
                   "each group brings all its artists' albums. Works without Gemini (names then come from genre "
                   "tags); with a Gemini key, one Gemini call checks the groups and names them by genre.")
    else:
        st.caption("Gemini sorts every genre/mood tag in your library into clusters; each album joins the "
                   "cluster most of its tracks' tags point to. Best when your tags are clean and you want "
                   "specific cluster names.")
    return mode


# --- 2. Clusters --------------------------------------------------------------

def _render_relational_clusters(music_section, gemini_api_key, lastfm_api_key=None):
    st.subheader("2. Clusters")
    if lastfm.available(lastfm_api_key):
        cached = len(lastfm.load_cache()["artists"])
        st.caption(f"🎵 Last.fm on — genre tags and similar artists shape the groups and their names "
                   f"({cached} artist(s) cached; new ones are fetched on the next scan or build).")
    else:
        st.caption("🎵 Last.fm off — add a Last.fm API key (sidebar, or LASTFM_API_KEY) for genre-aware groups "
                   "and better names.")
    if st.button("🔎 Scan & propose", type="primary",
                 help="Tries several cluster counts on your artists' similarity links and picks the one with the "
                      "most clearly separated groups (at most 19). With a Gemini key, one Gemini call names the "
                      "proposed groups."):
        with st.spinner("Scanning the library and trying different cluster counts..."):
            try:
                proposal = propose_clusters(
                    music_section, album_features=cluster_registry.load_album_features()["albums"],
                    api_key=gemini_api_key, lastfm_api_key=lastfm_api_key,
                )
                st.session_state['cluster_proposal'] = proposal
                st.session_state['cluster_total'] = proposal['count']
            except Exception as e:
                st.error(f"Couldn't propose clusters: {e}")
    _render_proposal()
    total_clusters = int(st.number_input(
        "Number of clusters", min_value=2, max_value=40, key="cluster_total",
        help="The strongest this-many groups are kept. Committing also makes it the album clusters' cap, so "
             "weekly curation never grows past it.",
    ))

    with st.expander("⚙️ Advanced settings", expanded=False):
        sonic_boost = st.slider("Sonic boost", 0.0, 1.0, 0.5, 0.05, key="sonic_boost",
                                help="How much audio matching can strengthen an existing Similar-Artist link. "
                                     "It never creates a link on its own — 0 = pure Similar-Artist topology.")
        sample_size = st.slider("Sampled tracks per artist", 3, 10, 6, 1, key="sonic_artist_sample_size")
        use_cache = st.checkbox("Reuse cached artist profiles", value=True, key="sonic_use_cache")
        gemini_check = st.checkbox(
            "🤖 Let Gemini check and name the groups", value=bool(gemini_api_key), disabled=not gemini_api_key,
            key="gemini_check",
            help="One Gemini call after grouping: artists that clearly don't fit their group's genre (e.g. a "
                 "post-punk band in a rap group) move to a better group, or are left for scoring on commit "
                 "(at most 15% of a group's artists), and every group gets a genre name. Off: names come from "
                 "genre tags. Needs a Gemini key.",
        )
        _render_rescan_option()
    return {
        "clustering_mode": "sonic", "total_clusters": total_clusters, "locked_clusters": [],
        "sonic_boost": sonic_boost, "sonic_artist_sample_size": sample_size, "sonic_use_cache": use_cache,
        "refine_unsorted": False, "sonic_weight": 0.0, "reassign_tagged_via_sonic": False,
        "sonic_propagation_rounds": 2, "dry_run": False, "remap": False,
        "gemini_check": bool(gemini_check and gemini_api_key),
        "force_rescan": st.session_state.get("force_rescan_artists", False),
    }


def _render_proposal():
    proposal = st.session_state.get('cluster_proposal')
    if not proposal:
        return
    with st.container(border=True):
        st.markdown(
            f"**{proposal['count']} clusters** covering {proposal['coverage']:.0%} of your albums — the count "
            f"where your artists split into the most clearly separated groups. "
            f"{proposal['unlinked_artists']} artist(s) have no 'Similar Artist' links in Plex; their albums are "
            f"scored individually when you commit. The count below is set to {proposal['count']}."
            + (" Names by 🤖 Gemini." if proposal.get("named_by_gemini") else " Names from genre tags.")
        )
        for c in proposal["clusters"]:
            st.markdown(f"- **{c['name']}** — {c['albums']} albums · {c['artists']} artists  \n"
                        f"  <small>{', '.join(c['top_artists'])}</small>", unsafe_allow_html=True)


def _render_tags_clusters(music_section, gemini_api_key):
    st.subheader("2. Clusters")
    total_clusters = int(st.number_input(
        "Maximum number of clusters", min_value=2, max_value=40, key="cluster_total",
        help="Gemini may use fewer if your tags don't support this many distinct groups. Committing also "
             "makes it the album clusters' cap.",
    ))
    if gemini_api_key:
        _render_suggest(music_section, total_clusters, gemini_api_key)
    locked_input = st.text_input(
        "Cluster names (optional, comma-separated)", placeholder="e.g. Metal, Punk Rock, Classic Rock",
        key="cluster_locked",
        help="Gemini builds exactly these clusters (plus its own, up to the maximum). Committed clusters with "
             "these names are locked against weekly curation.",
    )
    locked_clusters = [c.strip() for c in locked_input.split(",") if c.strip()]
    _render_tag_list(music_section)

    with st.expander("⚙️ Advanced settings", expanded=False):
        remap = st.checkbox("Ask Gemini again", value=False, key="tags_remap",
                            help="Otherwise the last answer for the same tags, names and count is reused "
                                 "(free).")
        dry_run = st.checkbox("🧪 Dry run — offline keyword matcher instead of Gemini", value=False,
                              key="tags_dry_run",
                              help="Clusters won't be accurate, but the whole flow can be tested for free.")
        refine = st.checkbox("Place untagged tracks by sonic similarity", value=False, key="tags_refine",
                             help="Infers a cluster for untagged/unmatched tracks from Plex's sonic "
                                  "neighbours. Local Plex lookups, no Gemini.")
        sonic_weight = st.slider("Sonic influence", 0, 100, 0, 10, disabled=not refine, key="tags_sonic_weight") / 100.0
        reassign = st.checkbox("Also let sonic evidence move already-tagged tracks", value=False,
                               disabled=not refine, key="tags_reassign")
        rounds = st.slider("Sonic propagation rounds", 1, 4, 2, disabled=not refine, key="tags_rounds")
        _render_rescan_option()
    return {
        "clustering_mode": "tags", "total_clusters": total_clusters, "locked_clusters": locked_clusters,
        "sonic_boost": 0.5, "sonic_artist_sample_size": 6, "sonic_use_cache": True,
        "refine_unsorted": refine, "sonic_weight": sonic_weight, "reassign_tagged_via_sonic": reassign,
        "sonic_propagation_rounds": rounds, "dry_run": dry_run, "remap": remap, "gemini_check": False,
        "force_rescan": st.session_state.get("force_rescan_artists", False),
    }


def _render_rescan_option():
    st.checkbox("Rescan the library (ignore the cached artist list)", value=False, key="force_rescan_artists",
                help="The artist list is cached in memory for a while. Check this if you've just added music.")


def _render_tag_list(music_section):
    with st.expander("🏷️ Your library's genre/mood tags"):
        preview = st.session_state.get('cluster_tag_preview')
        if not preview:
            if st.button("Load tags", key="load_tags"):
                with st.spinner("Reading tags..."):
                    genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
                st.session_state['cluster_tag_preview'] = {"genre_tags": genre_tags, "mood_tags": mood_tags}
                st.rerun()
            return
        total = len(preview['genre_tags']) + len(preview['mood_tags'])
        st.caption(f"{len(preview['genre_tags'])} genre tags, {len(preview['mood_tags'])} mood tags — as a rule "
                   f"of thumb ~1 cluster per 15–25 tags, so roughly {max(2, total // 25)}–{max(2, total // 15)} "
                   f"clusters.")
        st.write("**Genre tags:**")
        st.code(", ".join(preview['genre_tags']) or "(none found)")
        st.write("**Mood tags:**")
        st.code(", ".join(preview['mood_tags']) or "(none found)")


def _render_suggest(music_section, total_clusters, gemini_api_key):
    if st.button("🔍 Suggest names (Gemini)",
                 help="One Gemini call proposes a full set of cluster names from your tags. Untick any you "
                      "don't want, then use them — accepting them unchanged skips a second Gemini call at build "
                      "time."):
        with st.spinner("Asking Gemini about your library's genres/moods..."):
            try:
                genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
                clusters, mapping = suggest_cluster_names(genre_tags, mood_tags, total_clusters, gemini_api_key)
                st.session_state['suggested_snapshot'] = {
                    "genre_tags": genre_tags, "mood_tags": mood_tags, "total_clusters": total_clusters,
                    "clusters": clusters, "mapping": mapping,
                }
                st.rerun()
            except Exception as e:
                st.error(f"Failed to suggest names: {e}")

    snap = st.session_state.get('suggested_snapshot')
    if not snap:
        return
    with st.container(border=True):
        st.write(f"**Gemini suggests {len(snap['clusters'])} clusters** — untick any you don't want:")
        selected = [name for name in snap['clusters']
                    if st.checkbox(name, value=True, key=f"suggest_review_{name}")]
        c1, c2 = st.columns(2)
        if c1.button("✅ Use these names", disabled=not selected, use_container_width=True):
            st.session_state['cluster_locked'] = ", ".join(selected)
            st.rerun()
        if c2.button("✕ Dismiss", use_container_width=True):
            st.session_state['suggested_snapshot'] = None
            st.rerun()


# --- 3. Build -----------------------------------------------------------------

def _render_build_step(plex, music_section, gemini_api_key, debug_box, settings, lastfm_api_key=None):
    st.subheader("3. Build")
    tags_mode = settings["clustering_mode"] == "tags"
    if tags_mode and not gemini_api_key and not settings["dry_run"]:
        st.warning("Tags mode needs a Gemini API key (sidebar) — or tick 🧪 Dry run under Advanced settings to "
                   "test for free. Relational mode needs no key.")
        return
    if len(settings["locked_clusters"]) > settings["total_clusters"]:
        st.error("You've named more clusters than the maximum number of clusters allows.")
        return
    if not st.button("🧩 Build clusters", type="primary", use_container_width=True):
        return

    preloaded = None
    snap = st.session_state.get('suggested_snapshot')
    if tags_mode and snap and not settings["remap"] and not settings["dry_run"]:
        genre_tags, mood_tags = get_all_genre_and_mood_tags(music_section)
        same_tags = sorted(genre_tags) == sorted(snap["genre_tags"]) and sorted(mood_tags) == sorted(snap["mood_tags"])
        same_total = settings["total_clusters"] == snap["total_clusters"]
        same_clusters = (sorted(settings["locked_clusters"], key=str.lower)
                         == sorted(snap["clusters"], key=str.lower))
        if same_tags and same_total and same_clusters:
            preloaded = (snap["clusters"], snap["mapping"])
            debug_box.write("**Using the suggested names' mapping directly — no extra Gemini call.**")

    with st.spinner("Building clusters over the whole library..."):
        try:
            if settings["force_rescan"]:
                clear_artist_scan_cache()
            build = build_clusters(
                music_section, plex,
                locked_clusters=settings["locked_clusters"], total_clusters=settings["total_clusters"],
                api_key=gemini_api_key, debug=debug_box, force_remap=settings["remap"],
                dry_run=settings["dry_run"], preloaded_mapping=preloaded,
                refine_unsorted=settings["refine_unsorted"], sonic_weight=settings["sonic_weight"],
                reassign_tagged_via_sonic=settings["reassign_tagged_via_sonic"],
                sonic_propagation_rounds=settings["sonic_propagation_rounds"],
                clustering_mode=settings["clustering_mode"],
                sonic_artist_sample_size=int(settings["sonic_artist_sample_size"]),
                sonic_boost=float(settings["sonic_boost"]), sonic_use_cache=bool(settings["sonic_use_cache"]),
                album_features=cluster_registry.load_album_features()["albums"],
                lastfm_api_key=lastfm_api_key, gemini_check=settings["gemini_check"],
            )
            st.session_state['cluster_build'] = build
            st.session_state['cluster_merge_plan'] = []
            save_build_cache(build, [])
            st.rerun()
        except Exception as e:
            st.error(f"Failed to build clusters: {e}")


# --- Review & commit ----------------------------------------------------------

LEAVE_OUT = "— leave out (scored on commit)"


def _render_suggestions(raw_build):
    """Gemini's suggested artist moves: nothing is applied until you tick
    them. You can also change a suggestion's target before applying."""
    applied = raw_build.get("applied_moves") or []
    done = {(m["artist_key"], m["from"]) for m in applied}
    pending = [m for m in (raw_build.get("suggestions") or []) if (m["artist_key"], m["from"]) not in done]
    if not pending and not applied:
        return
    with st.expander(f"🤖 Gemini suggests moving {len(pending)} artist(s)"
                     + (f" · {len(applied)} applied" if applied else ""), expanded=bool(pending)):
        if pending:
            st.caption("Tick the moves that make sense and press Apply — the rest is simply dropped. You can "
                       "change where an artist goes. 'Leave out' takes the artist out of every cluster; its "
                       "albums are then scored on commit and land where they fit best (or in Unclustered).")
            options = sorted(raw_build["clusters"]) + [LEAVE_OUT]
            df = pd.DataFrame([{
                "Apply": False, "Artist": m["artist"], "From": m["from"],
                "To": m["to"] if m["to"] in raw_build["clusters"] else LEAVE_OUT,
                "Why": m.get("reason", ""), "id": m["id"],
            } for m in pending])
            edited = st.data_editor(
                df, hide_index=True, use_container_width=True, key=f"suggest_moves_{len(applied)}",
                disabled=["Artist", "From", "Why"],
                column_config={"Apply": st.column_config.CheckboxColumn(width="small"),
                               "To": st.column_config.SelectboxColumn(options=options, width="medium"),
                               "id": None},
            )
            by_id = {m["id"]: m for m in pending}
            chosen = [dict(by_id[r["id"]], to=None if r["To"] == LEAVE_OUT else r["To"])
                      for _, r in edited.iterrows() if r["Apply"] and r["To"] != r["From"]]
            if st.button(f"✅ Apply {len(chosen)} move(s)", type="primary", disabled=not chosen,
                         key="apply_suggested_moves"):
                moved = apply_artist_moves(raw_build, chosen)
                save_build_cache(raw_build, st.session_state['cluster_merge_plan'])
                st.session_state['moves_msg'] = f"Moved {moved} artist(s)."
                st.rerun()
        msg = st.session_state.pop('moves_msg', None)
        if msg:
            st.success(msg)
        if applied:
            st.write("**Applied:** " + "; ".join(
                f"{m['artist']} → {m['to'] or 'left out'}" for m in applied))
            if st.button("↩️ Undo applied moves", key="undo_suggested_moves"):
                undo_artist_moves(raw_build)
                save_build_cache(raw_build, st.session_state['cluster_merge_plan'])
                st.rerun()


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
    if build.get("lastfm"):
        mode += " + Last.fm"
    if build.get("named_by_gemini"):
        mode += f" · 🤖 {build['named_by_gemini']} name(s) by Gemini"
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

    _render_suggestions(raw_build)

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
            st.session_state.pop('cluster_proposal', None)
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

