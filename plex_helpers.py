"""Small, self-contained helpers for pulling data out of plexapi objects.
No Streamlit UI code lives here — these are pure functions/utilities reused
across the recommendation, artist-mix, and clustering pipelines."""

import random


def get_stream_url(track, plex_url, plex_token):
    """
    Builds a direct, authenticated HTTP URL to the track's underlying media
    file part. We deliberately use the raw part path (/library/parts/...)
    rather than track.getStreamURL(), because getStreamURL() spins up a
    Plex "universal transcode" session that expects the client to keep
    polling it — a plain <audio> tag doesn't do that, so Plex closes the
    connection partway through and the browser throws
    ERR_INCOMPLETE_CHUNKED_ENCODING. Hitting the file part directly streams
    the original file with normal HTTP range support instead.
    Returns None if the track has no accessible media part.
    """
    try:
        part = track.media[0].parts[0]
        return f"{plex_url.rstrip('/')}{part.key}?X-Plex-Token={plex_token}"
    except (AttributeError, IndexError, KeyError):
        return None


def get_sonic_match_percent(track):
    """
    plexapi doesn't expose the sonic-similarity score as a named attribute,
    but Plex's hub response includes it in the raw element data (varies by
    server version: 'score' or 'distance'). Score is already a 0-1 similarity
    value; distance is the inverse (0 = identical), so we normalize both to
    a 0-100 "match %". Returns None if neither field is present so callers
    can gracefully omit the percentage rather than show a fake number.
    """
    raw = getattr(track, '_data', None)
    if raw is None:
        return None
    try:
        attrib = raw.attrib if hasattr(raw, 'attrib') else raw
        score = attrib.get('score')
        if score is not None:
            score = float(score)
            # Some servers report 0-1, others 0-100 — normalize either way.
            pct = score * 100 if score <= 1 else score
            return max(0, min(100, round(pct)))
        distance = attrib.get('distance')
        if distance is not None:
            distance = float(distance)
            pct = (1 - distance) * 100 if distance <= 1 else (100 - distance)
            return max(0, min(100, round(pct)))
    except (TypeError, ValueError):
        return None
    return None


def get_top_tracks_for_artist(real_artist, limit=4, per_album_sample=2):
    """
    plexapi's Artist has no topTracks() method, so we approximate "top tracks"
    ourselves:
      1. Walk the artist's albums and pull tracks from each.
      2. Rank all collected tracks by viewCount (play count) descending, since
         that's the only popularity signal Plex actually exposes.
      3. If nothing has been played (all viewCount == 0, common on fresh
         libraries), fall back to a random sample of `per_album_sample`
         tracks per album instead of just taking the first N in album order.
    """
    candidate_tracks = []
    try:
        albums = real_artist.albums()
    except Exception:
        albums = []

    for album in albums:
        try:
            album_tracks = album.tracks()
        except Exception:
            continue
        if not album_tracks:
            continue
        # Take a random sample per album (not just the first N) so the
        # fallback pool isn't biased toward track-1-of-every-album.
        sample = random.sample(album_tracks, min(len(album_tracks), per_album_sample))
        candidate_tracks.extend(sample)

    if not candidate_tracks:
        return []

    ranked = sorted(
        candidate_tracks,
        key=lambda t: getattr(t, 'viewCount', 0) or 0,
        reverse=True
    )

    top_view_count = getattr(ranked[0], 'viewCount', 0) or 0

    if top_view_count > 0:
        # Real play-count signal exists — use it.
        return ranked[:limit]
    else:
        # No play data at all — a viewCount-based ranking would be meaningless
        # (everything ties at 0), so just shuffle and take a random slice.
        random.shuffle(candidate_tracks)
        return candidate_tracks[:limit]
