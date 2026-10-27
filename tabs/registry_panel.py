"""Album cluster registry panel (rendered inside 🗂️ Library Clusters): the
auto-curated, album-level clusters that Live Cluster Mixes follow. Shows
the current generation, the background job status, the last curation
report, per-cluster details (lock / pin albums), tuning params and
rollback. All heavy work runs in background threads via
cluster_registry.run_background_job so the page never blocks on it."""

import streamlit as st

import cluster_registry as cr
import curation
import live_playlists as lp


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
            st.write(f"- 🔗 Merged **{ev.get('name')}** into **{ev.get('into_name')}**")
        elif ev["type"] == "split":
            st.write(f"- ✂️ Split **{ev.get('name')}** → {', '.join(ev.get('new_names') or [])}")
        elif ev["type"] == "spawned":
            st.write(f"- 🌱 New cluster **{ev.get('name')}** ({ev.get('albums')} albums)")
    for rec in report.get("deleted_subscriptions") or []:
        sub = rec["sub"]
        what = "playlist deleted" if rec.get("playlist_deleted") else (rec.get("note") or "removed")
        st.write(f"- 🧹 Stale mix **{sub.get('playlist_name')}** — {(sub.get('stale') or {}).get('reason')} ({what})")


def render(plex, plex_url, plex_token, gemini_api_key):
    reg = cr.load_registry()
    with st.expander("🧬 Album clusters — auto-curated every week", expanded=reg is not None):
        status = _render_job_status()
        busy = status["running"]

        next_cur = lp.get_next_curation_time()
        if reg is None:
            st.info(
                "No album cluster registry yet. Build clusters below, then press "
                "**📌 Commit build as album clusters** under Results. (If you skip this, the first "
                "weekly curation bootstraps from your last saved build automatically.)"
            )
            return

        rows = cr.list_clusters(reg)
        n_albums = len(reg["albums"])
        n_unclustered = next((n for cid, _c, n in rows if cid == cr.UNCLUSTERED_ID), 0)
        st.caption(
            f"Generation **{reg['generation']}** · {len(rows) - 1} clusters · {n_albums} albums "
            f"({n_unclustered} unclustered) · last updated {reg.get('updated_at')} · "
            f"next weekly curation: {_fmt_time(next_cur)}. New albums are assigned every day "
            f"before the Live Mix refresh."
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
            st.write("**Last run**")
            _render_report(reports[-1])
            if len(reports) > 1:
                with st.expander(f"Earlier runs ({len(reports) - 1})"):
                    for r in reversed(reports[:-1]):
                        _render_report(r)
                        st.write("")

        st.write("**Clusters**")
        for cid, cluster, count in rows:
            stats = cluster.get("stats") or {}
            label_bits = [f"{count} albums"]
            if stats.get("cohesion") is not None:
                label_bits.append(f"cohesion {stats['cohesion']:.2f}")
            if stats.get("plays_28d") is not None:
                label_bits.append(f"{stats['plays_28d']} plays/28d")
            lock = "🔒 " if cluster.get("locked") else ""
            with st.expander(f"{lock}{cluster.get('name')} — {' · '.join(label_bits)}"):
                _render_cluster_detail(reg, cid, cluster, busy)

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


def _render_cluster_detail(reg, cid, cluster, busy):
    if cid != cr.UNCLUSTERED_ID:
        locked = st.toggle(
            "Lock (never merged, split, retired, or loses albums)", value=bool(cluster.get("locked")),
            key=f"registry_lock_{cid}", disabled=busy,
        )
        if locked != bool(cluster.get("locked")):
            cr.set_cluster_locked(cid, locked)
            st.rerun()

    feats = cr.load_album_features()["albums"]
    members = cr.cluster_album_keys(reg, cid)

    def _label(a):
        f = feats.get(a, {})
        return f"{f.get('artist_title') or '?'} — {f.get('title') or a}"

    pinned_now = [a for a in members if reg["albums"][a].get("pinned")]
    if cid != cr.UNCLUSTERED_ID:
        pinned = st.multiselect(
            "Pinned albums (never moved by curation)", members, default=pinned_now,
            format_func=_label, key=f"registry_pins_{cid}", disabled=busy,
        )
        if set(pinned) != set(pinned_now):
            cr.set_albums_pinned(set(pinned) - set(pinned_now), True)
            cr.set_albums_pinned(set(pinned_now) - set(pinned), False)
            st.rerun()

    ranked = sorted(members, key=lambda a: reg["albums"][a].get("score") or 0, reverse=True)
    lines = []
    for a in ranked[:60]:
        score = reg["albums"][a].get("score")
        lines.append(f"{'📌 ' if reg['albums'][a].get('pinned') else ''}{_label(a)}"
                     + (f"  ({score:.2f})" if score is not None else ""))
    st.code("\n".join(lines) or "(empty)")
    if len(ranked) > 60:
        st.caption(f"…and {len(ranked) - 60} more.")


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
        "max_new_clusters": st.number_input("Max new clusters per week", 0, 20, int(p["max_new_clusters"])),
        "w_sonic": st.slider("Weight: sonic", 0.0, 1.0, float(p["w_sonic"]), 0.05),
        "w_tag": st.slider("Weight: tags", 0.0, 1.0, float(p["w_tag"]), 0.05),
        "w_artist": st.slider("Weight: artist", 0.0, 1.0, float(p["w_artist"]), 0.05),
    }
    if st.button("💾 Save settings", disabled=busy):
        cr.update_params(**new)
        st.success("Saved — applies from the next sync/curation.")
