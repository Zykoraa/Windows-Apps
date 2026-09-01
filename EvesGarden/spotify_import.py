"""Bringing a Spotify account's playlists across as local ones.

Pasting a playlist link already downloaded its tracks, but that was the whole
of it: the songs landed in the library as loose files and the playlist itself
-- its name, its order, the fact that those tracks belong together -- was
thrown away at the door. This keeps it. Each playlist chosen here becomes a
real playlist in the library, holding the tracks already owned immediately
and taking the rest as they finish downloading.

Only list_playlists() and read_playlist() talk to Spotify. Everything below
them is pure, so the matching -- the part that can quietly put the wrong
recording in a playlist -- is testable without an account.
"""

import os

import library_index

# Liked Songs is not a playlist. It lives behind /me/tracks and has no id, so
# it needs a stand-in to travel through the same code as the real ones.
LIKED_ID = "liked-songs"
LIKED_NAME = "Liked Songs"

PAGE = 50

# Two recordings that normalise to the same artist and title -- a studio cut
# and a live one, say -- are told apart by length. Anything within this many
# seconds is treated as the same song.
DURATION_SLACK = 12.0


# ------------------------------------------------------------ reading

def list_playlists(user_sp):
    """Every playlist on the account, with Liked Songs at the front.

    Returns dicts, not Spotify objects, so the caller never has to know the
    shape of the API response.
    """
    if user_sp is None:
        return []

    me = user_sp.current_user()
    mine = (me or {}).get("id")

    out = [{
        "id": LIKED_ID,
        "name": LIKED_NAME,
        "owner": (me or {}).get("display_name") or mine or "you",
        "total": _liked_total(user_sp),
        "mine": True,
        "liked": True,
    }]

    results = user_sp.current_user_playlists(limit=PAGE)
    while results:
        for item in results.get("items") or []:
            # A collaborative playlist that has since been deleted comes back
            # as a null item rather than being left out.
            if not item:
                continue
            owner = (item.get("owner") or {}).get("id")
            out.append({
                "id": item["id"],
                "name": item.get("name") or "Untitled",
                "owner": ((item.get("owner") or {}).get("display_name")
                          or owner or ""),
                "total": (item.get("tracks") or {}).get("total") or 0,
                "mine": owner == mine,
                "liked": False,
            })
        results = user_sp.next(results) if results.get("next") else None

    return out


def _liked_total(user_sp):
    try:
        return user_sp.current_user_saved_tracks(limit=1).get("total") or 0
    except Exception:
        return 0


def read_playlist(user_sp, playlist):
    """Ordered track metadata for one playlist, ready for process_track."""
    if playlist.get("liked"):
        items = _pages(user_sp, user_sp.current_user_saved_tracks(limit=PAGE))
    else:
        items = _pages(user_sp, user_sp.playlist_items(
            playlist["id"], limit=100, additional_types=("track",)))

    tracks = []
    for item in items:
        track = (item or {}).get("track")
        # Local files added from someone's own machine, and podcast episodes,
        # both arrive here with nothing to download.
        if not track or track.get("is_local") or track.get("type") != "track":
            continue
        meta = as_metadata(track)
        if meta:
            tracks.append(meta)
    return tracks


def _pages(user_sp, results):
    items = list(results.get("items") or [])
    while results.get("next"):
        results = user_sp.next(results)
        items.extend(results.get("items") or [])
    return items


def as_metadata(track):
    """A Spotify track object in the shape process_track already accepts.

    Reading a playlist hands back every field the downloader would otherwise
    fetch one track at a time, so an import of 300 songs costs the API calls
    to page the playlist and nothing more.
    """
    artists = [a["name"] for a in (track.get("artists") or []) if a.get("name")]
    if not artists or not track.get("name"):
        return None
    album = track.get("album") or {}
    images = album.get("images") or []
    return {
        "name": track["name"],
        "artists": artists,
        "album": album.get("name") or "",
        "album_artist": ((album.get("artists") or [{"name": artists[0]}])[0]
                         .get("name") or artists[0]),
        "cover_url": images[0]["url"] if images else None,
        "track_number": track.get("track_number"),
        "disc_number": track.get("disc_number"),
        "release_date": (album.get("release_date") or "")[:4] or None,
        "duration_ms": track.get("duration_ms"),
        "spotify_url": (track.get("external_urls") or {}).get("spotify"),
    }


# ------------------------------------------------------------ matching

def fingerprint(meta):
    """The key a Spotify track and a local file are compared on."""
    artists = meta.get("artists") or [""]
    return (library_index.normalise_artist(artists[0]),
            library_index.normalise_title(meta.get("name")))


def match(meta, owned):
    """The local file for this track, or None.

    `owned` is {fingerprint: [(path, duration_seconds), ...]}. When more than
    one file shares a fingerprint the closest length wins, which is what keeps
    a two-minute radio edit out of the slot meant for the album version.
    """
    key = fingerprint(meta)
    if not key[1]:
        return None
    candidates = owned.get(key)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]

    want = (meta.get("duration_ms") or 0) / 1000.0
    if not want:
        return candidates[0][0]
    path, gap = min(
        ((p, abs((d or 0) - want)) for p, d in candidates),
        key=lambda pair: pair[1])
    return path if gap <= DURATION_SLACK else candidates[0][0]


def predicted_path(meta, library_dir):
    """Where process_track will put this track once it downloads.

    The filename is derived from the metadata, not discovered afterwards, so
    a playlist slot can be reserved before the download starts and filled in
    without having to match the file all over again.
    """
    import downloader
    label = "%s - %s" % (", ".join(meta["artists"]), meta["name"])
    return os.path.join(library_dir,
                        downloader.sanitize_filename(label) + ".mp3")


def plan(tracks, owned, library_dir):
    """Work out, for a playlist's tracks, what is here and what is not.

    Returns (paths, missing): `paths` is every track in playlist order as the
    path it will eventually live at, and `missing` is the subset that has to
    be downloaded first. Keeping the order in one list is what lets the
    playlist be rebuilt in the right sequence when the batch finishes, rather
    than ending up sorted by whichever download happened to complete first.
    """
    paths, missing, seen = [], [], set()
    for meta in tracks:
        path = match(meta, owned)
        if path is None:
            path = predicted_path(meta, library_dir)
            if path not in seen:
                missing.append(meta)
        # A playlist can list the same song twice; a playlist here cannot
        # hold it twice, so the repeat is dropped rather than shifting every
        # position after it.
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths, missing
