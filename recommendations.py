"""Playlist-vibe recommendation engine: given an existing playlist, finds
sonically similar tracks and tracks from related artists."""

import random

from plex_helpers import get_sonic_match_percent, get_top_tracks_for_artist


def generate_playlist_vibe_recommendations(playlist, plex, debug_box, count=10):
    tracks = playlist.items()
    if not tracks:
        return []

    existing_keys = {t.ratingKey for t in tracks}
    raw_pool = []

    valid_tracks = [t for t in tracks if getattr(t, 'ratingKey', None) is not None]
    if not valid_tracks:
        return []

    # Pick seed tracks
    seeds = random.sample(valid_tracks, min(len(valid_tracks), 6))
    debug_box.write(f"**Selected Seeds:** {len(seeds)} tracks")

    for seed in seeds:
        seed_name = f"{getattr(seed, 'grandparentTitle', 'Unknown')} - {seed.title}"
        debug_box.markdown(f"**Seed:** `{seed_name}`")

        # 1. NATIVE SONIC ENGINE
        try:
            sonic_matches = seed.sonicallySimilar(limit=15)
            if sonic_matches:
                debug_box.write(f"└ ✅ Sonic Engine found {len(sonic_matches)} tracks.")
                for match in sonic_matches:
                    if getattr(match, 'ratingKey', None) and match.ratingKey not in existing_keys:
                        setattr(match, 'recommendation_type', 'Sonic Match')
                        setattr(match, 'match_percent', get_sonic_match_percent(match))
                        setattr(match, 'match_seed', seed_name)
                        raw_pool.append(match)
        except Exception as e:
            debug_box.write(f"└ ❌ Sonic Call Failed: `{str(e)}`")

        # 2. FIXED PLEXAMP CLONE LOGIC (Using .tag instead of .title)
        try:
            artist_key = getattr(seed, 'grandparentRatingKey', None)
            if artist_key:
                artist = plex.fetchItem(artist_key)

                if hasattr(artist, 'similar'):
                    similar_artists = artist.similar() if callable(artist.similar) else artist.similar
                else:
                    similar_artists = []

                if similar_artists:
                    debug_box.write(f"└ ✅ Found {len(similar_artists)} Similar Artists.")
                    chosen_artists = random.sample(similar_artists, min(len(similar_artists), 3))

                    for sim_artist in chosen_artists:
                        try:
                            # Pull the correct string identifier name (.tag) from the Similar metadata object
                            artist_name = getattr(sim_artist, 'tag', None)
                            if artist_name:
                                music_section = next(s for s in plex.library.sections() if s.type in ['artist', 'music'])
                                full_artist_matches = music_section.searchArtists(title=artist_name)

                                if full_artist_matches:
                                    real_artist = full_artist_matches[0]
                                    top_tracks = get_top_tracks_for_artist(real_artist, limit=4, per_album_sample=2)
                                    if top_tracks:
                                        debug_box.write(f"  └ ✅ Pulled {len(top_tracks)} tracks for {real_artist.title}.")
                                    else:
                                        debug_box.write(f"  └ ℹ️ No album tracks found for {real_artist.title}.")
                                    for top_track in top_tracks:
                                        if getattr(top_track, 'ratingKey', None) and top_track.ratingKey not in existing_keys:
                                            setattr(top_track, 'recommendation_type', f'Related Artist ({real_artist.title})')
                                            raw_pool.append(top_track)
                        except Exception as e_inner:
                            debug_box.write(f"  └ ❌ Artist Fetch Failed for {getattr(sim_artist, 'tag', 'Unknown')}: `{str(e_inner)}`")
                else:
                    debug_box.write("└ ℹ️ No similar artists mapped for this seed.")
        except Exception as e:
            debug_box.write(f"└ ❌ Plexamp Metadata Route Failed: `{str(e)}`")

    if not raw_pool:
        return []

    # De-duplicate pool items cleanly
    unique_dict = {}
    for track in raw_pool:
        rk = getattr(track, 'ratingKey', None)
        if not rk:
            continue
        if rk not in unique_dict:
            unique_dict[rk] = track
        elif getattr(track, 'recommendation_type', '') == 'Sonic Match':
            unique_dict[rk] = track

    # Split into the two categories so the final list is a balanced mix
    # rather than whatever ratio happened to survive de-duplication.
    sonic_tracks = [
        t for t in unique_dict.values()
        if getattr(t, 'recommendation_type', '') == 'Sonic Match'
    ]
    related_tracks = [
        t for t in unique_dict.values()
        if getattr(t, 'recommendation_type', '').startswith('Related Artist')
    ]
    random.shuffle(sonic_tracks)
    random.shuffle(related_tracks)

    half = count // 2  # 5 when count=10
    sonic_take = min(half, len(sonic_tracks))
    related_take = min(count - sonic_take, len(related_tracks))

    final_pool = sonic_tracks[:sonic_take] + related_tracks[:related_take]

    # If one category came up short, backfill from the other so we still
    # try to reach `count` total recommendations.
    still_needed = count - len(final_pool)
    if still_needed > 0:
        leftover = sonic_tracks[sonic_take:] + related_tracks[related_take:]
        random.shuffle(leftover)
        final_pool += leftover[:still_needed]

    random.shuffle(final_pool)
    return final_pool[:count]
