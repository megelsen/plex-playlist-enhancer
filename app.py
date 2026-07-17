import streamlit as st
from plexapi.server import PlexServer
import random
import os
from collections import defaultdict

st.set_page_config(layout="wide", page_title="Smart Playlist Enhancer")

# --- CLEAN BUTTON COMPACTING ---
st.markdown("""
    <style>
    /* Target buttons via multiple selector strategies since Streamlit's
       internal data-testid names AND auto-generated emotion-cache class
       names shift between versions/reruns — belt and suspenders so this
       doesn't silently stop matching again. The attribute selectors below
       catch any button carrying a class starting with "st-emotion-cache",
       which is what actually supplies the blue fill + border seen before. */
    .stButton > button,
    div[data-testid="stButton"] button,
    div[data-testid="stColumn"] button,
    button[kind="secondary"],
    button[kind="secondaryFormSubmit"],
    button[class*="st-emotion-cache"],
    div.stButton > button[class*="st-emotion-cache"] {
        background-color: transparent !important;
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: #888 !important;
        font-size: 1.9rem !important;
        padding: 4px 14px !important;
        width: auto !important;
        min-height: 2.6rem !important;
        min-width: 2.6rem !important;
        line-height: 1 !important;
    }
    /* Cover + title/artist/source block. The wrapping div is ours, so
       track-card-source's inline-vs-block behavior is fully controlled by
       our own CSS below — no dependency on Streamlit's internal DOM. */
    .track-card-top {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        margin-bottom: 0.15rem;
    }
    .track-card-top img {
        border-radius: 4px;
        flex-shrink: 0;
    }
    .track-card-text {
        min-width: 0;
    }
    .track-card-title {
        font-size: 0.95rem;
        line-height: 1.3;
    }
    .track-card-source {
        color: #888 !important;
        font-size: 0.8rem !important;
    }
    /* Desktop (default): source stays on the same line as the title,
       exactly like the very first version of this layout. */
    .track-card-source {
        display: inline;
    }
    /* Mobile: source drops to its own line under cover+title. */
    @media (max-width: 640px) {
        .track-card-source {
            display: block;
            margin-top: 0.1rem;
        }
    }
    /* Play/add buttons live in a column nested inside another column
       (text column vs. controls column). Streamlit auto-stacks columns
       when their container gets narrow — that's exactly what we want for
       the OUTER text-vs-controls split, but NOT for the inner play/add
       pair, which must always sit side by side. Targeting "a horizontal
       block nested inside a column" only ever matches that inner pair
       (the outer split isn't itself inside a column), so this is safe
       regardless of Streamlit version and doesn't rely on any
       container(key=...) generated class name. */
    div[data-testid="stColumn"] div[data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap !important;
        gap: 0.3rem !important;
    }
    div[data-testid="stColumn"] div[data-testid="stColumn"] {
        width: auto !important;
        min-width: 0 !important;
        flex: 0 0 auto !important;
    }
    .stButton > button:hover,
    div[data-testid="stButton"] button:hover,
    div[data-testid="stColumn"] button:hover,
    button[kind="secondary"]:hover,
    button[class*="st-emotion-cache"]:hover {
        color: #ddd !important;
        background-color: rgba(128, 128, 128, 0.15) !important;
        border: none !important;
    }
    .stButton > button:focus,
    .stButton > button:active,
    div[data-testid="stButton"] button:focus,
    div[data-testid="stButton"] button:active,
    button[kind="secondary"]:focus,
    button[kind="secondary"]:active,
    button[class*="st-emotion-cache"]:focus,
    button[class*="st-emotion-cache"]:active {
        outline: none !important;
        box-shadow: none !important;
        border: none !important;
        background-color: rgba(128, 128, 128, 0.2) !important;
        color: #ddd !important;
    }
    div[data-testid="stHorizontalBlock"] {
        align-items: center !important;
        gap: 0.4rem !important;
    }
    div[data-testid="stVerticalBlock"] > div {
        gap: 0.25rem !important;
    }
    div[data-testid="stMarkdownContainer"] p {
        margin-bottom: 0px !important;
        line-height: 1.3 !important;
    }
    </style>
""", unsafe_allow_html=True)

# --- SIDEBAR & CONNECTION ---
st.sidebar.title("Plex Connection")
DEFAULT_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
DEFAULT_TOKEN = os.environ.get("PLEX_TOKEN", "")

PLEX_URL = st.sidebar.text_input("Plex URL", value=DEFAULT_URL)
PLEX_TOKEN = st.sidebar.text_input("Plex Token", value=DEFAULT_TOKEN, type="password")

@st.cache_resource
def get_plex_connection(url, token):
    if not token: return None
    try: return PlexServer(url, token)
    except Exception as e:
        st.sidebar.error(f"Connection failed: {e}")
        return None

plex = get_plex_connection(PLEX_URL, PLEX_TOKEN)

if not plex:
    st.info("👈 Please enter your Plex URL and Token in the left sidebar to connect.")
    st.stop()

# Live API Monitor in the Streamlit Sidebar
st.sidebar.write("---")
st.sidebar.title("🪲 Live API Debugger")
debug_box = st.sidebar.container()

if 'recommendations' not in st.session_state:
    st.session_state['recommendations'] = []
if 'last_loaded_playlist' not in st.session_state:
    st.session_state['last_loaded_playlist'] = ""


def get_stream_url(track):
    """
    Builds a direct, authenticated HTTP URL to the track's underlying media
    file part. We deliberately use the raw part path (/library/parts/...)
    rather than track.getStreamURL(), because getStreamURL() spins up a
    Plex "universal transcode" session that expects the client to keep
    polling it — a plain <audio> tag doesn't do that, so Plex closes the
    connection partway through and the browser throws
    ERR_INCOMPLETE_CHUNKED_ENCODING. Hitting the file part directly streams
    the original file with normal HTTP range support instead.
    Returns None if the track has no accessible media part.
    """
    try:
        part = track.media[0].parts[0]
        return f"{PLEX_URL.rstrip('/')}{part.key}?X-Plex-Token={PLEX_TOKEN}"
    except (AttributeError, IndexError, KeyError):
        return None


def get_sonic_match_percent(track):
    """
    plexapi doesn't expose the sonic-similarity score as a named attribute,
    but Plex's hub response includes it in the raw element data (varies by
    server version: 'score' or 'distance'). Score is already a 0-1 similarity
    value; distance is the inverse (0 = identical), so we normalize both to
    a 0-100 "match %". Returns None if neither field is present so callers
    can gracefully omit the percentage rather than show a fake number.
    """
    raw = getattr(track, '_data', None)
    if raw is None:
        return None
    try:
        attrib = raw.attrib if hasattr(raw, 'attrib') else raw
        score = attrib.get('score')
        if score is not None:
            score = float(score)
            # Some servers report 0-1, others 0-100 — normalize either way.
            pct = score * 100 if score <= 1 else score
            return max(0, min(100, round(pct)))
        distance = attrib.get('distance')
        if distance is not None:
            distance = float(distance)
            pct = (1 - distance) * 100 if distance <= 1 else (100 - distance)
            return max(0, min(100, round(pct)))
    except (TypeError, ValueError):
        return None
    return None


def get_top_tracks_for_artist(real_artist, limit=4, per_album_sample=2):
    """
    plexapi's Artist has no topTracks() method, so we approximate "top tracks"
    ourselves:
      1. Walk the artist's albums and pull tracks from each.
      2. Rank all collected tracks by viewCount (play count) descending, since
         that's the only popularity signal Plex actually exposes.
      3. If nothing has been played (all viewCount == 0, common on fresh
         libraries), fall back to a random sample of `per_album_sample`
         tracks per album instead of just taking the first N in album order.
    """
    candidate_tracks = []
    try:
        albums = real_artist.albums()
    except Exception:
        albums = []

    for album in albums:
        try:
            album_tracks = album.tracks()
        except Exception:
            continue
        if not album_tracks:
            continue
        # Take a random sample per album (not just the first N) so the
        # fallback pool isn't biased toward track-1-of-every-album.
        sample = random.sample(album_tracks, min(len(album_tracks), per_album_sample))
        candidate_tracks.extend(sample)

    if not candidate_tracks:
        return []

    ranked = sorted(
        candidate_tracks,
        key=lambda t: getattr(t, 'viewCount', 0) or 0,
        reverse=True
    )

    top_view_count = getattr(ranked[0], 'viewCount', 0) or 0

    if top_view_count > 0:
        # Real play-count signal exists — use it.
        return ranked[:limit]
    else:
        # No play data at all — a viewCount-based ranking would be meaningless
        # (everything ties at 0), so just shuffle and take a random slice.
        random.shuffle(candidate_tracks)
        return candidate_tracks[:limit]


def generate_playlist_vibe_recommendations(playlist, count=10):
    tracks = playlist.items()
    if not tracks: return []
    
    existing_keys = {t.ratingKey for t in tracks}
    raw_pool = []
    
    valid_tracks = [t for t in tracks if getattr(t, 'ratingKey', None) is not None]
    if not valid_tracks: return []
    
    # Pick seed tracks
    seeds = random.sample(valid_tracks, min(len(valid_tracks), 6))
    debug_box.write(f"**Selected Seeds:** {len(seeds)} tracks")
    
    for seed in seeds:
        seed_name = f"{getattr(seed, 'grandparentTitle', 'Unknown')} - {seed.title}"
        debug_box.markdown(f"**Seed:** `{seed_name}`")
            
        # 1. NATIVE SONIC ENGINE
        try:
            sonic_matches = seed.sonicallySimilar(limit=15)
            if sonic_matches:
                debug_box.write(f"└ ✅ Sonic Engine found {len(sonic_matches)} tracks.")
                for match in sonic_matches:
                    if getattr(match, 'ratingKey', None) and match.ratingKey not in existing_keys:
                        setattr(match, 'recommendation_type', 'Sonic Match')
                        setattr(match, 'match_percent', get_sonic_match_percent(match))
                        setattr(match, 'match_seed', seed_name)
                        raw_pool.append(match)
        except Exception as e:
            debug_box.write(f"└ ❌ Sonic Call Failed: `{str(e)}`")

         # 2. FIXED PLEXAMP CLONE LOGIC (Using .tag instead of .title)
        try:
            artist_key = getattr(seed, 'grandparentRatingKey', None)
            if artist_key:
                artist = plex.fetchItem(artist_key)
                
                if hasattr(artist, 'similar'):
                    similar_artists = artist.similar() if callable(artist.similar) else artist.similar
                else:
                    similar_artists = []
                
                if similar_artists:
                    debug_box.write(f"└ ✅ Found {len(similar_artists)} Similar Artists.")
                    chosen_artists = random.sample(similar_artists, min(len(similar_artists), 3))
                    
                    for sim_artist in chosen_artists:
                        try:
                            # Pull the correct string identifier name (.tag) from the Similar metadata object
                            artist_name = getattr(sim_artist, 'tag', None)
                            if artist_name:
                                music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
                                full_artist_matches = music_section.searchArtists(title=artist_name)
                                
                                if full_artist_matches:
                                    real_artist = full_artist_matches[0]
                                    top_tracks = get_top_tracks_for_artist(real_artist, limit=4, per_album_sample=2)
                                    if top_tracks:
                                        debug_box.write(f"  └ ✅ Pulled {len(top_tracks)} tracks for {real_artist.title}.")
                                    else:
                                        debug_box.write(f"  └ ℹ️ No album tracks found for {real_artist.title}.")
                                    for top_track in top_tracks:
                                        if getattr(top_track, 'ratingKey', None) and top_track.ratingKey not in existing_keys:
                                            # NOTE: real_artist.title would always equal this track's own
                                            # grandparentTitle (we pulled the track FROM real_artist's albums),
                                            # so showing it here just echoes the artist column back at the user.
                                            # What's actually useful is which artist in their playlist this
                                            # was found similar to — that's the seed, not real_artist.
                                            seed_artist_name = getattr(seed, 'grandparentTitle', 'Unknown Artist')
                                            setattr(top_track, 'recommendation_type', f'Related Artist (like {seed_artist_name})')
                                            raw_pool.append(top_track)
                        except Exception as e_inner:
                            debug_box.write(f"  └ ❌ Artist Fetch Failed for {getattr(sim_artist, 'tag', 'Unknown')}: `{str(e_inner)}`")
                else:
                    debug_box.write("└ ℹ️ No similar artists mapped for this seed.")
        except Exception as e:
            debug_box.write(f"└ ❌ Plexamp Metadata Route Failed: `{str(e)}`")

    if not raw_pool:
        return []

    # De-duplicate pool items cleanly
    unique_dict = {}
    for track in raw_pool:
        rk = getattr(track, 'ratingKey', None)
        if not rk: continue
        if rk not in unique_dict:
            unique_dict[rk] = track
        elif getattr(track, 'recommendation_type', '') == 'Sonic Match':
            unique_dict[rk] = track

    # Split into the two categories so the final list is a balanced mix
    # rather than whatever ratio happened to survive de-duplication.
    sonic_tracks = [
        t for t in unique_dict.values()
        if getattr(t, 'recommendation_type', '') == 'Sonic Match'
    ]
    related_tracks = [
        t for t in unique_dict.values()
        if getattr(t, 'recommendation_type', '').startswith('Related Artist')
    ]
    random.shuffle(sonic_tracks)
    random.shuffle(related_tracks)

    half = count // 2  # 5 when count=10
    sonic_take = min(half, len(sonic_tracks))
    related_take = min(count - sonic_take, len(related_tracks))

    final_pool = sonic_tracks[:sonic_take] + related_tracks[:related_take]

    # If one category came up short, backfill from the other so we still
    # try to reach `count` total recommendations.
    still_needed = count - len(final_pool)
    if still_needed > 0:
        leftover = sonic_tracks[sonic_take:] + related_tracks[related_take:]
        random.shuffle(leftover)
        final_pool += leftover[:still_needed]

    random.shuffle(final_pool)
    return final_pool[:count]

# --- UI DRAWING ---
st.title("🎧 Recommended for this Playlist")
st.caption("A streamlined recommendation drawer fueled by your server's Sonic Analysis and Plexamp Related Artist mappings.")

# Fetch your Plex Playlists
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
        st.session_state['recommendations'] = generate_playlist_vibe_recommendations(current_playlist, count=10)

if st.button("🔄 Refresh Recommendations"):
    with st.spinner("Fetching fresh music..."):
        st.session_state['recommendations'] = generate_playlist_vibe_recommendations(current_playlist, count=10)
    st.rerun()

st.write("---")

rec_list = st.session_state['recommendations']
if not rec_list:
    st.info("No recommendations found matching this playlist vibe. Try adding more tracks.")
else:
    if 'now_playing_key' not in st.session_state:
        st.session_state['now_playing_key'] = None

    for idx, track in enumerate(rec_list):
        thumb_path = getattr(track, 'parentThumb', None) if getattr(track, 'parentThumb', None) else getattr(track, 'thumb', None)
        artwork_url = f"{PLEX_URL.rstrip('/')}{thumb_path}?X-Plex-Token={PLEX_TOKEN}" if thumb_path else "https://unsplash.com"

        # Row 1: cover + title/artist locked on the same line via flex CSS
        # (column widths don't hold up on narrow screens, hence the raw HTML).
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

        text_col, controls_col = st.columns([5, 2])

        with text_col:
            st.markdown(
                f"""
                <div class="track-card-top">
                    <img src="{artwork_url}" width="48" height="48">
                    <div class="track-card-text">
                        <span class="track-card-title"><b>{track.title}</b> — {artist_title}</span>
                        <span class="track-card-source"> · <i>{album_title}</i> · <code>{rec_type_display}</code></span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        with controls_col:
            # Nested columns for play/add — forced to stay side by side via
            # the CSS rule above, regardless of how narrow controls_col gets.
            play_col, btn_add_col = st.columns([1, 1])

            with play_col:
                is_playing = st.session_state['now_playing_key'] == track.ratingKey
                # \uFE0E forces "text presentation" on these glyphs instead of
                # the platform's colored emoji rendering, so they pick up the
                # grey CSS color like the rest of the icons.
                icon = "\u23F8\uFE0E" if is_playing else "\u25B6\uFE0E"  # ⏸︎ / ▶︎
                if st.button(icon, key=f"play_{track.ratingKey}_{idx}"):
                    # Toggle: clicking the currently-playing track stops it,
                    # clicking any other track switches playback to it.
                    st.session_state['now_playing_key'] = None if is_playing else track.ratingKey
                    st.rerun()

            with btn_add_col:
                if st.button("\uFF0B", key=f"add_{track.ratingKey}_{idx}"):  # ＋ fullwidth plus, renders as plain text
                    current_playlist.addItems([track])
                    st.session_state['recommendations'].pop(idx)
                    st.toast(f"Added: {track.title}")
                    st.rerun()

        if st.session_state['now_playing_key'] == track.ratingKey:
            stream_url = get_stream_url(track)
            if stream_url:
                st.audio(stream_url, autoplay=True)
            else:
                st.warning("No playable audio found for this track.")

        st.markdown("<hr style='margin:2px 0; opacity:0.15;'>", unsafe_allow_html=True)