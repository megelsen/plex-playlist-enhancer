"""Finding and writing Plex playlists.

Playlists are found by their stored ratingKey first, so renaming one in
Plex doesn't make the script create a second playlist; the title is only
the fallback (first run, or the stored playlist was deleted).
"""


def _is_playlist(item):
    return getattr(item, "TYPE", None) == "playlist" or getattr(item, "type", None) == "playlist"


def find_playlist(plex, title, rating_key=None):
    if rating_key:
        try:
            item = plex.fetchItem(int(rating_key))
            if _is_playlist(item):
                return item
        except Exception:
            pass
    return next((p for p in plex.playlists() if p.title == title), None)


def playlist_keys(plex, title, rating_key=None):
    """ratingKeys (str) currently in a playlist; empty set if it doesn't exist."""
    p = find_playlist(plex, title, rating_key)
    if p is None:
        return set()
    try:
        return {str(i.ratingKey) for i in p.items()}
    except Exception:
        return set()


def replace_playlist(plex, title, items, rating_key=None):
    """Make the playlist hold exactly `items`, in order; create it if needed.
    Returns the playlist."""
    existing = find_playlist(plex, title, rating_key)
    if existing:
        existing.removeItems(existing.items())
        existing.addItems(items)
        return existing
    return plex.createPlaylist(title, items=items)


def set_summary(playlist, text, log=print):
    try:
        playlist.editSummary(text)
    except Exception as e:
        log(f"  warning: couldn't set playlist summary: {e}")
