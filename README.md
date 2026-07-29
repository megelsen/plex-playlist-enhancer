# Smart Playlist Enhancer

A Streamlit app that connects to your Plex server and helps you discover and
organize music across three tools:

- **🎧 Playlist Enhancer** — suggests new tracks for an existing playlist,
  either **Sonic Match** (tracks that sound similar to what's already in the
  playlist, via Plex's built-in sonic-similarity hub) or **Related Artist**
  (tracks by artists similar to ones already in the playlist). Preview each
  suggestion (play/pause) and add it to the playlist with one tap.
- **🎨 Artist Mix** — pick one artist and build a blended mix of their own
  tracks, sonically similar tracks, and tracks from related artists, then
  save it as a new Plex playlist.
- **🗂️ Library Clusters** — groups your *entire* library into a handful of
  genre/mood sections (e.g. Metal, Indie, Folk). You can lock in cluster
  names you already know you want; an LLM (Google Gemini by default) invents
  the rest and sorts every genre tag in your library into one of them. Each
  cluster shows its most-played tracks and can be saved as a playlist.

All three tabs share a mobile-friendly UI with in-browser play/pause preview
streamed directly from Plex.

## Features

- Connects to any Plex Media Server using your server URL + auth token.
- Pulls recommendations from Plex's sonic-similarity and related-artist hubs.
- Shows a match percentage for sonic matches, and which artist in your
  playlist triggered a related-artist suggestion.
- Builds artist-centered mixes with configurable caps (max total, max from
  the artist, max per related artist, max sonic matches per seed).
- Clusters the whole library by genre/mood via a single cached LLM call —
  re-running "Build Clusters" costs nothing extra unless your library's
  genre tags actually change or you explicitly force a re-map.
- In-browser play/pause preview streamed directly from Plex.
- One-tap "add to playlist" per suggestion; save any mix or cluster as a new
  Plex playlist.
- Responsive layout: single-line track rows on desktop, stacked rows on
  mobile.
- Sidebar API debug log for troubleshooting Plex responses.

## Project structure

The app is split into small, single-purpose modules rather than one big
script:

```
plex-playlist-enhancer/
├── app.py                  # entrypoint: sidebar, Plex connection, tab wiring
├── styles.py                 # injected CSS (button styling, mobile layout)
├── plex_helpers.py           # stream URL, sonic match %, top-tracks-for-artist
├── ui_components.py          # shared track-row renderer
├── recommendations.py        # Playlist Enhancer's recommendation engine
├── artist_mix.py              # Artist Mix builder
├── clustering.py              # Library Clusters: genre tag collection + Gemini mapping
├── tabs/
│   ├── enhance_tab.py         # 🎧 Playlist Enhancer UI
│   ├── mix_tab.py             # 🎨 Artist Mix UI
│   └── clusters_tab.py        # 🗂️ Library Clusters UI
├── requirements.txt
└── Dockerfile
```

## Requirements

- Python 3.9+
- A running Plex Media Server and an [auth token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- (Optional, for Library Clusters) a [Google Gemini API key](https://aistudio.google.com/app/apikey)
  — the free tier is plenty for this; the only LLM call in the app is a
  single one-time genre-tag mapping, cached so it doesn't re-run on every
  click
- Python packages listed in `requirements.txt`

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/<your-username>/<your-repo>.git
   cd <your-repo>
   ```
2. (Recommended) create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. (Optional) set default connection details as environment variables so you
   don't have to type them in every time:
   ```bash
   export PLEX_URL="http://<your-plex-host>:32400"
   export PLEX_TOKEN="<your-plex-token>"
   export GEMINI_API_KEY="<your-gemini-api-key>"   # only needed for Library Clusters
   ```
5. Run the app:
   ```bash
   streamlit run app.py
   ```
6. Open the URL Streamlit prints (defaults to `http://localhost:8501`; the
   Docker setup below instead exposes it on `8502`), enter your Plex
   URL/token (and Gemini key, if using Library Clusters) in the sidebar if
   you didn't set them as environment variables, and pick a tab to get
   started.

## Running with Docker

This repo is set up to run as a service named `plex-playlist-enhancer`,
built from a subfolder (matching the layout below) and exposed on port
`8502`.

### Expected folder structure

```
your-repo/
├── docker-compose.yml
└── plex-playlist-enhancer/
    ├── Dockerfile
    ├── app.py
    ├── styles.py
    ├── plex_helpers.py
    ├── ui_components.py
    ├── recommendations.py
    ├── artist_mix.py
    ├── clustering.py
    ├── tabs/
    │   ├── enhance_tab.py
    │   ├── mix_tab.py
    │   └── clusters_tab.py
    └── requirements.txt
```

### docker-compose.yml

```yaml
services:
  plex-playlist-enhancer:
    build: ./plex-playlist-enhancer
    container_name: plex-playlist-enhancer
    ports:
      - "8502:8502"
    environment:
      - PLEX_URL=${PLEX_URL}
      - PLEX_TOKEN=${PLEX_TOKEN}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
    restart: unless-stopped
```

`PLEX_URL`/`PLEX_TOKEN`/`GEMINI_API_KEY` are left blank in the compose file
itself and pulled from your environment (or a local `.env` file — see below)
rather than committed to the repo.

### Dockerfile (place inside `plex-playlist-enhancer/`)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py styles.py plex_helpers.py ui_components.py recommendations.py artist_mix.py clustering.py ./
COPY tabs/ ./tabs/

EXPOSE 8502
CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0"]
```

### Set your Plex/Gemini credentials via `.env`

Create a `.env` file next to `docker-compose.yml` (and make sure it's in
`.gitignore` — see below):

```
PLEX_URL=http://<your-plex-host>:32400
PLEX_TOKEN=<your-plex-token>
GEMINI_API_KEY=<your-gemini-api-key>
```

### Build and run

```bash
docker compose up -d --build
```

The app will then be available at `http://<host>:8502`.

## Configuration

| Environment variable | Description                                         | Default                  |
|-----------------------|-------------------------------------------------------|---------------------------|
| `PLEX_URL`            | Base URL of your Plex server                          | `http://localhost:32400`  |
| `PLEX_TOKEN`          | Your Plex authentication token                        | *(none — required)*       |
| `GEMINI_API_KEY`      | Google Gemini API key, used only by Library Clusters  | *(none — optional)*       |

These are just defaults for the sidebar fields — you can always override them
in the running app itself.

## Notes

- Streaming previews hit the track's raw media file part directly (rather
  than Plex's transcode endpoint), so playback works with a plain HTML
  `<audio>` element without needing an active transcode session.
- Sonic-match percentages come from Plex's internal similarity score/distance
  field, which isn't officially exposed by `plexapi` — the app reads it from
  the raw hub response and normalizes it to a 0–100 scale.
- Library Clusters caches its genre-tag → cluster-name mapping (keyed on the
  library's actual tag list, locked cluster names, and total cluster count),
  so re-running "Build / Refresh Clusters" only re-scans tracks and play
  counts — it does **not** call the LLM again unless the underlying tags
  change or you explicitly click "Force Re-map Genres."

## License

MIT — see [LICENSE](./LICENSE).
