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

import audio_files
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
        "readable": True,
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
                "total": _total(item),
                "mine": owner == mine,
                # Spotify stopped serving the contents of playlists you only
                # follow: the endpoint answers 403 for anything you neither
                # own nor collaborate on, however public it is.
                "readable": owner == mine or bool(item.get("collaborative")),
                "liked": False,
            })
        results = user_sp.next(results) if results.get("next") else None

    return out


def _total(playlist):
    """How many tracks a playlist holds.

    Spotify renamed this block from `tracks` to `items` and stopped sending
    the old one at all, so every playlist reported nothing. The old name is
    still read, because a rename in one direction can happen in the other.
    """
    for key in ("items", "tracks"):
        block = playlist.get(key)
        if isinstance(block, dict) and block.get("total") is not None:
            return block["total"]
    return 0


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
        items = _playlist_items(user_sp, playlist["id"])

    tracks = []
    for item in items:
        track = _track_of(item)
        # Local files added from someone's own machine, and podcast episodes,
        # both arrive here with nothing to download.
        if not track or track.get("is_local") or track.get("type") != "track":
            continue
        meta = as_metadata(track)
        if meta:
            tracks.append(meta)
    return tracks


def _playlist_items(user_sp, playlist_id):
    """Page one playlist's contents.

    Spotify moved this from /playlists/{id}/tracks to /playlists/{id}/items,
    and the old path now answers 403 for every playlist -- including ones you
    own. spotipy's playlist_items() still calls the old path, so this asks
    for the new one directly. Only a missing method falls back; an HTTP error
    is left to travel, because a 403 here means something the caller has to
    tell the user about rather than paper over.
    """
    try:
        page = user_sp._get("playlists/%s/items" % playlist_id, limit=100)
    except AttributeError:
        page = user_sp.playlist_items(playlist_id, limit=100,
                                      additional_types=("track",))
    return _pages(user_sp, page)


def _track_of(item):
    """The track on one playlist entry.

    It used to be `track`. On the /items endpoint it is `item`. Liked Songs
    comes from a different endpoint and still says `track`, so both names
    have to work.
    """
    if not item:
        return None
    return item.get("item") or item.get("track")


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
    without having to match the file all over again. Only the extension is a
    guess: a download keeps whatever the source was, so it may land as .opus
    or .m4a instead. resolve() is what turns this back into the real file.
    """
    return _stem(meta, library_dir) + audio_files.EXTENSIONS[0]


def _stem(meta, library_dir):
    import downloader
    label = "%s - %s" % (", ".join(meta["artists"]), meta["name"])
    return os.path.join(library_dir, downloader.sanitize_filename(label))


def resolve(path):
    """The file actually on disk for a reserved slot, or None.

    A slot is reserved under the name the download will have, but not under
    the extension: that is whatever the source turned out to be. So the
    reservation is really a stem, and this is what reads it back.
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    stem = os.path.splitext(path)[0]
    for ext in audio_files.EXTENSIONS:
        candidate = stem + ext
        if os.path.exists(candidate):
            return candidate
    return None


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
            # The file can be on disk and still not match: its tags may
            # disagree with its name, the index may not have caught up, or it
            # may be sitting under a different extension than the one guessed
            # above. Queueing it would download nothing -- process_track
            # skips what already exists -- but it would tell the user a
            # download was needed, and the count is the only thing they see.
            found = resolve(path)
            if found:
                path = found
            elif path not in seen:
                missing.append(meta)
        # A playlist can list the same song twice; a playlist here cannot
        # hold it twice, so the repeat is dropped rather than shifting every
        # position after it.
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths, missing
