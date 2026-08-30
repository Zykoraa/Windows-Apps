"""SQLite index over the local MP3 library.

The app writes rich ID3 tags on every download and then used to browse by
filename, splitting "Artist - Title" back out of the basename. That capped
what the UI could do: no album or artist views, no sorting by year, no
finding a track whose filename convention differs, and a rename lost
everything. This module reads the tags once, caches them, and refreshes
incrementally by mtime.
"""

import os
import re
import sqlite3
import threading
import time

from mutagen.mp3 import MP3
from mutagen.id3 import ID3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    path         TEXT PRIMARY KEY,
    mtime        REAL NOT NULL,
    size         INTEGER NOT NULL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    albumartist  TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         TEXT,
    duration     REAL,
    bitrate      INTEGER,
    has_art      INTEGER DEFAULT 0,
    added        REAL,
    play_count   INTEGER DEFAULT 0,
    last_played  REAL,
    cover_url    TEXT,
    liked        INTEGER DEFAULT 0,
    liked_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_album  ON tracks(album);
CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_played ON tracks(last_played);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Featured-artist tracks carry "Tom Misch, De La Soul" in TPE1, and files
# tagged before album-artist was written have no TPE2 at all -- grouping on
# the raw artist shattered one album into five. Collapse to the primary
# artist for grouping while leaving the full credit on the track itself.
PRIMARY_ARTIST = """COALESCE(NULLIF(albumartist,''),
    CASE WHEN instr(COALESCE(artist,''), ',') > 0
         THEN TRIM(substr(artist, 1, instr(artist, ',') - 1))
         ELSE artist END, '')"""

# Qualifiers that do not change which song it is. Kept as explicit
# alternatives rather than one clever pattern, because the failure mode of a
# clever one is silently merging two different recordings.
_QUALIFIERS = (
    r"feat\.?|ft\.?|featuring|with"
    r"|remastered?(?:\s*\d{2,4})?|re-?master"
    r"|live(?:\s+(?:at|from|in)\b.*)?"
    r"|explicit|clean|dirty"
    r"|radio\s*edit|single\s*(?:version|edit)|album\s*version"
    r"|bonus(?:\s*track)?|deluxe|mono|stereo"
    r"|\d{1,3}(?:st|nd|rd|th)\s+anniversary|anniversary"
    r"|original\s*mix|extended\s*(?:mix|version)"
)

# "(Remastered 2011)", "[Explicit]" -- anything bracketed that is only a
# qualifier.
_NOISE_BRACKET = re.compile(
    r"\s*[\(\[]\s*(?:" + _QUALIFIERS + r")[^\)\]]*[\)\]]",
    re.IGNORECASE)

# "- Remastered 2011", "- Live at Wembley" -- a trailing dashed qualifier.
_NOISE_DASH = re.compile(
    r"\s+-\s*(?:" + _QUALIFIERS + r").*$",
    re.IGNORECASE)

# Split an artist credit at the first collaborator marker.
_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\bx\b|\bvs\.?\b)\s*",
    re.IGNORECASE)

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalise_title(title):
    """Reduce a title to the part that identifies the song."""
    text = (title or "").lower()
    previous = None
    while previous != text:
        previous = text
        text = _NOISE_BRACKET.sub("", text)
        text = _NOISE_DASH.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def normalise_artist(artist):
    """Primary artist only, so a feature credit does not split a pair."""
    text = (artist or "").lower()
    first = _ARTIST_SPLIT.split(text)[0] if text else ""
    return _SPACE.sub(" ", _PUNCT.sub(" ", first)).strip()


SORTS = {
    "Title":   "LOWER(COALESCE(NULLIF(title,''), path))",
    "Artist":  "LOWER(COALESCE(NULLIF(artist,''), '')), LOWER(COALESCE(album,'')), disc_no, track_no",
    "Album":   "LOWER(COALESCE(NULLIF(album,''), '')), disc_no, track_no",
    "Year":    "COALESCE(year,'') DESC, LOWER(COALESCE(album,''))",
    "Recently added":  "added DESC",
    "Recently played": "COALESCE(last_played, 0) DESC",
    "Most played":     "play_count DESC, LOWER(COALESCE(title,''))",
    "Recently liked":  "COALESCE(liked_at, 0) DESC",
}


def _first(tags, key):
    frame = tags.get(key) if tags else None
    if frame is None:
        return None
    try:
        value = str(frame.text[0]).strip()
    except (AttributeError, IndexError):
        return None
    return value or None


def _int(value):
    """ID3 numbers are strings and often "3/12"."""
    if not value:
        return None
    head = str(value).split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def read_tags(path):
    """Pull the fields we index out of one file. Never raises."""
    row = {
        "title": None, "artist": None, "album": None, "albumartist": None,
        "track_no": None, "disc_no": None, "year": None,
        "duration": None, "bitrate": None, "has_art": 0,
    }
    try:
        audio = MP3(path, ID3=ID3)
    except Exception:
        return row

    try:
        row["duration"] = float(audio.info.length)
        row["bitrate"] = int(audio.info.bitrate)
    except Exception:
        pass

    tags = audio.tags
    if tags:
        row["title"] = _first(tags, "TIT2")
        row["artist"] = _first(tags, "TPE1")
        row["album"] = _first(tags, "TALB")
        row["albumartist"] = _first(tags, "TPE2")
        row["track_no"] = _int(_first(tags, "TRCK"))
        row["disc_no"] = _int(_first(tags, "TPOS"))
        row["year"] = (_first(tags, "TDRC") or "")[:4] or None
        row["has_art"] = 1 if any(
            getattr(f, "FrameID", "") == "APIC" for f in tags.values()
        ) else 0

    # Fall back to the old "Artist - Title" filename convention so files
    # without tags (hand-copied, or recovered by repair) still show sensibly.
    stem = os.path.splitext(os.path.basename(path))[0]
    if not row["title"]:
        row["title"] = stem.partition(" - ")[2].strip() or stem if " - " in stem else stem
    if not row["artist"] and " - " in stem:
        row["artist"] = stem.partition(" - ")[0].strip() or None

    return row


class LibraryIndex:
    """Thread-safe-enough index: one connection guarded by a lock."""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Databases created before cover_url existed need the column added
            # rather than being thrown away.
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tracks)")}
            for column, ddl in (("cover_url", "cover_url TEXT"),
                                ("liked", "liked INTEGER DEFAULT 0"),
                                ("liked_at", "liked_at REAL")):
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {ddl}")
            self._conn.commit()

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------- scanning

    def scan(self, library_dir, progress=None):
        """Refresh the index against the folder. Returns (added, updated, removed).

        Only files whose mtime or size changed are re-read, so a rescan of an
        unchanged library costs one stat() per file.
        """
        if not os.path.isdir(library_dir):
            return (0, 0, 0)

        on_disk = {}
        for name in os.listdir(library_dir):
            if not name.lower().endswith(".mp3"):
                continue
            path = os.path.join(library_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            on_disk[path] = (st.st_mtime, st.st_size)

        with self._lock:
            known = {
                r["path"]: (r["mtime"], r["size"])
                for r in self._conn.execute("SELECT path, mtime, size FROM tracks")
            }

        stale = [p for p, v in on_disk.items() if known.get(p) != v]
        removed = [p for p in known if p not in on_disk]

        now = time.time()
        rows = []
        for i, path in enumerate(stale):
            if progress:
                progress(i + 1, len(stale), os.path.basename(path))
            tags = read_tags(path)
            mtime, size = on_disk[path]
            rows.append((
                path, mtime, size,
                tags["title"], tags["artist"], tags["album"], tags["albumartist"],
                tags["track_no"], tags["disc_no"], tags["year"],
                tags["duration"], tags["bitrate"], tags["has_art"], now,
            ))

        with self._lock:
            if rows:
                # Preserve play stats across a re-read of an edited file.
                self._conn.executemany(
                    """INSERT INTO tracks
                       (path, mtime, size, title, artist, album, albumartist,
                        track_no, disc_no, year, duration, bitrate, has_art, added)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         mtime=excluded.mtime, size=excluded.size,
                         title=excluded.title, artist=excluded.artist,
                         album=excluded.album, albumartist=excluded.albumartist,
                         track_no=excluded.track_no, disc_no=excluded.disc_no,
                         year=excluded.year, duration=excluded.duration,
                         bitrate=excluded.bitrate, has_art=excluded.has_art""",
                    rows,
                )
            if removed:
                self._conn.executemany(
                    "DELETE FROM tracks WHERE path = ?", [(p,) for p in removed]
                )
            self._conn.commit()

        added = sum(1 for p in stale if p not in known)
        return (added, len(stale) - added, len(removed))

    # -------------------------------------------------------------- queries

    def _query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def count(self):
        rows = self._query("SELECT COUNT(*) AS n FROM tracks")
        return rows[0]["n"] if rows else 0

    def tracks(self, search=None, sort="Title", album=None, artist=None,
               liked_only=False, played_only=False):
        """Filtered, sorted track rows.

        Search covers title, artist, album and filename -- the old filter only
        matched the filename, so a track whose tags disagreed with its name
        was unfindable.
        """
        where, params = [], []
        if search:
            needle = f"%{search.strip().lower()}%"
            where.append(
                "(LOWER(COALESCE(title,'')) LIKE ? OR LOWER(COALESCE(artist,'')) LIKE ?"
                " OR LOWER(COALESCE(album,'')) LIKE ?"
                " OR LOWER(COALESCE(albumartist,'')) LIKE ? OR LOWER(path) LIKE ?)"
            )
            params += [needle] * 5
        if album is not None:
            where.append("COALESCE(album,'') = ?")
            params.append(album)
        if artist is not None:
            where.append(f"({PRIMARY_ARTIST} = ? OR COALESCE(artist,'') = ?)")
            params += [artist, artist]
        if liked_only:
            where.append("liked = 1")
        if played_only:
            where.append("COALESCE(last_played, 0) > 0")

        sql = "SELECT * FROM tracks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + SORTS.get(sort, SORTS["Title"])
        return self._query(sql, params)

    def albums(self, search=None):
        where, params = ["COALESCE(album,'') <> ''"], []
        if search:
            needle = f"%{search.strip().lower()}%"
            where.append(f"(LOWER(album) LIKE ? OR LOWER({PRIMARY_ARTIST}) LIKE ?)")
            params += [needle, needle]
        return self._query(
            f"SELECT album, {PRIMARY_ARTIST} AS artist,"
            " COUNT(*) AS n, MIN(year) AS year, MAX(has_art) AS has_art,"
            " MIN(path) AS cover_path, SUM(COALESCE(duration,0)) AS total"
            " FROM tracks WHERE " + " AND ".join(where) +
            f" GROUP BY LOWER(album), LOWER({PRIMARY_ARTIST})"
            f" ORDER BY LOWER({PRIMARY_ARTIST}), year, LOWER(album)",
            params,
        )

    def artists(self, search=None):
        where, params = [f"{PRIMARY_ARTIST} <> ''"], []
        if search:
            where.append(f"LOWER({PRIMARY_ARTIST}) LIKE ?")
            params.append(f"%{search.strip().lower()}%")
        return self._query(
            f"SELECT {PRIMARY_ARTIST} AS artist, COUNT(*) AS n,"
            " COUNT(DISTINCT LOWER(COALESCE(album,''))) AS albums, MIN(path) AS cover_path"
            " FROM tracks WHERE " + " AND ".join(where) +
            f" GROUP BY LOWER({PRIMARY_ARTIST}) ORDER BY LOWER({PRIMARY_ARTIST})",
            params,
        )

    def duplicates(self, duration_slack=8.0):
        """Group tracks that look like the same song.

        Exact title matching misses most real duplicates, because the same
        song arrives as "Song", "Song (Remastered 2011)" and
        "Song (feat. X) - Radio Edit". Titles are normalised down to their
        core before grouping, and a duration check keeps genuinely different
        recordings apart -- a 3-minute single and a 9-minute live cut of the
        same name are not duplicates.
        """
        rows = self._query(
            "SELECT path, title, artist, album, duration, bitrate, size,"
            " play_count, added FROM tracks")

        buckets = {}
        for row in rows:
            key = (normalise_artist(row["artist"]), normalise_title(row["title"]))
            if not key[1]:
                continue
            buckets.setdefault(key, []).append(row)

        groups = []
        for (artist, title), members in buckets.items():
            if len(members) < 2:
                continue
            # Split a bucket further when durations disagree, so a remix or a
            # live version is not offered up for deletion.
            members.sort(key=lambda r: r["duration"] or 0)
            runs, current = [], [members[0]]
            for row in members[1:]:
                a = current[-1]["duration"] or 0
                b = row["duration"] or 0
                if abs(a - b) <= duration_slack:
                    current.append(row)
                else:
                    runs.append(current)
                    current = [row]
            runs.append(current)

            for run in runs:
                if len(run) < 2:
                    continue
                # Suggest keeping the best copy: highest bitrate, then
                # longest, then most played, then the one you had first.
                run.sort(key=lambda r: (-(r["bitrate"] or 0),
                                        -(r["duration"] or 0),
                                        -(r["play_count"] or 0),
                                        r["added"] or 0))
                groups.append({
                    "artist": run[0]["artist"] or artist,
                    "title": run[0]["title"] or title,
                    "keep": run[0],
                    "extra": run[1:],
                    "reclaim": sum(r["size"] or 0 for r in run[1:]),
                })

        groups.sort(key=lambda g: -g["reclaim"])
        return groups

    def forget(self, path):
        """Drop a row after its file is deleted."""
        with self._lock:
            self._conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
            self._conn.commit()

    def is_liked(self, path):
        rows = self._query("SELECT liked FROM tracks WHERE path = ?", (path,))
        return bool(rows and rows[0]["liked"])

    def set_liked(self, path, liked=True):
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET liked = ?, liked_at = ? WHERE path = ?",
                (1 if liked else 0, time.time() if liked else None, path))
            self._conn.commit()
        return bool(liked)

    def toggle_liked(self, path):
        return self.set_liked(path, not self.is_liked(path))

    def liked_count(self):
        rows = self._query("SELECT COUNT(*) AS n FROM tracks WHERE liked = 1")
        return rows[0]["n"] if rows else 0

    def cover_url(self, path):
        rows = self._query("SELECT cover_url FROM tracks WHERE path = ?", (path,))
        return rows[0]["cover_url"] if rows else None

    def set_cover_url(self, path, url):
        """Remember a remote cover URL, so it is looked up once per track."""
        with self._lock:
            self._conn.execute("UPDATE tracks SET cover_url = ? WHERE path = ?",
                               (url, path))
            self._conn.commit()

    def record_play(self, path):
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET play_count = play_count + 1, last_played = ?"
                " WHERE path = ?", (time.time(), path),
            )
            self._conn.commit()

    # ----------------------------------------------------------- key/value

    def get_meta(self, key, default=None):
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._conn.commit()
