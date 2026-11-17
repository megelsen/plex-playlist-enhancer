# For You

`scripts/for-you/script.py` builds a 50-track playlist from your own
library, refreshed daily. It sits next to the two weekly playlists:

| Playlist | What it answers | Refresh |
|---|---|---|
| **On Repeat** | what am I playing a lot right now | weekly |
| **Discover Weekly** | what haven't I heard that I'd like | weekly |
| **For You** | new, half-known and beloved tracks from all of my taste | **daily, about 25% swapped** |

It never contains:
- tracks currently in On Repeat or Discover Weekly
- Discover Weekly's picks of the last `HISTORY_WEEKS` (6)
- tracks rated 2★ or lower
- tracks shorter than `MIN_TRACK_SECONDS` (90)

---

## 1. Buckets

A **song** is every copy of a track together (live, remaster,
compilation), using the same `song_key` as Discover Weekly. **Plays** are
Plex `viewCount` summed over the copies. **P_k** is the k-th percentile of
plays over the songs you've played, so the thresholds follow your library.

| Bucket | Rule | Default |
|---|---|---|
| **Unheard** | 0 plays, and linked to your taste (a sonic match, an artist you play or that is similar to one you play, a style fit ≥ 0.3, or a cluster you play) | 20 |
| **Rarely played** | 1 … max(2, P40) plays, none in the last 30 days, and not mostly skipped (skips ÷ (skips + plays) < 0.5) | 18 |
| **Classic** | ≥ max(5, P85) plays, none in the last 21 days | 12 |

**Fewer than 24 Classics available.** If fewer than 2 × 12 songs qualify,
for example because your favourites were all played recently, the
thresholds relax in steps. The run prints the level it used.
1. P70 and 14 days
2. P55 and 7 days

They never drop into the Rarely played range.

**Which copy is used:**
- Unheard: the copy with the most Last.fm listeners
- the other buckets: the copy you've played most

---

## 2. Scores

Every signal is 0–1. Weights are relative. A signal with no data (clusters
when no registry is mounted) is left out, and the remaining weights are
renormalised.

| Signal | Meaning | Unheard | Rarely | Classic |
|---|---|---|---|---|
| `sonic` | Plex sonic closeness to your seed tracks (see recency below) | 0.35 | 0.30 | |
| `sonic_now` | closeness to the recent seeds only | | | 0.30 |
| `artist` | your affinity for the artist, or its similar-artist link to artists you play | 0.25 | 0.25 | |
| `style` | album styles/genres vs. your taste profile | 0.10 | 0.10 | |
| `cluster` | how much you play the album's cluster (app registry, optional) | 0.10 | 0.10 | 0.15 |
| `pop` | Last.fm listeners, `log1p(ratingCount) / log1p(max)`; Unheard: average with the song's rank within its album | 0.20 | 0.25 | |
| `track_aff` | `log1p(plays) / log1p(max plays)` | | | 0.35 |
| `forgotten` | `1 − exp(−days since last play / 60)` | | | 0.20 |

**Sonic closeness.** For each seed, Plex returns its `SONIC_LIMIT` (40)
nearest tracks.
- A neighbour at rank *r* scores `seed weight × (1 − r / 40)`, and it keeps
  its best seed.
- It gets +0.1 for every other seed it is also near.
- So a song scores high when it is very close to one of your most-played
  seeds, or close to several of them.

### Recency bias

`sonic`, `artist`, `style` and `cluster` each exist twice:
- **recent** — what you've played lately
- **long-term** — your whole history

The score uses

    signal = RECENCY_BIAS × recent + (1 − RECENCY_BIAS) × long-term

| | Recent | Long-term |
|---|---|---|
| sonic seeds | your 20 most-played tracks of the last 30 days, each play weighted `0.5^(days/14)` | your 20 most-played songs of all time (Plex plays, at most 2 per artist), weighted `log1p(plays)` |
| artist, style, cluster | Tautulli plays weighted `0.5^(days / RECENT_HALF_LIFE_DAYS)` (14) | weighted `0.5^(days / TASTE_HALF_LIFE_DAYS)` (180), plus plays from before Tautulli |
| similar artists | linked to the artists you play lately | linked to the artists you play overall |

**What the bias values mean:**
- **`RECENCY_BIAS = 0.6`** (default): Unheard and Rarely played mostly follow
  your recent hits, with your long-term taste still contributing. The
  reasons in the run log say which, for example
  `sounds like Artist – Song (recent)` versus `sounds like Artist – Song` (an
  all-time favourite).
- **`0`**: your whole history counts as much as last month.
- **`1`**: only recent listening counts.
- **Classics** always use the recent seeds for `sonic_now` (a favourite that
  fits your current mood). Their cluster signal follows the bias too.

**How much difference it makes:**
- If your taste hasn't changed, the two sides agree and the bias does little.
- If it has changed, the bias matters. In a test library where two styles
  were only played 150–400 days ago, those styles took 16 of the top 20
  Unheard picks at bias 0, and 0 at bias 0.6.
- The backtest (§7) compares several bias values on your own history.

**Feedback:** past picks of **both** Discover Weekly and For You re-weight
artists. Discover Weekly reads For You's picks too.
- A pick you **played** (Plex `viewCount` went up) or rated ≥ 4★ multiplies
  its artist by 1.25–1.5.
- A pick you **skipped** or rated ≤ 2★ multiplies it by 0.8.
- **Benched:** an artist with 2 such picks and none liked.
- Classics and Rediscover picks don't count, because they say nothing about
  discovery taste.

**Picking:**
- Weighted random from the top `POOL_FACTOR` × quota of each bucket. This is
  the Discover Weekly sampler, with `TEMPERATURE` 0.15.
- Classics are picked first, then Rarely played, then Unheard. Classics are
  the most constrained group: they sit on the albums you play most.
- **Caps:**
  - 2 per artist (Unheard: 1 per artist)
  - 2 per album
  - one copy per song
  - at most 35% from one cluster (with a registry)
- A short bucket is backfilled in the order Rarely played → Unheard →
  Classic.

---

## 3. Order

**Full build:**
1. The list opens with a Classic, then the buckets are interleaved.
2. Each 20-track stretch is DJ-ordered: the next track is the one that
   sounds most like the previous one, never the same artist twice in a row.
3. **At most 2 Unheard tracks in a row.** A look-ahead keeps enough familiar
   tracks back to space out the rest.

**Daily rotation:** the kept tracks stay in their order. Each new track is
slotted into the gap where it sounds most like both neighbours. Gaps that
would double an artist, make an unheard run of 3, or open with a
non-Classic are only used if nothing else fits.

---

## 4. Daily rotation

The script runs at `RUN_HOUR:RUN_MINUTE` (05:30, container `TZ`).

**What leaves each day, in this order:**
1. Tracks that left the library, or are now excluded (they moved into On
   Repeat or Discover Weekly, were rated ≤ 2★, or their artist got benched).
2. Tracks you **played** since they were added. You got them; they count as
   positive feedback.
3. Tracks you **skipped** since they were added.
4. The oldest, until `ROTATION_PERCENT` (25%) of the list is new.

**Afterwards:**
- Removed tracks cool down for `COOLDOWN_DAYS` (14) before they can return.
- Each bucket is refilled to its quota.
- `REBUILD_WEEKDAY=mon` builds a fresh list every Monday instead. Empty
  (the default) means rotate only. `FULL_REBUILD=true` forces one fresh list.

**State** (`scripts/for-you/state.json`):

| Key | Content |
|---|---|
| `playlist_rating_key` | the playlist is found by this first, so renaming it in Plex is fine |
| `current` | `[{rk, bucket, added_ts, views, skips}]`, in playlist order |
| `cooldown` | `{rk: until_ts}` |
| `links` | cached sonic links between the current tracks, so only new tracks cost a lookup |
| `runs` | the new picks per day, kept `HISTORY_WEEKS`, read as feedback |

**Cost per daily run:**
- 3 bulk library searches
- the Tautulli history
- up to 40 `sonicallySimilar` calls for the seeds (20 recent + 20 all-time,
  fewer when they overlap)
- about 13 for the new tracks' links (50 on a full build)

---

## 5. Units

| Value | Source unit | Used as |
|---|---|---|
| Tautulli `date`, `started`, `stopped` | unix seconds | days = Δ / 86400 |
| listen time | seconds | ≥ `MIN_LISTEN_SECONDS` = a qualifying Tautulli play (taste, seeds) |
| Plex `viewCount` | plays at ~90% completion | plays, play/skip feedback |
| Plex `duration` | **milliseconds** | ÷ 1000 → seconds (`common/library.py`) |
| Plex `lastViewedAt`, `addedAt` | datetime | unix seconds |
| Plex `userRating` | 0–10 (= 0–5★) | ≤ 4 → excluded, ≥ 8 → liked |
| Plex `ratingCount` | Last.fm listeners | log-scaled popularity |
| sonic `distance` | 0–1, 0 = identical | link weight `1 − distance` |
| `*_DAYS` settings | days | |

**Two kinds of play count, kept apart.** Tautulli plays (≥ 30 s listened)
build the taste profile; Plex `viewCount` (completed) decides whether a pick
was played. The taste profile also adds plays from before Tautulli existed
as `viewCount − Tautulli plays`. Earlier versions of Discover Weekly added
the whole `viewCount`, which counted every Tautulli-era play twice.

---

## 6. Clusters (optional)

Mount the app's `cluster_registry.json` read-only and set
`CLUSTER_REGISTRY_PATH` (README compose example). Albums in Unclustered or
marked "never cluster" have no cluster.

- **Cluster profile:** your decayed plays (90-day half-life) per cluster,
  scaled to the most-played one.
- **Uses:** it is a scoring signal, and no cluster may take more than
  `MAX_CLUSTER_SHARE` (35%) of the list.

---

## 7. Backtest

`BACKTEST_DAYS=14` checks the scoring offline and writes nothing.

**How it rewinds:**
- It turns the library back 14 days: Plex play counts minus the qualifying
  Tautulli plays since then, and last-played from Tautulli.
- It builds the taste profile from the history before that.
- It ranks the Unheard and Rarely played candidates.

**What it reports:** recall@50 and recall@200, for `RECENCY_BIAS` 0,
0.3, 0.6, 0.9 and your setting. That is the share of songs
you then played (first play for Unheard, a replay for Rarely played) that
ranked in the top k. Two baselines are shown:
- popularity-only ranking
- the random expectation

Run it once against your real server before tuning the weights in §2:

```bash
docker compose run --rm -e BACKTEST_DAYS=14 plex-for-you
```

---

## 8. Tests

```bash
python scripts/tests/test_scripts.py
```

This runs against a fake Plex and Tautulli (`scripts/tests/fake_plex.py`).
It checks:
- **One full build:** 50 tracks, nothing from On Repeat or Discover Weekly,
  opens with a Classic, at most 2 unheard in a row, one copy per song, no
  artist twice in a row, no tracks under 90 s.
- **Four rotation days:**
  - played and skipped tracks leave, and 12–15 new tracks come in each day
  - kept tracks keep their order
  - removed tracks cool down
  - a renamed playlist is reused rather than duplicated
- **Clusters:** the cluster cap holds.
- **Recency bias:** on a library whose taste moved on, the bias pulls
  Unheard and Rarely played picks toward the recent styles.
- **Dry run:** writes nothing.
- **Backtest:** runs.
- **Discover Weekly:** still runs on the shared feedback a week later.

The refactor of Discover Weekly and On Repeat onto `scripts/common/` was
checked separately: their output was byte-identical to the old scripts on
the same fake libraries and seeds, before the double-count fix (§5).
