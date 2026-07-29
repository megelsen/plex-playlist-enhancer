"""Playlist Enhancer tab: pick a playlist, get sonic/related-artist
recommendations, add them in with one tap."""

import streamlit as st

from recommendations import generate_playlist_vibe_recommendations
from ui_components import render_track_row


def render(plex, plex_url, plex_token, debug_box):
    st.title("🎧 Recommended for this Playlist")
    st.caption("A streamlined recommendation drawer fueled by your server's Sonic Analysis and Plexamp Related Artist mappings.")

    all_playlists = [pl for pl in plex.playlists() if pl.playlistType == "audio"]
    playlist_names = [pl.title for pl in all_playlists]

    if not playlist_names:
        st.warning("No audio playlists found on your Plex server.")
        st.stop()

    # NATIVE SEARCHABLE DROP-DOWN (clears itself after each pick so you don't
    # have to backspace the old name before typing a new search)
    if 'playlist_selector_key' not in st.session_state:
        st.session_state['playlist_selector_key'] = 0
    if 'chosen_playlist_name' not in st.session_state:
        st.session_state['chosen_playlist_name'] = playlist_names[0]

    newly_selected = st.selectbox(
        "Search and select a playlist to enhance:",
        playlist_names,
        index=None,
        placeholder="Type to search for a playlist...",
        key=f"playlist_search_{st.session_state['playlist_selector_key']}"
    )

    if newly_selected is not None and newly_selected != st.session_state['chosen_playlist_name']:
        st.session_state['chosen_playlist_name'] = newly_selected
        st.session_state['recommendations'] = []
        st.session_state['last_loaded_playlist'] = newly_selected
        # Bump the key so the widget remounts blank on the next run instead of
        # keeping the just-picked name in the search field.
        st.session_state['playlist_selector_key'] += 1
        st.rerun()

    selected_pl_name = st.session_state['chosen_playlist_name']
    st.caption(f"Currently enhancing: **{selected_pl_name}**")
    current_playlist = next(pl for pl in all_playlists if pl.title == selected_pl_name)

    if not st.session_state['recommendations']:
        with st.spinner("Analyzing playlist vibe..."):
            st.session_state['recommendations'] = generate_playlist_vibe_recommendations(
                current_playlist, plex, debug_box, count=10
            )

    if st.button("🔄 Refresh Recommendations"):
        with st.spinner("Fetching fresh music..."):
            st.session_state['recommendations'] = generate_playlist_vibe_recommendations(
                current_playlist, plex, debug_box, count=10
            )
        st.rerun()

    st.write("---")

    rec_list = st.session_state['recommendations']
    if not rec_list:
        st.info("No recommendations found matching this playlist vibe. Try adding more tracks.")
    else:
        for idx, track in enumerate(rec_list):
            render_track_row(
                track, idx, key_prefix="enhance", mode="enhance",
                plex_url=plex_url, plex_token=plex_token, current_playlist=current_playlist
            )
