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
    div[data-testid="column"] button,
    button[kind="secondary"],
    button[kind="secondaryFormSubmit"],
    button[class*="st-emotion-cache"],
    div.stButton > button[class*="st-emotion-cache"] {
        background-color: transparent !important;
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        color: #888 !important;
        font-size: 1.5rem !important;
        padding: 2px 8px !important;
        width: auto !important;
        min-height: 0 !important;
        line-height: 1 !important;
    }
    .stButton > button:hover,
    div[data-testid="stButton"] button:hover,
    div[data-testid="column"] button:hover,
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

    /* --- MOBILE ROW PINNING ---
       Flexbox tricks (nowrap, flex-basis, fixed widths) kept losing to
       Streamlit's own inline styles on the column divs, which is why
       the add button kept vanishing off-screen instead of wrapping.
       CSS Grid sidesteps that fight entirely: once the row container
       is display:grid with explicit track sizes, the *tracks* control
       each column's width regardless of any width/flex-basis Streamlit
       puts inline on the column div itself — there's nothing left for
       their responsive JS/CSS to override. render_track_row always
       calls st.columns([8, 1, 1]) (content, play, add) — three tracks,
       fixed here to match: a flexible content column plus two fixed
       40px icon columns that never move regardless of screen width. */
    div[data-testid="stHorizontalBlock"] {
        display: grid !important;
        grid-template-columns: 1fr 40px 40px !important;
        align-items: center !important;
        gap: 0.4rem !important;
    }
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
        width: auto !important;
        min-width: 0 !important;
        max-width: none !important;
        flex: none !important;
    }
    /* Track title/artwork line: prevent long titles from wrapping or
       pushing the play/add buttons out of the row; truncate instead. */
    .track-row-text {
        overflow: hidden !important;
        white-space: nowrap !important;
        text-overflow: ellipsis !important;
        display: flex !important;
        align-items: center !important;
        gap: 0.5rem !important;
    }
    .track-row-text img {
        flex: 0 0 auto !important;
        border-radius: 3px;
    }
    .track-row-text span {
        overflow: hidden !important;
        white-space: nowrap !important;
        text-overflow: ellipsis !important;
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
if 'now_playing_key' not in st.session_state:
    st.session_state['now_playing_key'] = None

tab_enhance, tab_mix = st.tabs(["🎧 Playlist Enhancer", "🎨 Artist Mix"])


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


def render_track_row(track, idx, key_prefix, mode, current_playlist=None):
    """
    Shared row renderer used by both the Playlist Enhancer and Artist Mix
    tabs: cover + play/pause + action button on one line, full-width track
    info below, inline audio player if this track is the one playing.

    mode='enhance': action button adds the track to `current_playlist` and
        removes it from st.session_state['recommendations'].
    mode='mix': action button removes the track from
        st.session_state['artist_mix_result'] (no Plex write — just curating
        the in-progress mix before saving it).
    """
    thumb_path = getattr(track, 'parentThumb', None) if getattr(track, 'parentThumb', None) else getattr(track, 'thumb', None)
    artwork_url = f"{PLEX_URL.rstrip('/')}{thumb_path}?X-Plex-Token={PLEX_TOKEN}" if thumb_path else "https://unsplash.com"

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

    if st.session_state['now_playing_key'] == track.ratingKey:
        stream_url = get_stream_url(track)
        if stream_url:
            st.audio(stream_url, autoplay=True)
        else:
            st.warning("No playable audio found for this track.")

    st.markdown("<hr style='margin:2px 0; opacity:0.15;'>", unsafe_allow_html=True)


def build_artist_mix(artist, max_total=30, max_artist=10, max_related=2, max_sonic=2, debug=None):
    """
    Builds a varied mix centered on one artist:
      1. A pool of up to `max_artist` tracks from the artist itself.
      2. Sonic matches seeded from those tracks, capped at `max_sonic` per seed.
      3. Tracks from related artists, capped at `max_related` per artist.
      4. Rule 5: if the pool is still short of `max_total` after the caps
         above, progressively relax the caps in order: sonic (4) → related
         (3) → artist (2) — pulling more from whichever leftover candidates
         are still available, round-robin across seeds/artists so it doesn't
         just dump everything from a single seed/artist.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    try:
        all_own_tracks = artist.tracks()
    except Exception as e:
        d(f"❌ Couldn't fetch tracks for {artist.title}: `{e}`")
        all_own_tracks = []

    random.shuffle(all_own_tracks)
    selected_own = all_own_tracks[:max_artist]
    leftover_own = all_own_tracks[max_artist:]

    for t in selected_own:
        setattr(t, 'recommendation_type', f'{artist.title} (Artist Pick)')

    pool = {}
    for t in selected_own:
        rk = getattr(t, 'ratingKey', None)
        if rk:
            pool[rk] = t

    d(f"**Artist tracks:** picked {len(selected_own)} of {len(all_own_tracks)} total.")

    # --- Sonic matches, seeded from the artist's own picked tracks ---
    sonic_by_seed = {}
    for seed in selected_own:
        seed_name = f"{getattr(seed, 'grandparentTitle', artist.title)} - {seed.title}"
        try:
            matches = seed.sonicallySimilar(limit=15)
        except Exception as e:
            d(f"└ ❌ Sonic lookup failed for `{seed_name}`: `{e}`")
            continue
        candidates = []
        for m in matches:
            rk = getattr(m, 'ratingKey', None)
            if not rk or rk in pool:
                continue
            setattr(m, 'recommendation_type', 'Sonic Match')
            setattr(m, 'match_percent', get_sonic_match_percent(m))
            setattr(m, 'match_seed', seed_name)
            candidates.append(m)
        sonic_by_seed[rk if (rk := getattr(seed, 'ratingKey', None)) else seed_name] = candidates
        d(f"└ ✅ `{seed_name}` → {len(candidates)} sonic candidates.")

    initial_sonic = []
    for cands in sonic_by_seed.values():
        for t in cands[:max_sonic]:
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                pool[rk] = t
                initial_sonic.append(t)

    # --- Related artists ---
    try:
        similar_artists = artist.similar() if hasattr(artist, 'similar') else []
        if callable(similar_artists):
            similar_artists = similar_artists()
    except Exception as e:
        d(f"❌ Similar-artist fetch failed: `{e}`")
        similar_artists = []

    try:
        music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        music_section = None

    related_by_artist = {}
    for sim in (similar_artists or []):
        name = getattr(sim, 'tag', None)
        if not name or music_section is None:
            continue
        try:
            found = music_section.searchArtists(title=name)
            if not found:
                continue
            real_artist = found[0]
            # Ask for more than max_related upfront so there's a reserve to
            # draw from later if Rule 5 needs to relax this cap.
            candidates = get_top_tracks_for_artist(real_artist, limit=max_related * 3 or 6, per_album_sample=2)
            candidates = [t for t in candidates if getattr(t, 'ratingKey', None) not in pool]
            for t in candidates:
                setattr(t, 'recommendation_type', f'Related Artist ({real_artist.title})')
            related_by_artist[real_artist.ratingKey] = candidates
            d(f"└ ✅ Related artist `{real_artist.title}` → {len(candidates)} candidates.")
        except Exception as e:
            d(f"└ ❌ Related artist `{name}` failed: `{e}`")

    for cands in related_by_artist.values():
        for t in cands[:max_related]:
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                pool[rk] = t

    d(f"**Pool after initial pass:** {len(pool)} tracks (target {max_total}).")

    def _round_robin_fill(grouped_leftovers, needed):
        added = []
        queues = [list(v) for v in grouped_leftovers]
        while needed > 0 and any(queues):
            for q in queues:
                if needed <= 0:
                    break
                while q:
                    candidate = q.pop(0)
                    rk = getattr(candidate, 'ratingKey', None)
                    if rk and rk not in pool:
                        pool[rk] = candidate
                        added.append(candidate)
                        needed -= 1
                        break
        return added

    # Rule 5: relax order is sonic cap (4) → related cap (3) → artist cap (2)
    if len(pool) < max_total:
        needed = max_total - len(pool)
        added = _round_robin_fill([c[max_sonic:] for c in sonic_by_seed.values()], needed)
        d(f"**Rule 5, step A (relax sonic cap):** added {len(added)} more.")

    if len(pool) < max_total:
        needed = max_total - len(pool)
        added = _round_robin_fill([c[max_related:] for c in related_by_artist.values()], needed)
        d(f"**Rule 5, step B (relax related-artist cap):** added {len(added)} more.")

    if len(pool) < max_total:
        needed = max_total - len(pool)
        added_count = 0
        for t in leftover_own:
            if needed <= 0:
                break
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                setattr(t, 'recommendation_type', f'{artist.title} (Artist Pick)')
                pool[rk] = t
                needed -= 1
                added_count += 1
        d(f"**Rule 5, step C (relax artist cap):** added {added_count} more, pool now {len(pool)} tracks.")

    final = list(pool.values())
    random.shuffle(final)
    return final[:max_total]


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
                                            setattr(top_track, 'recommendation_type', f'Related Artist ({real_artist.title})')
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
with tab_enhance:
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
        for idx, track in enumerate(rec_list):
            render_track_row(track, idx, key_prefix="enhance", mode="enhance", current_playlist=current_playlist)


# --- ARTIST MIX TAB ---
with tab_mix:
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

    @st.cache_data(show_spinner=False, ttl=300)  # refetch every 5 minutes
    def _get_all_artist_names(_section, section_key):
        # Leading underscore on `_section` tells st.cache_data to skip
        # hashing the (unhashable) Plex object; `section_key` is the
        # actual cache key so results still refresh per-library.
        return sorted([a.title for a in _section.searchArtists()], key=str.lower)
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
            render_track_row(track, idx, key_prefix="mix", mode="mix")

        st.write("---")
        default_name = f"{selected_artist.title} Mix" if selected_artist else "Artist Mix"
        playlist_name = st.text_input("New playlist name:", value=default_name, key="artist_mix_playlist_name")
        if st.button("💾 Save Mix as New Plex Playlist"):
            try:
                plex.createPlaylist(title=playlist_name, items=mix_list)
                st.success(f"Created playlist '{playlist_name}' with {len(mix_list)} tracks.")
            except Exception as e:
                st.error(f"Failed to create playlist: {e}")