"""Reading and writing audio files, whatever container they are in.

The library could only see .mp3, and only ever wrote ID3, because that was
the only thing the downloader produced. Five separate places opened files
with MP3(path, ID3=ID3) and pulled APIC frames out by hand, so anything else
-- a FLAC collection, an m4a bought from anywhere, the Opus the downloader
now keeps rather than transcoding -- was invisible or untaggable.

This is the one place that knows about formats. Everything else asks it for
tags, cover art, or to write both, and does not care what the file is.
"""

import base64
import os

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

# What the library will index. Everything here decodes through ffmpeg, which
# is what playback uses, so anything listed can actually be played.
EXTENSIONS = (".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga",
              ".opus", ".wav", ".wma", ".aiff", ".aif")

# mutagen's "easy" keys are the same words for every format it supports.
# Per-format frame names only matter for artwork, further down.
_TEXT = ("title", "artist", "album", "albumartist")


def is_audio(name):
    return name.lower().endswith(EXTENSIONS)


def _first(tags, key):
    try:
        value = tags.get(key)
    except Exception:
        return None
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0]
    text = str(value).strip()
    return text or None


def _int(value):
    """Track and disc numbers are strings, and often "3/12"."""
    if value is None:
        return None
    head = str(value).split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def read_tags(path):
    """The fields the library indexes, from any supported file. Never raises."""
    row = {
        "title": None, "artist": None, "album": None, "albumartist": None,
        "track_no": None, "disc_no": None, "year": None,
        "duration": None, "bitrate": None, "has_art": 0,
    }

    try:
        easy = mutagen.File(path, easy=True)
    except Exception:
        easy = None

    if easy is not None:
        info = getattr(easy, "info", None)
        try:
            row["duration"] = float(info.length)
        except Exception:
            pass
        try:
            # A lossless file may report no bitrate at all; working it out
            # from the size keeps it from sorting as though it had none.
            row["bitrate"] = (int(getattr(info, "bitrate", 0))
                              or _implied_bitrate(path, row["duration"]))
        except Exception:
            pass

        tags = easy.tags
        if tags:
            for field in _TEXT:
                row[field] = _first(tags, field)
            row["track_no"] = _int(_first(tags, "tracknumber"))
            row["disc_no"] = _int(_first(tags, "discnumber"))
            row["year"] = (_first(tags, "date") or "")[:4] or None

    row["has_art"] = 1 if cover_bytes(path) is not None else 0

    # Fall back to the "Artist - Title" filename convention, so files with no
    # tags at all -- hand-copied, or recovered by repair -- still read.
    stem = os.path.splitext(os.path.basename(path))[0]
    if not row["title"]:
        row["title"] = (stem.partition(" - ")[2].strip() or stem
                        if " - " in stem else stem)
    if not row["artist"] and " - " in stem:
        row["artist"] = stem.partition(" - ")[0].strip() or None

    return row


def _implied_bitrate(path, duration):
    if not duration:
        return 0
    try:
        return int(os.path.getsize(path) * 8 / duration)
    except OSError:
        return 0


# ------------------------------------------------------------------- art

def cover_bytes(path):
    """Embedded cover art as bytes, or None. Never raises."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None

    pictures = getattr(audio, "pictures", None)          # FLAC
    if pictures:
        return pictures[0].data

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None

    getall = getattr(tags, "getall", None)               # ID3
    if getall is not None:
        try:
            frames = getall("APIC")
            if frames:
                return frames[0].data
        except Exception:
            pass

    try:
        covers = tags.get("covr")                        # MP4
        if covers:
            return bytes(covers[0])
    except Exception:
        pass

    try:
        blocks = tags.get("metadata_block_picture")      # Ogg
        if blocks:
            return Picture(base64.b64decode(blocks[0])).data
    except Exception:
        pass

    return None


def has_art(path):
    return cover_bytes(path) is not None


# ---------------------------------------------------------------- writing

def write_tags(path, meta, cover=None):
    """Tag a downloaded file, whatever container it landed in.

    Text goes through mutagen's easy interface, which speaks the same words
    for every format; artwork has no such interface, so that is dispatched on
    what the file turns out to be. Returns True if anything was written.
    """
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        audio = None
    if audio is None:
        return False

    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            pass

    fields = (
        ("title", meta.get("name")),
        ("artist", ", ".join(meta.get("artists") or [])),
        ("album", meta.get("album")),
        ("albumartist", meta.get("album_artist")),
        ("tracknumber", meta.get("track_number")),
        ("discnumber", meta.get("disc_number")),
        ("date", meta.get("release_date")),
    )
    written = 0
    for key, value in fields:
        if value in (None, ""):
            continue
        try:
            audio[key] = str(value)
            written += 1
        except Exception:
            # Not every container carries every field, and an Opus file
            # having no notion of a disc number is not worth failing the
            # whole tagging over.
            pass
    try:
        audio.save()
    except Exception:
        return False
    if not written:
        # A WAV takes no tags at all through this interface. Saying so is
        # better than reporting a success that left the file untouched.
        return False

    if cover:
        embed_cover(path, cover)
    return True


def embed_cover(path, data, mime="image/jpeg"):
    """Attach cover art. Returns True if the format could take it."""
    try:
        audio = mutagen.File(path)
        if audio is None:
            return False

        if isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(_picture(data, mime))
            audio.save()
            return True

        if isinstance(audio, MP4):
            fmt = (MP4Cover.FORMAT_PNG if mime.endswith("png")
                   else MP4Cover.FORMAT_JPEG)
            audio["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
            return True

        if isinstance(audio, (OggOpus, OggVorbis)):
            # Ogg has no picture block of its own: the convention is a FLAC
            # picture block, base64'd into an ordinary comment.
            audio["metadata_block_picture"] = [
                base64.b64encode(_picture(data, mime).write()).decode("ascii")]
            audio.save()
            return True

        tags = ID3(path)
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=3)
        return True
    except Exception:
        return False


def _picture(data, mime):
    picture = Picture()
    picture.data = data
    picture.type = 3            # front cover
    picture.mime = mime
    return picture
