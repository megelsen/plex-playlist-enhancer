"""Artist Mix tab: pick an artist, build a blended mix of their tracks,
sonic matches, and related-artist tracks, then save it as a playlist."""

import streamlit as st

from artist_mix import build_artist_mix
from ui_components import render_track_row


def render(plex, plex_url, plex_token, debug_box):
    st.title("🎨 Build an Artist Mix")
    st.caption("Pulls tracks from one artist, blends in sonically similar tracks, and rounds it out with related artists.")

    if 'artist_mix_result' not in st.session_state:
        st.session_state['artist_mix_result'] = []
    if 'artist_mix_selector_key' not in st.session_state:
        st.session_state['artist_mix_selector_key'] = 0
    if 'chosen_artist_name' not in st.session_state:
        st.session_state['chosen_artist_name'] = None

    try:
        mix_music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        mix_music_section = None
        st.error("No music library section found on this Plex server.")
        st.stop()

    # ttl=300 so the cached artist list automatically refetches every 5
    # minutes instead of staying stale forever, since section_key (the
    # library's key) never changes when artists are added.
    @st.cache_data(show_spinner=False, ttl=300)  # refetch every 5 minutes
    def _get_all_artist_names(_section, section_key):
        # Leading underscore on `_section` tells st.cache_data to skip
        # hashing the (unhashable) Plex object; `section_key` is the
        # actual cache key so results still refresh per-library.
        return sorted([a.title for a in _section.searchArtists()], key=str.lower)

    # Manual refresh button so the user can force an immediate refetch
    # (e.g. right after adding a new artist to Plex) instead of waiting on
    # the TTL above.
    header_col, refresh_col = st.columns([6, 1])
    with header_col:
        st.caption("Artist list refreshes automatically every few minutes.")
    with refresh_col:
        if st.button("🔄", key="refresh_artist_list", help="Refresh artist list now"):
            _get_all_artist_names.clear()
            st.rerun()

    with st.spinner("Loading artist library..."):
        artist_names = _get_all_artist_names(mix_music_section, mix_music_section.key)

    if not artist_names:
        st.warning("No artists found in this music library.")
        st.stop()

    # NATIVE SEARCHABLE DROP-DOWN — same pattern as the playlist selector:
    # one dialog, options filtered live as you type, and it clears itself
    # after each pick so you don't have to backspace the old name first.
    newly_selected_artist = st.selectbox(
        "Search and select an artist:",
        artist_names,
        index=None,
        placeholder="Type to search for an artist...",
        key=f"artist_search_{st.session_state['artist_mix_selector_key']}"
    )

    if newly_selected_artist is not None and newly_selected_artist != st.session_state['chosen_artist_name']:
        st.session_state['chosen_artist_name'] = newly_selected_artist
        st.session_state['artist_mix_result'] = []
        st.session_state['artist_mix_selector_key'] += 1
        st.rerun()

    selected_artist = None
    if st.session_state['chosen_artist_name']:
        matches = mix_music_section.searchArtists(title=st.session_state['chosen_artist_name'])
        selected_artist = next(
            (a for a in matches if a.title == st.session_state['chosen_artist_name']),
            matches[0] if matches else None
        )
        st.caption(f"Selected artist: **{st.session_state['chosen_artist_name']}**")

    with st.expander("⚙️ Mix settings"):
        max_total = st.number_input("Max songs total", min_value=1, max_value=200, value=30, key="mix_max_total")
        max_artist = st.number_input("Max songs from selected artist", min_value=0, max_value=100, value=10, key="mix_max_artist")
        max_related = st.number_input("Max songs per related artist", min_value=0, max_value=20, value=2, key="mix_max_related")
        max_sonic = st.number_input("Max sonically similar songs per seed", min_value=0, max_value=20, value=2, key="mix_max_sonic")

    if selected_artist:
        if st.button("🎛️ Build Mix"):
            with st.spinner(f"Building a mix for {selected_artist.title}..."):
                st.session_state['artist_mix_result'] = build_artist_mix(
                    selected_artist,
                    plex,
                    max_total=int(max_total),
                    max_artist=int(max_artist),
                    max_related=int(max_related),
                    max_sonic=int(max_sonic),
                    debug=debug_box
                )
            st.rerun()

    st.write("---")

    mix_list = st.session_state['artist_mix_result']
    if not mix_list:
        st.info("Search for an artist above and build a mix to see tracks here.")
    else:
        st.caption(f"{len(mix_list)} tracks — tap ✕ to drop one before saving.")
        for idx, track in enumerate(mix_list):
            render_track_row(track, idx, key_prefix="mix", mode="mix", plex_url=plex_url, plex_token=plex_token)

        st.write("---")
        default_name = f"{selected_artist.title} Mix" if selected_artist else "Artist Mix"
        playlist_name = st.text_input("New playlist name:", value=default_name, key="artist_mix_playlist_name")
        if st.button("💾 Save Mix as New Plex Playlist"):
            try:
                plex.createPlaylist(title=playlist_name, items=mix_list)
                st.success(f"Created playlist '{playlist_name}' with {len(mix_list)} tracks.")
            except Exception as e:
                st.error(f"Failed to create playlist: {e}")
