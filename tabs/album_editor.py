"""Placing albums in clusters by hand (🧬 Album clusters):

  - Unclustered → 🧩 Assign: every album with its best-fitting clusters
    (and why), pre-filled when the fit is clear; apply all edits at once,
    accept every suggestion above a fit, assign checked albums, make a new
    cluster from them, or mark them "never cluster".
  - Any other cluster → 💿 Albums: the same table with "Move to".

Albums placed by hand are pinned (weekly curation never moves them) and
survive commits (cluster_registry.assign_albums / _carry_manual_albums).
Table view for desktop, card view for phones; every change can be undone
for the rest of the session.
"""

import time

import pandas as pd
import streamlit as st

import cluster_registry as cr
from clustering import genre_first_names
from ui_components import render_track_row

TABLE_MAX_ROWS = 300
CARDS_PER_PAGE = 20
PLAYS_CACHE_SECONDS = 600
UNDO_KEEP = 20


# --- data -------------------------------------------------------------------

def _album_plays(plex):
    """{album key: plays} from one library-wide album listing, cached for 10 min."""
    cache = st.session_state.get("album_plays_cache")
    if cache and time.time() - cache["at"] < PLAYS_CACHE_SECONDS:
        return cache["plays"]
    try:
        section = cr.get_music_section(plex)
        plays = {str(a.ratingKey): getattr(a, "viewCount", 0) or 0 for a in section.albums()}
    except Exception:
        plays = {}
    st.session_state["album_plays_cache"] = {"at": time.time(), "plays": plays}
    return plays


def _suggestions(reg, cid, members):
    """Suggestions for a cluster's albums, cached per registry version."""
    key = (reg.get("revision"), cid)
    cache = st.session_state.setdefault("album_suggestions_cache", {})
    if key not in cache:
        cache.clear()
        cache[key] = cr.suggest_clusters(reg, members)
    return cache[key]


def _cluster_names(reg, exclude=()):
    """{name: cluster_id} for clusters you can place albums in."""
    return {c["name"]: k for k, c in reg["clusters"].items() if k not in exclude and k != cr.UNCLUSTERED_ID}


# --- writes + undo -------------------------------------------------------------

def _record_undo(label, previous, after):
    stack = st.session_state.setdefault("album_undo", [])
    stack.append({"label": label, "previous": previous, "after": after})
    del stack[:-UNDO_KEEP]


def _apply(label, func, album_keys, *args, **kwargs):
    """Runs a cluster_registry write with conflict protection and records undo."""
    reg = cr.load_registry()
    previous = cr.album_state(reg, album_keys)
    result = func(album_keys, *args, expected=previous, **kwargs)
    applied, skipped = (result[1], result[2]) if isinstance(result, tuple) and len(result) == 3 else result
    if applied:
        _record_undo(label, {a: previous[a] for a in applied}, cr.album_state(cr.load_registry(), applied))
    msg = f"{label}: {len(applied)} album(s)"
    if skipped:
        msg += f" — {len(skipped)} skipped (changed meanwhile, e.g. by a background job)"
    st.session_state["album_editor_msg"] = msg
    return result


def _assign_grouped(targets, names_to_id, label_prefix):
    """targets: {target name: [album keys]} -> one write per target."""
    for name, keys in targets.items():
        if name == cr.UNCLUSTERED_NAME:
            _apply(f"{label_prefix} → Unclustered", cr.unassign_albums, keys)
        else:
            _apply(f"{label_prefix} → {name}", cr.assign_albums, keys, names_to_id[name])


def render_undo(busy):
    """Last change's message + one session-wide undo button. Rendered once by
    the 🧬 panel (not per cluster table)."""
    stack = st.session_state.get("album_undo") or []
    msg = st.session_state.pop("album_editor_msg", None)
    if msg:
        st.success(msg)
    if stack and st.button(f"↩️ Undo: {stack[-1]['label']}", disabled=busy, key="album_undo_btn"):
        last = stack.pop()
        applied, skipped = cr.restore_album_states(last["previous"], last["after"])
        st.session_state["album_editor_msg"] = (f"Undone: {last['label']} ({len(applied)} album(s)"
                                                + (f", {len(skipped)} changed since — left as they are" if skipped else "")
                                                + ")")
        st.rerun()


# --- table ---------------------------------------------------------------------

def _rows(reg, members, feats, plays, sugg, group_by_artist, search, unclustered):
    rows = []
    for a in members:
        f = feats.get(a) or {}
        s = sugg.get(a) or []
        top = s[0] if s else None
        rows.append({
            "key": a,
            "artist": f.get("artist_title") or "?",
            "album": ("📌 " if reg["albums"][a].get("manual") else "") + (f.get("title") or a),
            "plays": plays.get(a, 0),
            "fit_here": reg["albums"][a].get("score"),
            "sugg_cid": top[0] if top else None,
            "sugg": reg["clusters"][top[0]]["name"] if top else "",
            "fit": top[1] if top else 0.0,
            "why": top[2] if top else "",
        })
    if search:
        q = search.lower()
        rows = [r for r in rows if q in r["artist"].lower() or q in r["album"].lower()]
    if not group_by_artist:
        return rows
    grouped = {}
    for r in rows:
        g = grouped.setdefault(r["artist"], {"keys": [], "albums": [], "plays": 0, "votes": {}})
        g["keys"].append(r["key"])
        g["albums"].append(r["album"])
        g["plays"] += r["plays"]
        if r["sugg"]:
            v = g["votes"].setdefault(r["sugg"], [0.0, 0, r["why"]])
            v[0] += r["fit"]
            v[1] += 1
    out = []
    for artist, g in grouped.items():
        best = max(g["votes"].items(), key=lambda kv: kv[1][0]) if g["votes"] else None
        out.append({
            "key": "|".join(g["keys"]), "artist": artist,
            "album": f"{len(g['albums'])} album(s): " + ", ".join(g["albums"][:3])
                     + ("…" if len(g["albums"]) > 3 else ""),
            "plays": g["plays"], "fit_here": None,
            "sugg": best[0] if best else "", "fit": round(best[1][0] / len(g["keys"]), 3) if best else 0.0,
            "why": best[1][2] if best else "",
        })
    return out


def _sort(rows, sort):
    if sort == "Fit":
        return sorted(rows, key=lambda r: -r["fit"])
    if sort == "Artist":
        return sorted(rows, key=lambda r: (r["artist"].lower(), r["album"].lower()))
    return sorted(rows, key=lambda r: -r["plays"])


def render(plex, plex_url, plex_token, reg, cid, feats, busy):
    unclustered = cid == cr.UNCLUSTERED_ID
    members = cr.cluster_album_keys(reg, cid)
    if unclustered:
        st.caption("Albums that don't fit any cluster well enough yet. Place them here — albums you place are "
                   "pinned, so weekly curation keeps them where you put them (📌), and they stay through commits.")
    if not members:
        st.info("No albums here right now.")
        if unclustered:
            _render_excluded(reg, feats, busy)
        return

    plays = _album_plays(plex)
    with st.spinner("Scoring albums against every cluster…"):
        sugg = _suggestions(reg, cid, members)
    names_to_id = _cluster_names(reg, exclude=(cid,))
    options = sorted(names_to_id) + ([] if unclustered else [cr.UNCLUSTERED_NAME])

    c1, c2 = st.columns(2)
    search = c1.text_input("Search artist or album", key=f"ae_search_{cid}")
    sort = c2.selectbox("Sort by", ["Plays", "Fit", "Artist"], key=f"ae_sort_{cid}")
    t1, t2 = st.columns(2)
    group_by_artist = t1.toggle("Group by artist", key=f"ae_group_{cid}",
                                help="One row per artist — its albums usually belong together.")
    cards = t2.toggle("📱 Card view", key=f"ae_cards_{cid}", help="Easier on a phone.")

    if cards:
        _render_cards(plex, plex_url, plex_token, reg, cid, members, feats, plays, sugg, names_to_id,
                      options, search, sort, busy)
    else:
        _render_table(plex, plex_url, plex_token, reg, cid, members, feats, plays, sugg, names_to_id,
                      options, search, sort, group_by_artist, busy)
    if unclustered:
        _render_excluded(reg, feats, busy)


def _render_table(plex, plex_url, plex_token, reg, cid, members, feats, plays, sugg, names_to_id, options,
                  search, sort, group_by_artist, busy):
    """Editable album table. Only TICKED rows (✓) are ever changed: the target
    column is pre-filled with the suggestion as a proposal, nothing more."""
    unclustered = cid == cr.UNCLUSTERED_ID
    prefill = 0.0
    tick_at = st.session_state.get(f"ae_tick_at_{cid}")  # set by "Tick suggestions with fit ≥ x"
    if unclustered:
        prefill = st.slider("Show the suggestion as the target when its fit is at least", 0.0, 1.0, 0.25, 0.05,
                            key=f"ae_prefill_{cid}",
                            help="Fit is the same 0–1 score curation uses. It only pre-fills 'Assign to' — "
                                 "nothing changes until you tick the row and press Apply.")
    rows = _sort(_rows(reg, members, feats, plays, sugg, group_by_artist, search, unclustered), sort)
    shown = rows[:TABLE_MAX_ROWS]
    target_col = "Assign to" if unclustered else "Move to"

    def _prefilled(r):
        return bool(unclustered and r["sugg"] and r["fit"] >= min(prefill, tick_at if tick_at is not None else 1.0))

    df = pd.DataFrame([{
        "✓": bool(tick_at is not None and r["sugg"] and r["fit"] >= tick_at),
        "Artist": r["artist"], "Album": r["album"], "Plays": r["plays"],
        **({} if unclustered else {"Fit here": r["fit_here"]}),
        "Suggested": r["sugg"], "Fit": r["fit"], "Why": r["why"],
        target_col: r["sugg"] if _prefilled(r) else None,
        "key": r["key"],
    } for r in shown])
    editor_key = (f"ae_table_{cid}_{reg.get('revision')}_{group_by_artist}_{sort}_{search}_{prefill}_{tick_at}")
    edited = st.data_editor(
        df, key=editor_key, hide_index=True, use_container_width=True,
        disabled=[c for c in df.columns if c not in ("✓", target_col)] if not busy else True,
        column_config={
            "✓": st.column_config.CheckboxColumn(width="small"),
            target_col: st.column_config.SelectboxColumn(options=options, width="medium"),
            "Fit": st.column_config.NumberColumn(format="%.2f", width="small"),
            "Fit here": st.column_config.NumberColumn(format="%.2f", width="small"),
            "Plays": st.column_config.NumberColumn(width="small"),
            "key": None,
        },
        column_order=["✓", "Artist", "Album", "Plays"] + ([] if unclustered else ["Fit here"])
                     + ["Suggested", "Fit", "Why", target_col],
    )
    if len(rows) > len(shown):
        st.caption(f"Showing {len(shown)} of {len(rows)} — search or sort to reach the rest.")

    def _keys(cell):
        return cell.split("|")

    targets = {}
    unticked_with_target = 0
    for _, row in edited.iterrows():
        target = row[target_col]
        if not (target and isinstance(target, str)):
            continue
        if row["✓"]:
            targets.setdefault(target, []).extend(_keys(row["key"]))
        else:
            unticked_with_target += 1
    checked = [k for _, row in edited.iterrows() if row["✓"] for k in _keys(row["key"])]

    n_targets = sum(len(v) for v in targets.values())
    if st.button(f"✅ Apply {n_targets} ticked change(s)", type="primary", disabled=busy or not n_targets,
                 use_container_width=True, key=f"ae_apply_{cid}"):
        _assign_grouped(targets, names_to_id, "Placed by hand")
        st.session_state.pop(f"ae_tick_at_{cid}", None)
        st.rerun()
    st.caption("Only ticked rows (✓) with a target are applied"
               + (f" — {unticked_with_target} unticked row(s) with a target are left as they are." if unticked_with_target else ".")
               + " Tick a row without a target to use the actions below.")

    if unclustered:
        c1, c2 = st.columns([3, 2])
        threshold = c1.slider("Tick every suggestion with fit at least", 0.0, 1.0, 0.35, 0.05,
                              key=f"ae_tick_threshold_{cid}")
        n_tick = sum(1 for r in rows if r["sugg"] and r["fit"] >= threshold)
        if c2.button(f"☑️ Tick {n_tick} row(s)", disabled=busy or not n_tick, use_container_width=True,
                     key=f"ae_tick_btn_{cid}", help="Only ticks them (with the suggestion as target) — review, "
                                                     "untick what's wrong, then press Apply."):
            st.session_state[f"ae_tick_at_{cid}"] = threshold
            st.rerun()
        if tick_at is not None and st.button("✕ Clear ticks", key=f"ae_tick_clear_{cid}"):
            st.session_state.pop(f"ae_tick_at_{cid}", None)
            st.rerun()

    _render_checked_actions(plex, plex_url, plex_token, reg, cid, checked, feats, names_to_id, options, busy)


def _render_checked_actions(plex, plex_url, plex_token, reg, cid, checked, feats, names_to_id, options, busy):
    unclustered = cid == cr.UNCLUSTERED_ID
    st.write(f"**With the {len(checked)} checked album(s):**" if checked else
             "*Tick ✓ in the table for more actions (move together, new cluster"
             + (", never cluster" if unclustered else "") + ", listen).*")
    if not checked:
        return

    c1, c2 = st.columns(2)
    target = c1.selectbox("Move checked to", options, key=f"ae_checked_target_{cid}")
    if c2.button("📦 Move checked", disabled=busy, use_container_width=True, key=f"ae_checked_move_{cid}"):
        _assign_grouped({target: checked}, names_to_id, "Moved by hand")
        st.rerun()

    with st.container(border=True):
        groups = cr._album_groups_for_naming(feats, {"new": checked})
        existing = [c["name"] for c in reg["clusters"].values()]
        suggested = genre_first_names(groups, used_names=existing)["new"]
        name = st.text_input("➕ New cluster from the checked albums — name", value=suggested,
                             key=f"ae_new_name_{cid}_{len(checked)}")
        lock = st.checkbox("Lock it", value=True, key=f"ae_new_lock_{cid}",
                           help="Locked: curation won't retire it for being small, merge it away under the cluster "
                                "cap, or move its albums. It also survives commits.")
        cap = reg["params"].get("max_clusters") or 0
        n_clusters = sum(1 for k, c in reg["clusters"].items() if k != cr.UNCLUSTERED_ID)
        if cap and n_clusters >= cap and not lock:
            st.warning(f"You're at the cluster cap ({n_clusters}/{cap}): an unlocked new cluster will be merged into "
                       "its most similar one at the next curation. Lock it, or raise 'Max clusters'.")
        if len(checked) < reg["params"]["min_cluster_albums"] and not lock:
            st.warning(f"Fewer than {reg['params']['min_cluster_albums']} albums: an unlocked cluster this small is "
                       "retired at the next curation.")
        if st.button("➕ Create cluster", disabled=busy or not name.strip(), key=f"ae_new_btn_{cid}"):
            new_cid, applied, skipped = _apply(f"New cluster '{name.strip()}'", cr.create_cluster_from_albums,
                                               checked, name.strip(), locked=lock)
            st.rerun()

    b1, b2 = st.columns(2)
    if unclustered:
        if b1.button("🚫 Never cluster these", disabled=busy, use_container_width=True, key=f"ae_exclude_{cid}",
                     help="For audiobooks, soundtracks, comedy…: kept out of clusters, curation and mixes."):
            _apply("Never cluster", cr.exclude_albums, checked)
            st.rerun()
    else:
        pinned = [a for a in checked if reg["albums"][a].get("pinned")]
        if b1.button(f"📌 Unpin {len(pinned)}", disabled=busy or not pinned, use_container_width=True,
                     key=f"ae_unpin_{cid}", help="Curation may move them again."):
            cr.set_albums_pinned(pinned, False)
            st.session_state["album_editor_msg"] = f"Unpinned {len(pinned)} album(s)"
            st.rerun()
    if b2.button("🎧 Listen to the first checked", use_container_width=True, key=f"ae_listen_{cid}"):
        st.session_state[f"ae_listen_album_{cid}"] = checked[0]
    album_key = st.session_state.get(f"ae_listen_album_{cid}")
    if album_key in checked:
        _render_listen(plex, plex_url, plex_token, album_key, cid)


def _render_listen(plex, plex_url, plex_token, album_key, cid):
    try:
        tracks = plex.fetchItem(int(album_key)).tracks()
    except Exception as e:
        st.error(f"Couldn't load the album: {e}")
        return
    top = sorted(tracks, key=lambda t: getattr(t, "viewCount", 0) or 0, reverse=True)[:3]
    for idx, track in enumerate(top):
        render_track_row(track, idx, key_prefix=f"ae_listen_{cid}", mode="view",
                         plex_url=plex_url, plex_token=plex_token)


# --- cards (phones) -----------------------------------------------------------------

def _render_cards(plex, plex_url, plex_token, reg, cid, members, feats, plays, sugg, names_to_id, options,
                  search, sort, busy):
    unclustered = cid == cr.UNCLUSTERED_ID
    rows = _sort(_rows(reg, members, feats, plays, sugg, False, search, unclustered), sort)
    pages = max(1, -(-len(rows) // CARDS_PER_PAGE))
    page = st.number_input(f"Page (of {pages})", 1, pages, 1, key=f"ae_page_{cid}") if pages > 1 else 1
    for r in rows[(page - 1) * CARDS_PER_PAGE: page * CARDS_PER_PAGE]:
        a = r["key"]
        with st.container(border=True):
            st.markdown(f"**{r['artist']}** — {r['album']}  \n{r['plays']} plays"
                        + (f" · fit here {r['fit_here']:.2f}" if r["fit_here"] is not None else ""))
            for i, (scid, score, why) in enumerate(sugg.get(a) or []):
                name = reg["clusters"][scid]["name"]
                if st.button(f"→ {name}  ·  {score:.2f}", key=f"ae_card_{cid}_{a}_{i}", help=why or None,
                             disabled=busy, use_container_width=True):
                    _assign_grouped({name: [a]}, names_to_id, "Placed by hand")
                    st.rerun()
            other = st.selectbox("Something else", [""] + options, key=f"ae_card_other_{cid}_{a}")
            if other and st.button(f"→ {other}", key=f"ae_card_other_btn_{cid}_{a}", disabled=busy,
                                   use_container_width=True):
                _assign_grouped({other: [a]}, names_to_id, "Placed by hand")
                st.rerun()
            if unclustered and st.button("🚫 Never cluster", key=f"ae_card_excl_{cid}_{a}", disabled=busy):
                _apply("Never cluster", cr.exclude_albums, [a])
                st.rerun()


def _render_excluded(reg, feats, busy):
    excluded = cr.excluded_album_keys(reg)
    if not excluded:
        return
    with st.expander(f"🚫 Never clustered ({len(excluded)})"):
        back = st.multiselect("Include again", excluded, format_func=lambda a: (
            f"{(feats.get(a) or {}).get('artist_title') or '?'} — {(feats.get(a) or {}).get('title') or a}"),
            key="ae_excluded_back")
        if st.button("↩️ Include again", disabled=busy or not back, key="ae_excluded_back_btn"):
            _apply("Include again", cr.exclude_albums, back, False)
            st.rerun()
