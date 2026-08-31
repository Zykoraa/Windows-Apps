import argparse
import os
import re
import sys
import time
import requests
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, APIC, error


# Windows reserved device names -- a file called "CON.mp3" cannot be created.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class SpotifyAuthError(RuntimeError):
    """Raised when Spotify API credentials are missing or rejected."""


class DownloadError(RuntimeError):
    """Raised when a track could not be downloaded or converted."""


import credentials

# Populate os.environ from wherever credentials live: the portable file next
# to the exe first, then AppData.
credentials.load()


def get_config_dir():
    """Writable directory for logs, the index and the token cache."""
    if getattr(sys, "frozen", False):
        path = credentials.user_dir()
    else:
        path = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(path, exist_ok=True)
    return path


def setup_spotify():
    """Build a Spotify client from the configured credentials.

    Nothing is hardcoded: a secret compiled into the binary is recoverable in
    plaintext from the PyInstaller archive by anyone holding the file.
    """
    client_id = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret or client_id == "your_client_id_here":
        raise SpotifyAuthError(
            "Spotify credentials are not set up yet.\n\n"
            "Create a free app at https://developer.spotify.com/dashboard, "
            "then paste its Client ID and Client Secret into the setup screen."
        )

    # spotipy caches its token in ./.cache by default, which fails when the
    # app runs from a read-only directory. Pin it somewhere writable.
    cache_handler = spotipy.cache_handler.CacheFileHandler(
        cache_path=os.path.join(get_config_dir(), ".spotify-token-cache")
    )
    return spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
            cache_handler=cache_handler,
        ),
        requests_timeout=15,
        retries=3,
    )


def get_spotify_track_info(sp, track_url):
    track_info = sp.track(track_url)
    artists = [artist["name"] for artist in track_info["artists"]]
    album = track_info["album"]
    images = album.get("images") or []

    return {
        "name": track_info["name"],
        "artists": artists,
        "album": album["name"],
        "album_artist": (album.get("artists") or [{"name": artists[0]}])[0]["name"],
        "cover_url": images[0]["url"] if images else None,
        "track_number": track_info.get("track_number"),
        "disc_number": track_info.get("disc_number"),
        "release_date": (album.get("release_date") or "")[:4] or None,
        "duration_ms": track_info.get("duration_ms"),
    }


def search_spotify_track(sp, query):
    results = sp.search(q=query, limit=1, type="track")
    tracks = results["tracks"]["items"]
    if tracks:
        return tracks[0]["external_urls"]["spotify"]
    return None


def search_spotify_artist(sp, query):
    results = sp.search(q=query, limit=5, type="artist")
    return results["artists"]["items"]


def get_artist_albums(sp, artist_id):
    # Spotify caps this endpoint at 10 per page for app-only credentials --
    # anything larger comes back as "400 Invalid limit". The pagination loop
    # below still walks the whole discography.
    results = sp.artist_albums(artist_id, album_type="album,single", limit=10)
    albums = results["items"]
    while results["next"]:
        results = sp.next(results)
        albums.extend(results["items"])

    # deduplicate by name
    seen = set()
    unique_albums = []
    for album in albums:
        name = album["name"]
        if name not in seen:
            seen.add(name)
            unique_albums.append(album)
    return unique_albums


def pick_suggestions(seed, candidates, limit=5):
    """Choose suggestions from a candidate pool, dropping repeats.

    Kept pure and separate from the fetching so the interesting half -- not
    offering somebody the remaster of the song they are already listening to
    -- can be tested without a network.
    """
    from library_index import normalise_title

    seen_ids = {seed.get("id")}
    seen_titles = {normalise_title(seed.get("name") or "")}
    out = []
    for track in candidates or []:
        track_id = track.get("id")
        if not track_id or track_id in seen_ids:
            continue
        if not (track.get("external_urls") or {}).get("spotify"):
            continue                     # nothing to open or download
        title = normalise_title(track.get("name") or "")
        if not title or title in seen_titles:
            continue                     # a live cut or remaster of one above
        seen_ids.add(track_id)
        seen_titles.add(title)
        out.append(track)
        if len(out) >= limit:
            break
    return out


def get_related_tracks(sp, seed_track_id, limit=5):
    """More by the artist you are listening to.

    This was three fallbacks deep and every one of them has since closed.
    Spotify retired /v1/recommendations for newly registered apps -- it
    answers 404 -- and in late 2024 moved artist top-tracks and
    related-artists behind extended quota, so both answer 403 for the
    app-only credentials this uses. The whole function was dead, and because
    it runs on a worker whose exceptions are swallowed, it failed silently.

    What is left open is /search (including its artist: field),
    /artists/{id}/albums and /albums/{id}/tracks, so the suggestions are now
    honestly what those can support: more of the same artist. The caller
    labels them that way rather than calling them recommendations.
    """
    seed = sp.track(seed_track_id)
    artist = (seed.get("artists") or [{}])[0]
    candidates = []

    name = (artist.get("name") or "").replace('"', "")
    if name:
        try:
            candidates.extend(sp.search(q='artist:"%s"' % name, type="track",
                                        limit=10)["tracks"]["items"])
        except Exception:
            pass

    # Top up from the back catalogue: search alone leans hard on whatever is
    # popular right now, so it tends to return the same handful of singles.
    if artist.get("id") and len(candidates) < limit * 4:
        try:
            albums = sp.artist_albums(artist["id"], album_type="album",
                                      limit=4)["items"]
            for album in albums:
                candidates.extend(
                    sp.album_tracks(album["id"], limit=10)["items"])
                if len(candidates) >= limit * 6:
                    break
        except Exception:
            pass

    return pick_suggestions(seed, candidates, limit)


# Spotify's own editorial and algorithmic playlists -- Discover Weekly, Daily
# Mix, Release Radar, Today's Top Hits and the rest -- all sit under this
# prefix. Spotify closed them to the API in late 2024; they 404 for everyone,
# including the user they were generated for.
SPOTIFY_OWNED_PREFIX = "37i9dQZ"


def is_liked_songs(url):
    """Liked Songs is not a playlist; it lives behind /me/tracks."""
    return "collection/tracks" in (url or "")


def get_liked_songs(user_sp):
    """Every track in Liked Songs, newest first."""
    if user_sp is None:
        raise SpotifyAuthError(
            "Reading your Liked Songs needs you to sign in to Spotify.\n\n"
            "Use the Sign in to Spotify button, then paste the link again."
        )
    try:
        results = user_sp.current_user_saved_tracks(limit=50)
    except spotipy.SpotifyException as e:
        if e.http_status == 403:
            raise SpotifyAuthError(
                "Your saved sign-in predates Liked Songs support.\n\n"
                "Press Signed in to sign out, then sign in again to grant "
                "the extra permission."
            ) from e
        raise

    items = results["items"]
    while results["next"]:
        results = user_sp.next(results)
        items.extend(results["items"])
    return [
        i["track"]["external_urls"]["spotify"]
        for i in items
        if i.get("track") and i["track"].get("external_urls")
    ]


def get_spotify_playlist_tracks(sp, playlist_url, user_sp=None):
    """Read a playlist's tracks.

    Spotify now requires user authentication for every playlist, public ones
    included, so this prefers a signed-in client when one is available.
    """
    if is_liked_songs(playlist_url):
        return get_liked_songs(user_sp)

    client = user_sp or sp
    try:
        results = client.playlist_items(playlist_url, additional_types=("track",))
    except spotipy.SpotifyException as e:
        if e.http_status in (401, 403, 404):
            if SPOTIFY_OWNED_PREFIX in (playlist_url or ""):
                raise SpotifyAuthError(
                    "Spotify's own playlists can't be read by any app.\n\n"
                    "Discover Weekly, Daily Mix, Release Radar, Today's Top "
                    "Hits and the rest of Spotify's editorial and algorithmic "
                    "playlists were closed to the API in 2024. This is a "
                    "Spotify restriction, not something the app can work "
                    "around.\n\n"
                    "You can copy the tracks into a playlist of your own and "
                    "paste that instead."
                ) from e
            if user_sp is None:
                raise SpotifyAuthError(
                    "Reading a playlist needs you to sign in to Spotify.\n\n"
                    "Spotify no longer lets apps read playlists -- even public "
                    "ones -- without a signed-in user.\n\n"
                    "Use the Sign in to Spotify button, approve it in the "
                    "browser, then paste the playlist link again."
                ) from e
            raise SpotifyAuthError(
                "That playlist could not be read even while signed in.\n\n"
                "Playlists made by Spotify itself are blocked from the API, "
                "and someone else's private or collaborative playlist is only "
                "readable by an account it is shared with.\n\n"
                "Your own playlists should work -- if this is one of yours, "
                "tell me the link."
            ) from e
        raise

    tracks = results["items"]
    while results["next"]:
        results = client.next(results)
        tracks.extend(results["items"])

    return [
        item["track"]["external_urls"]["spotify"]
        for item in tracks
        if item.get("track") and item["track"].get("external_urls")
    ]


def get_spotify_album_tracks(sp, album_url):
    results = sp.album_tracks(album_url)
    tracks = results["items"]
    while results["next"]:
        results = sp.next(results)
        tracks.extend(results["items"])
    return [item["external_urls"]["spotify"] for item in tracks]


def get_spotify_album_tracks_info(sp, url):
    album_id = url.split("/")[-1].split("?")[0]
    results = sp.album_tracks(album_id)
    tracks = results["items"]
    while results["next"]:
        results = sp.next(results)
        tracks.extend(results["items"])
    return [{"name": t["name"], "url": t["external_urls"]["spotify"]} for t in tracks]


def get_ffmpeg_path():
    if getattr(sys, "frozen", False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, "bin")


def _ffmpeg_exe():
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return os.path.join(get_ffmpeg_path(), name)


_BAD_TITLE_WORDS = (
    ("live", 40), ("cover", 60), ("remix", 40), ("karaoke", 120),
    ("instrumental", 80), ("reaction", 200), ("sped up", 60),
    ("slowed", 60), ("nightcore", 120), ("8d audio", 80),
    ("full album", 200), ("tutorial", 150), ("lesson", 150),
)


def _score_candidate(entry, target_ms, metadata):
    """Rank a YouTube search hit against the Spotify track. Lower is better."""
    duration = entry.get("duration")
    if not duration:
        return 1e9

    # Duration mismatch is the strongest signal we found the wrong thing:
    # a live cut, an extended mix, a "full album" upload, a reaction video.
    delta = abs(duration * 1000 - target_ms) / 1000.0 if target_ms else 0
    score = delta * 10

    title = (entry.get("title") or "").lower()
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()

    for bad, penalty in _BAD_TITLE_WORDS:
        if bad in title:
            score += penalty

    if "topic" in uploader or "official" in uploader:
        score -= 50
    if "official" in title or "audio" in title:
        score -= 15

    artist0 = metadata["artists"][0].lower()
    if artist0 in title or artist0 in uploader:
        score -= 30
    if metadata["name"].lower() in title:
        score -= 30

    return score


def _base_ydl_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        # `quiet` alone does not stop the progress bar; without this every
        # download wrote thousands of carriage-return lines to the log file.
        "noprogress": True,
        "noplaylist": True,
        "ffmpeg_location": get_ffmpeg_path(),
        "socket_timeout": 30,
        "retries": 3,
    }


def pick_youtube_source(metadata, log_callback=print, num_results=5):
    """Search YouTube and return the URL whose duration best matches Spotify."""
    query = f"{', '.join(metadata['artists'])} - {metadata['name']}"
    opts = dict(_base_ydl_opts(), skip_download=True, extract_flat="in_playlist")

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{num_results}:{query} audio", download=False)

    entries = [e for e in ((info or {}).get("entries") or []) if e]
    if not entries:
        raise DownloadError(f"No YouTube results for '{query}'")

    target_ms = metadata.get("duration_ms")
    best = min(entries, key=lambda e: _score_candidate(e, target_ms, metadata))

    if target_ms and best.get("duration"):
        drift = abs(best["duration"] * 1000 - target_ms) / 1000.0
        if drift > 15:
            log_callback(
                f"  ! closest match is {drift:.0f}s off the Spotify duration"
                " -- may be the wrong version"
            )

    return best.get("url") or f"https://www.youtube.com/watch?v={best['id']}"


def _cleanup_partials(output_path_base):
    """Delete half-finished downloads so they never masquerade as library files."""
    removed = []
    directory = os.path.dirname(output_path_base) or "."
    stem = os.path.basename(output_path_base)
    try:
        names = os.listdir(directory)
    except OSError:
        return ""
    for name in names:
        if name == stem or (name.startswith(stem + ".") and not name.endswith(".mp3")):
            try:
                os.remove(os.path.join(directory, name))
                removed.append(name)
            except OSError:
                pass
    return ", ".join(removed)


def download_audio(source_url, output_path_base, log_callback=print, quality="192"):
    """Download `source_url` and transcode to `<output_path_base>.mp3`.

    Returns the mp3 path, or raises DownloadError with a real reason. The
    previous version swallowed every exception and returned None, which left
    undecoded .webm/.m4a streams with no extension sitting in the library.
    """
    ffmpeg = _ffmpeg_exe()
    if not os.path.exists(ffmpeg):
        raise DownloadError(f"ffmpeg is missing at {ffmpeg} -- cannot convert to MP3.")

    ydl_opts = dict(
        _base_ydl_opts(),
        format="bestaudio/best",
        # The %(ext)s matters: without it yt-dlp writes the raw stream to a
        # file with no extension, and a failed postprocess leaves it there.
        outtmpl=output_path_base + ".%(ext)s",
        postprocessors=[
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }
        ],
    )

    final_path = output_path_base + ".mp3"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(source_url, download=True)
    except Exception as e:
        _cleanup_partials(output_path_base)
        detail = str(e).replace("\n", " ").strip()[:300]
        raise DownloadError(detail or type(e).__name__) from e

    if not os.path.exists(final_path):
        leftovers = _cleanup_partials(output_path_base)
        raise DownloadError(
            "MP3 conversion produced no output"
            + (f" (removed leftover {leftovers})" if leftovers else "")
        )

    return final_path


_cover_cache = {}


def _fetch_cover(url):
    """Fetch cover art, with a timeout and a small cache for album downloads."""
    if url in _cover_cache:
        return _cover_cache[url]
    try:
        response = requests.get(url, timeout=15)
        data = response.content if response.status_code == 200 else None
    except requests.RequestException:
        data = None
    if len(_cover_cache) > 32:
        _cover_cache.clear()
    _cover_cache[url] = data
    return data


def apply_metadata(file_path, metadata):
    try:
        audio = MP3(file_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
    except error:
        audio = MP3(file_path)
        audio.add_tags()

    tags = audio.tags
    tags.add(TIT2(encoding=3, text=metadata["name"]))
    tags.add(TPE1(encoding=3, text=", ".join(metadata["artists"])))
    tags.add(TALB(encoding=3, text=metadata["album"]))

    if metadata.get("album_artist"):
        tags.add(TPE2(encoding=3, text=metadata["album_artist"]))
    if metadata.get("track_number"):
        tags.add(TRCK(encoding=3, text=str(metadata["track_number"])))
    if metadata.get("disc_number"):
        tags.add(TPOS(encoding=3, text=str(metadata["disc_number"])))
    if metadata.get("release_date"):
        tags.add(TDRC(encoding=3, text=str(metadata["release_date"])))

    if metadata.get("cover_url"):
        cover = _fetch_cover(metadata["cover_url"])
        if cover:
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # 3 is for the cover image
                    desc="Cover",
                    data=cover,
                )
            )

    audio.save(v2_version=3)


def sanitize_filename(filename):
    """Make a string safe to use as a Windows filename."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", filename)
    cleaned = re.sub(r"[\x00-\x1f]", "", cleaned)
    # Windows silently drops trailing dots and spaces, which desynchronises the
    # "already downloaded?" check from what actually lands on disk.
    cleaned = cleaned.rstrip(". ").strip()

    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned

    # Leave room for ".mp3" inside the 255-character path-component limit.
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip(". ")

    return cleaned or "untitled"


def process_track(sp, track, output_dir, log_callback=print, quality="192"):
    """Download one track. Returns a result dict.

    `track` is either a Spotify URL to look up, or a metadata dict that has
    already been resolved -- which is how tracks found through a provider
    that has no Spotify URL to give (iTunes) reach the same pipeline.

    The dict always carries `ok`, so callers can count real successes; the
    previous version returned metadata even when the download had failed.
    """
    if isinstance(track, dict):
        metadata = track
    else:
        try:
            metadata = get_spotify_track_info(sp, track)
        except Exception as e:
            log_callback(f"  x Could not read track metadata: {e}")
            return {"ok": False, "error": str(e), "metadata": None}

    label = f"{', '.join(metadata['artists'])} - {metadata['name']}"
    safe_filename = sanitize_filename(label)
    output_path_base = os.path.join(output_dir, safe_filename)
    final_path = output_path_base + ".mp3"

    if os.path.exists(final_path):
        log_callback(f"  = Already have {safe_filename}.mp3 -- skipping")
        return {"ok": True, "skipped": True, "path": final_path, "metadata": metadata}

    try:
        log_callback(f"  > {label}")
        source = pick_youtube_source(metadata, log_callback=log_callback)
        path = download_audio(
            source, output_path_base, log_callback=log_callback, quality=quality
        )
        apply_metadata(path, metadata)
        log_callback(f"  + Done: {safe_filename}.mp3")
        return {"ok": True, "skipped": False, "path": path, "metadata": metadata}
    except DownloadError as e:
        log_callback(f"  x Failed: {label} -- {e}")
        return {"ok": False, "error": str(e), "metadata": metadata}
    except Exception as e:
        _cleanup_partials(output_path_base)
        log_callback(f"  x Failed: {label} -- {type(e).__name__}: {e}")
        return {"ok": False, "error": str(e), "metadata": metadata}


def download_many(sp, track_urls, output_dir, jobs=3, log_callback=print,
                  quality="192", should_stop=None):
    """Download tracks concurrently, preserving input order in the results."""
    from concurrent.futures import ThreadPoolExecutor

    jobs = max(1, min(jobs, 8))
    results = [None] * len(track_urls)

    def work(index_url):
        i, u = index_url
        if should_stop is not None and should_stop():
            return i, {"ok": False, "error": "cancelled", "metadata": None}
        return i, process_track(
            sp, u, output_dir, log_callback=log_callback, quality=quality
        )

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for i, res in pool.map(work, enumerate(track_urls)):
            results[i] = res

    return results


# What yt-dlp writes to while a download is still running, and leaves behind
# when it is interrupted. These are incomplete by definition.
PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".download",
                    ".crdownload")

# A download in flight has exactly the same magic bytes as an abandoned one,
# so age is the only thing separating them.
PARTIAL_GRACE_SECONDS = 120


def is_partial_download(name):
    """True for a file yt-dlp is still writing, or gave up on."""
    lowered = os.path.basename(name).lower()
    if lowered.endswith(PARTIAL_SUFFIXES):
        return True
    # Fragmented downloads: "Artist - Title.webm.part-Frag12"
    return ".part-frag" in lowered


def find_orphaned_downloads(library_dir):
    """Raw streams an earlier failed postprocess left behind, by magic bytes.

    Partials are excluded. A ".part" file is a truncated stream that still
    starts with valid EBML, so it passed the magic-byte check and got
    converted -- which is where "Newcomers Club - David.webm.part.mp3" came
    from: 113 seconds of a 258-second track, indexed as its own song. Picking
    one up mid-download would also have pulled the file out from under
    yt-dlp.
    """
    orphans = []
    try:
        names = sorted(os.listdir(library_dir))
    except OSError:
        return orphans

    for name in names:
        path = os.path.join(library_dir, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in (".mp3", ".json"):
            continue
        if is_partial_download(name):
            continue
        try:
            if time.time() - os.path.getmtime(path) < PARTIAL_GRACE_SECONDS:
                continue        # still being written
        except OSError:
            continue
        try:
            with open(path, "rb") as f:
                head = f.read(12)
        except OSError:
            continue
        # Matroska/WebM (EBML) or ISO base media format (m4a/mp4)
        if head[:4] == b"\x1a\x45\xdf\xa3" or head[4:8] == b"ftyp":
            orphans.append(path)
    return orphans


def repair_library(sp, library_dir, log_callback=print):
    """Convert orphaned raw downloads into tagged MP3s.

    Earlier builds left the undecoded stream behind whenever the ffmpeg
    postprocess failed. Those files are invisible to the library (which globs
    *.mp3) but still occupy disk, so recover them rather than discard them.
    """
    import subprocess

    ffmpeg = _ffmpeg_exe()
    if not os.path.exists(ffmpeg):
        log_callback(f"ffmpeg missing at {ffmpeg} -- cannot repair.")
        return 0

    orphans = find_orphaned_downloads(library_dir)
    if not orphans:
        log_callback("No orphaned downloads found.")
        return 0

    log_callback(f"Found {len(orphans)} unconverted download(s). Repairing...")
    repaired = 0
    for path in orphans:
        # The extension has to come off. Using the whole basename produced
        # names like "Artist - Title.webm.mp3", made the "already exists"
        # check below compare against a name that could never match, and left
        # the Spotify lookup searching for a title ending in ".webm".
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(library_dir, sanitize_filename(stem) + ".mp3")
        if os.path.exists(out):
            log_callback(f"  = {os.path.basename(out)} exists -- discarding raw file")
            os.remove(path)
            continue
        try:
            subprocess.run(
                [ffmpeg, "-nostdin", "-y", "-i", path, "-vn",
                 "-codec:a", "libmp3lame", "-b:a", "192k", out],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or b"")[-160:].decode("utf-8", "replace").strip()
            log_callback(f"  x {stem}: ffmpeg failed ({tail})")
            continue

        # Re-tag from Spotify using the "Artist - Title" filename convention.
        try:
            if sp and " - " in stem:
                artist, _, title = stem.partition(" - ")
                url = search_spotify_track(sp, f"track:{title} artist:{artist}")
                if url:
                    apply_metadata(out, get_spotify_track_info(sp, url))
        except Exception:
            pass

        os.remove(path)
        repaired += 1
        log_callback(f"  + Recovered {os.path.basename(out)}")

    log_callback(f"Repaired {repaired} of {len(orphans)} file(s).")
    return repaired


def main():
    parser = argparse.ArgumentParser(description="Spotify Downloader CLI")
    parser.add_argument("url", nargs="?", help="Spotify track, album or playlist URL")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory")
    parser.add_argument("-q", "--quality", default="192", help="MP3 bitrate, e.g. 320")
    parser.add_argument("-j", "--jobs", type=int, default=3,
                        help="Parallel downloads (default 3)")
    parser.add_argument("--repair", action="store_true",
                        help="Convert orphaned raw downloads in the output dir")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    try:
        sp = setup_spotify()
    except SpotifyAuthError as e:
        print(f"Error: {e}")
        return 1

    if args.repair:
        repair_library(sp, args.output)
        return 0

    if not args.url:
        parser.error("a URL or search query is required unless --repair is given")

    url = args.url
    try:
        if "track" in url:
            urls = [url]
        elif "playlist" in url:
            print("Fetching playlist tracks...")
            urls = get_spotify_playlist_tracks(sp, url)
        elif "album" in url:
            print("Fetching album tracks...")
            urls = get_spotify_album_tracks(sp, url)
        else:
            print(f"Searching Spotify for '{url}'...")
            found = search_spotify_track(sp, url)
            if not found:
                print(f"Could not find any track matching '{url}' on Spotify.")
                return 1
            urls = [found]
    except SpotifyAuthError as e:
        print(f"Error: {e}")
        return 1

    print(f"Processing {len(urls)} track(s) with {args.jobs} worker(s).")
    results = download_many(sp, urls, args.output, jobs=args.jobs, quality=args.quality)
    ok = sum(1 for r in results if r and r.get("ok"))
    print(f"\nFinished: {ok} succeeded, {len(results) - ok} failed.")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
