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
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

# The same reduction the library uses to decide two files are the same song:
# it already strips "(Deluxe Edition)", "- Remastered" and the rest, and it
# is already under test. An album title needs exactly that.
import spotify_auth
from library_index import normalise_title

MAX_RESULTS = 25
ARTIST_RESULTS = 6
# Spotify caps every paged endpoint at 10 per call for app-only credentials --
# anything larger answers "400 Invalid limit" -- but offset paging still
# works, so everything here asks for small pages and walks them.
PAGE = 10

# A resolved YouTube URL is signed and dies on a clock -- about six hours,
# and it says so in its own query string. Cached past that, pressing play got
# a 403 and, before start() learned to check, silence with nothing to say
# why. This is the fallback for a URL that carries no expiry of its own.
STREAM_TTL = 900.0


def _url_expiry(url):
    """When a signed media URL stops working, from the URL itself."""
    try:
        stamp = urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query).get("expire", [None])[0]
        if stamp:
            # A minute short of the real thing, so a stream cannot expire
            # between being handed over and being opened.
            return float(stamp) - 60.0
    except (ValueError, TypeError):
        pass
    return time.time() + STREAM_TTL


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


def _album_key(name):
    """A title reduced to what a match should care about."""
    return "".join(ch for ch in normalise_title(name) if ch.isalnum())


def album_matches(wanted, candidate):
    """Whether a search result is plausibly the record that was asked for.

    A catalogue always answers something. Searching for an album that is not
    in it returned whatever ranked first -- so a track whose album tag was a
    typo opened a stranger's single, presented as the record you were
    listening to. Better to say it could not be found.
    """
    a, b = _album_key(wanted), _album_key(candidate)
    if not a or not b:
        return False
    if a == b:
        return True
    if a not in b and b not in a:
        return False
    # Containment on its own is not enough in the shorter-candidate
    # direction: a two-word title sits inside plenty of unrelated ones, and
    # "zzz" is contained in "zzzzz not a real album". The overlap has to be
    # most of both names, which "Continuum" and "Continuum (Deluxe)" are.
    return min(len(a), len(b)) / float(max(len(a), len(b))) >= 0.5


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


def _pick_album(wanted, candidates):
    """The best of a catalogue's answers, or nothing if none of them fit.

    An exact title beats whatever ranked first -- searching for Continuum
    otherwise lands on a compilation that merely mentions it.
    """
    plausible = [a for a in candidates if a and album_matches(wanted, a["name"])]
    key = _album_key(wanted)
    for album in plausible:
        if _album_key(album["name"]) == key:
            return album
    return plausible[0] if plausible else None


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
        if self._spotify() is None:
            return self._fallback_search(query, limit)
        try:
            found = self._spotify_search(query, limit)
        except Exception as e:
            found = self._refused(e)
        return found or self._fallback_search(query, limit)

    def _spotify(self):
        """The Spotify client, unless it has just told us to go away.

        A rate limit lasts hours, so trying anyway costs a round trip per
        search to be refused again. Every question asked here can also be
        put to the keyless catalogue, so while a limit is in force that one
        is asked first rather than second.
        """
        if self.sp is None or spotify_auth.rate_limited():
            return None
        return self.sp

    def _refused(self, exc):
        """Note a refusal. Always returns nothing found, for the callers."""
        spotify_auth.note_refusal(exc)
        return []

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
        if self._spotify() is None:
            return self._fallback("search_artists", query, limit=limit)
        try:
            found = self._spotify_artists(query, limit)
        except Exception as e:
            found = self._refused(e)
        return found or self._fallback("search_artists", query, limit=limit)

    def artist_albums(self, artist):
        """Every release credited to an artist, newest first.

        Routed by the artist's own source rather than by what is configured:
        a Spotify id means nothing to Apple, and vice versa, so an artist
        found through one provider is always opened through that one.
        """
        if not artist:
            return []
        if artist.get("source") != "spotify":
            return dedupe_albums(self._fallback("artist_albums", artist))

        found = []
        if self._spotify() is not None:
            try:
                found = self._spotify_artist_albums(artist["id"])
            except Exception as e:
                found = self._refused(e)
        # Refused, rate-limited, or simply nothing: an artist can be found
        # by name on the keyless catalogue even though their Spotify id
        # means nothing to it, so this no longer dead-ends on an empty page.
        return dedupe_albums(found or self._keyless_artist_albums(artist))

    def _keyless_artist_albums(self, artist):
        """The same artist's releases, from the provider that needs no account."""
        name = artist.get("name") or ""
        if not name:
            return []
        for other in self._fallback("search_artists", name, limit=1) or []:
            return self._fallback("artist_albums", other)
        return []

    def find_album(self, name, artist=""):
        """The release a track came from, by name.

        The now playing bar knows an album's name and nothing else -- tags
        carry no ids -- so getting from "the song I am listening to" to the
        record it is on means looking it up again.
        """
        name = (name or "").strip()
        if not name:
            return None
        candidates = []
        if self._spotify() is not None:
            try:
                candidates = self._spotify_albums(name, artist)
            except Exception as e:
                candidates = self._refused(e)
        if not candidates:
            candidates = self._fallback("search_albums", name, artist) or []
        return _pick_album(name, candidates)

    def _spotify_albums(self, name, artist):
        query = 'album:"%s"' % name
        if artist:
            query += ' artist:"%s"' % artist
        items = self.sp.search(q=query, limit=PAGE,
                               type="album")["albums"]["items"]
        if not items:
            items = self.sp.search(q=name, limit=PAGE,
                                   type="album")["albums"]["items"]
        return [_as_album(a) for a in items if a]

    def album_tracks(self, album):
        """The tracks on one release, ready to preview or download."""
        if not album:
            return []
        if album.get("source") != "spotify":
            return self._fallback("album_tracks", album)

        # Routed by where the album came from, not by whether Spotify is up:
        # a Spotify id means nothing to the keyless catalogue, so handing it
        # one is the same as asking for nothing. While a rate limit is in
        # force there is no client to ask at all, and the release has to be
        # found again by name either way.
        refusal = None
        if self._spotify() is not None:
            try:
                found = self._spotify_album_tracks(album)
            except Exception as e:
                found, refusal = self._refused(e), e
            if found:
                return found
        found = self._keyless_album_tracks(album)
        if not found and refusal is not None:
            # Nothing from either provider, and Spotify said why. Saying
            # "this album has no tracks" would be a different, wrong
            # answer -- so the reason travels instead.
            raise refusal
        return found

    def _keyless_album_tracks(self, album):
        """The same release, read from the provider that needs no account.

        A Spotify id means nothing to Apple, so the release has to be found
        there by name first. Worth the extra lookup: this runs when Spotify
        has refused -- a rate limit, most often -- and the alternative is
        telling somebody their album has no tracks on it.
        """
        name = album.get("name") or ""
        if not name:
            return []
        match = _pick_album(name, self._fallback("search_albums", name,
                                                 album.get("artist") or "") or [])
        return self._fallback("album_tracks", match) if match else []

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
        if hit and hit["expires_at"] > time.time():
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
                    "title": best.get("title") or track["title"],
                    "expires_at": _url_expiry(url)}
        with self._lock:
            self._stream_cache[track["id"]] = resolved
        return resolved

    def forget(self, track):
        """Drop a cached stream, so the next play resolves it again.

        For when a URL turns out not to work after all: the signature can be
        rejected before it has formally expired, and re-resolving is the only
        way to find out.
        """
        try:
            with self._lock:
                self._stream_cache.pop(track["id"], None)
        except Exception:
            pass

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
