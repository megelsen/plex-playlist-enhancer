import os

import streamlit as st
from plexapi.server import PlexServer

from styles import inject_custom_css
from tabs import enhance_tab, mix_tab, clusters_tab, galaxy_tab

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

tab_enhance, tab_mix, tab_clusters, tab_galaxy = st.tabs(
    ["🎧 Playlist Enhancer", "🎨 Artist Mix", "🗂️ Library Clusters", "🌌 Library Galaxy"]
)

with tab_enhance:
    enhance_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box)

with tab_mix:
    mix_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box)

with tab_clusters:
    clusters_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box, GEMINI_API_KEY)

with tab_galaxy:
    galaxy_tab.render(plex, PLEX_URL, PLEX_TOKEN, debug_box, GEMINI_API_KEY)
