"""CSS injected once at app startup to compact/theme Streamlit's buttons
and pin the track-row layout on mobile widths."""

import streamlit as st


def inject_custom_css():
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

        /* --- PLOTLY CHART FULL WIDTH (Library Galaxy) ---
           Streamlit renders Plotly charts inside an iframe wrapped in an
           element-container; that wrapper (and sometimes the iframe itself)
           can end up narrower than the actual block-container width on
           mobile, leaving the chart looking squeezed into a fraction of the
           screen even with use_container_width=True. Forcing 100% width on
           every layer, and centering the block-container itself, fixes it
           without touching the chart's own layout config. */
        div[data-testid="stElementContainer"]:has(iframe),
        div[data-testid="element-container"]:has(iframe) {
            width: 100% !important;
            max-width: 100% !important;
        }
        iframe {
            width: 100% !important;
        }
        @media (max-width: 640px) {
            .block-container {
                padding-left: 0.5rem !important;
                padding-right: 0.5rem !important;
                max-width: 100% !important;
                margin-left: auto !important;
                margin-right: auto !important;
            }
        }
        </style>
    """, unsafe_allow_html=True)
