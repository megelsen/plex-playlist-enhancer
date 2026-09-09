"""Live Playlists tab: ONE overview of every active live mix (artist +
cluster), plus a way to spin up a new Live Cluster Mix without leaving this
tab. There used to be two separate ways to create a live cluster mix (a
thin form here, and the full-featured flow in Library Clusters) — this tab
now reuses the exact same options and the exact same
live_playlists.save_cluster_as_live_mix() call that Library Clusters uses,
so there is exactly one save path, just reachable from two screens.

Live Artist Mixes are still only created from the 🎨 Artist Mix tab (build
the mix, then check "Keep this updated automatically"), since building one
here from scratch would mean duplicating that whole builder UI for no
benefit — but once saved, it appears in the unified list below immediately,
same as a cluster mix does.
"""

import streamlit as st

import live_playlists as lp
from clustering import load_cluster_results_cache


def render(plex, plex_url, plex_token, debug_box):
    st.title("🔴 Live Playlists")

    next_run = lp.get_next_run_time()
    next_run_str = next_run.strftime("%Y-%m-%d %H:%M %Z") if next_run else "not started yet"
    last_heartbeat = lp.get_last_scheduler_heartbeat()
    heartbeat_str = last_heartbeat.strftime("%Y-%m-%d %H:%M") if last_heartbeat else "never"
    st.caption(
        f"Live Mixes refresh automatically once a day (scheduled for "
        f"{lp.LIVE_PLAYLISTS_HOUR:02d}:{lp.LIVE_PLAYLISTS_MINUTE:02d} server time — "
        f"next run: {next_run_str}, last scheduled run: {heartbeat_str}). To create a new "
        f"Live Artist Mix, build one in 🎨 Artist Mix and check 'Keep this updated "
        f"automatically'. To create a new Live Cluster Mix, use the section below."
    )

    if st.button("▶️ Run ALL live playlists now", help="Manually trigger every enabled subscription immediately."):
        with st.spinner("Running all live playlists..."):
            try:
                summary = lp.run_all_subscriptions(plex_url, plex_token, debug=debug_box)
                for name, count, err in summary["artist_mixes"] + summary["cluster_mixes"]:
                    if err:
                        st.error(f"{name}: {err}")
                    else:
                        st.success(f"{name}: {count} tracks")
            except Exception as e:
                st.error(f"Run failed: {e}")

    st.write("---")
    st.subheader("Active Live Mixes")
    _render_unified_overview(plex_url, plex_token, debug_box)

    st.write("---")
    st.subheader("🗂️ Save a Cluster as a Live Mix")
    st.caption(
        "All clusters from your last saved build in 🗂️ Library Clusters — same options as "
        "saving from that tab. Saving here shows up in the overview above immediately."
    )
    _render_cluster_save_section(plex, plex_url, plex_token, debug_box)


def _render_unified_overview(plex_url, plex_token, debug_box):
    subs = lp.list_all_subscriptions()
    if not subs:
        st.info(
            "No live mixes yet. Build one in 🎨 Artist Mix (check 'Keep this updated "
            "automatically') or save a cluster below."
        )
        return

    if st.button("🔄 Sync all now", key="sync_all_unified"):
        with st.spinner("Syncing all live mixes..."):
            try:
                summary = lp.run_all_subscriptions(plex_url, plex_token, debug=debug_box)
                for name, count, err in summary["artist_mixes"] + summary["cluster_mixes"]:
                    if err:
                        st.error(f"{name}: {err}")
                    else:
                        st.success(f"{name}: {count} tracks")
            except Exception as e:
                st.error(f"Sync failed: {e}")
        st.rerun()

    for sub in subs:
        is_artist = sub["kind"] == "artist_mix"
        source_label = sub["artist_name"] if is_artist else sub["cluster_name"]
        source_icon = "🎨" if is_artist else "🗂️"

        with st.container(border=True):
            st.write(f"{source_icon} **{source_label}** → *{sub['playlist_name']}*")
            status_bits = []
            if not is_artist:
                status_bits.append(
                    f"{round(sub.get('cluster_ratio', 0.67) * 100)}% cluster / "
                    f"{round((1 - sub.get('cluster_ratio', 0.67)) * 100)}% sonic"
                )
                status_bits.append(f"rotation {sub.get('rotation_percent', 30)}%")
                status_bits.append(f"target {sub.get('target_size', 30)} tracks")
            if sub.get("last_run"):
                status_bits.append(f"last run {sub['last_run']}")
            if sub.get("last_track_count") is not None:
                status_bits.append(f"{sub['last_track_count']} tracks")
            if sub.get("last_error"):
                status_bits.append(f"⚠️ {sub['last_error']}")
            st.caption(" · ".join(status_bits) if status_bits else "Not run yet.")

            ctrl1, ctrl2, ctrl3 = st.columns(3)
            with ctrl1:
                enabled = st.toggle("On", value=sub.get("enabled", True), key=f"toggle_{sub['kind']}_{sub['id']}")
                if enabled != sub.get("enabled", True):
                    lp.set_subscription_enabled(sub["kind"], sub["id"], enabled)
                    st.rerun()
            with ctrl2:
                if st.button("▶️ Run", key=f"run_{sub['kind']}_{sub['id']}", use_container_width=True):
                    with st.spinner(f"Rebuilding {source_label}..."):
                        try:
                            count = lp.run_single_subscription(sub["kind"], sub["id"], plex_url, plex_token, debug=debug_box)
                            st.success(f"{source_label}: {count} tracks")
                        except Exception as e:
                            st.error(str(e))
                    st.rerun()
            with ctrl3:
                if st.button("🗑️ Remove", key=f"remove_{sub['kind']}_{sub['id']}", use_container_width=True):
                    lp.remove_subscription(sub["kind"], sub["id"])
                    st.rerun()

            with st.expander("⚙️ Edit configuration"):
                st.caption("Changes apply on the next run (scheduled or manual) — they don't rebuild the playlist immediately.")
                if is_artist:
                    e_max_total = st.number_input(
                        "Max songs total", min_value=1, max_value=200,
                        value=sub.get("max_total", 30), key=f"edit_total_{sub['id']}"
                    )
                    e_max_artist = st.number_input(
                        "Max songs from the artist", min_value=0, max_value=100,
                        value=sub.get("max_artist", 10), key=f"edit_artist_{sub['id']}"
                    )
                    e_max_related = st.number_input(
                        "Max songs per related artist", min_value=0, max_value=20,
                        value=sub.get("max_related", 2), key=f"edit_related_{sub['id']}"
                    )
                    e_max_sonic = st.number_input(
                        "Max sonic matches per seed", min_value=0, max_value=20,
                        value=sub.get("max_sonic", 2), key=f"edit_sonic_{sub['id']}"
                    )
                    if st.button("💾 Save changes", key=f"edit_save_{sub['id']}", use_container_width=True):
                        lp.update_subscription_settings(
                            sub["kind"], sub["id"],
                            max_total=int(e_max_total), max_artist=int(e_max_artist),
                            max_related=int(e_max_related), max_sonic=int(e_max_sonic),
                        )
                        st.success("Saved — will apply on the next run.")
                        st.rerun()
                else:
                    e_target_size = st.number_input(
                        "Target size", min_value=5, max_value=200,
                        value=sub.get("target_size", 30), key=f"edit_target_{sub['id']}"
                    )
                    e_cluster_pct = st.slider(
                        "Cluster-seeded %", min_value=10, max_value=100,
                        value=round(sub.get("cluster_ratio", 0.67) * 100), key=f"edit_pct_{sub['id']}"
                    )
                    e_sonic_seeds = st.number_input(
                        "Sonic expansion seed songs", min_value=1, max_value=30,
                        value=sub.get("sonic_seed_count", 10), key=f"edit_seeds_{sub['id']}"
                    )
                    e_rotation_pct = st.slider(
                        "Daily rotation %", min_value=5, max_value=100, step=5,
                        value=sub.get("rotation_percent", 30), key=f"edit_rot_{sub['id']}"
                    )
                    if st.button("💾 Save changes", key=f"edit_save_{sub['id']}", use_container_width=True):
                        lp.update_subscription_settings(
                            sub["kind"], sub["id"],
                            target_size=int(e_target_size), rotation_percent=int(e_rotation_pct),
                            cluster_ratio=e_cluster_pct / 100, sonic_seed_count=int(e_sonic_seeds),
                        )
                        st.success("Saved — will apply on the next run.")
                        st.rerun()


def _render_cluster_save_section(plex, plex_url, plex_token, debug_box):
    cluster_names, existing_live_names = _get_available_clusters(plex, debug_box)
    if not cluster_names:
        st.warning(
            "No saved cluster build found — open 🗂️ Library Clusters, build clusters, "
            "and save at least one first."
        )
        return

    for cluster_name in cluster_names:
        already_live = cluster_name in existing_live_names
        with st.expander(f"{'🎧 ' if already_live else ''}{cluster_name}", expanded=False):
            if already_live:
                st.success("✅ Already a Live Mix — saving again below updates its settings and re-runs it now.")

            live_target_size = st.number_input(
                "Target size", min_value=5, max_value=200, value=30,
                key=f"live_overview_target_{cluster_name}"
            )
            live_cluster_pct = st.slider(
                "Cluster-seeded %", min_value=10, max_value=100, value=67,
                key=f"live_overview_pct_{cluster_name}"
            )
            live_sonic_seeds = st.number_input(
                "Sonic expansion seed songs", min_value=1, max_value=30, value=10,
                key=f"live_overview_seeds_{cluster_name}"
            )
            live_rotation_pct = st.slider(
                "Daily rotation %", min_value=5, max_value=100, value=85, step=5,
                key=f"live_overview_rot_{cluster_name}"
            )

            if st.button("🎧 Save as Live Mix", key=f"live_overview_save_{cluster_name}", use_container_width=True):
                live_name = f"[Mix] {cluster_name}"
                with st.spinner(f"Saving '{live_name}'..."):
                    try:
                        # Same call clusters_tab.py makes — one save path.
                        lp.save_cluster_as_live_mix(
                            plex, plex_url, plex_token, cluster_name, live_name,
                            target_size=live_target_size, rotation_percent=live_rotation_pct,
                            cluster_ratio=live_cluster_pct / 100, sonic_seed_count=live_sonic_seeds,
                            debug=debug_box,
                        )
                        st.success(f"'{live_name}' saved and subscribed — it'll refresh automatically every day.")
                    except Exception as e:
                        st.error(f"Failed to set up live mix: {e}")
                st.rerun()


def _get_available_clusters(plex, debug_box):
    """Prefer the current session's cluster_results (freshest, includes
    unsaved merges), falling back to whatever's saved on disk so this tab
    works even if Library Clusters hasn't been visited this session.
    Also returns the set of cluster names that already have a live mix, so
    the expander above can flag them."""
    session_results = st.session_state.get("cluster_results")
    if session_results:
        names = sorted(session_results.keys())
    else:
        results, _tag_mapping, _saved = load_cluster_results_cache(plex, debug=debug_box)
        names = sorted(results.keys()) if results else []

    existing_live_names = {s["cluster_name"] for s in lp.list_cluster_mix_subscriptions()}
    return names, existing_live_names
