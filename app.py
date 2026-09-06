import os

import streamlit as st
from plexapi.server import PlexServer

from styles import inject_custom_css
from tabs import enhance_tab, mix_tab, clusters_tab, galaxy_tab, live_tab
import live_playlists

st.set_page_config(layout="wide", page_title="Smart Playlist Enhancer")
inject_custom_css()

# --- SIDEBAR & CONNECTION ---
st.sidebar.title("Plex Connection")
DEFAULT_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
DEFAULT_TOKEN = os.environ.get("PLEX_TOKEN", "")

PLEX_URL = st.sidebar.text_input("Plex URL", value=DEFAULT_URL)
PLEX_TOKEN = st.sidebar.text_input("Plex Token", value=DEFAULT_TOKEN, type="password")

# Gemini key follows the same sidebar text_input pattern as Plex URL/token.
# Used only for the one-time genre-tag -> cluster-name mapping call; a
# budget-tier model (gemini-2.5-flash) is plenty and cheap for this.
DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEY = st.sidebar.text_input("Gemini API Key", value=DEFAULT_GEMINI_KEY, type="password")


@st.cache_resource
def get_plex_connection(url, token):
    if not token:
        return None
    try:
        return PlexServer(url, token)
    except Exception as e:
        st.sidebar.error(f"Connection failed: {e}")
        return None


plex = get_plex_connection(PLEX_URL, PLEX_TOKEN)

if not plex:
    st.info("👈 Please enter your Plex URL and Token in the left sidebar to connect.")
    st.stop()

# Starts the Live Playlists daily scheduler exactly once per container
# process (safe to call on every rerun — see live_playlists.py). The
# scheduled job itself authenticates via PLEX_URL/PLEX_TOKEN environment
# variables, not the sidebar fields above, since it runs with no browser
# session attached.
live_playlists.ensure_scheduler_started()

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

# --- SECTION NAV -----------------------------------------------------------
#
# Deliberately NOT st.tabs(): st.tabs() only hides inactive tab content in
# the browser — every st.tabs() block's Python still executes top-to-bottom
# on EVERY rerun regardless of which tab is visible. With Library Clusters
# (a heavy Gemini/Louvain pipeline) and Library Galaxy (an O(n^2)-ish
# spring layout) as tabs, that meant clicking a play button in Playlist
# Enhancer was silently re-running the entire clustering pipeline and
# re-laying out the galaxy in the background on every single interaction —
# the main cause of the app feeling sluggish everywhere, not just in those
# two tabs.
#
# st.radio (as horizontal pills below) only ever runs the render() call for
# the section actually selected; every other section's code doesn't execute
# at all until you switch to it. Each tab's own st.session_state stays
# intact across switches exactly as before — this only changes how often
# their code runs, not what it does.
SECTIONS = {
    "🎧 Playlist Enhancer": enhance_tab,
    "🎨 Artist Mix": mix_tab,
    "🗂️ Library Clusters": clusters_tab,
    "🌌 Library Galaxy": galaxy_tab,
    "🔴 Live Playlists": live_tab,
}

if "active_section" not in st.session_state:
    st.session_state["active_section"] = list(SECTIONS.keys())[0]

st.session_state["active_section"] = st.radio(
    "Section",
    list(SECTIONS.keys()),
    key="active_section_radio",
    index=list(SECTIONS.keys()).index(st.session_state["active_section"]),
    horizontal=True,
    label_visibility="collapsed",
)
st.write("")

active = st.session_state["active_section"]

if active == "🎧 Playlist Enhancer":
    enhance_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box)
elif active == "🎨 Artist Mix":
    mix_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box)
elif active == "🗂️ Library Clusters":
    clusters_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box, GEMINI_API_KEY)
elif active == "🌌 Library Galaxy":
    galaxy_tab.render(plex, debug_box)
elif active == "🔴 Live Playlists":
    live_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box)
