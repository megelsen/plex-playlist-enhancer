# Smart Playlist Enhancer

A Streamlit app that connects to your Plex server and suggests new tracks
for an existing playlist — either **Sonic Match** (tracks that sound similar
to what's already in the playlist, via Plex's built-in sonic-similarity hub)
or **Related Artist** (tracks by artists similar to ones already in the
playlist). You can preview each suggestion (play/pause) and add it to the
playlist with one tap, right from a mobile-friendly UI.

## Features

- Connects to any Plex Media Server using your server URL + auth token.
- Pulls recommendations from Plex's sonic-similarity and related-artist hubs.
- Shows a match percentage for sonic matches, and which artist in your
  playlist triggered a related-artist suggestion.
- In-browser play/pause preview streamed directly from Plex.
- One-tap "add to playlist" per suggestion.
- Responsive layout: single-line track rows on desktop, stacked rows on
  mobile.
- Sidebar API debug log for troubleshooting Plex responses.

## Requirements

- Python 3.9+
- A running Plex Media Server and an [auth token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
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
   ```
5. Run the app:
   ```bash
   streamlit run app.py
   ```
6. Open the URL Streamlit prints (defaults to `http://localhost:8501`; the
   Docker setup below instead exposes it on `8502`), enter your Plex
   URL/token in the sidebar if you didn't set them as environment
   variables, and pick a playlist to enhance.

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
    ├── requirements.txt
    └── (tracks.css, etc. if used)
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
    restart: unless-stopped
```

`PLEX_URL`/`PLEX_TOKEN` are left blank in the compose file itself and pulled
from your environment (or a local `.env` file — see below) rather than
committed to the repo.

### Dockerfile (place inside `plex-playlist-enhancer/`)

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8502
CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0"]
```

### Set your Plex credentials via `.env`

Create a `.env` file next to `docker-compose.yml` (and make sure it's in
`.gitignore` — see below):

```
PLEX_URL=http://<your-plex-host>:32400
PLEX_TOKEN=<your-plex-token>
```

### Build and run

```bash
docker compose up -d --build
```

The app will then be available at `http://<host>:8502`.

## Configuration

| Environment variable | Description                         | Default                 |
|-----------------------|--------------------------------------|--------------------------|
| `PLEX_URL`            | Base URL of your Plex server         | `http://localhost:32400` |
| `PLEX_TOKEN`          | Your Plex authentication token       | *(none — required)*      |

These are just defaults for the sidebar fields — you can always override them
in the running app itself.

## Notes

- Streaming previews hit the track's raw media file part directly (rather
  than Plex's transcode endpoint), so playback works with a plain HTML
  `<audio>` element without needing an active transcode session.
- Sonic-match percentages come from Plex's internal similarity score/distance
  field, which isn't officially exposed by `plexapi` — the app reads it from
  the raw hub response and normalizes it to a 0–100 scale.

## License

Add your preferred license here (e.g. MIT) — see [choosealicense.com](https://choosealicense.com/) if you're not sure which to pick.
