"""SQLite index over the local MP3 library.

The app writes rich ID3 tags on every download and then used to browse by
filename, splitting "Artist - Title" back out of the basename. That capped
what the UI could do: no album or artist views, no sorting by year, no
finding a track whose filename convention differs, and a rename lost
everything. This module reads the tags once, caches them, and refreshes
incrementally by mtime.
"""

import os
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
    cover_url    TEXT
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

SORTS = {
    "Title":   "LOWER(COALESCE(NULLIF(title,''), path))",
    "Artist":  "LOWER(COALESCE(NULLIF(artist,''), '')), LOWER(COALESCE(album,'')), disc_no, track_no",
    "Album":   "LOWER(COALESCE(NULLIF(album,''), '')), disc_no, track_no",
    "Year":    "COALESCE(year,'') DESC, LOWER(COALESCE(album,''))",
    "Recently added":  "added DESC",
    "Recently played": "COALESCE(last_played, 0) DESC",
    "Most played":     "play_count DESC, LOWER(COALESCE(title,''))",
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
            if "cover_url" not in columns:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN cover_url TEXT")
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

    def tracks(self, search=None, sort="Title", album=None, artist=None):
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

    def duplicates(self):
        """Same artist+title under different paths -- invisible when browsing by filename."""
        return self._query(
            "SELECT LOWER(COALESCE(artist,'')) AS a, LOWER(COALESCE(title,'')) AS t,"
            " COUNT(*) AS n, GROUP_CONCAT(path, '|') AS paths"
            " FROM tracks WHERE COALESCE(title,'') <> ''"
            " GROUP BY a, t HAVING n > 1 ORDER BY n DESC"
        )

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
