# Plan: "For You" recommended playlist

Status: **plan only, nothing implemented yet.**

This plan builds on three existing pieces:
- `scripts/discover-weekly/script.py` and `scripts/on-repeat/script.py`
  (the `playlist-factory` branch)
- the Streamlit app's album cluster registry and Live Mixes (the
  `weekly-recluster` branch)

---

## 1. Where it fits

| Playlist | What it answers | Familiarity | Refresh |
|---|---|---|---|
| **On Repeat** | what am I playing a lot *right now* | 100% played, recent | weekly |
| **Discover Weekly** | what haven't I heard that I'd like | about 87% unplayed and 13% Rediscover (≥ 3 plays, ≥ 180 d quiet) | weekly |
| **Cluster Mixes** (app) | music of one style | popular / random split | daily, 20% rotation |
| **For You** (new) | a balanced mix across all my taste: new, half-known and beloved | about **40% unheard / 35% rarely played / 25% classics** | **daily, about 25% rotation** |

What For You adds that nothing covers today:
- **Rarely played:** tracks with 1 to a few plays. Discover Weekly only takes
  tracks with 0 plays, or 3 or more plays after 180 days of silence. The
  1–2 play middle ground is never surfaced.
- **Classics as anchors.** Familiar tracks woven between unfamiliar ones
  make the new ones easier to take in.
- **Daily change** without reshuffling everything.
- **Cluster awareness:** fit to the clusters you play most, plus a spread
  over them.

**No overlap rule:**
- For You never contains a track that is currently in **On Repeat** or
  **Discover Weekly**.
- It never contains a track Discover Weekly picked in the last
  `HISTORY_WEEKS`.
- Discover Weekly already excludes On Repeat.

---

## 2. Architecture

**Recommendation:** follow the script pattern, not the Streamlit app. For You
needs the same inputs as Discover Weekly:
- the Tautulli history, which has real listen seconds, so 30-second skips
  can be filtered out
- the bulk library snapshot
- the taste profile
- sonic seeds
- feedback
- the DJ order

Discover Weekly already has about 500 lines of exactly that. A third copy
would drift, so step 0 is to share it:

```
scripts/
  common/                 # NEW: extracted from discover-weekly, no behaviour change
    tautulli.py           # tautulli_get, fetch_history(after_ts=None), is_qualifying_play
    library.py            # load_library, song_key, norm, to_ts
    taste.py              # build_taste, sonic_scores, similar_artist_scores
    feedback.py           # compute_feedback (now over ALL playlists' state)
    select.py             # weighted_order, select(take with caps), interleave
    dj.py                 # sonic_links, dj_order, dj_stretches
    plexio.py             # find playlist (by stored ratingKey, else title), replace items, summary
    state.py              # load/save state.json (atomic)
    clusters.py           # optional: read the app's cluster_registry.json
  discover-weekly/script.py   # thin: config + buckets + main loop
  on-repeat/script.py         # thin: unchanged scoring, shared tautulli/plexio
  for-you/script.py           # NEW
  for-you/.env.example
```

**Deployment:**
- Each script keeps its own container and `.env`, as in the README compose
  example.
- The only change is the volume: mount `./plex-playlist-enhancer/scripts`
  as `/app` and run `python for-you/script.py`, with `PYTHONPATH=/app` so
  `common` imports.
- Each script keeps its own `STATE_FILE` (for example
  `/app/for-you/state.json`).
- **Optional:** mount the app's data volume read-only, for example
  `/appdata/cluster_registry.json`, and set `CLUSTER_REGISTRY_PATH` to use
  clusters. Without it, For You falls back to Discover Weekly's style tags.

---

## 3. Units (traced: Tautulli / Plex → script)

| Quantity | Source | Unit at source | In script |
|---|---|---|---|
| play time | Tautulli `date`, `started`, `stopped` | **unix seconds** | `days_ago = (now_ts − date) / 86400` → days |
| listen time | `stopped − started − paused_counter` | **seconds** | compared with `MIN_LISTEN_SECONDS` (s) |
| last played | Plex `lastViewedAt` | datetime | `to_ts()` → unix s → days via /86400 |
| track length | Plex `duration` | **milliseconds** | `/1000` → s (Discover Weekly already does this; keep it) |
| lifetime plays | Plex `viewCount` | integer, counted at about 90% completion | plays |
| qualifying plays | Tautulli rows passing `is_qualifying_play` | integer, ≥ 30 s listened | plays |
| skips | Plex `skipCount` | integer | skips |
| rating | Plex `userRating` | **0–10 (= 0–5★)** | ≤ 4 → ≤ 2★ (ban); ≥ 8 → ≥ 4★ |
| global popularity | Plex `ratingCount` | **Last.fm listeners** (integer) | `log1p(x) / log1p(max)` → 0–1, or within-artist percentile |
| sonic match | `sonicallySimilar` result `.distance` | 0–1, 0 = identical | `1 − distance` → 0–1; seed score uses rank `1 − rank/SONIC_LIMIT` |
| half-lives | env `*_HALF_LIFE_DAYS` | **days** | `0.5 ** (days_ago / half_life)` |

All scores are unitless 0–1 before weighting.

**Issues found in the existing scripts** (fix in step 0, each one small):

1. **Double-counted plays.** Discover Weekly's `build_taste` adds
   `0.1 × viewCount` to artist and style affinity "for plays before
   Tautulli". But `viewCount` is lifetime and *includes* the Tautulli-era
   plays, so those are counted twice. Fix: add only the pre-Tautulli
   remainder, `max(0, viewCount − tautulli_plays(track))`.
2. **Two definitions of a play.** Tautulli counts at ≥ 30 s; Plex
   `viewCount` counts at about 90% completion. The two are mixed in the
   feedback (`views > pick.views`) and in the taste profile. Keep both, but
   name them: `plays_tautulli` (≥ 30 s) for taste, `views_plex` (completed)
   for "accepted". Say so in comments so nobody compares them directly.
3. **On Repeat reads history twice.** It fetches 30 days, then 3650 days;
   the second includes the first. Do one fetch and split it by `date`.
4. **Discover Weekly fetches the entire history every run.** It has no
   `after=` filter. That's fine for now, but `common.tautulli.fetch_history`
   should take `after_ts` (unix s), as On Repeat does.
5. **Playlists are found by title.** If you rename "Discover Weekly" in
   Plex, a second playlist gets created. Store the playlist's `ratingKey`
   in `state.json` and look it up by key first (the app's
   `_upsert_playlist` does this).

---

## 4. For You: buckets

All thresholds are relative to the library. `views` is the song-level sum
over copies (Discover Weekly's `song_key` already merges live, remaster and
compilation copies). `P_k` is the k-th percentile of `views` over songs with
`views ≥ 1`.

| Bucket | Rule | Default (of 50) |
|---|---|---|
| **Unheard** | `views == 0`, not in Discover Weekly's current list or recent picks | 20 |
| **Rarely played** | `1 ≤ views ≤ max(2, P40)` and last play ≥ `RARE_MIN_DAYS` (30) ago, and skip ratio `skips/(skips+views) < 0.5` | 18 |
| **Classics** | `views ≥ max(5, P85)` and last play ≥ `CLASSIC_MIN_DAYS` (21) ago, not in On Repeat | 12 |

Always excluded:
- ≤ 2★ tracks
- banned artists (from the feedback)
- tracks under `MIN_TRACK_SECONDS`
- anything in On Repeat or Discover Weekly right now

---

## 5. For You: scoring

Signals, all 0–1. Most come straight from `common.taste`:

| Signal | Source |
|---|---|
| `sonic` | `sonic_scores(seeds)`: closeness to recent and all-time seed tracks |
| `sonic_now` | the same, but with the **recent seeds only** ("fits what I'm into this month") |
| `artist` | `max(artist_aff, similar_artist_link)` |
| `style` | album style/genre vs the taste profile (Discover Weekly's `album_style`) |
| `cluster` | (optional) `cluster_profile[cluster(album)] / max`; `cluster_profile` = decayed plays per registry cluster |
| `pop` | `log1p(ratingCount) / log1p(max ratingCount)`; for Unheard, `0.5·pop + 0.5·within-album rank` (the album's best-known song as a way in) |
| `track_aff` | `log1p(views) / log1p(max views)` |
| `forgotten` | `1 − exp(−days_since_last / 60)` |

Weights per bucket (relative). With no registry, `cluster` is dropped and
the rest renormalised:

| | Unheard | Rarely played | Classics |
|---|---|---|---|
| `sonic` | 0.35 | 0.30 | — |
| `sonic_now` | — | — | 0.30 |
| `artist` | 0.25 | 0.25 | — |
| `style` | 0.10 | 0.10 | — |
| `cluster` | 0.10 | 0.10 | 0.15 |
| `pop` | 0.20 | 0.25 | — |
| `track_aff` | — | — | 0.35 |
| `forgotten` | — | — | 0.20 |

After that, the feedback multipliers apply: `compute_feedback` ×0.8 to ×1.5,
and banned artists are dropped. The feedback now reads **every** script's
`state.json`, so a skip in Discover Weekly also counts for For You.

---

## 6. Selection, order, rotation

1. **Selection.** `common.select.weighted_order` (top pool × `POOL_FACTOR`,
   temperature) plus `take()` with caps:
   - `MAX_PER_ARTIST` 2 (Unheard: 1 per artist, as in Discover Weekly)
   - `MAX_PER_ALBUM` 1
   - one per `song_key`
   - **at most 35% from one cluster** (new; only with a registry)

   Short buckets backfill in the order Rarely played → Unheard → Classics.
2. **Order.** `interleave` gets bucket rules:
   - open with a Classic
   - never more than 2 Unheard in a row
   - a Classic about every 4–5 tracks

   Then `dj_stretches` (20-track stretches) as in Discover Weekly.
3. **Daily rotation** (`ROTATION_PERCENT`, 25):
   - Remove, in this order:
     1. tracks played since yesterday (`views` went up: you got it; it now
        counts as positive feedback)
     2. tracks skipped since yesterday
     3. the oldest by days-in-list
   - Refill each bucket back to its quota.
   - Tracks removed get a `COOLDOWN_DAYS` (14) cooldown.
   - The kept tracks stay in place, and new picks are slotted in with the
     DJ similarity, so the list doesn't reshuffle.
   - Weekly full rebuild (Monday after Discover Weekly runs), or never; this
     is a config setting.
4. **State** (`for-you/state.json`):
   - `playlist_rating_key`
   - `current: [{rk, bucket, added_ts, views, skips}]`
   - `cooldown: {rk: until_ts}`
   - `runs: [...]`, kept `HISTORY_WEEKS`, in the same format as Discover
     Weekly so the shared feedback reads it

**Plex calls per daily run:**
- bulk library search (3 calls)
- Tautulli history since the last run
- about 25 `sonicallySimilar` calls for seeds
- about 13 for the DJ links of new tracks; links between kept tracks are
  cached in state

---

## 7. Steps

0. **Extract `scripts/common/`** from Discover Weekly and fix §3 issues 1–5.
   Check: `DRY_RUN=true` with `random.seed(0)` produces the same Discover
   Weekly list before and after (except where issue 1 intentionally
   changes scores; check that with the fix toggled off).
1. **For You, `DRY_RUN` only.** Implement buckets and scoring, and print
   bucket sizes, the P40/P85 thresholds and each pick with its score
   breakdown. Tune on your real library.
2. **Writes and rotation.** Create/update the playlist, daily rotation,
   state, cooldown, exclusions against On Repeat and Discover Weekly.
3. **Clusters (optional).** Read `cluster_registry.json`, add the cluster
   signal and the 35% cap.
4. **Backtest** (offline, `DRY_RUN`). Build the taste profile from history
   up to 14 days ago, then measure:
   - how many first-time plays in those 14 days rank in the top 200
     Unheard/Rarely-played candidates
   - the same for random and popularity-only

   Adjust the §5 weights until the engine clearly beats both.
5. **Docs.** Write `scripts/for-you/README` and add a compose snippet to the
   main README.
6. **Later (optional).** A read-only "🏭 Playlist Factory" tab in the
   Streamlit app showing the three scripts' last run, next run and current
   lists from their `state.json`.

---

## 8. Open decisions

1. **Split and size.** 40 / 35 / 25 of 50?
2. **Refresh.** Daily 25% rotation, or one fresh list a week (offset from
   Discover Weekly, for example Thursday)?
3. **Clusters.** Use the app's cluster registry (mount it read-only), or keep
   the scripts fully independent of the app?
4. **Shared code.** Is it fine to refactor Discover Weekly and On Repeat onto
   `scripts/common/` (same behaviour, plus the §3 fixes)? The alternative is
   leaving them untouched and duplicating the code in For You.
