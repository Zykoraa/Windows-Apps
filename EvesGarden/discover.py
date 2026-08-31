"""Finding new music and previewing it without downloading.

Spotify supplies the catalogue -- names, artists, albums, artwork, length --
but not the audio: preview_url has returned None for newly registered apps
since late 2024. So the audio comes from the same YouTube source the
downloader already uses, resolved to a direct URL and streamed rather than
saved.

Resolving that URL is the slow part (a few seconds of searching), so results
are cached per track: previewing the same song twice only pays it once.
"""

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

MAX_RESULTS = 25


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
        if self.fallback is None:
            return []
        try:
            return self.fallback.search(query, limit=limit)
        except Exception:
            return []

    def _spotify_search(self, query, limit=MAX_RESULTS):
        # Spotify caps this endpoint at 10 per call for app-only credentials
        # -- anything larger answers "400 Invalid limit" -- but offset paging
        # still works, so ask for several small pages.
        items, offset = [], 0
        while len(items) < limit:
            page = self.sp.search(q=query, limit=10, offset=offset,
                                  type="track")["tracks"]["items"]
            if not page:
                break
            items.extend(page)
            offset += 10
            if len(page) < 10:
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
