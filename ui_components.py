"""Shared Streamlit rendering components used across all tabs."""

import streamlit as st

from plex_helpers import get_stream_url


def render_track_row(track, idx, key_prefix, mode, plex_url, plex_token, current_playlist=None, cluster_name=None):
    """
    Shared row renderer used by the Playlist Enhancer, Artist Mix, and
    Library Clusters tabs: cover + play/pause + action button on one line,
    full-width track info below, inline audio player if this track is the
    one playing.

    mode='enhance': action button adds the track to `current_playlist` and
        removes it from st.session_state['recommendations'].
    mode='mix': action button removes the track from
        st.session_state['artist_mix_result'] (no Plex write — just curating
        the in-progress mix before saving it).
    mode='cluster': action button records the track's ratingKey in
        st.session_state['cluster_removed_keys'][cluster_name] (requires
        cluster_name) instead of popping it from a results list directly —
        the Library Clusters tab recomputes its merged view fresh from the
        raw fine-grained build plus the current merge plan on every run, so
        a direct pop would get silently undone; tracking removed keys
        separately lets them persist across merge-plan changes too.
    mode='view': read-only row — no destructive action, action_col stays
        empty so layout still lines up with play_col.
    """
    thumb_path = getattr(track, 'parentThumb', None) if getattr(track, 'parentThumb', None) else getattr(track, 'thumb', None)
    artwork_url = f"{plex_url.rstrip('/')}{thumb_path}?X-Plex-Token={plex_token}" if thumb_path else "https://unsplash.com"

    rec_type = getattr(track, 'recommendation_type', 'Vibe Match')
    match_pct = getattr(track, 'match_percent', None)
    match_seed = getattr(track, 'match_seed', None)
    if rec_type == 'Sonic Match' and match_pct is not None and match_seed:
        rec_type_display = f"{rec_type} ({match_seed}: {match_pct}%)"
    elif rec_type == 'Sonic Match' and match_pct is not None:
        rec_type_display = f"{rec_type} ({match_pct}%)"
    else:
        rec_type_display = rec_type
    artist_title = getattr(track, 'grandparentTitle', 'Unknown Artist')
    album_title = getattr(track, 'parentTitle', 'Unknown Album')

    # Single flat row of sibling columns (no nesting) — artwork is
    # embedded as inline HTML inside the text column rather than given
    # its own st.columns slot, so there are only 3 columns total for
    # Streamlit's flex engine to size instead of 4, and their widths
    # (from the ratio below) always sum to exactly 100% of the row.
    content_col, play_col, action_col = st.columns([8, 1, 1])

    with content_col:
        st.markdown(
            f"""<div class="track-row-text">
                <img src="{artwork_url}" width="40" height="40" />
                <span><strong>{track.title}</strong> — {artist_title} · <em>{album_title}</em> · <code>{rec_type_display}</code></span>
            </div>""",
            unsafe_allow_html=True
        )

    with play_col:
        is_playing = st.session_state['now_playing_key'] == track.ratingKey
        # \uFE0E forces "text presentation" on these glyphs instead of the
        # platform's colored emoji rendering, so they pick up the grey CSS
        # color like the rest of the icons.
        icon = "\u23F8\uFE0E" if is_playing else "\u25B6\uFE0E"  # ⏸︎ / ▶︎
        if st.button(icon, key=f"play_{key_prefix}_{track.ratingKey}_{idx}"):
            st.session_state['now_playing_key'] = None if is_playing else track.ratingKey
            st.rerun()

    with action_col:
        if mode == 'enhance':
            if st.button("\uFF0B", key=f"add_{key_prefix}_{track.ratingKey}_{idx}"):  # ＋
                current_playlist.addItems([track])
                st.session_state['recommendations'].pop(idx)
                st.toast(f"Added: {track.title}")
                st.rerun()
        elif mode == 'mix':
            if st.button("\u2715\uFE0E", key=f"remove_{key_prefix}_{track.ratingKey}_{idx}"):  # ✕︎
                st.session_state['artist_mix_result'].pop(idx)
                st.toast(f"Removed: {track.title}")
                st.rerun()
        elif mode == 'cluster':
            if st.button("\u2715\uFE0E", key=f"remove_{key_prefix}_{track.ratingKey}_{idx}"):  # ✕︎
                st.session_state.setdefault('cluster_removed_keys', {}).setdefault(cluster_name, set()).add(track.ratingKey)
                st.toast(f"Removed: {track.title}")
                st.rerun()
        elif mode == 'view':
            pass

    if st.session_state['now_playing_key'] == track.ratingKey:
        stream_url = get_stream_url(track, plex_url, plex_token)
        if stream_url:
            st.audio(stream_url, autoplay=True)
        else:
            st.warning("No playable audio found for this track.")

    st.markdown("<hr style='margin:2px 0; opacity:0.15;'>", unsafe_allow_html=True)
