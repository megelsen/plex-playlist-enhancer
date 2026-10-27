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
- **🗂️ Library Clusters** — groups your *entire* library into album
  clusters (e.g. Metal, Indie, Folk).
  - **Build a set** in one of two ways:
    - **Relational** (recommended): Louvain communities over Plex's
      Similar-Artist graph, optionally strengthened by audio matching.
    - **Tags only**: Gemini sorts every genre/mood tag into a cluster, and
      each album follows its tracks' tags.
  - **Review and commit:** review the full result (albums, artists and top
    artists per cluster), optionally combine clusters, and commit them as
    **🧬 album clusters**. Every album then gets a cluster, or goes to
    `Unclustered`.
  - **Make playlists** from any cluster. They draw on every track in the
    cluster, with adjustable length, max share per artist and
    popular/discovery mix, and can be saved as a static playlist or a daily
    Live Mix.
  - **Stays up to date:** new albums are assigned daily, and the clusters
    are re-curated automatically every Monday at 03:00 (Paris time) — see
    [docs/weekly-recluster.md](./docs/weekly-recluster.md).
- **🔴 Live Playlists** — subscribe an Artist Mix or a Library Cluster to a
  daily 4am refresh (configurable) so the playlist stays current without
  manual rebuilding. Artist Mixes simply re-run with the same settings each
  day. Cluster Mixes replace 20% of their songs per day (configurable;
  longest-in-the-mix first) with new picks from the same cluster, keeping
  the vibe intact while the specific songs change, and play in DJ order:
  a random first song, then always the most similar-sounding next one. Runs in the background regardless of
  whether the app is open in a browser. Cluster Mixes follow their album
  cluster through renames/merges/splits; a mix whose cluster disappears is
  marked stale and its playlist is deleted at the next weekly curation.

All four tabs share a mobile-friendly UI with in-browser play/pause preview
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
- Hybrid clustering mode blends real audio similarity, shared genre/mood
  tags, and agreement with your Gemini-defined clusters into one weighted
  graph (Louvain community detection at the artist level), with each
  signal's weight independently adjustable — favor sonic + cluster
  agreement for crisp, sharply-defined clusters, or raw tag overlap for
  looser, more inclusive ones.
- Per-artist sonic profiles (used by hybrid/sonic clustering) are cached to
  disk, keyed per artist, so re-running "Build Clusters" against an
  unchanged library re-analyzes nothing — only new or changed artists cost
  a fresh Plex sonic-similarity lookup.
- Live Playlists: subscribe any Artist Mix or saved Cluster to a daily
  automatic refresh, independent of the browser being open. Enable/disable,
  remove, or manually trigger any subscription from the Live Playlists tab
  at any time.
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
├── clustering.py              # Library Clusters: tag mapping, sonic profiles, hybrid/Louvain clustering
├── live_playlists.py          # Live Playlists: subscription storage, rotation logic, scheduler (daily + weekly)
├── cluster_registry.py        # album-level cluster registry: features, affinity scoring, assignment, curation pass
├── curation.py                # weekly curation job, guardrails, reports, commit-from-build, rollback
├── galaxy.py                  # Library Galaxy similarity graph + figure
├── docs/
│   └── weekly-recluster.md    # reference: album clusters, weekly curation, stale Live Mixes
├── tabs/
│   ├── enhance_tab.py         # 🎧 Playlist Enhancer UI
│   ├── mix_tab.py             # 🎨 Artist Mix UI
│   ├── clusters_tab.py        # 🗂️ Library Clusters UI
│   ├── registry_panel.py      # 🧬 album clusters panel (inside Library Clusters)
│   ├── galaxy_tab.py          # 🌌 Library Galaxy UI
│   └── live_tab.py            # 🔴 Live Playlists UI
├── requirements.txt
└── Dockerfile
```

## Requirements

- Python 3.9+
- A running Plex Media Server and an [auth token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
- (Optional, for Library Clusters) a [Google Gemini API key](https://aistudio.google.com/app/apikey)
  — the free tier is plenty for this; the only LLM call in the app is a
  single one-time genre-tag mapping, cached so it doesn't re-run on every
  click. Not needed for Sonic-only clustering, since that mode doesn't
  involve Gemini for membership (only for optional community naming).
- `networkx` and `python-louvain` (both in `requirements.txt`) power Library
  Clusters' Hybrid and Sonic clustering modes (Louvain community detection).
  Tags-only clustering doesn't need either.
- `apscheduler` (in `requirements.txt`) runs Live Playlists' daily refresh
  job in the background.
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
    ├── galaxy.py
    ├── live_playlists.py
    ├── cluster_registry.py
    ├── curation.py
    ├── tabs/
    │   ├── enhance_tab.py
    │   ├── mix_tab.py
    │   ├── clusters_tab.py
    │   └── live_tab.py
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
      - CLUSTER_CACHE_PATH=/app/data/cluster_mapping.json
      - ARTIST_PROFILE_CACHE_PATH=/app/data/artist_sonic_profiles.json
      - LIVE_PLAYLISTS_PATH=/app/data/live_playlists.json
      - LIVE_PLAYLISTS_HOUR=4
      - LIVE_PLAYLISTS_MINUTE=0
      - CLUSTER_REGISTRY_PATH=/app/data/cluster_registry.json
      - ALBUM_FEATURES_PATH=/app/data/album_features.json
      - CURATION_REPORTS_PATH=/app/data/curation_reports.json
      - SCHEDULER_TZ=Europe/Paris
      - CURATION_DAY_OF_WEEK=mon
      - CURATION_HOUR=3
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

`PLEX_URL` and `PLEX_TOKEN` must be real values (not blank) for Live
Playlists to work — its daily job runs with no browser session attached,
so it authenticates via these environment variables directly rather than
the sidebar fields.

`PLEX_URL`/`PLEX_TOKEN`/`GEMINI_API_KEY` are left blank in the compose file
itself and pulled from your environment (or a local `.env` file — see below)
rather than committed to the repo. The `./data` volume is what makes the two
cluster caches survive a rebuild — without it, both are recomputed from
scratch (an extra Gemini call, and a fresh sonic-analysis pass per artist)
every time the container restarts.

### Dockerfile (place inside `plex-playlist-enhancer/`)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py styles.py plex_helpers.py ui_components.py recommendations.py artist_mix.py clustering.py galaxy.py live_playlists.py cluster_registry.py curation.py ./
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
| `CLUSTER_CACHE_PATH`  | Disk path for the cached tag → cluster mapping         | `/app/data/cluster_mapping.json` |
| `ARTIST_PROFILE_CACHE_PATH` | Disk path for cached per-artist sonic profiles (Hybrid/Sonic-artist clustering) | `/app/data/artist_sonic_profiles.json` |
| `LIVE_PLAYLISTS_PATH` | Disk path for Live Playlists subscriptions               | `/app/data/live_playlists.json` |
| `LIVE_PLAYLISTS_HOUR` | Hour (0-23, in `SCHEDULER_TZ`) the daily refresh runs    | `4` |
| `LIVE_PLAYLISTS_MINUTE` | Minute the daily refresh runs                           | `0` |
| `SCHEDULER_TZ`        | Timezone for the daily refresh and the weekly curation   | `Europe/Paris` |
| `CURATION_DAY_OF_WEEK` / `CURATION_HOUR` / `CURATION_MINUTE` | When the weekly cluster curation runs | `mon` / `3` / `0` |
| `CLUSTER_REGISTRY_PATH` | Album cluster registry (+ `.snapshots/` for rollback)  | `/app/data/cluster_registry.json` |
| `ALBUM_FEATURES_PATH` | Cached per-album tags + sonic neighbours                 | `/app/data/album_features.json` |
| `CURATION_REPORTS_PATH` | Last 12 curation reports                               | `/app/data/curation_reports.json` |

These are just defaults for the sidebar fields — you can always override them
in the running app itself. The cache/subscription paths and the scheduled
hour/minute are the exception: they're read once at process start (not
sidebar-editable). The cache paths only matter if you want cluster/sonic-
profile/live-playlist state to survive container restarts — point them at a
mounted volume in Docker (see below) for that. `PLEX_URL`/`PLEX_TOKEN` are
also read directly from the environment for Live Playlists' background job,
regardless of what's typed into the sidebar in any given session.

## Live Playlists

Turns an Artist Mix or a saved Library Cluster into a playlist that refreshes
itself automatically once a day, without opening the app.

- **How scheduling works:** a background job (APScheduler) runs inside the
  same process as the Streamlit app, so it keeps firing on schedule as long
  as the container is up — a browser tab does not need to be open. It's
  started once per container process and survives Streamlit's normal
  page-rerun behavior.
- **Artist Mixes:** re-run daily with the exact same settings (max total,
  max from the artist, max related, max sonic) saved when you subscribed.
  Different songs come out naturally, since the underlying builder already
  shuffles its candidate pool each run.
- **Cluster Mixes:** rather than re-running Gemini/Louvain daily (which
  would let the vibe drift over time), the daily refresh draws from every
  track of the cluster's current 🧬 albums, plus a sonic-similarity
  expansion from outside the cluster.
  - **Rotation:** each run replaces `Daily rotation %` (default **20%**, so
    each song stays about 5 days) of the songs that have been in the mix
    longest. A song that was just rotated out won't come straight back
    (one-rotation cooldown).
  - **New picks:** chosen like static cluster playlists — a popular share
    weighted by play count, the rest at random, and at most `Max per
    artist %` from one artist across the whole mix.
  - **DJ order** (on by default): a random first song, then always the
    remaining song that sounds most like the previous one (Plex sonic
    analysis, falling back to album-level similarity), avoiding the same
    artist twice in a row.
- **Managing subscriptions:** the Live Playlists tab lets you add, enable/
  disable, manually trigger, or remove any subscription — and shows each
  one's last run time, track count, and any error from the most recent
  attempt.
- **Requirements:** a Cluster Mix needs committed 🧬 album clusters (or
  at least a saved build) in Library Clusters first — Live Playlists draws
  from them and doesn't rebuild clusters itself. Both subscription types
  need real `PLEX_URL`/`PLEX_TOKEN` values set as container environment
  variables, since the background job has no sidebar to read from.

## Album clusters & weekly curation

Full reference: [docs/weekly-recluster.md](./docs/weekly-recluster.md).

- **Commit a build:** in Library Clusters → 🛠️ Build clusters, build, review
  and press **📌 Commit as album clusters**.
  - The build's full album membership seeds the clusters, capped at the
    build's cluster count.
  - Every album is then scored against every cluster (sonic neighbours,
    tags, artist). Unseeded albums join a cluster only above a threshold,
    else they go to `Unclustered`.
- **Playlists:** 🧬 → a cluster → 🎧 Playlist. They draw on every track of
  the cluster, with length, max share per artist and popular/discovery mix
  adjustable and a preview before saving. Tick "Keep it updated daily" for
  a Live Mix instead.
- **A Live Mix for every cluster:** 🧬 → 🎧 Live Mixes for all clusters
  creates the missing ones in one go. With **🔁 Keep it that way** on, new
  clusters from weekly curation (or a new commit) get a mix automatically.
- **Daily (04:00):** albums added since the last scan are assigned to a
  fitting cluster before the Live Mixes refresh.
- **Weekly (Monday 03:00, Paris):** the clusters are re-curated
  automatically:
  - albums move only when another cluster fits clearly better two weeks in
    a row
  - thin clusters retire, near-duplicates merge, mixed clusters split, and
    new clusters form from `Unclustered`
  - listening history weights clusters toward what you play, but never
    decides what belongs together
- **Guardrails:** a run is aborted if it would change too much at once.
- **Stale Live Mixes** are deleted at the weekly run: mixes whose cluster
  was dissolved, or merged into one that already has a mix. Deletion only
  ever uses the playlist ratingKey the app created.
- **Rollback:** every run is snapshotted and can be rolled back from the 🧬
  panel, which also recreates deleted playlists.

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
- Hybrid/Sonic-artist clustering also caches a per-artist sonic profile
  (sampled top tracks + their audio-similarity matches), keyed on that
  artist's own last-updated timestamp — so a rebuild only re-analyzes
  artists that actually changed, not the whole library. Both caches persist
  to disk (`CLUSTER_CACHE_PATH` / `ARTIST_PROFILE_CACHE_PATH`) rather than
  just in-memory, so they survive container restarts if you've mounted a
  volume for them (see Docker setup above).

## License

MIT — see [LICENSE](./LICENSE).
