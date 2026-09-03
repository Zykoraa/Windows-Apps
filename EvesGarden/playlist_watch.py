"""Noticing when a playlist you imported starts losing tracks.

A Spotify playlist decays. Licensing lapses, a rights holder pulls a
catalogue, an album is replaced by a remaster with different ids -- and the
track goes grey, or disappears from the listing entirely. Nothing tells you,
and once it has gone there is no longer anything to say what it was: the
playlist is simply shorter than you remember.

This app is in an unusual position to fix that. It imported the playlist, so
it wrote down what was in it, and it downloaded the audio, so it still has
the track. All that is missing is somebody looking, which is what this is.

The comparison is pure -- it takes what was recorded and what Spotify says
now, and returns the difference -- so it can be tested without an account.
"""

import collections

# Still listed, but Spotify will not play it where you are.
UNAVAILABLE = "unavailable"
# Not in the playlist at all any more.
REMOVED = "removed"

Changes = collections.namedtuple(
    "Changes", "gone returned added still_here")


def compare(recorded, current):
    """What has changed since this playlist was last read.

    `recorded` is {track_url: row} from the snapshot; `current` is the track
    metadata Spotify returns now, each carrying `spotify_url` and, when it
    said so, `is_playable`.

    Returns the tracks that have gone (each with a reason), the ones that had
    gone and are back, the ones that are new, and the ones still present and
    playable.
    """
    by_url = {}
    for track in current:
        url = track.get("spotify_url")
        if url:
            by_url[url] = track

    gone, still_here = [], []
    for url, track in by_url.items():
        # is_playable is only sent when Spotify has an opinion; absent means
        # no opinion, which is not the same as unplayable.
        if track.get("is_playable") is False:
            gone.append((url, track, UNAVAILABLE))
        else:
            still_here.append((url, track))

    for url, row in recorded.items():
        if url not in by_url:
            gone.append((url, None, REMOVED))

    # A track counts as newly gone only if the snapshot did not already know.
    fresh = [(url, track, reason) for url, track, reason in gone
             if not (recorded.get(url) or {}).get("gone_at")]

    returned = [url for url, _track in still_here
                if (recorded.get(url) or {}).get("gone_at")]
    added = [track for url, track in still_here if url not in recorded]

    return Changes(gone=fresh, returned=returned, added=added,
                   still_here=still_here)


def entries_for(tracks, paths=()):
    """Snapshot rows for a playlist's tracks, paired with their local files."""
    out = []
    for index, track in enumerate(tracks):
        url = track.get("spotify_url")
        if not url:
            continue
        out.append({
            "url": url,
            "title": track.get("name"),
            "artist": ", ".join(track.get("artists") or []),
            "path": paths[index] if index < len(paths) else None,
        })
    return out


def summarise(losses):
    """One line for a report: how many went, and how many are still here.

    The second number is the whole point of this feature and the only part
    of it that is good news.
    """
    if not losses:
        return ""
    kept = sum(1 for row in losses if row.get("kept"))
    what = "track" if len(losses) == 1 else "tracks"
    if kept == len(losses):
        return "%d %s gone from Spotify -- you have all of them." % (
            len(losses), what)
    if kept:
        return "%d %s gone from Spotify -- you have %d of them." % (
            len(losses), what, kept)
    return "%d %s gone from Spotify." % (len(losses), what)
