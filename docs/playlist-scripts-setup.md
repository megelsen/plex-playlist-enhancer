# Setup guide: On Repeat, Discover Weekly and For You

Step by step, on the Linux machine that runs your Docker containers. Each
step says what to run, what you should see, and what to do if you don't.

What changes compared to what you run today:
- **For You is new.** It is a third playlist: 50 tracks a day, mixing
  unheard, rarely played and classic songs, with a recency bias.
- **On Repeat and Discover Weekly** share code in `scripts/common/` now.
  - **Compose change:** their containers must mount the whole `scripts/`
    folder (step 3).
  - **Discover Weekly scoring:** it changes slightly, because a play-counting
    bug was fixed. Plays from before Tautulli were counted twice.
- **Renamed playlists.** All three find their playlist by its Plex ID, so
  renaming a playlist in Plex no longer creates a duplicate.

Time needed: about 20 minutes.

---

## Step 0 — What you need

| You need | Where to find it |
|---|---|
| Your existing `docker-compose.yml` with `plex-on-repeat` and `plex-discover-weekly` | the folder that also contains `plex-playlist-enhancer/` |
| The `.env` files of both scripts | `plex-playlist-enhancer/scripts/on-repeat/.env`, `.../discover-weekly/.env` |
| Optional: the Streamlit app's data folder (for clusters) | the folder mounted as `/app/data` in the app's compose service, usually `./data` |

For You uses the same Tautulli URL, Tautulli API key, Plex URL, Plex token
and music section ID as Discover Weekly, so you don't need anything new.

---

## Step 1 — Get the code

```bash
cd ~/path/to/plex-playlist-enhancer      # the folder next to docker-compose.yml
git status
```

**Check `git status` first.**
- If it lists modified files you want to keep, commit or stash them first:
  `git stash`.
- Your `state.json` and `.env` files are ignored by git and are not touched.

```bash
git fetch origin
git checkout ccr-58ab723e-pblgmn
git pull
ls scripts
```

You should see `common  discover-weekly  docker-compose.example.yml  for-you  on-repeat  tests`.

> This branch also contains the cluster-registry work from 2 Nov
> (`weekly-recluster`). If you'd rather have it all on `main`, ask for a pull
> request and merge that instead; the steps below stay the same.

---

## Step 2 — Stop the two old script containers

```bash
cd ..                                    # back to the folder with docker-compose.yml
docker compose stop plex-on-repeat plex-discover-weekly
```

Their playlists stay in Plex. `scripts/discover-weekly/state.json` stays where
it is, and the new layout reads the same file.

---

## Step 3 — Update `docker-compose.yml`

Open `docker-compose.yml` and **replace** the `plex-on-repeat` and
`plex-discover-weekly` services with the three services from
`plex-playlist-enhancer/scripts/docker-compose.example.yml`. Copy them
under your existing `services:` key. Don't paste the file's own
`services:` line.

What changed in the two existing services:

```yaml
    volumes:
      - ./plex-playlist-enhancer/scripts:/scripts          # was .../scripts/on-repeat:/app
    working_dir: /scripts/on-repeat                         # was /app
```

**In `plex-for-you`, check two things:**
1. **Time zone.** `TZ=Europe/Amsterdam` is yours: the daily run is at 05:30
   in that zone.
2. **Clusters.**
   - **If you use the Streamlit app's clusters,** `./data:/appdata:ro` must
     point at the app's data folder: the one with `cluster_registry.json`
     in it. Mount the folder, not the file, because the app replaces the
     file on every save.
   - **If you don't use clusters,** delete that volume line and the
     `CLUSTER_REGISTRY_PATH` line.

Check that the file is valid:

```bash
docker compose config --services
```

You should see your other services plus `plex-on-repeat`,
`plex-discover-weekly` and `plex-for-you`.

---

## Step 4 — Environment files

**On Repeat and Discover Weekly** keep their `.env`. Check one thing:

```bash
grep -n STATE_FILE plex-playlist-enhancer/scripts/*/.env
```

If a line says `STATE_FILE=/app/state.json`, **delete it**. The default is
now `state.json` next to the script, which is the same file on the host.
Nothing printed means nothing to do.

**For You** gets a new `.env`, made from the example plus your five values
from Discover Weekly:

```bash
cd plex-playlist-enhancer/scripts/for-you
cp .env.example .env
grep -E '^(TAUTULLI_URL|TAUTULLI_APIKEY|PLEX_URL|PLEX_TOKEN|MUSIC_SECTION_ID)=' ../discover-weekly/.env
```

Copy those five lines over the placeholder lines at the top of the new
`.env` (with `nano .env`), then check:

```bash
grep -E '^[A-Z]' .env
cd ../../..
```

You should see exactly the five required lines with your values. All other
settings are commented out and use their defaults.

---

## Step 5 — Dry run (writes nothing)

```bash
docker compose run --rm -e DRY_RUN=true plex-for-you
```

The first time takes about 30 s, because it installs `plexapi` and
`requests`. Then you should see something like:

```
  library: 41230 tracks, 3120 albums; 18342 Tautulli plays
  thresholds: rarely played = 1..2 plays (not in 30 days), classic >= 14 plays (not in 21 days); 9120 played songs
  candidates: Unheard 20211, Rarely played 3011, Classic 802
  recency bias 0.6 (half-life 14 days); lately: <your 5 most-played artists of the last weeks>
  20 recent + 20 all-time seeds, 1310 sonic neighbours, 0 artists re-weighted, 0 benched; excluded 25 On Repeat, 60 Discover Weekly, 0 cooling down; 14 clusters
  full rebuild
   1.+  ·  [Classic      ] Artist - Song  (31 plays, last 40 days ago)
   2.+ 81% [Rarely played] Artist - Song  (1 play; sounds like Artist - Song (recent))
   ...
DRY_RUN set: playlist and state left untouched.
```

**Check these:**

| Line | Should be | If not |
|---|---|---|
| `library` | your real track count and a non-zero number of plays | 0 plays: wrong `MUSIC_SECTION_ID` or `TAUTULLI_*` |
| `lately:` | artists you really played the last weeks | — |
| `excluded … On Repeat, … Discover Weekly` | the sizes of those playlists | 0: they don't exist yet, or `ON_REPEAT_TITLE` / `DISCOVER_TITLE` don't match their names |
| `… clusters` at the end | present if you mounted the registry | missing: wrong `./data` path, see Troubleshooting |
| `classic >= … (not in 21 days)` | 21 days | 14 or 7: you have few forgotten favourites, so it relaxed automatically; fine |
| the list | 50 numbered lines, opening with a Classic | fewer: some buckets are small; the reasons say why tracks were chosen |

---

## Step 6 — Backtest (writes nothing, optional but recommended)

```bash
docker compose run --rm -e BACKTEST_DAYS=14 plex-for-you
```

This rebuilds your taste as it was 14 days ago. It then checks how high it
would have ranked the songs you actually played since: songs you played
for the first time (Unheard) and songs you played again (Rarely played).

```
  Unheard: 20211 candidates, 37 played since.
    engine, bias 0        @50   3%   @200  11%
    engine, bias 0.3      @50   5%   @200  16%
    engine, bias 0.6 *    @50   8%   @200  22%
    engine, bias 0.9      @50   8%   @200  19%
    popularity only       @50   0%   @200   3%
    random                @50   0%   @200   1%
```

**How to read it:**
- **The rows:** each percentage is the share of songs you played that
  ranked in the top 50 or top 200. `*` marks your setting.
- **The baselines:** the engine rows should beat `popularity only` and
  `random`.
- **Choosing a bias:** if a different bias is clearly better, set it in
  `for-you/.env`, for example `RECENCY_BIAS=0.9`.
- **Small samples:** with only a handful of songs "played since", the
  numbers jump around. Try `BACKTEST_DAYS=28` for more.

---

## Step 7 — Start everything

**Order matters on the first start.** For You leaves out what is in On
Repeat and Discover Weekly, so those two go first.

```bash
docker compose up -d plex-on-repeat plex-discover-weekly
docker compose logs -f plex-on-repeat plex-discover-weekly
```

**What you should see:**
- **On Repeat:** it rebuilds right away and prints `'On Repeat' updated
  with 25 tracks`.
- **Discover Weekly:** it prints `Next build at <date>; sleeping...`, or
  rebuilds first if its last build is a week old.
- **Errors:** a `ModuleNotFoundError: No module named 'common'` means step 3
  isn't applied (the old `/app` mount); see Troubleshooting.

Press `Ctrl+C` to stop watching the logs (the containers keep running).

```bash
docker compose up -d plex-for-you
docker compose logs -f plex-for-you
```

You should see the same output as the dry run, ending with:

```
'For You' updated with 50 tracks (50 new).
Next build at 2026-11-18 05:30; sleeping...
```

---

## Step 8 — Check in Plex / Plexamp

1. Open Plexamp → Library → Playlists. **For You** is there. Its
   description reads "New, half-known and beloved tracks from your library …
   Unheard 20 · Rarely played 18 · Classic 12".
2. Pin it or add it to your home screen if you like.
3. You can rename it. The script keeps updating the renamed playlist, because
   it remembers the playlist's Plex ID.

---

## Step 9 — What happens from now on

| When | What |
|---|---|
| Every day at 05:30 (your `TZ`) | For You swaps about 25% of its tracks (played and skipped ones first, then the oldest) |
| Every 7 days | On Repeat rebuilds |
| 7 days after its last build | Discover Weekly rebuilds |
| Container restart | nothing is reshuffled; each script waits for its next due time |

**Tracks that leave For You:**
- **Played to the end:** it leaves the next day. You got it, and it counts
  as a positive signal for that artist in Discover Weekly and For You.
- **Skipped:** it leaves the next day. Skipping two picks of the same
  artist (and liking none) benches that artist.
- **Rest period:** removed tracks rest 14 days before they can come back.

**If you delete For You in Plex,** it is recreated at the next 05:30 run.
To stop it, stop the container: `docker compose stop plex-for-you`.

---

## Step 10 — Tuning (in `scripts/for-you/.env`, then restart)

After changing `.env`: `docker compose up -d plex-for-you` (recreates the
container with the new values). Add `-e FULL_REBUILD=true` once via
`docker compose run --rm -e FULL_REBUILD=true plex-for-you` to see a fresh
list right away instead of waiting for tomorrow's rotation.

| Setting | Default | Turn it… |
|---|---|---|
| `UNHEARD` / `RARELY_PLAYED` / `CLASSICS` | 20 / 18 / 12 | change the mix and the length |
| `RECENCY_BIAS` | 0.6 | up (to 0.9) to follow what you play lately; down (to 0.3) for your whole history |
| `RECENT_HALF_LIFE_DAYS` | 14 | shorter = "lately" means the last week or two; longer = last couple of months |
| `ROTATION_PERCENT` | 25 | how much changes per day |
| `REBUILD_WEEKDAY` | empty | `mon` for a completely fresh list every Monday |
| `CLASSIC_MIN_DAYS` | 21 | how long a favourite must have been quiet to count as a Classic |
| `RARE_MIN_DAYS` | 30 | same for rarely played songs |
| `MAX_UNHEARD_RUN` | 2 | max unheard tracks in a row |
| `MAX_PER_ARTIST` / `MAX_PER_ALBUM` | 2 / 2 | variety |
| `RUN_HOUR` / `RUN_MINUTE` | 5 / 30 | when the daily swap happens |

All settings, with comments: `scripts/for-you/.env.example`. How the scoring
works: `docs/for-you.md`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'common'` | The container still mounts one script folder at `/app`. Use the step 3 volume (`./plex-playlist-enhancer/scripts:/scripts`) and `working_dir: /scripts/<name>`, then `docker compose up -d <service>`. |
| `Missing required environment variable: 'TAUTULLI_URL'` | `.env` not found or incomplete: check the `env_file` path in compose and step 4. |
| `Tautulli API error on get_history` | wrong `TAUTULLI_APIKEY` (Tautulli → Settings → Web Interface → API key). |
| `Connection refused` / timeouts | The container can't reach Plex or Tautulli. Inside Docker, `localhost` is the container itself: use the server's LAN IP or the other container's name in `PLEX_URL` / `TAUTULLI_URL`, as in the Discover Weekly `.env`. |
| `library: … 0 Tautulli plays` | wrong `MUSIC_SECTION_ID` or `USER_ID`. |
| No `… clusters` in the log | `cluster_registry.json` isn't at `/appdata/cluster_registry.json`. Check with `docker compose run --rm plex-for-you ls /appdata`. The app only writes it after you commit clusters in 🗂️ Library Clusters. |
| A `warning: couldn't read cluster registry` line | same as above; For You still works without clusters. |
| Next build time looks off by hours | set `TZ` in the service's `environment`. |
| Want to start For You from scratch | `docker compose stop plex-for-you`, delete `scripts/for-you/state.json`, `docker compose up -d plex-for-you` (it rebuilds and reuses the playlist with the same name). |
| Discover Weekly picks look slightly different | expected once: the double-counted plays fix changes its taste profile a little. |

---

## Undo

To go back to the previous scripts:

```bash
docker compose stop plex-on-repeat plex-discover-weekly plex-for-you
cd plex-playlist-enhancer && git checkout main && cd ..
```

Then restore the old two services in `docker-compose.yml` (the
`.../scripts/on-repeat:/app` layout) and run
`docker compose up -d plex-on-repeat plex-discover-weekly`. The For You
playlist stays in Plex until you delete it.

---

## Optional: run the tests

No Plex needed. In a throwaway container:

```bash
docker run --rm -v "$PWD/plex-playlist-enhancer/scripts:/scripts" -w /scripts python:3.11-slim \
  sh -c "pip install -q -r for-you/requirements.txt && python tests/test_scripts.py"
```

It ends with `All checks passed.`

`scripts/tests/fake_server.py` is a fake Plex + Tautulli over HTTP. Point
the `.env` files at it to watch the real containers build playlists
without touching your server. That's how this setup was verified.
