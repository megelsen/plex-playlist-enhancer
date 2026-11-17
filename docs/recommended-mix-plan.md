# Plan: "For You" recommended mix

Status: **plan only, nothing implemented yet.** Builds on the
`weekly-recluster` branch: album registry, album features, Last.fm cache,
Live Mix rotation, DJ order.

The goal is one playlist, refreshed automatically, that mixes three kinds of
tracks from your own library:

| Bucket | What it is | Default share |
|---|---|---|
| **New to you** (U) | tracks you have never played | 40% |
| **Rare gems** (R) | tracks with only a few plays, none recently | 30% |
| **Classics** (C) | your most played tracks that you haven't heard for a while | 30% |

Every pick is ranked by **sonic similarity**, **relatedness** (same or
similar artists), **cluster fit** and **popularity**, all based on what you
actually listen to.

> **Discover Weekly / On Repeat:** I couldn't find either one in this repo
> (no branch, commit, file or identifier) or in `plexamp-widget`. If they live
> somewhere uncommitted, they can share the same engine. "Discover" is the U
> bucket at 100%, and "On Repeat" is the C bucket at 100% with no cooldown
> (§4). §9 has presets for both.

---

## 1. What we already have (reused, not rebuilt)

| Need | Existing code | Units |
|---|---|---|
| Play history | `curation.listening_stats` → `plex.history(mindate=…)` (single account) | integer plays, `viewedAt` datetime |
| Sonic neighbours, track level | `Track.sonicallySimilar(limit, maxDistance)` + `plex_helpers.get_sonic_match_percent` | match % 0–100 (÷100 → 0–1); `maxDistance` 0–1, server default 0.25 |
| Sonic neighbours, album level | `album_features.json` → `sonic: {album: weight}` | 0–1 (Plex match % ÷ 100; 0.5 if Plex gives no score) |
| Plex similar artists | `album_features.json` → `artist_similar` | link only, no weight |
| Last.fm similar artists and tags | `lastfm.py` cache (`similar` match 0–1, tag weight 0–100) | as listed |
| Cluster per album | `cluster_registry` (`cluster_album_keys`, `artist_cluster_map`) | stable `cluster_id` |
| Weighted pick + artist cap | `live_playlists._pick_tracks`, `_artist_cap` | weight = plays + 1 |
| Daily rotation | `live_playlists._rotate_pool` (age, then fewest plays) | runs (days) in mix |
| Running order | `live_playlists.dj_order` | sonic match 0–1 (+1 when direct) |
| Feedback ("shown but never played") | `live_playlists._update_fit_stats` pattern | exposures, `viewCount` delta |
| Scheduling | APScheduler daily 04:00 + weekly Mon 03:00, `JOB_LOCK` | `SCHEDULER_TZ` |

Per-track fields Plex already returns (checked in `plexapi/audio.py`), all
read in one bulk `section.searchTracks()` call:

| Field | Meaning | Units |
|---|---|---|
| `viewCount` | all-time plays (counted at ~90% completion) | integer |
| `lastViewedAt` | last play | datetime |
| `skipCount` | all-time skips | integer |
| `userRating` | star rating | **0.0–10.0 = 0–5 stars** |
| `ratingCount` | **global popularity: Last.fm listeners who scrobbled the track** | integer listeners |
| `addedAt` | added to library | datetime |
| `duration` | length | **milliseconds** |
| `parentRatingKey` / `grandparentRatingKey` | album / artist | key |

So global popularity costs no extra Last.fm calls: `ratingCount` is already
on every track. `Artist.popularTracks()` sorts by the same field.

---

## 2. Unit conventions (traced end to end)

- **Every score is a unitless 0–1 number.** Each raw input is converted once,
  at the point where it is read, and the conversion is named in code:
  - `match_pct / 100`
  - `userRating / 10`
  - `lastfm_tag_weight / 100`
  - `log1p(ratingCount) / log1p(max_ratingCount)`
- **Time is always days.** Ages are computed as
  `(now − viewedAt).total_seconds() / 86400`. Never mix in Plex `duration`,
  which is in milliseconds. Use `duration / 60000` only for minute-based
  length limits.
- **Decay constants τ are in days**, and their names say so:
  `TAU_SHORT_DAYS`, `TAU_LONG_DAYS`.
- **Two kinds of play count, kept apart.** `viewCount` is all-time.
  History-window counts only cover `history_days`, so a track can have
  `viewCount = 40` and 0 plays in the window (it was played before the
  window, or imported). The bucket rules below say which count they use.
- **Bug to check first: tz-aware vs naive datetimes.** `curation.py` already
  calls `viewedAt.replace(tzinfo=None)`. The new code should use one helper
  `_age_days(dt)` everywhere.

---

## 3. Taste profile (`taste_profile.py`, new)

Built once a day by the 04:00 job and cached to
`/app/data/taste_profile.json`.

**Input:**
- `plex.history(mindate=now − 365 d)`, one entry per play
- bulk track attributes (§1)
- the registry
- the Last.fm cache

**Decayed affinity**, per track / album / artist / cluster *x*:

```
A_τ(x) = Σ over plays e of x:  exp(−age_days(e) / τ)          # "effective plays"
a_τ(x) = A_τ(x) / (A_τ(x) + K_τ)                              # saturate to 0–1
         K_τ = median A_τ over entities with A_τ > 0           # library-relative
```

- **Short term:** `TAU_SHORT_DAYS = 14`, what you're into now.
- **Long term:** `TAU_LONG_DAYS = 120`, what you always come back to.
- **Tracks with all-time plays but no history in the window** get a long-term
  floor of `0.5 · pct_viewCount(t)`, so old favourites still count.

**Explicit and negative signals:**

| Signal | Rule |
|---|---|
| `userRating ≥ 8` (4–5★) | +0.2 to the track's long-term affinity (capped at 1) |
| `userRating ≤ 4` (≤ 2★) | **excluded** from all buckets; the artist gets a −0.1 relatedness penalty |
| skips | `skip_ratio = skipCount / (skipCount + viewCount)`; if `skipCount ≥ 3` and ratio > 0.5 → ×0.5 |
| recommended, never played | ×`0.6^n` for *n* exposures with no play (§7) |

**Derived:**
- `cluster_profile_long` / `cluster_profile_short`: affinity share per
  cluster, summing to 1.
- **Seeds:** the top 20 tracks by short-term affinity plus the top 20 by
  long-term affinity, at most 2 per artist. These are the "what you like"
  anchors for the sonic search.

---

## 4. Buckets (candidate sets)

All thresholds are relative to the library, so they work on small and large
libraries alike. `P_k` is the k-th percentile of `viewCount` over tracks with
`viewCount ≥ 1`.

| Bucket | Rule |
|---|---|
| **U — New to you** | `viewCount == 0` and no history entry. Two sub-pools: **U-album** (the whole album is unplayed: really new) and **U-deep** (an unplayed track on an album you know). Default split 60/40. |
| **R — Rare gems** | `1 ≤ viewCount ≤ max(2, P40)` and `days_since_last ≥ 30` |
| **C — Classics** | `viewCount ≥ max(5, P85)` and `days_since_last ≥ COOLDOWN_DAYS` (21). "Forgotten favourites" rather than "on repeat". |

Always excluded:
- ≤ 2★ tracks
- non-music items (`type != "track"`)
- tracks played in the last 7 days
- tracks recommended in the last `RECO_COOLDOWN_RUNS` runs (§7)
- duplicate versions: at most one per normalised `(artist, title)`, so
  live, remaster and compilation copies don't double up. Prefer the copy with
  the highest `viewCount`, then the highest `ratingCount`.
- compilations: the "artist > 40 albums" rule from the registry
  (`SAME_ARTIST_MAX_ALBUMS`) means "Various Artists" never counts as a
  related artist.

---

## 5. Signals per candidate (all 0–1)

**Recall.** This step only touches cached data and about 40 sonic calls.

| Signal | How | Plex calls |
|---|---|---|
| `sonic` | For each seed *s*: `s.sonicallySimilar(limit=30, maxDistance=0.30)`. `sonic(t) = max_s [match(t,s) · a(s)]`. Covers tracks the seeds point to directly. | ~40 |
| `album_sonic` | From `album_features.sonic`: `Σ_A a_long(A) · w(A→album(t)) / Σ a_long`. Reaches every track on neighbour albums with no calls. | 0 |
| `related` | `max(a_long(artist(t))` (same artist), `max_L a_long(L) · sim(L, artist(t)))`. `sim` is the Last.fm match (0–1); a Plex similar-artist link counts as 0.6. | 0 |
| `cluster` | `cluster_profile(cluster(album(t))) / max(cluster_profile)` | 0 |
| `pop_global` | `log1p(ratingCount) / log1p(max ratingCount in library)` | 0 |
| `pop_album` | the track's popularity rank within its own album by `ratingCount` (1 = the album's hit) — the best way into an unheard album | 0 |
| `pop_personal` | percentile of `viewCount` among played tracks | 0 |
| `forgotten` | `1 − exp(−days_since_last / 60)` | 0 |

Combined sonic: `sonic_c = max(sonic, 0.8 · album_sonic)`. As in
`_album_similarity`, a track-level match is worth more than an album-level one.

U candidates with every signal at 0 are dropped. That keeps the pool to
"things connected to your taste" rather than the whole library.

---

## 6. Scoring and selection

**Per-bucket weights** (relative; normalised over the components that have
data, like the registry affinity):

| Component | U | R | C |
|---|---|---|---|
| `sonic_c` | 0.35 | 0.35 | 0.20 *(to **short-term** seeds = fits current mood)* |
| `related` | 0.25 | 0.20 | — |
| `cluster` | 0.15 | 0.15 | 0.20 *(short-term profile)* |
| popularity | 0.25 `(0.5 pop_global + 0.5 pop_album)` | 0.15 `pop_global` | — |
| affinity | — | 0.15 `a_long(artist)` | 0.40 `a_long(track)` |
| `forgotten` | — | — | 0.20 |

`score = weighted_sum × penalties` (§3), giving 0–1.

**Selection** (new `_pick_recommended`, built on `_pick_tracks`):

1. **Quotas.** `round(size × share)` per bucket. If a bucket can't fill its
   quota, the gap goes to the others in proportion.
2. **Ranking.** Within a bucket, weighted random sampling without replacement
   (Efraimidis–Spirakis, the same as `_pick_tracks`) with weight `score^γ`,
   γ = 3. Good tracks dominate, but each run differs.
3. **Diversity** (shared across buckets):
   - artist cap `_artist_cap(size, 0.10)`
   - at most 2 tracks per album
   - at most 35% from one cluster
   - an MMR-style −0.15 for a candidate that is a direct sonic neighbour of an
     already-picked track in the same bucket, so 5 near-identical tracks don't
     all win
4. **Label.** `recommendation_type` shows the reason, like the existing
   Popular/Discovery labels. Examples:
   - `New · sounds like "<seed>" (82%)`
   - `Rare gem · fans of <artist> also like this`
   - `Classic · 47 plays, last heard 5 months ago`

**Order:** `dj_order` gets one optional new rule, `max_unfamiliar_run=2`: no
more than 2 U tracks in a row, and the playlist opens with a C track.
Familiar anchors make the new material easier to take in. The same eligible
plus look-ahead mechanism as the no-doubling rule applies.

---

## 7. Live subscription and feedback

**New subscription kind:** `recommended_mixes` in `live_playlists.json`, next
to the artist and cluster mixes. It shows in the 🔴 Live Playlists overview.

**Refresh modes:**
- `weekly` (default): a full regeneration on Monday at the 04:00 daily run,
  after the 03:00 curation, so it sees the new clusters. Tuesday to Sunday it
  only records feedback.
- `daily`: per-bucket rotation via `_rotate_pool` at `rotation_percent` (20).

**Feedback state** (`recommendation_stats` on the subscription, the same
idea as `fit_stats`), per shown track:
`{first_shown, last_shown, exposures, plays_since_shown}`.
- **Played** (`viewCount` went up): `accepted`. The next profile counts the
  play anyway; the track then moves from U to R naturally.
- **Shown, not played:** exposures increase. The fatigue penalty `0.6^n`
  applies only if *other* tracks in the same mix were played in that period,
  the same guard as the mix-fit penalty, so a week of no listening at all
  penalises nothing.
- **Cooldown:** a track that rotated out isn't shown again for
  `RECO_COOLDOWN_RUNS` (4 weekly runs or 28 daily runs).
- **Acceptance rate per bucket** (plays ÷ exposures) shows in the UI. It is
  the main health metric for tuning the weights.

---

## 8. Files to add or change

| File | Change |
|---|---|
| `taste_profile.py` (new) | history → decayed affinities, seeds, cluster profiles, cache |
| `recommender.py` (new; the current file only does playlist vibe) | buckets, recall, signals, scoring, `_pick_recommended`, labels. Kept separate from `recommendations.py`, which stays as is. |
| `live_playlists.py` | `recommended_mixes` CRUD, `run_recommended_subscription`, feedback update, wiring into `run_all_subscriptions` and the daily job; `dj_order(max_unfamiliar_run=…)` |
| `tabs/foryou_tab.py` (new) | a "✨ For You" section: bucket share sliders, size, cadence, a preview table with the reason column and per-bucket counts, ▶ play preview, 💾 save / "keep updated" |
| `app.py` | add the section |
| `docs/recommended-mix.md` | the final reference in the same style as `weekly-recluster.md` (replaces this plan) |

**Cost per run:**
- 1 bulk `searchTracks`
- 1 paged `history` call
- about 40 `sonicallySimilar` calls for seeds
- about 50 for `dj_order`
- Last.fm only for artists not cached yet (2 calls each, already throttled
  in `lastfm.py`)

---

## 9. Presets (one engine, several playlists)

| Preset | U / R / C | Notes |
|---|---|---|
| **For You** (default) | 40 / 30 / 30 | weekly |
| **Discover** | 100 / 0 / 0 | U-album 80%, weekly |
| **Deep cuts** | 50 (U-deep only) / 50 / 0 | |
| **Rediscover** | 0 / 40 / 60 | daily, forgotten-favourite heavy |
| **On Repeat** | 0 / 0 / 100 | `COOLDOWN_DAYS = 0`, uses the short-term affinity instead of `forgotten` |
| **Per cluster** | 40 / 30 / 30 | candidates limited to one `cluster_id`, so "For You: Post-Rock" |

---

## 10. Build order

1. **Profile and buckets, read only.** Build `taste_profile.py` plus a debug
   view of the bucket sizes, the P40/P85 thresholds, the top seeds and the
   cluster profile. Check the numbers against your real library before any
   scoring.
2. **Scoring and preview.** Add `recommender.py` and the For You tab with
   preview and reasons. No Plex writes except the explicit 💾.
3. **Offline backtest.** Hide the last 14 days of history, build the profile
   from the rest, and measure:
   - how many of the tracks and artists you first played in those 14 days
     land in the top 200 U/R candidates (recall@200)
   - the same against two baselines: random, and popularity-only

   Tune the weights in §6 with a small grid until the engine clearly beats
   both baselines.
4. **Live mix and feedback.** Subscription, weekly/daily modes, acceptance
   stats, fatigue, cooldown.
5. **Presets and the DJ rule.** §9, `max_unfamiliar_run`, per-cluster
   variants.
6. **Docs.** Write `docs/recommended-mix.md`.

---

## 11. Open decisions

1. **Default mix.** 40/30/30 and 50 tracks? Or lean more towards
   discovery?
2. **Cadence.** Weekly fresh list (Discover-style) or daily 20% rotation?
3. **Overlap with Cluster Mixes.** Should For You avoid tracks currently in
   other Live Mixes? This is cheap: their `current_*_keys` are already
   stored.
4. **Classics cooldown.** Is 21 days right, or should "On Repeat" territory
   be excluded more aggressively?
