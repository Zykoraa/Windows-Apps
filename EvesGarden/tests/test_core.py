"""Tests for the pure logic: the index, the queue, duplicate matching and
filename safety.

Deliberately no GUI here. Everything below runs headless in about a second,
which is the point -- these are the parts where a regression is silent, and
every regression this project has had was caught by a human noticing the app
misbehave rather than by anything automated.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from library_index import (LibraryIndex, normalise_title, normalise_artist)
from play_queue import PlayQueue
import downloader


class TitleNormalisation(unittest.TestCase):
    """Qualifiers that do not change which song it is should collapse."""

    def test_qualifiers_collapse(self):
        for variant in ["Gravity",
                        "Gravity (Remastered 2011)",
                        "Gravity - Live",
                        "Gravity - Live at Wembley",
                        "Gravity (feat. Someone)",
                        "Gravity [Explicit]",
                        "Gravity - Radio Edit",
                        "Gravity (Deluxe)",
                        "Gravity (Single Version)"]:
            self.assertEqual(normalise_title(variant), "gravity", variant)

    def test_different_songs_stay_apart(self):
        # A remix or a reprise really is a different recording.
        for variant in ["Gravity Remix", "Gravity Reprise", "Anti-Gravity"]:
            self.assertNotEqual(normalise_title(variant), "gravity", variant)

    def test_primary_artist_only(self):
        for credit in ["John Mayer", "John Mayer, Katy Perry",
                       "John Mayer feat. Someone", "John Mayer & Friends"]:
            self.assertEqual(normalise_artist(credit), "john mayer", credit)

    def test_handles_empty(self):
        self.assertEqual(normalise_title(None), "")
        self.assertEqual(normalise_artist(None), "")


class Filenames(unittest.TestCase):
    def test_windows_reserved_names_escape(self):
        self.assertTrue(downloader.sanitize_filename("CON").startswith("_"))
        self.assertTrue(downloader.sanitize_filename("LPT1").startswith("_"))

    def test_illegal_characters_removed(self):
        self.assertEqual(downloader.sanitize_filename('AC/DC: Back?'), "ACDC Back")

    def test_trailing_dots_and_spaces_go(self):
        # Windows silently drops these, which desynchronises the
        # "already downloaded?" check from what lands on disk.
        self.assertEqual(downloader.sanitize_filename("Song. "), "Song")

    def test_never_returns_empty(self):
        self.assertEqual(downloader.sanitize_filename("///"), "untitled")

    def test_length_capped(self):
        self.assertLessEqual(len(downloader.sanitize_filename("x" * 400)), 180)


class Queue(unittest.TestCase):
    def setUp(self):
        self.q = PlayQueue()
        self.q.set_context(["a", "b", "c", "d"], start="a")

    def test_context_advances_in_order(self):
        self.assertEqual(self.q.next_path(current="a"), "b")
        self.assertEqual(self.q.next_path(current="b"), "c")

    def test_queued_tracks_come_first(self):
        self.q.add(["x", "y"])
        self.assertEqual(self.q.next_path(current="a"), "x")
        self.assertEqual(self.q.next_path(current="x"), "y")
        # Queue drained; back to the browsing list.
        self.assertEqual(self.q.next_path(current="y"), "b")

    def test_play_next_jumps_the_line(self):
        self.q.add(["x"])
        self.q.add("z", next_up=True)
        self.assertEqual(self.q.upcoming, ["z", "x"])

    def test_repeat_holds_the_track(self):
        self.assertEqual(self.q.next_path(current="b", repeat=True), "b")

    def test_shuffle_never_repeats_the_current_track(self):
        for _ in range(30):
            self.assertNotEqual(self.q.next_path(current="a", shuffle=True), "a")

    def test_previous_walks_actual_history(self):
        self.q.next_path(current="a")
        self.q.next_path(current="b")
        self.assertEqual(self.q.previous_path(), "b")
        self.assertEqual(self.q.previous_path(), "a")
        self.assertIsNone(self.q.previous_path())

    def test_wraps_at_the_end(self):
        self.q.set_context(["a", "b"], start="b")
        self.assertEqual(self.q.next_path(current="b"), "a")

    def test_empty_context_is_safe(self):
        self.assertIsNone(PlayQueue().next_path())


class Index(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.ix = LibraryIndex(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.ix.close()

    def _add(self, path, title, artist, duration=200, bitrate=192000,
             size=5_000_000, album="Album"):
        with self.ix._lock:
            self.ix._conn.execute(
                "INSERT INTO tracks(path, mtime, size, title, artist, album,"
                " duration, bitrate, added, play_count)"
                " VALUES(?,?,?,?,?,?,?,?,?,0)",
                (path, 1, size, title, artist, album, duration, bitrate,
                 time.time()))
            self.ix._conn.commit()

    def test_liked_round_trip(self):
        self._add("a.mp3", "A", "X")
        self.assertFalse(self.ix.is_liked("a.mp3"))
        self.assertTrue(self.ix.toggle_liked("a.mp3"))
        self.assertEqual(self.ix.liked_count(), 1)
        self.assertEqual(len(self.ix.tracks(liked_only=True)), 1)
        self.ix.toggle_liked("a.mp3")
        self.assertEqual(self.ix.liked_count(), 0)

    def test_playlist_ordering_survives(self):
        for i in range(4):
            self._add(f"{i}.mp3", f"T{i}", "X")
        pid = self.ix.create_playlist("Mix")
        self.assertEqual(self.ix.add_to_playlist(pid, [f"{i}.mp3" for i in range(4)]), 4)
        # Adding the same track twice must not duplicate it.
        self.assertEqual(self.ix.add_to_playlist(pid, ["0.mp3"]), 0)

        self.ix.reorder_playlist(pid, ["3.mp3", "2.mp3", "1.mp3", "0.mp3"])
        order = [t["path"] for t in self.ix.playlist_tracks(pid)]
        self.assertEqual(order, ["3.mp3", "2.mp3", "1.mp3", "0.mp3"])

        self.ix.remove_from_playlist(pid, "3.mp3")
        self.assertEqual(len(self.ix.playlist_tracks(pid)), 3)
        self.ix.delete_playlist(pid)
        self.assertEqual(self.ix.playlists(), [])

    def test_duplicates_group_variants_and_keep_the_best(self):
        self._add("a.mp3", "Gravity", "John Mayer", 247, 320000, 9_000_000)
        self._add("b.mp3", "Gravity (Remastered 2011)", "John Mayer",
                  249, 192000, 6_000_000)
        self._add("c.mp3", "Gravity - Radio Edit", "John Mayer, Katy Perry",
                  245, 128000, 4_000_000)
        groups = self.ix.duplicates()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keep"]["path"], "a.mp3")   # best bitrate
        self.assertEqual(len(groups[0]["extra"]), 2)

    def test_a_live_cut_is_not_a_duplicate(self):
        # Same normalised title, wildly different length: different recording.
        self._add("a.mp3", "Gravity", "John Mayer", 247)
        self._add("d.mp3", "Gravity - Live at Wembley", "John Mayer", 520)
        self.assertEqual(self.ix.duplicates(), [])

    def test_unrelated_songs_never_group(self):
        self._add("a.mp3", "Gravity", "John Mayer")
        self._add("e.mp3", "Clarity", "John Mayer")
        self.assertEqual(self.ix.duplicates(), [])

    def test_search_covers_tags_not_just_filenames(self):
        self._add("xyz123.mp3", "South of the River", "Tom Misch",
                  album="Geography")
        self.assertEqual(len(self.ix.tracks(search="river")), 1)
        self.assertEqual(len(self.ix.tracks(search="geography")), 1)
        self.assertEqual(len(self.ix.tracks(search="misch")), 1)

    def test_album_grouping_collapses_featured_artists(self):
        self._add("1.mp3", "One", "Tom Misch", album="Geography")
        self._add("2.mp3", "Two", "Tom Misch, De La Soul", album="Geography")
        albums = self.ix.albums()
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["n"], 2)



# Magic bytes the orphan scan matches on: EBML (webm) and ISO base media.
WEBM_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 8
M4A_HEAD = b"\x00\x00\x00\x18" + b"ftyp" + b"M4A "


class OrphanedDownloads(unittest.TestCase):
    """A partial download must never be mistaken for a recoverable stream.

    A truncated ".part" file still begins with valid EBML, so it passed the
    magic-byte check and got converted -- which is how 113 seconds of a
    258-second track ended up in the library as a song in its own right.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-orphan-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, head=WEBM_HEAD, age=3600):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(head + b"\x00" * 64)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_finds_a_real_orphan(self):
        path = self.write("Artist - Title.webm")
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [path])

    def test_finds_m4a_orphan(self):
        path = self.write("Artist - Title.m4a", head=M4A_HEAD)
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [path])

    def test_skips_partials(self):
        for name in ("Artist - Title.webm.part",
                     "Artist - Title.webm.ytdl",
                     "Artist - Title.webm.part-Frag12",
                     "Artist - Title.m4a.temp",
                     "Artist - Title.webm.download"):
            self.write(name)
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [])

    def test_skips_downloads_still_in_flight(self):
        # Byte-identical to an abandoned stream; only its age says otherwise.
        self.write("Artist - Title.webm", age=5)
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [])

    def test_ignores_finished_library_files(self):
        self.write("Artist - Title.mp3")
        self.write("recent.json")
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [])

    def test_partial_detection(self):
        for name in ("a.webm.part", "a.m4a.ytdl", "A.WEBM.PART",
                     "a.webm.part-Frag3", "a.opus.temp", "a.mp4.crdownload"):
            self.assertTrue(downloader.is_partial_download(name), name)
        for name in ("a.webm", "a.m4a", "a.mp3", "Partial Eclipse.webm"):
            self.assertFalse(downloader.is_partial_download(name), name)


class OrphanNaming(unittest.TestCase):
    """A recovered MP3 is named from the stem, not the whole filename."""

    def test_stem_drops_the_source_extension(self):
        # repair_library used os.path.basename(path), so a recovered
        # "Artist - Title.webm" was written as "Artist - Title.webm.mp3", the
        # "already exists" check compared a name that could never match, and
        # the Spotify re-tag searched for a title ending in ".webm".
        source = os.path.join("lib", "Newcomers Club - David.webm")
        stem = os.path.splitext(os.path.basename(source))[0]
        self.assertEqual(stem, "Newcomers Club - David")
        self.assertEqual(downloader.sanitize_filename(stem) + ".mp3",
                         "Newcomers Club - David.mp3")


class Scoring(unittest.TestCase):
    """The YouTube candidate ranking that decides which version you get."""

    META = {"artists": ["John Mayer"], "name": "Gravity"}

    def _score(self, title, duration, uploader=""):
        return downloader._score_candidate(
            {"title": title, "duration": duration, "uploader": uploader},
            247000, self.META)

    def test_official_audio_beats_a_live_cut(self):
        official = self._score("John Mayer - Gravity (Official Audio)", 247,
                               "John Mayer - Topic")
        live = self._score("Gravity - John Mayer LIVE at Crossroads", 480)
        self.assertLess(official, live)

    def test_full_album_uploads_are_penalised(self):
        self.assertGreater(self._score("John Mayer Full Album", 3100),
                           self._score("Gravity", 247))

    def test_missing_duration_is_worst(self):
        self.assertGreaterEqual(self._score("Gravity", None), 1e9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
