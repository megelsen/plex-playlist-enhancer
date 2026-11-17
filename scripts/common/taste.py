"""Taste profile from listening history: artist affinity, style profile,
seed tracks, and sonic / similar-artist scores around them.

All returned scores are unitless 0..1 unless stated. Time is converted to
days as (now_ts - unix_seconds) / 86400; half-lives are in days.
"""

from collections import defaultdict, namedtuple

from .library import normalize
from .tautulli import is_qualifying_play

Taste = namedtuple("Taste", "artist_aff style seeds recent album_aff plays_tautulli")
# artist_aff     {artist_key: 0..1}  decayed plays (half-life taste_half_life_days), sqrt-flattened
# style          {tag: weight}       decayed plays per album style/genre tag (unnormalised)
# seeds          {rk: 0..1}          recent seeds (normalised) + all-time favourites (0.6)
# recent         {rk: 0..1}          recent seeds only
# album_aff      {album_key: w}      decayed plays per album (half-life taste_half_life_days)
# plays_tautulli {rk: int}           qualifying Tautulli plays per track, whole history read


def build_taste(tracks, albums, history, now_ts, *, min_listen_seconds, taste_half_life_days,
                seed_days, seed_half_life_days, seed_recent, seed_alltime):
    """Artist affinity, style profile and seed tracks from listening history."""
    artist_aff = defaultdict(float)
    style = defaultdict(float)
    recent = defaultdict(float)
    album_aff = defaultdict(float)
    plays_tautulli = defaultdict(int)

    for row in history:
        if not is_qualifying_play(row, min_listen_seconds):
            continue
        rk = str(row.get("rating_key"))
        t = tracks.get(rk)
        if not t:
            continue
        plays_tautulli[rk] += 1
        days_ago = max(0.0, (now_ts - int(row.get("date", now_ts))) / 86400)
        w = 0.5 ** (days_ago / taste_half_life_days)
        artist_aff[t["artist_key"]] += w
        album_aff[t["album_key"]] += w
        for tag in albums.get(t["album_key"], {}).get("tags", ()):
            style[tag] += w
        if days_ago <= seed_days:
            recent[rk] += 0.5 ** (days_ago / seed_half_life_days)

    # Plex lifetime counts (views_plex) also cover plays from before Tautulli
    # existed. They include every Tautulli-era play too, so only the part
    # Tautulli doesn't know about is added (0.1 per play, undecayed), or those
    # plays would count twice. Tautulli counts a play at >= 30 s and Plex at
    # ~90%, so plays_tautulli is usually >= the Plex count for the same period
    # and the remainder is a lower bound for the pre-Tautulli plays.
    for t in tracks.values():
        pre_tautulli = max(0, t["views"] - plays_tautulli.get(t["rk"], 0))
        if pre_tautulli:
            artist_aff[t["artist_key"]] += 0.1 * pre_tautulli
            for tag in albums.get(t["album_key"], {}).get("tags", ()):
                style[tag] += 0.1 * pre_tautulli

    seeds = {}
    recent_norm = normalize(recent)
    for rk, s in sorted(recent_norm.items(), key=lambda x: x[1], reverse=True)[:seed_recent]:
        seeds[rk] = s
    recent_seeds = dict(seeds)
    alltime = sorted((t for t in tracks.values() if t["views"]),
                     key=lambda t: t["views"], reverse=True)
    for t in [t for t in alltime if t["rk"] not in seeds][:seed_alltime]:
        seeds[t["rk"]] = 0.6

    return Taste(normalize(artist_aff, flatten=True), style, seeds, recent_seeds,
                 dict(album_aff), dict(plays_tautulli))


def sonic_neighbours(plex, seed_keys, limit, log=print):
    """{seed rk: [neighbour rk, ...]} in Plex's sonic order (closest first);
    one sonicallySimilar call per seed."""
    out = {}
    for rk in seed_keys:
        try:
            similar = plex.fetchItem(int(rk)).sonicallySimilar(limit=limit)
        except Exception as e:
            log(f"  warning: sonic lookup failed for {rk}: {e}")
            continue
        out[rk] = [str(item.ratingKey) for item in similar]
    return out


def sonic_scores(neighbours, seeds, limit):
    """0..1 closeness to the seeds: max over seeds of seed weight x
    (1 - rank / limit), +0.1 for every extra seed a track sounds like."""
    scores = defaultdict(float)
    hits = defaultdict(int)
    for rk, weight in seeds.items():
        for rank, key in enumerate(neighbours.get(rk, ())):
            scores[key] = max(scores[key], weight * (1 - rank / limit))
            hits[key] += 1
    return {k: min(1.0, s + 0.1 * (hits[k] - 1)) for k, s in scores.items()}


def similar_artist_scores(similar, artist_aff, seed_artists):
    """0..1 link of each artist to the top 3 x seed_artists you play: the
    seed's affinity for a direct Plex similar-artist link, half of it for a
    second-hand one."""
    top = sorted(artist_aff.items(), key=lambda x: x[1], reverse=True)[:seed_artists * 3]
    link = defaultdict(float)
    for artist, aff in top:
        for s in similar.get(artist, ()):
            link[s] = max(link[s], aff)
            for s2 in similar.get(s, ()):
                link[s2] = max(link[s2], 0.5 * aff)
    return link
