# Album clusters & weekly re-curation

Reference for the album-level cluster registry (`cluster_registry.py`), the
weekly curation job (`curation.py`), and how Live Cluster Mixes follow
clusters over time (`live_playlists.py`). UI: **🗂️ Library Clusters → 🧬
Album clusters** and **🔴 Live Playlists**.

---

## 1. Why a registry

A Library Clusters build used to be a one-off grouping. It stored only a
~30-track display sample per cluster and identified clusters by **name**. That
caused three problems:

- Most albums were not in any stored cluster.
- Rebuilding or renaming a cluster broke its Live Mix ("Cluster not found"
  every day) and left the Plex playlist orphaned.
- New albums only appeared after a full manual rebuild.

The registry fixes all three:

| | Build (clustering.py) | Registry (cluster_registry.py) |
|---|---|---|
| Unit | tracks (tags mode) / artists (relational mode) | **albums** |
| Coverage | full album membership of the build's clusters (unsorted albums left out) | **every album** in the library |
| Identity | cluster name | **stable `cluster_id`** (`c_xxxxxxxxxx`) |
| Updated | manually | **daily** (new albums) + **weekly** (curation) |
| Leftovers | `Unsorted` / dropped communities | new clusters, else the `Unclustered` system cluster |

The build (`clustering.build_clusters`) is how you *seed* clusters. It returns
each cluster's **full album membership** over the whole library:

- **Tags mode:** every track is sorted by its tags, and each album goes to
  the cluster that holds most of its tracks. An artist whose albums span
  genres is split per album.
- **Relational mode:** each Louvain community brings **all** albums of all
  its artists.

Review the build in **🛠️ Build clusters → 4. Review & commit**: albums,
artists, plays and top artists per cluster, and optionally combine
clusters. Then click **📌 Commit as album clusters**.

**What the commit does:**
- It seeds a new registry generation with exactly that membership, merges
  included.
- It sets the cluster cap to the build's "Maximum number of clusters".
- It then scores every album. A seeded album only moves if another cluster
  clearly fits better; unseeded albums join by threshold or go to
  `Unclustered`.

**Playlists:** they are made from the committed clusters (🧬 → a cluster →
🎧 Playlist) and draw on **every track of every album** in the cluster.
See §12.

---

## 2. Album features (cached)

`album_features.json` holds one entry per album, keyed by album ratingKey. An
entry is recomputed only when the album's fingerprint changes. The
fingerprint is built from `updatedAt` and the feature version.

| Field | Source | Units |
|---|---|---|
| `genres` | album genres + styles (lower-cased) | tags |
| `moods` | album moods | tags |
| `artist_genres` | the album artist's genres | tags |
| `sonic` | `{neighbour_album: weight}`: `sonicallySimilar(limit=15)` on the album's 2 most-played tracks, matches mapped to their album | weight = Plex match % ÷ 100 (0–1); 0.5 when Plex gives no score |
| `artist_key` | album's artist ratingKey | — |

The file also stores `artist_similar`, which is Plex's "Similar Artist" links
between artists in your library.

Cost is one `reload()` + one `tracks()` + two `sonicallySimilar` calls per
**new or changed** album. The first commit analyses the whole library once.

---

## 3. Affinity score (album → cluster)

All scores are **unitless 0–1**. For each album *a* and cluster *C*:

```
sonic  = Σ weight of a's sonic neighbours in C / Σ weight of all a's neighbours known to the registry
tag    = cosine(a's tag vector, C's tag centroid)          # idf-weighted, leave-one-out for a's own cluster
artist = 0.7 · (share of the same artist's OTHER albums in C)
       + 0.3 · (share of a's Plex-similar artists whose majority cluster is C)

affinity = (w_sonic·sonic + w_tag·tag + w_artist·artist) / (sum of weights of the components available)
           − mix-fit penalty (if any, see §6)
```

- **Tag vectors:** genre weight 1.0, mood 0.6, artist genre 0.4, each
  multiplied by `ln(1 + N/df)` so ubiquitous tags ("rock") count less.
  Generic tags ("music", "other", …) are ignored.
- **Centroid weighting:** each album contributes to its cluster's centroid
  with weight `1 + ln(1 + plays)`, so the albums you play most define the
  cluster.
- **Missing data:** a component the album has no data for (no sonic
  neighbours, no tags, single-album artist) is skipped and the remaining
  weights are renormalised.
- **Compilations:** artists with more than 40 albums ("Various Artists") are
  ignored for the same-artist part.

---

## 4. Assignment rules

| Situation | Rule |
|---|---|
| Unassigned album (new, or in Unclustered) | Joins the best cluster if `score ≥ assign_threshold` **and** it leads the runner-up by `≥ assign_margin`. Otherwise it goes to **Unclustered**. |
| Assigned album, a better cluster exists | Moves if `best ≥ assign_threshold` and `best − current ≥ move_hysteresis × min(2, 1 + 0.5·ln(1 + plays))`. Heavily played albums need a bigger gain. |
| Assigned album, fit collapsed | Goes to Unclustered if `current < eject_threshold` and nothing reaches `assign_threshold`. |
| Weekly mode | A move must be the leader **two curations in a row** (tracked in `pending_moves`). At most `max_move_share` of a cluster's albums can leave per week. |
| Pinned album | Never moves. |
| Locked cluster | Its albums never move; it is never merged, split or retired. |

---

## 5. Structural pass (bootstrap + weekly)

Run order inside `cluster_registry.curate()`:

1. **Assignment / moves** (§4).
2. **Retire.** A cluster with fewer than `min_cluster_albums` albums
   dissolves, and its albums are re-scored. Some find another cluster; the
   rest go to Unclustered.
3. **Merge.** Clusters *A* and *B* merge when:
   - their mean cross-affinity is at least `merge_ratio` × their mean
     cohesion, **and**
   - the album graph can't separate them (same density test as coalescing
     below), **and**
   - the combined size stays within `max_cluster_albums`.

   The bigger cluster survives and keeps its ID and name. A cluster is
   merged at most once per run.
3b. **Cluster cap.** While there are more than `max_clusters` clusters
   (`Unclustered` not counted), the most similar pair is merged. The
   `merge_ratio` and graph tests are ignored here. Locked clusters are never
   merged, so they can keep the count above the cap. These merges are marked
   `reason: "cap"` and don't count toward the weekly guardrail.
4. **Split.** Weekly runs only: a commit keeps exactly the clusters you
   built. At most `max_splits_per_run` splits per run, largest clusters
   first, and only while there is room under `max_clusters`. If there is
   room for only some of the parts, the largest ones are split off and the
   rest stay in the original cluster. Applies to any cluster with at least
   2 × `min_cluster_albums` albums that wasn't just merged:
   - Louvain runs on its album graph, and the communities are then
     **coalesced**.
   - It splits only if:
     - ≥ 2 parts have `min_cluster_albums` albums or more, **and**
     - modularity ≥ `split_min_modularity`, **and**
     - ≥ `split_min_internal` of the edge weight stays inside the parts.
   - The largest part keeps the ID and name; the other parts become new
     clusters. Clusters over `max_cluster_albums` skip the coalescing step,
     so they split along their weakest seam.
5. **Spawn.** Louvain runs over **Unclustered** albums (at resolution 0.5),
   and the communities are coalesced.
   - Communities of at least `min_cluster_albums` become candidates, most
     played first, up to `max_new_clusters` **and** the room left under
     `max_clusters`. When the build already filled the cap, leftovers stay
     in `Unclustered` until you raise the cap.
   - A candidate is kept only if its albums' mean affinity to it reaches
     `spawn_cohesion`.
6. **Final pass.** Remaining Unclustered albums try again against all
   clusters, including the new ones.
7. **Naming.** Applies only to clusters created in this run:
   - The name is the cluster's most distinctive genre, which must cover at
     least 30% of its albums, plus a dominant mood when there is one.
   - If no genre qualifies, Gemini names it from the top artists (only when
     `GEMINI_API_KEY` is set). Otherwise the name is "Artist A & Artist B
     Mix".
   - Surviving clusters keep their names.

**Album graph:** edges are sonic neighbours (weight = similarity) plus
same-artist links (+0.3; skipped for artists with more than 40 albums).
Nodes are added in sorted order, so Louvain gives the same result in every
run.

**Coalescing:** after Louvain, two communities are glued back together when
the edge density between them is at least half the internal density of the
sparser one. Louvain likes to cut one uniform group into artist-sized pieces,
and this undoes that. Genuinely separate groups have few edges between them
and stay apart.

---

## 6. Listening habits

History comes from `plex.history(mindate=now − history_days)`. There is only
one account on the server, so no account filter is applied. Play counts are
integers.

**Not used:** "played in the same session" (co-listening). A day of listening
mixes very different music, so it would add noise.

**Used:**

| Signal | Effect |
|---|---|
| Plays per album over `history_days` | Centroid weight `1 + ln(1 + plays)`; higher move resistance (§4); spawn candidates ordered by plays |
| Plays in the last 28 and 7 days | Reported per cluster (`plays_28d`, `plays_7d`); never deletes anything |
| **Live-Mix fit** (`fit_stats` per subscription) | See below |

**How Live-Mix fit works:**
- **Recording.** After each daily refresh, every cluster-seeded track that
  was in the mix since the last run adds an exposure for its album. Its
  `viewCount` increase is added to the album's plays. Plex counts a play at
  about 90% completion, so skipped tracks add nothing.
- **Penalty.** If an album reaches `mixfit_min_exposures` exposures with zero
  plays, while other tracks in the same mix *were* played, its affinity to
  that cluster drops by `mixfit_penalty` (0.05).
- **Strength.** The penalty is deliberately smaller than `move_hysteresis`,
  so it can only tip a borderline album and never moves one by itself.

---

## 7. Schedules

All times are in `SCHEDULER_TZ`, default **Europe/Paris** (CET/CEST). Paris
changes its clocks on Sundays, so Monday 03:00 is never skipped or repeated.

| Job | When | What |
|---|---|---|
| Weekly curation | **Monday 03:00** (`CURATION_DAY_OF_WEEK`, `CURATION_HOUR`, `CURATION_MINUTE`) | §8 |
| Daily refresh | every day 04:00 (`LIVE_PLAYLISTS_HOUR`, `LIVE_PLAYLISTS_MINUTE`) | ① assign new albums (`sync_new_albums`), ② refresh every Live Mix |

- **No overlap.** Both jobs, and the UI's background jobs, share
  `cluster_registry.JOB_LOCK`. If Monday's curation is still running at
  04:00, the daily refresh waits for it.
- **Startup catch-up.** When the container starts, it runs the curation
  first if none has run for more than 8 days. It then runs the daily refresh
  if none has run for more than 25 hours.
- **First deployment.** With no registry, the first curation bootstraps one
  from the last saved Library Clusters build.

---

## 8. Weekly curation (`curation.run_weekly_curation`)

The weekly run applies its changes automatically. Each step:

1. **Snapshot.** The registry is saved as
   `cluster_registry.json.snapshots/genNNNN.json`. The newest 8 are kept.
2. **Scan.** New and changed albums get features; albums no longer in the
   library are dropped.
3. **Listening history** and Live-Mix fit penalties are read (§6).
4. **Curate** runs in `weekly` mode on a copy of the registry, as generation
   N+1 (§4, §5).
5. **Guardrails.** If either check below fails, the run is **aborted**:
   nothing is committed and no playlist is touched. The report says why.
   - Individual moves exceed `guard_max_moved_share` (15%) of the albums
     that were assigned.
   - Retired + merged clusters exceed `guard_max_structural_share` (30%) of
     clusters. At least 1 is always allowed.
6. **Commit** generation N+1.
7. **Reconcile Live Mixes** (§9): re-point, rename, or mark stale.
8. **Delete stale Live Mixes** (§9).
9. **Report.** Written to `curation_reports.json` (the last 12 are kept) and
   shown in the 🧬 panel.

The 🧬 panel's **🧭 Run weekly curation now** button runs exactly the same
steps.

---

## 9. Live Mixes: what "stale" means

A Cluster Mix subscription stores `cluster_id` and the `playlist_rating_key`
its playlist was created under. Legacy subscriptions get matched by name the
first time they meet the registry.

| What happened to the cluster | Mix |
|---|---|
| Continued (same ID), even if renamed or drifted over many weeks | Keeps running. Its `[Mix] <name>` playlist is renamed to follow the cluster; custom playlist names are kept. The tab shows "cluster changed X% since subscribed". |
| Split: the largest part keeps the ID | Continues on the largest part. |
| Merged into a cluster **without** a mix | Re-pointed to the surviving cluster and renamed. |
| Merged into a cluster that **already has** a mix | **Stale** (duplicate). |
| Retired (dissolved) | **Stale**. |
| ID no longer exists (orphaned, e.g. after a rollback or a commit where nothing matched) | **Stale**. |

`Unclustered` is a permanent system cluster, so its mix is never stale.

**Lifecycle of a stale mix:**
- **When it's marked.** A stale mix stops refreshing immediately. A manual
  **Commit build** only *marks* mixes stale.
- **When it's deleted.** Deletion happens at the **weekly curation**,
  right after the new generation is committed.
- **What gets deleted.** The Plex playlist is deleted **only by its stored
  ratingKey, and only if that item really is a playlist**. Nothing is ever
  deleted by title. A subscription with no stored ratingKey is removed
  without touching Plex.
- **Undo.** The deleted playlist's track list is kept in the report, so a
  rollback can recreate it.
- **Retries.** If deletion fails on a Plex error, the subscription is kept
  and the next run retries.

**If you delete the playlist yourself in Plex,** the app treats that as
intent. The subscription is switched off, not silently recreated. Switching
it back on creates a fresh playlist.

**Manual commit (build → registry).** The new clusters inherit old IDs by
album overlap:
- **Continued:** Jaccard (shared albums ÷ all albums in either) ≥ 0.5,
  matched greedily, best first. The new cluster takes the old ID.
- **Merged:** an old cluster with ≥ 50% of its albums inside one new
  cluster.
- **Retired:** everything else.

The same table then applies.

---

## 10. Rollback

**🧬 → ↩️ Roll back** has three effects:
- It restores a snapshot generation.
- For every committed run that started from that generation or later, it
  recreates the deleted Live Mix playlists from their saved track lists and
  re-adds their subscriptions.
- Mixes created afterwards on clusters that don't exist in the restored
  generation become stale.

---

## 11. Files & settings

| Env var | Default | Content |
|---|---|---|
| `CLUSTER_REGISTRY_PATH` | `/app/data/cluster_registry.json` | registry (+ `.snapshots/`) |
| `ALBUM_FEATURES_PATH` | `/app/data/album_features.json` | per-album features cache |
| `CURATION_REPORTS_PATH` | `/app/data/curation_reports.json` | last 12 run reports (+ `.heartbeat`) |
| `SCHEDULER_TZ` | `Europe/Paris` | timezone for both jobs |
| `CURATION_DAY_OF_WEEK` / `CURATION_HOUR` / `CURATION_MINUTE` | `mon` / `3` / `0` | weekly curation time |
| `GEMINI_API_KEY` | — | optional, only for naming new clusters that have no clear genre |

**Tunable parameters.** These live in the registry's `params`. The common
ones are editable in 🧬 → ⚙️ Curation settings.

| Param | Default | Meaning |
|---|---|---|
| `assign_threshold` | 0.35 | min affinity to join a cluster |
| `assign_margin` | 0.05 | min lead over the runner-up |
| `move_hysteresis` | 0.08 | min gain to move (×1–2 by plays) |
| `eject_threshold` | 0.15 | below this (and nothing ≥ threshold) → Unclustered |
| `max_move_share` | 0.10 | max share of a cluster's albums leaving per week |
| `w_sonic` / `w_tag` / `w_artist` | 0.5 / 0.3 / 0.2 | affinity weights |
| `max_clusters` | from the build | hard cap on clusters, set to the build's "Maximum number of clusters" when you commit; 0 = no cap |
| `max_splits_per_run` | 2 | splits per weekly run |
| `min_cluster_albums` | 6 | smaller clusters retire; min size of a split part or new cluster |
| `max_cluster_albums` | 150 | merges never exceed it; bigger clusters get split |
| `split_min_modularity` | 0.30 | min modularity for a split |
| `split_min_internal` | 0.70 | min share of edge weight inside the split parts |
| `merge_ratio` | 0.70 | cross-affinity ÷ cohesion needed to merge |
| `spawn_cohesion` | 0.35 | min mean affinity of a new cluster's albums |
| `max_new_clusters` | 3 | new clusters per run |
| `history_days` | 180 | days of play history read |
| `mixfit_min_exposures` / `mixfit_penalty` | 3 / 0.05 | Live-Mix fit signal |
| `guard_max_moved_share` / `guard_max_structural_share` | 0.15 / 0.30 | weekly abort limits |

**Behaviour verified with a synthetic library** (40 random libraries ×
different hash seeds, all passing):
- Mis-seeded albums move to the right cluster.
- An unseeded group spawns its own cluster.
- Tagless, neighbourless albums stay Unclustered.
- New albums get assigned daily.
- Weekly moves need two runs.
- Thin clusters retire.
- Two halves of one genre merge.
- Two genres combined in one cluster split back apart.
- A cap of 15 holds through a commit and repeated weekly runs. A registry
  that had grown to 36 merges back to 15, with every genre whole again and
  no guardrail abort.
- Three weekly runs on a stable library change nothing.
- Stale mixes are re-pointed, marked, and deleted by ratingKey.

None of this has been run against a real Plex server yet.

---

## 12. Playlists from a cluster

🧬 → open a cluster → **🎧 Playlist** (`live_playlists.build_cluster_playlist`).
The pool is every track of every album currently in the cluster.

| Setting | Default | Meaning |
|---|---|---|
| Songs | 50 | playlist length |
| Max per artist (%) | 10 | at most `max(1, ceil(songs × %))` tracks per artist (10% of 50 = 5); 0 = no cap |
| Popular (%) | 50 | this share is picked by weighted random sampling with weight = plays + 1, so played tracks are likelier but unplayed ones still have a chance; the rest is picked uniformly from the remaining tracks (discovery) |
| DJ order | on | running order, see below; off = shuffled |

- **Different every time.** The selection is random, so **🎲 Generate
  preview** gives a new set each time. You can play the preview before
  **💾 Save to Plex**, which replaces an existing playlist with the same name.
- **Shorter playlists.** If the per-artist cap can't fill the requested
  length (a cluster with few artists), the playlist comes out shorter and
  the preview says why.
- **Live Mix.** Ticking **Keep it updated daily** saves the playlist as a
  Live Mix (`[Mix] <cluster>`) instead, with the same three settings. It
  follows the cluster through curation (§9).

**Live Mixes for all clusters** (🧬 → 🎧 Live Mixes for all clusters):
- **🔄 Create & update all:** one set of settings (songs, max per artist,
  popular %, daily rotation, cluster/sonic ratio, sonic seeds, DJ order,
  optionally including `Unclustered`) for every non-empty cluster:
  - **Missing mixes are created** as `[Mix] <cluster>`.
  - **Existing cluster mixes get the settings,** and their playlist names
    are kept.
  - **"Rebuild existing mixes from scratch"** (on by default) gives them a
    fresh selection under the new settings. Off, they keep their songs and
    only today's rotation follows the new settings; lowering "Songs" still
    trims each mix right away, oldest songs first.
  - **A mix whose playlist was deleted in Plex** is recreated.
  - **A mix you switched off** gets the settings but stays off.
- **First build:** everything is built right away, in a background job.
  The settings become the defaults for automatic mixes. The number of
  mixes follows the cluster cap.
- **🔁 Keep it that way** (`auto_cluster_mixes` in `live_playlists.json`):
  after every weekly curation and every commit, clusters without a mix
  (newly formed or split off) get one with the saved settings. Created
  mixes are listed in the run report.
- **Removals:** mixes whose cluster disappears are still marked stale and
  removed at the weekly curation (§9), so with auto on, the set of mixes
  tracks the set of clusters.

**DJ order** (`live_playlists.dj_order`):
1. **Opener:** a random first song.
2. **Never the same artist twice in a row.** This is a hard rule with
   lookahead: a song is only allowed next if it's by a different artist
   **and** the songs left after it can still be ordered without doubling.
   That's possible when no artist holds more than half of them, and the
   artist just played holds at most half, rounded down.
3. **Smoothest transition:** among the allowed songs, the next one has the
   best transition score from the current song:
   - **Direct sonic match:** `1 + Plex match %/100` when one song is in the
     other's `sonicallySimilar` list (50 neighbours each).
   - **Same album:** 0.6.
   - **Otherwise, album-level similarity** from the cached album features:
     the sonic-neighbour weight, else tag overlap × 0.5.
   - **Recent artist:** −0.4 if the artist played within the last 3 songs,
     to spread artists further.
4. **Ties:** broken at random.

- **When doubling is unavoidable:** only if one artist holds more than half
  of the playlist (the per-artist cap normally prevents this). That artist
  is then woven in as often as possible (D x D x … D D), which gives the
  minimum possible number of doubles.
- **Tested:** 400 random playlists with skewed artist mixes, where
  same-artist songs are each other's strongest sonic match. Every feasible
  one came out with 0 doubles, and every infeasible one at the minimum.
- **Limits:** the smoothest transitions are at the start, and the last few
  songs are whatever is left. It costs one `sonicallySimilar` call per song.

**Live Mix daily refresh** (`run_cluster_mix_subscription`):

| Part | Share | Pool |
|---|---|---|
| Cluster | `cluster_ratio` (67%) of `target_size` | every track of the cluster's albums |
| Sonic expansion | the rest | `sonicallySimilar` matches from **outside** the cluster, seeded from 10 random songs of today's cluster part, plus yesterday's sonic songs |

- **What gets replaced:** each part replaces `rotation_percent` (default
  **20%**) of the songs it currently holds. The songs that have been in the
  mix longest go first (`track_ages`), then the least played.
- **Artist cap:** kept songs that break the cap are dropped too, for
  example after you tighten it.
- **Refill:** `_pick_tracks` refills each part with the same popular/random
  split and one artist cap across the whole mix. The songs just removed
  are skipped for one run, unless the pool is too small.
- **Order:** the result is put in DJ order (or shuffled).
- **Existing mixes** keep the rotation % they were created with (older
  mixes used 85%). Change it in 🔴 Live Playlists → ⚙️ Edit configuration.
  The artist cap and popular split now apply to them with the defaults
  (10%, 50%).

There are no "tracks per cluster" or "songs per artist per cluster" build
settings any more: cluster membership and playlist length are independent.

