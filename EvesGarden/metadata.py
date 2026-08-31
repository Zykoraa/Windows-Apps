"""Track metadata for people who have not signed up for anything.

Spotify is the better catalogue and it is what this app was built on, but it
asks a lot before it does anything: register a developer application, copy a
client ID and secret, paste them into a setup screen. Until that is done the
downloader is inert, which is a poor first five minutes. It has also been
shrinking -- /v1/recommendations, artist top-tracks and related-artists have
all closed to app-only credentials since this was written.

Apple's iTunes Search endpoint needs no key, no account and no registration,
and returns everything the tagger actually writes: title, artist, album, album
artist, track and disc number, year, duration and artwork. So it stands in
whenever Spotify is not configured or not answering.

It does not replace Spotify. Only Spotify can reach the user's own liked songs
and playlists, and its catalogue matching is better -- so it stays the
preferred source when it is available. This is the floor, not the ceiling.
"""

import threading
import time

import requests

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_TIMEOUT = 12
# Apple asks for about twenty calls a minute; the cache is what keeps a user
# typing in the palette from getting anywhere near that.
CACHE_LIMIT = 120


def _artwork(url, size=600):
    """iTunes hands back a 100px thumbnail; the size is just in the path."""
    if not url:
        return None
    for token in ("100x100", "60x60", "30x30"):
        if token in url:
            return url.replace(token, "%dx%d" % (size, size))
    return url


class ITunesProvider:
    """Search and metadata from Apple's public search endpoint."""

    name = "iTunes"
    needs_setup = False

    def __init__(self, country="US", session=None):
        self.country = country
        self._session = session or requests.Session()
        self._cache = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- search

    def search(self, query, limit=25):
        """Tracks for a query, in the shape the rest of the app expects."""
        query = (query or "").strip()
        if not query:
            return []
        key = (query.lower(), int(limit))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit

        try:
            response = self._session.get(
                SEARCH_URL,
                params={"term": query, "media": "music", "entity": "song",
                        "limit": max(1, min(int(limit), 50)),
                        "country": self.country},
                timeout=LOOKUP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []

        out = [self._as_track(item) for item in payload.get("results") or []]
        out = [track for track in out if track]
        with self._lock:
            if len(self._cache) > CACHE_LIMIT:
                self._cache.clear()
            self._cache[key] = out
        return out

    def _as_track(self, item):
        if not item.get("trackName") or not item.get("artistName"):
            return None
        millis = item.get("trackTimeMillis") or 0
        artist = item["artistName"]
        return {
            "source": "itunes",
            "id": "itunes:%s" % item.get("trackId"),
            "title": item["trackName"],
            # iTunes gives one credit string rather than a list, and splitting
            # it on commas would mangle names that contain one.
            "artists": [artist],
            "artist": artist,
            "album": item.get("collectionName") or "",
            "year": (item.get("releaseDate") or "")[:4],
            "duration": millis / 1000.0,
            "duration_ms": millis,
            "url": item.get("trackViewUrl") or "",
            "cover_url": item.get("artworkUrl100"),
            "cover_large": _artwork(item.get("artworkUrl100")),
            "track_number": item.get("trackNumber"),
            "disc_number": item.get("discNumber"),
            "album_artist": item.get("collectionArtistName") or artist,
        }

    # -------------------------------------------------------------- tagging

    @staticmethod
    def track_info(track):
        """A track in this module's shape, as the tagger's metadata dict."""
        return {
            "name": track["title"],
            "artists": list(track.get("artists") or [track.get("artist", "")]),
            "album": track.get("album") or "",
            "album_artist": track.get("album_artist") or track.get("artist"),
            "cover_url": track.get("cover_large") or track.get("cover_url"),
            "track_number": track.get("track_number"),
            "disc_number": track.get("disc_number"),
            "release_date": track.get("year") or None,
            "duration_ms": track.get("duration_ms") or 0,
        }

    def lookup(self, query):
        """Best single match for a free-text query, ready to tag with."""
        found = self.search(query, limit=1)
        return self.track_info(found[0]) if found else None


def as_spotify_track(track):
    """Reshape a Spotify search item into this module's shape."""
    album = track.get("album") or {}
    images = album.get("images") or []
    artists = [a["name"] for a in track.get("artists") or []]
    return {
        "source": "spotify",
        "id": track["id"],
        "title": track["name"],
        "artists": artists,
        "artist": ", ".join(artists),
        "album": album.get("name") or "",
        "year": (album.get("release_date") or "")[:4],
        "duration": (track.get("duration_ms") or 0) / 1000.0,
        "duration_ms": track.get("duration_ms") or 0,
        "url": (track.get("external_urls") or {}).get("spotify", ""),
        "cover_url": images[-1]["url"] if images else None,
        "cover_large": images[0]["url"] if images else None,
    }
