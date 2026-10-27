"""🧬 Album clusters (the main section of 🗂️ Library Clusters): the
auto-curated, album-level clusters that playlists and Live Cluster Mixes
are built from. Shows the current generation, the background job status,
the last curation report, and per cluster:

  - 🎧 Playlist — a playlist drawn from EVERY track in the cluster
    (length, max share per artist, popular/discovery mix), previewed with
    play buttons, then saved as a static Plex playlist or a daily Live Mix.
  - 💿 Albums — the cluster's albums ranked by fit score.
  - ⚙️ Manage — lock the cluster, pin albums.

Heavy work (sync, curation, rollback) runs in background threads via
cluster_registry.run_background_job so the page never blocks on it."""

import streamlit as st

import cluster_registry as cr
import curation
import live_playlists as lp
from ui_components import render_track_row


def _fmt_time(dt):
    return dt.strftime("%a %Y-%m-%d %H:%M %Z") if dt else "not scheduled yet"


def _render_job_status():
    status = cr.get_job_status()
    if not status["name"]:
        return status
    if status["running"]:
        st.info(f"⏳ **{status['name']}** running since {status['started_at']}…")
    elif status["error"]:
        st.error(f"❌ **{status['name']}** failed at {status['finished_at']}: {status['error']}")
    else:
        st.success(f"✅ **{status['name']}** finished at {status['finished_at']}.")
    with st.expander("Job log", expanded=False):
        st.code("\n".join(status["log"][-60:]) or "(no output yet)")
    if status["running"] and st.button("🔄 Refresh status", key="registry_refresh_status"):
        st.rerun()
    return status


def _render_report(report):
    status = report.get("status")
    icon = {"committed": "✅", "aborted": "⛔", "failed": "❌", "skipped": "⏭️"}.get(status, "•")
    gens = ""
    if report.get("to_generation") is not None:
        gens = f" · gen {report.get('from_generation')} → {report['to_generation']}"
    rolled = " · ↩️ rolled back" if report.get("rolled_back") else ""
    st.write(f"{icon} **{report.get('trigger', '?')}** run {report.get('started_at')} — {status}{gens}{rolled}")
    if report.get("reason"):
        st.caption(report["reason"])
    bits = []
    for key, label in (("new_albums", "new albums"), ("assigned", "assigned"), ("moved", "moved"),
                       ("ejected", "→ Unclustered"), ("clusters", "clusters"), ("unclustered", "unclustered")):
        if report.get(key) is not None:
            bits.append(f"{report[key]} {label}")
    if bits:
        st.caption(" · ".join(bits))
    for ev in report.get("events") or []:
        if ev["type"] == "retired":
            st.write(f"- 🗑️ Retired **{ev.get('name')}** ({ev.get('albums')} albums)")
        elif ev["type"] == "merged":
            cap = " (cluster cap)" if ev.get("reason") == "cap" else ""
            st.write(f"- 🔗 Merged **{ev.get('name')}** into **{ev.get('into_name')}**{cap}")
        elif ev["type"] == "split":
            st.write(f"- ✂️ Split **{ev.get('name')}** → {', '.join(ev.get('new_names') or [])}")
        elif ev["type"] == "spawned":
            st.write(f"- 🌱 New cluster **{ev.get('name')}** ({ev.get('albums')} albums)")
    for rec in report.get("deleted_subscriptions") or []:
        sub = rec["sub"]
        what = "playlist deleted" if rec.get("playlist_deleted") else (rec.get("note") or "removed")
        st.write(f"- 🧹 Stale mix **{sub.get('playlist_name')}** — {(sub.get('stale') or {}).get('reason')} ({what})")


def render(plex, plex_url, plex_token, gemini_api_key):
    st.subheader("🧬 Album clusters")
    reg = cr.load_registry()
    status = _render_job_status()
    busy = status["running"]

    if reg is None:
        st.info(
            "No album clusters yet. Build a set below (**🛠️ Build clusters**), review it, then press "
            "**📌 Commit as album clusters**. Playlists and Live Mixes are made from the committed "
            "clusters. (If you skip the commit, the first weekly curation bootstraps from your last "
            "saved build automatically.)"
        )
        return

    rows = cr.list_clusters(reg)
    n_albums = len(reg["albums"])
    n_unclustered = next((n for cid, _c, n in rows if cid == cr.UNCLUSTERED_ID), 0)
    cap = reg["params"].get("max_clusters")
    st.caption(
        f"Generation **{reg['generation']}** · {len(rows) - 1} clusters{f' (cap {cap})' if cap else ''} · "
        f"{n_albums} albums ({n_unclustered} unclustered) · last updated {reg.get('updated_at')} · "
        f"next weekly curation: {_fmt_time(lp.get_next_curation_time())}. New albums are assigned every "
        f"day before the Live Mix refresh."
    )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("➕ Sync new albums now", disabled=busy, use_container_width=True):
            if not cr.run_background_job("Sync new albums", cr.sync_new_albums, plex):
                st.warning("Another cluster job is already running.")
            st.rerun()
    with c2:
        if st.button("🧭 Run weekly curation now", disabled=busy, use_container_width=True,
                     help="Same as the Monday run: may move albums, merge/split/retire clusters, "
                          "add new ones, and delete stale Live Mix playlists."):
            started = cr.run_background_job(
                "Weekly curation", curation.run_weekly_curation,
                plex_url, plex_token, api_key=gemini_api_key, trigger="manual",
            )
            if not started:
                st.warning("Another cluster job is already running.")
            st.rerun()

    reports = curation.load_reports()
    if reports:
        with st.expander(f"📋 Last run: {reports[-1].get('trigger')} {reports[-1].get('started_at')} — "
                         f"{reports[-1].get('status')}"):
            _render_report(reports[-1])
            for r in reversed(reports[:-1]):
                st.write("---")
                _render_report(r)

    feats = cr.load_album_features()["albums"]
    for cid, cluster, count in rows:
        stats = cluster.get("stats") or {}
        label_bits = [f"{count} albums"]
        if stats.get("cohesion") is not None:
            label_bits.append(f"cohesion {stats['cohesion']:.2f}")
        if stats.get("plays_28d") is not None:
            label_bits.append(f"{stats['plays_28d']} plays/28d")
        lock = "🔒 " if cluster.get("locked") else ""
        with st.expander(f"{lock}{cluster.get('name')} — {' · '.join(label_bits)}"):
            tab_pl, tab_albums, tab_manage = st.tabs(["🎧 Playlist", "💿 Albums", "⚙️ Manage"])
            with tab_pl:
                _render_playlist_tab(plex, plex_url, plex_token, reg, cid, cluster, count)
            with tab_albums:
                _render_album_list(reg, cid, feats)
            with tab_manage:
                _render_manage_tab(reg, cid, cluster, feats, busy)

    with st.expander("⚙️ Curation settings"):
        _render_params(reg, busy)

    snaps = cr.list_snapshots()
    if snaps:
        with st.expander("↩️ Roll back"):
            st.caption(
                "Restores the clusters as they were at a generation and recreates any Live Mix "
                "playlists that later curations deleted."
            )
            gen = st.selectbox("Generation", [g for g, _ in reversed(snaps)], key="registry_rollback_gen")
            if st.button("↩️ Roll back to this generation", disabled=busy):
                if not cr.run_background_job("Rollback", curation.rollback_to_generation, int(gen), plex):
                    st.warning("Another cluster job is already running.")
                st.rerun()


def _render_playlist_tab(plex, plex_url, plex_token, reg, cid, cluster, count):
    if not count:
        st.info("No albums in this cluster right now.")
        return
    name = cluster.get("name")
    st.caption(
        f"Drawn from every track of all {count} albums in this cluster — a different selection each "
        f"time you generate it."
    )
    c1, c2, c3 = st.columns(3)
    size = c1.number_input("Songs", min_value=5, max_value=500, value=50, step=5, key=f"pl_size_{cid}")
    artist_pct = c2.slider("Max per artist (%)", min_value=0, max_value=100, value=10, step=1,
                           key=f"pl_artist_{cid}",
                           help="Share of the playlist any one artist may fill (0 = no limit). "
                                "10% of 50 songs = at most 5 per artist.")
    popular_pct = c3.slider("Popular (%)", min_value=0, max_value=100, value=50, step=5, key=f"pl_pop_{cid}",
                            help="This share is picked weighted by your play counts; the rest is picked "
                                 "at random from the whole cluster (discovery).")

    make_live = st.checkbox(
        "🎧 Keep it updated daily (Live Mix)", key=f"pl_live_{cid}",
        help="Rebuilds every day from this cluster's current albums — part whole-cluster, part "
             "sonic-similarity expansion — and follows the cluster through weekly curation.",
    )
    if make_live:
        l1, l2, l3 = st.columns(3)
        live_cluster_pct = l1.slider("From the cluster (%)", 10, 100, 67, key=f"pl_live_pct_{cid}",
                                     help="The rest is sonic-similarity expansion from outside the cluster.")
        live_seeds = l2.number_input("Sonic seed songs", 1, 30, 10, key=f"pl_live_seeds_{cid}")
        live_rotation = l3.slider("Daily rotation (%)", 5, 100, 85, step=5, key=f"pl_live_rot_{cid}")
        playlist_name = f"[Mix] {name}"
        st.caption(f"Saved as **{playlist_name}**; renamed automatically if the cluster is renamed.")
    else:
        playlist_name = st.text_input("Playlist name", value=name, key=f"pl_name_{cid}")

    previews = st.session_state.setdefault("cluster_playlist_preview", {})
    b1, b2 = st.columns(2)
    with b1:
        if not make_live and st.button("🎲 Generate preview", key=f"pl_gen_{cid}", use_container_width=True):
            with st.spinner(f"Picking {size} songs from {count} albums…"):
                try:
                    previews[cid] = lp.build_cluster_playlist(
                        plex, cid, size=int(size), max_artist_share=artist_pct / 100,
                        popular_share=popular_pct / 100, registry=reg,
                    )
                except Exception as e:
                    st.error(f"Couldn't build the playlist: {e}")
    with b2:
        if st.button("💾 Save to Plex", key=f"pl_save_{cid}", use_container_width=True):
            with st.spinner("Saving…"):
                try:
                    if make_live:
                        _, n = lp.save_cluster_as_live_mix(
                            plex, plex_url, plex_token, name, playlist_name, target_size=int(size),
                            rotation_percent=int(live_rotation), cluster_ratio=live_cluster_pct / 100,
                            sonic_seed_count=int(live_seeds), cluster_id=cid,
                        )
                        st.success(f"'{playlist_name}' saved as a Live Mix ({n} songs) — it refreshes daily "
                                   "and shows up in 🔴 Live Playlists.")
                    else:
                        tracks = previews.get(cid) or lp.build_cluster_playlist(
                            plex, cid, size=int(size), max_artist_share=artist_pct / 100,
                            popular_share=popular_pct / 100, registry=reg,
                        )
                        lp.upsert_playlist(plex, playlist_name, tracks)
                        st.success(f"Saved '{playlist_name}' with {len(tracks)} songs "
                                   "(an existing playlist with that name is replaced).")
                except Exception as e:
                    st.error(f"Failed to save: {e}")

    preview = previews.get(cid)
    if preview and not make_live:
        artists = len({getattr(t, "grandparentRatingKey", None) for t in preview})
        short = ""
        if len(preview) < size and artist_pct:
            short = (f" Shorter than {size}: with {artists} artists and at most {artist_pct}% per artist, "
                     f"that's all that fits — raise 'Max per artist' for a longer playlist.")
        st.caption(f"Preview: {len(preview)} songs from {artists} artists.{short} Generate again for a new "
                   f"selection.")
        for idx, track in enumerate(preview):
            render_track_row(track, idx, key_prefix=f"clpl_{cid}", mode="view",
                             plex_url=plex_url, plex_token=plex_token)


def _album_label(feats, a):
    f = feats.get(a, {})
    return f"{f.get('artist_title') or '?'} — {f.get('title') or a}"


def _render_album_list(reg, cid, feats):
    members = cr.cluster_album_keys(reg, cid)
    ranked = sorted(members, key=lambda a: reg["albums"][a].get("score") or 0, reverse=True)
    artists = len({feats.get(a, {}).get("artist_key") for a in members})
    st.caption(f"{len(members)} albums by {artists} artists, best fit first (fit score 0–1).")
    lines = []
    for a in ranked[:100]:
        score = reg["albums"][a].get("score")
        lines.append(f"{'📌 ' if reg['albums'][a].get('pinned') else ''}{_album_label(feats, a)}"
                     + (f"  ({score:.2f})" if score is not None else ""))
    st.code("\n".join(lines) or "(empty)")
    if len(ranked) > 100:
        st.caption(f"…and {len(ranked) - 100} more.")


def _render_manage_tab(reg, cid, cluster, feats, busy):
    if cid == cr.UNCLUSTERED_ID:
        st.caption("Albums that don't fit any cluster well enough yet. The weekly curation tries to form "
                   "new clusters from them when there's room under the cluster cap.")
        return
    locked = st.toggle(
        "Lock (never merged, split, retired, or loses albums)", value=bool(cluster.get("locked")),
        key=f"registry_lock_{cid}", disabled=busy,
    )
    if locked != bool(cluster.get("locked")):
        cr.set_cluster_locked(cid, locked)
        st.rerun()

    members = cr.cluster_album_keys(reg, cid)
    pinned_now = [a for a in members if reg["albums"][a].get("pinned")]
    pinned = st.multiselect(
        "Pinned albums (never moved by curation)", members, default=pinned_now,
        format_func=lambda a: _album_label(feats, a), key=f"registry_pins_{cid}", disabled=busy,
    )
    if set(pinned) != set(pinned_now):
        cr.set_albums_pinned(set(pinned) - set(pinned_now), True)
        cr.set_albums_pinned(set(pinned_now) - set(pinned), False)
        st.rerun()


def _render_params(reg, busy):
    p = reg["params"]
    st.caption("Scores are unitless 0–1 affinities (sonic share, tag cosine, artist share).")
    new = {
        "assign_threshold": st.slider("Join threshold", 0.05, 0.9, float(p["assign_threshold"]), 0.01,
                                      help="Minimum affinity for an album to join a cluster."),
        "move_hysteresis": st.slider("Move margin", 0.0, 0.5, float(p["move_hysteresis"]), 0.01,
                                     help="How much better another cluster must fit before an album moves "
                                          "(scaled up for heavily played albums; must hold two weeks in a row)."),
        "min_cluster_albums": st.number_input("Min albums per cluster", 2, 100, int(p["min_cluster_albums"])),
        "max_cluster_albums": st.number_input("Max albums per cluster (bigger ones get split)", 10, 2000,
                                              int(p["max_cluster_albums"])),
        "max_clusters": st.number_input(
            "Max clusters (0 = no limit)", 0, 200, int(p.get("max_clusters") or 0),
            help="Hard cap, Unclustered not counted. Splits and new clusters only happen while there's "
                 "room; if there are more, the most similar ones are merged at the next curation."),
        "max_new_clusters": st.number_input("Max new clusters per week", 0, 20, int(p["max_new_clusters"])),
        "w_sonic": st.slider("Weight: sonic", 0.0, 1.0, float(p["w_sonic"]), 0.05),
        "w_tag": st.slider("Weight: tags", 0.0, 1.0, float(p["w_tag"]), 0.05),
        "w_artist": st.slider("Weight: artist", 0.0, 1.0, float(p["w_artist"]), 0.05),
    }
    if st.button("💾 Save settings", disabled=busy):
        cr.update_params(**new)
        st.success("Saved — applies from the next sync/curation.")
