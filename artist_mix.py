"""Artist-centered mix builder: pool of artist tracks + sonic matches +
related-artist tracks, with cap-relaxation ("Rule 5") if the pool falls
short of the requested total."""

import random

from plex_helpers import get_sonic_match_percent, get_top_tracks_for_artist


def build_artist_mix(artist, plex, max_total=30, max_artist=10, max_related=2, max_sonic=2, debug=None):
    """
    Builds a varied mix centered on one artist:
      1. A pool of up to `max_artist` tracks from the artist itself.
      2. Sonic matches seeded from those tracks, capped at `max_sonic` per seed.
      3. Tracks from related artists, capped at `max_related` per artist.
      4. Rule 5: if the pool is still short of `max_total` after the caps
         above, progressively relax the caps in order: sonic (4) → related
         (3) → artist (2) — pulling more from whichever leftover candidates
         are still available, round-robin across seeds/artists so it doesn't
         just dump everything from a single seed/artist.
    """
    d = debug.write if debug else (lambda *a, **k: None)

    try:
        all_own_tracks = artist.tracks()
    except Exception as e:
        d(f"❌ Couldn't fetch tracks for {artist.title}: `{e}`")
        all_own_tracks = []

    random.shuffle(all_own_tracks)
    selected_own = all_own_tracks[:max_artist]
    leftover_own = all_own_tracks[max_artist:]

    for t in selected_own:
        setattr(t, 'recommendation_type', f'{artist.title} (Artist Pick)')

    pool = {}
    for t in selected_own:
        rk = getattr(t, 'ratingKey', None)
        if rk:
            pool[rk] = t

    d(f"**Artist tracks:** picked {len(selected_own)} of {len(all_own_tracks)} total.")

    # --- Sonic matches, seeded from the artist's own picked tracks ---
    sonic_by_seed = {}
    for seed in selected_own:
        seed_name = f"{getattr(seed, 'grandparentTitle', artist.title)} - {seed.title}"
        try:
            matches = seed.sonicallySimilar(limit=15)
        except Exception as e:
            d(f"└ ❌ Sonic lookup failed for `{seed_name}`: `{e}`")
            continue
        candidates = []
        for m in matches:
            rk = getattr(m, 'ratingKey', None)
            if not rk or rk in pool:
                continue
            setattr(m, 'recommendation_type', 'Sonic Match')
            setattr(m, 'match_percent', get_sonic_match_percent(m))
            setattr(m, 'match_seed', seed_name)
            candidates.append(m)
        sonic_by_seed[rk if (rk := getattr(seed, 'ratingKey', None)) else seed_name] = candidates
        d(f"└ ✅ `{seed_name}` → {len(candidates)} sonic candidates.")

    initial_sonic = []
    for cands in sonic_by_seed.values():
        for t in cands[:max_sonic]:
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                pool[rk] = t
                initial_sonic.append(t)

    # --- Related artists ---
    try:
        similar_artists = artist.similar() if hasattr(artist, 'similar') else []
        if callable(similar_artists):
            similar_artists = similar_artists()
    except Exception as e:
        d(f"❌ Similar-artist fetch failed: `{e}`")
        similar_artists = []

    try:
        music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
    except StopIteration:
        music_section = None

    related_by_artist = {}
    for sim in (similar_artists or []):
        name = getattr(sim, 'tag', None)
        if not name or music_section is None:
            continue
        try:
            found = music_section.searchArtists(title=name)
            if not found:
                continue
            real_artist = found[0]
            # Ask for more than max_related upfront so there's a reserve to
            # draw from later if Rule 5 needs to relax this cap.
            candidates = get_top_tracks_for_artist(real_artist, limit=max_related * 3 or 6, per_album_sample=2)
            candidates = [t for t in candidates if getattr(t, 'ratingKey', None) not in pool]
            for t in candidates:
                setattr(t, 'recommendation_type', f'Related Artist ({real_artist.title})')
            related_by_artist[real_artist.ratingKey] = candidates
            d(f"└ ✅ Related artist `{real_artist.title}` → {len(candidates)} candidates.")
        except Exception as e:
            d(f"└ ❌ Related artist `{name}` failed: `{e}`")

    for cands in related_by_artist.values():
        for t in cands[:max_related]:
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                pool[rk] = t

    d(f"**Pool after initial pass:** {len(pool)} tracks (target {max_total}).")

    def _round_robin_fill(grouped_leftovers, needed):
        added = []
        queues = [list(v) for v in grouped_leftovers]
        while needed > 0 and any(queues):
            for q in queues:
                if needed <= 0:
                    break
                while q:
                    candidate = q.pop(0)
                    rk = getattr(candidate, 'ratingKey', None)
                    if rk and rk not in pool:
                        pool[rk] = candidate
                        added.append(candidate)
                        needed -= 1
                        break
        return added

    # Rule 5: relax order is sonic cap (4) → related cap (3) → artist cap (2)
    if len(pool) < max_total:
        needed = max_total - len(pool)
        added = _round_robin_fill([c[max_sonic:] for c in sonic_by_seed.values()], needed)
        d(f"**Rule 5, step A (relax sonic cap):** added {len(added)} more.")

    if len(pool) < max_total:
        needed = max_total - len(pool)
        added = _round_robin_fill([c[max_related:] for c in related_by_artist.values()], needed)
        d(f"**Rule 5, step B (relax related-artist cap):** added {len(added)} more.")

    if len(pool) < max_total:
        needed = max_total - len(pool)
        added_count = 0
        for t in leftover_own:
            if needed <= 0:
                break
            rk = getattr(t, 'ratingKey', None)
            if rk and rk not in pool:
                setattr(t, 'recommendation_type', f'{artist.title} (Artist Pick)')
                pool[rk] = t
                needed -= 1
                added_count += 1
        d(f"**Rule 5, step C (relax artist cap):** added {added_count} more, pool now {len(pool)} tracks.")

    final = list(pool.values())
    random.shuffle(final)
    return final[:max_total]
