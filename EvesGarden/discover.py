"""Finding new music and previewing it without downloading.

Spotify supplies the catalogue -- names, artists, albums, artwork, length --
but not the audio: preview_url has returned None for newly registered apps
since late 2024. So the audio comes from the same YouTube source the
downloader already uses, resolved to a direct URL and streamed rather than
saved.

Resolving that URL is the slow part (a few seconds of searching), so results
are cached per track: previewing the same song twice only pays it once.

Searching is not the only way in. A search also answers with artists, and an
artist opens onto their releases and each release onto its tracks, so a whole
album can be taken in one go rather than found a song at a time.
"""

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

MAX_RESULTS = 25
ARTIST_RESULTS = 6
# Spotify caps every paged endpoint at 10 per call for app-only credentials --
# anything larger answers "400 Invalid limit" -- but offset paging still
# works, so everything here asks for small pages and walks them.
PAGE = 10


def dedupe_albums(albums):
    """Fold reissues and alternate editions into one row, newest first.

    Every catalogue lists the same record more than once -- once per market,
    once per reissue, once for the explicit cut -- so a discography is mostly
    duplicates until they are folded together. The earliest pressing wins:
    someone looking for Continuum wants the 2006 album, not the 2016 repress
    of it. Where two editions are equally old the fuller one wins, so a
    deluxe is never dropped in favour of a shorter cut of itself.
    """
    best = {}
    for album in albums:
        if not album:
            continue
        key = ((album.get("name") or "").strip().lower(),
               album.get("album_type") or "album")
        previous = best.get(key)
        if previous is None:
            best[key] = album
            continue
        year = album.get("year") or "9999"
        against = previous.get("year") or "9999"
        older, same_age = year < against, year == against
        fuller = (album.get("total_tracks") or 0) > (previous.get("total_tracks") or 0)
        if older or (same_age and fuller):
            best[key] = album

    # Newest first, but alphabetical inside a year rather than reversed along
    # with it -- two sorts because the second is stable.
    out = sorted(best.values(), key=lambda album: (album.get("name") or "").lower())
    out.sort(key=lambda album: album.get("year") or "", reverse=True)
    return out


def _as_artist(artist):
    """A Spotify artist object, flattened to what the UI needs."""
    images = artist.get("images") or []
    return {
        "source": "spotify",
        "id": artist["id"],
        "name": artist["name"],
        "url": (artist.get("external_urls") or {}).get("spotify", ""),
        "image_url": images[-1]["url"] if images else None,
        "genres": list(artist.get("genres") or []),
    }


def _as_album(album):
    """A Spotify album object, flattened to what the UI needs."""
    images = album.get("images") or []
    artists = [a["name"] for a in album.get("artists") or []]
    return {
        "source": "spotify",
        "id": album["id"],
        "name": album["name"],
        "artist": ", ".join(artists),
        "year": (album.get("release_date") or "")[:4],
        "album_type": album.get("album_group") or album.get("album_type") or "album",
        "total_tracks": album.get("total_tracks") or 0,
        "url": (album.get("external_urls") or {}).get("spotify", ""),
        "cover_url": images[-1]["url"] if images else None,
        "cover_large": images[0]["url"] if images else None,
    }


class Discover:
    def __init__(self, sp, ydl_opts_factory, score_fn, fallback=None):
        self.sp = sp
        # Used when Spotify is not configured, or does not answer. It needs no
        # credentials, so the app is useful before any setup is done.
        self.fallback = fallback
        self._ydl_opts = ydl_opts_factory
        self._score = score_fn
        self._stream_cache = {}
        self._art_cache = {}
        self._lock = threading.Lock()
        # Two pools on purpose. Resolving a stream means a YouTube search and
        # takes ~10s; artwork takes ~0.1s. Sharing one pool let four prefetch
        # jobs occupy every worker, so covers queued behind them and never
        # appeared.
        self._art_pool = ThreadPoolExecutor(max_workers=6,
                                            thread_name_prefix="art")
        self._resolve_pool = ThreadPoolExecutor(max_workers=2,
                                                thread_name_prefix="resolve")

    # ---------------------------------------------------------------- search

    def search(self, query, limit=MAX_RESULTS):
        """Tracks for a query, flattened to what the UI needs.

        Spotify first because its catalogue matching is better, but a failure
        here is not the end of it: an empty answer or an exception falls
        through to whatever provider needs no account.
        """
        if not query.strip():
            return []
        if self.sp is None:
            return self._fallback_search(query, limit)
        try:
            found = self._spotify_search(query, limit)
        except Exception:
            found = []
        return found or self._fallback_search(query, limit)

    def _fallback_search(self, query, limit):
        return self._fallback("search", query, limit=limit)

    def _fallback(self, method, *args, **kwargs):
        """Ask the keyless provider instead. Never raises.

        A provider is allowed not to implement everything, so a missing
        method is the same answer as an empty one: nothing found.
        """
        fn = getattr(self.fallback, method, None)
        if fn is None:
            return []
        try:
            return fn(*args, **kwargs)
        except Exception:
            return []

    def _spotify_search(self, query, limit=MAX_RESULTS):
        items, offset = [], 0
        while len(items) < limit:
            page = self.sp.search(q=query, limit=PAGE, offset=offset,
                                  type="track")["tracks"]["items"]
            if not page:
                break
            items.extend(page)
            offset += PAGE
            if len(page) < PAGE:
                break

        seen = set()
        out = []
        for track in items[:limit]:
            if track["id"] in seen:
                continue
            seen.add(track["id"])
            album = track.get("album") or {}
            images = album.get("images") or []
            out.append({
                "source": "spotify",
                "id": track["id"],
                "title": track["name"],
                "artists": [a["name"] for a in track.get("artists") or []],
                "artist": ", ".join(a["name"] for a in track.get("artists") or []),
                "album": album.get("name") or "",
                "year": (album.get("release_date") or "")[:4],
                "duration": (track.get("duration_ms") or 0) / 1000.0,
                "duration_ms": track.get("duration_ms") or 0,
                "url": track["external_urls"]["spotify"],
                "cover_url": images[-1]["url"] if images else None,
                "cover_large": images[0]["url"] if images else None,
            })
        return out

    # ------------------------------------------------------------- browsing

    def search_artists(self, query, limit=ARTIST_RESULTS):
        """Artists matching a query, best first."""
        if not query.strip():
            return []
        if self.sp is None:
            return self._fallback("search_artists", query, limit=limit)
        try:
            found = self._spotify_artists(query, limit)
        except Exception:
            found = []
        return found or self._fallback("search_artists", query, limit=limit)

    def artist_albums(self, artist):
        """Every release credited to an artist, newest first.

        Routed by the artist's own source rather than by what is configured:
        a Spotify id means nothing to Apple, and vice versa, so an artist
        found through one provider is always opened through that one.
        """
        if not artist:
            return []
        if artist.get("source") == "spotify" and self.sp is not None:
            try:
                found = self._spotify_artist_albums(artist["id"])
            except Exception:
                return []
        else:
            found = self._fallback("artist_albums", artist)
        return dedupe_albums(found)

    def album_tracks(self, album):
        """The tracks on one release, ready to preview or download."""
        if not album:
            return []
        if album.get("source") == "spotify" and self.sp is not None:
            try:
                return self._spotify_album_tracks(album)
            except Exception:
                return []
        return self._fallback("album_tracks", album)

    def _spotify_artists(self, query, limit):
        items = self.sp.search(q=query, limit=min(limit, PAGE),
                               type="artist")["artists"]["items"]
        return [_as_artist(a) for a in items if a][:limit]

    def _spotify_artist_albums(self, artist_id):
        page = self.sp.artist_albums(
            artist_id, album_type="album,single,compilation", limit=PAGE)
        items = list(page["items"])
        while page.get("next"):
            page = self.sp.next(page)
            items.extend(page["items"])

        return [_as_album(item) for item in items if item]

    def _spotify_album_tracks(self, album):
        page = self.sp.album_tracks(album["id"], limit=PAGE)
        items = list(page["items"])
        while page.get("next"):
            page = self.sp.next(page)
            items.extend(page["items"])

        out = []
        for track in items:
            if not track:
                continue
            artists = [a["name"] for a in track.get("artists") or []]
            out.append({
                "source": "spotify",
                "id": track["id"],
                "title": track["name"],
                "artists": artists,
                "artist": ", ".join(artists),
                # A track listed inside an album carries no album of its own
                # -- it is already in one -- so the name, year and artwork
                # come from the album that was asked for. Without this every
                # row in an opened album would show blank and download
                # untagged.
                "album": album.get("name") or "",
                "year": album.get("year") or "",
                "duration": (track.get("duration_ms") or 0) / 1000.0,
                "duration_ms": track.get("duration_ms") or 0,
                "url": (track.get("external_urls") or {}).get("spotify", ""),
                "cover_url": album.get("cover_url"),
                "cover_large": album.get("cover_large"),
                "track_number": track.get("track_number"),
                "disc_number": track.get("disc_number"),
                "album_artist": album.get("artist") or "",
            })
        return out

    # ------------------------------------------------------------- artwork

    def fetch_cover(self, url, size, callback):
        """Download and resize a cover, then hand it back on the UI thread."""
        if not url:
            return
        key = (url, size)
        with self._lock:
            cached = self._art_cache.get(key)
        if cached is not None:
            callback(cached)
            return

        def work():
            try:
                from PIL import Image
                response = requests.get(url, timeout=12)
                if response.status_code != 200:
                    return
                image = Image.open(io.BytesIO(response.content)).convert("RGB")
                image = image.resize((size, size), Image.Resampling.LANCZOS)
            except Exception:
                return
            with self._lock:
                if len(self._art_cache) > 200:
                    self._art_cache.clear()
                self._art_cache[key] = image
            callback(image)

        self._art_pool.submit(work)

    # -------------------------------------------------------------- preview

    def stream_url(self, track):
        """A direct audio URL for a Spotify track, resolved through YouTube.

        Blocking and slow the first time -- call it off the UI thread.
        """
        with self._lock:
            hit = self._stream_cache.get(track["id"])
        if hit:
            return hit

        import yt_dlp
        query = f"{', '.join(track['artists'])} - {track['title']}"
        opts = dict(self._ydl_opts(), format="bestaudio/best",
                    skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch5:{query} audio", download=False)

        entries = [e for e in ((info or {}).get("entries") or []) if e]
        if not entries:
            raise RuntimeError(f"Nothing found on YouTube for '{query}'")

        metadata = {"artists": track["artists"], "name": track["title"]}
        best = min(entries,
                   key=lambda e: self._score(e, track["duration_ms"], metadata))

        url = best.get("url")
        if not url:
            # extract_flat entries carry an id but no direct URL.
            with yt_dlp.YoutubeDL(opts) as ydl:
                full = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={best['id']}",
                    download=False)
            url = full.get("url") or (full.get("formats") or [{}])[-1].get("url")
        if not url:
            raise RuntimeError("Could not resolve a playable audio stream")

        resolved = {"url": url,
                    "duration": best.get("duration") or track["duration"],
                    "title": best.get("title") or track["title"]}
        with self._lock:
            self._stream_cache[track["id"]] = resolved
        return resolved

    def prefetch(self, track):
        """Warm the cache in the background so pressing play feels instant."""
        def work():
            try:
                self.stream_url(track)
            except Exception:
                pass
        self._resolve_pool.submit(work)

    def close(self):
        for pool in (self._art_pool, self._resolve_pool):
            pool.shutdown(wait=False)
