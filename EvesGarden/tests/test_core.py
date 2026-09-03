"""Tests for the pure logic: the index, the queue, duplicate matching and
filename safety.

Deliberately no GUI here. Everything below runs headless in about a second,
which is the point -- these are the parts where a regression is silent, and
every regression this project has had was caught by a human noticing the app
misbehave rather than by anything automated.
"""

import ast
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from library_index import (LibraryIndex, normalise_title, normalise_artist)
from play_queue import PlayQueue
import colorsys
import downloader
import metadata
import audio_files
import loudness
import lyrics
import smart_playlists
import visualizers
import spotify_import
import themes
import ui_widgets


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

    def test_finds_an_extensionless_stream(self):
        # yt-dlp writing the raw stream with no extension at all is the other
        # shape a failed postprocess leaves behind.
        path = self.write("Artist - Title")
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [path])

    def test_a_kept_source_format_is_not_an_orphan(self):
        """The downloader keeps .m4a and .opus now instead of transcoding.

        A .m4a used to count as an orphan because nothing could play it: the
        library globbed *.mp3 and ignored everything else. It is a library
        file now, and repair converting it back to MP3 would undo the whole
        point of keeping it -- quietly, and to every track downloaded.
        """
        self.write("Artist - Title.m4a", head=M4A_HEAD)
        self.write("Artist - Other.opus", head=b"OggS" + bytes([0, 2]))
        self.write("Artist - Third.flac", head=b"fLaC")
        self.assertEqual(downloader.find_orphaned_downloads(self.dir), [])

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



class Themes(unittest.TestCase):
    """Every palette obeys the same rules, because they are all derived.

    The hand-written tables these replaced failed on all three counts: eleven
    of eighteen paired an accent with a hover colour from a different hue
    family, two put `surface` on the wrong side of `bg`, and the emphasis of
    secondary text ranged from 8:1 to 3:1 depending on which theme you picked.
    """

    def test_every_theme_is_consistent(self):
        for name, theme in themes.THEMES.items():
            self.assertEqual(themes.problems(theme), [], name)

    def test_hover_keeps_the_accent_hue(self):
        for name, theme in themes.THEMES.items():
            accent = colorsys.rgb_to_hsv(*_unit(theme["accent"]))
            hover = colorsys.rgb_to_hsv(*_unit(theme["accent_hover"]))
            if accent[1] < 0.15:
                continue                    # a grey accent has no hue to keep
            gap = abs(accent[0] - hover[0]) * 360
            gap = min(gap, 360 - gap)
            self.assertLess(gap, 25, "%s shifts hue by %.0f degrees"
                            % (name, gap))

    def test_secondary_emphasis_is_uniform(self):
        ratios = [themes.contrast(t["text_secondary"], t["bg"])
                  for t in themes.THEMES.values()]
        self.assertLess(max(ratios) - min(ratios), 0.5,
                        "secondary text carries different weight per theme")
        for r in ratios:
            self.assertGreaterEqual(round(r, 1), themes.SECONDARY_TARGET)

    def test_elevation_is_ordered(self):
        for name, t in themes.THEMES.items():
            steps = [themes.luminance(t[k])
                     for k in ("bg", "surface", "surface_hover")]
            ordered = steps == sorted(steps) or steps == sorted(steps,
                                                                reverse=True)
            self.assertTrue(ordered, "%s: %s" % (name, steps))

    def test_light_themes_are_still_light(self):
        # Deriving everything from the ink must not quietly flip a theme.
        for name in ("Rose Pine Dawn", "Nordic Light"):
            t = themes.THEMES[name]
            self.assertGreater(themes.luminance(t["bg"]), 0.5, name)
            self.assertLess(themes.luminance(t["text"]), 0.5, name)

    def test_build_is_pure(self):
        first = themes.build("#101010", "#ff0000", "#ffffff")
        second = themes.build("#101010", "#ff0000", "#ffffff")
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(themes.KEYS))

    def test_problems_catches_a_bad_palette(self):
        broken = dict(themes.THEMES["Nord"])
        broken["accent_hover"] = "#ffd866"      # a gold hover on a blue accent
        broken["surface"] = broken["bg"]        # and no elevation at all
        found = themes.problems(broken)
        self.assertTrue(any("indistinguishable" in p for p in found), found)


def _unit(value):
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (1, 3, 5))



class Suggestions(unittest.TestCase):
    """What the app offers you next, once Spotify closed the good endpoints.

    /v1/recommendations answers 404 for newly registered apps, and artist
    top-tracks and related-artists answer 403 for app-only credentials, so
    suggestions are drawn from a plain search instead. That makes the
    filtering the whole of the value: a search for an artist returns the same
    song four times over as single, album cut, live version and remaster.
    """

    def seed(self, name="Gravity", track_id="seed"):
        return {"id": track_id, "name": name}

    def track(self, name, track_id, url="https://open.spotify.com/track/x"):
        entry = {"id": track_id, "name": name, "artists": [{"name": "A"}]}
        if url:
            entry["external_urls"] = {"spotify": url}
        return entry

    def test_drops_the_seed_itself(self):
        picked = downloader.pick_suggestions(
            self.seed(), [self.track("Gravity", "seed"),
                          self.track("New Light", "b")])
        self.assertEqual([t["id"] for t in picked], ["b"])

    def test_drops_other_cuts_of_the_seed(self):
        # Same recording, different release: not a suggestion.
        picked = downloader.pick_suggestions(
            self.seed(), [self.track("Gravity - Live", "b"),
                          self.track("Gravity (Remastered 2011)", "c"),
                          self.track("Slow Dancing", "d")])
        self.assertEqual([t["id"] for t in picked], ["d"])

    def test_drops_repeats_within_the_pool(self):
        picked = downloader.pick_suggestions(
            self.seed(), [self.track("New Light", "b"),
                          self.track("New Light - Radio Edit", "c"),
                          self.track("Waiting", "d")])
        self.assertEqual([t["id"] for t in picked], ["b", "d"])

    def test_skips_entries_with_nothing_to_open(self):
        picked = downloader.pick_suggestions(
            self.seed(), [self.track("New Light", "b", url=None),
                          self.track("Waiting", "c")])
        self.assertEqual([t["id"] for t in picked], ["c"])

    def test_respects_the_limit(self):
        pool = [self.track("Song %d" % i, str(i)) for i in range(20)]
        self.assertEqual(len(downloader.pick_suggestions(self.seed(), pool,
                                                         limit=5)), 5)

    def test_survives_nothing_to_choose_from(self):
        self.assertEqual(downloader.pick_suggestions(self.seed(), None), [])
        self.assertEqual(downloader.pick_suggestions(self.seed(), []), [])
        self.assertEqual(downloader.pick_suggestions(
            self.seed(), [{"name": "no id"}]), [])



class QueuePeek(unittest.TestCase):
    """peek_next has to commit, or preloading is worse than useless.

    The player decodes the next track before the current one ends. If asking
    "what is next" a second time could answer differently -- which it can with
    shuffle on -- then the track that was decoded is not the track that plays,
    and the gap comes back with a wasted decode on top.
    """

    def queue(self, paths=("a", "b", "c", "d", "e"), start="a"):
        q = PlayQueue()
        q.set_context(list(paths), start=start)
        return q

    def test_peek_does_not_consume(self):
        q = self.queue()
        q.add(["x"])
        self.assertEqual(q.peek_next(current="a"), "x")
        self.assertEqual(q.upcoming, ["x"], "peek ate the queued track")
        self.assertEqual(q.next_path(current="a"), "x")
        self.assertEqual(q.upcoming, [])

    def test_peek_is_stable_under_shuffle(self):
        q = self.queue()
        first = q.peek_next(shuffle=True, current="a")
        for _ in range(20):
            self.assertEqual(q.peek_next(shuffle=True, current="a"), first)
        self.assertEqual(q.next_path(shuffle=True, current="a"), first)

    def test_next_matches_the_peek_in_order(self):
        q = self.queue()
        self.assertEqual(q.peek_next(current="a"), "b")
        self.assertEqual(q.next_path(current="a"), "b")
        self.assertEqual(q.peek_next(current="b"), "c")
        self.assertEqual(q.next_path(current="b"), "c")

    def test_queueing_something_invalidates_the_peek(self):
        q = self.queue()
        self.assertEqual(q.peek_next(current="a"), "b")
        q.add(["late"], next_up=True)
        self.assertEqual(q.peek_next(current="a"), "late")
        self.assertEqual(q.next_path(current="a"), "late")

    def test_changing_shuffle_invalidates_the_peek(self):
        q = self.queue()
        self.assertEqual(q.peek_next(current="a"), "b")
        # A different question deserves a fresh answer.
        again = q.peek_next(shuffle=True, current="a")
        self.assertEqual(q.next_path(shuffle=True, current="a"), again)

    def test_repeat_peeks_the_same_track(self):
        q = self.queue()
        self.assertEqual(q.peek_next(repeat=True, current="c"), "c")
        self.assertEqual(q.next_path(repeat=True, current="c"), "c")

    def test_peek_on_an_empty_queue(self):
        q = PlayQueue()
        self.assertIsNone(q.peek_next(current=None))
        self.assertIsNone(q.next_path(current=None))

    def test_context_cursor_follows_a_peeked_shuffle(self):
        q = self.queue()
        chosen = q.peek_next(shuffle=True, current="a")
        q.next_path(shuffle=True, current="a")
        # The next hop should continue from where shuffle landed.
        self.assertEqual(q.context_after(1),
                         [["a", "b", "c", "d", "e"][
                             ["a", "b", "c", "d", "e"].index(chosen) + 1]]
                         if chosen != "e" else [])



class Smart(unittest.TestCase):
    """Smart playlists are queries, so the thing to test is what they select.

    Each rule is a WHERE clause interpolated straight into SQL, which is only
    reasonable while every one of them lives in smart_playlists and is exactly
    what it claims to be.
    """

    DAY = 86400.0

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.ix = LibraryIndex(os.path.join(self.dir, "t.db"))
        self.now = time.time()

    def tearDown(self):
        self.ix.close()

    def add(self, path, added=0, plays=0, last=None, liked=0, duration=200):
        with self.ix._lock:
            self.ix._conn.execute(
                "INSERT INTO tracks(path, mtime, size, title, artist, album,"
                " duration, bitrate, added, play_count, last_played, liked)"
                " VALUES(?,1,1,?,?,'Album',?,192000,?,?,?,?)",
                (path, path, "Artist", duration,
                 self.now - added * self.DAY, plays,
                 None if last is None else self.now - last * self.DAY, liked))
            self.ix._conn.commit()

    def paths(self, key):
        rule = smart_playlists.by_key(key)
        return [r["path"] for r in self.ix.smart_tracks(rule)]

    def test_recently_added_is_a_moving_window(self):
        self.add("new.mp3", added=3)
        self.add("old.mp3", added=90)
        self.assertEqual(self.paths("recent-adds"), ["new.mp3"])

    def test_never_played_means_never(self):
        self.add("fresh.mp3", plays=0)
        self.add("once.mp3", plays=1, last=1)
        self.assertEqual(self.paths("never-played"), ["fresh.mp3"])

    def test_forgotten_favourites_needs_both_halves(self):
        self.add("liked-stale.mp3", liked=1, plays=4, last=60)
        self.add("liked-fresh.mp3", liked=1, plays=4, last=2)
        self.add("unliked-stale.mp3", liked=0, plays=4, last=60)
        self.assertEqual(self.paths("forgotten"), ["liked-stale.mp3"])

    def test_never_played_liked_tracks_count_as_forgotten(self):
        # last_played is NULL, which must not silently drop out of the
        # comparison the way a bare column would.
        self.add("liked-unplayed.mp3", liked=1, plays=0, last=None)
        self.assertEqual(self.paths("forgotten"), ["liked-unplayed.mp3"])

    def test_on_repeat_ranks_by_plays(self):
        self.add("a.mp3", plays=9, last=1)
        self.add("b.mp3", plays=4, last=1)
        self.add("c.mp3", plays=1, last=1)
        self.assertEqual(self.paths("on-repeat"), ["a.mp3", "b.mp3"])

    def test_long_players_uses_duration(self):
        self.add("short.mp3", duration=200)
        self.add("epic.mp3", duration=700)
        self.assertEqual(self.paths("long-players"), ["epic.mp3"])

    def test_summary_matches_the_rows(self):
        self.add("a.mp3", plays=0, duration=100)
        self.add("b.mp3", plays=0, duration=150)
        summary = self.ix.smart_summary(smart_playlists.by_key("never-played"))
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["total"], 250)

    def test_every_rule_runs_against_an_empty_library(self):
        for rule in smart_playlists.RULES:
            self.assertEqual(self.ix.smart_tracks(rule), [], rule.key)
            self.assertEqual(self.ix.smart_summary(rule)["n"], 0, rule.key)

    def test_keys_are_unique_and_findable(self):
        keys = [r.key for r in smart_playlists.RULES]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIsNotNone(smart_playlists.by_key(key))
        self.assertIsNone(smart_playlists.by_key("nope"))

    def test_cutoffs_are_recomputed_per_query(self):
        # A rule that froze its cutoff at import would drift out of date the
        # longer the app stayed open.
        rule = smart_playlists.by_key("recent-adds")
        first = rule.params()
        time.sleep(0.01)
        self.assertGreater(rule.params()[0], first[0])



ITUNES_ITEM = {
    "trackId": 12345, "trackName": "Gravity", "artistName": "John Mayer",
    "collectionName": "Continuum", "collectionArtistName": "John Mayer",
    "releaseDate": "2006-09-12T07:00:00Z", "trackTimeMillis": 245773,
    "trackNumber": 4, "discNumber": 1,
    "trackViewUrl": "https://music.apple.com/track/12345",
    "artworkUrl100": "https://example.test/a/100x100bb.jpg",
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Counts calls, so the cache can be shown to actually cache."""

    def __init__(self, payload=None, explode=False):
        self.payload = payload if payload is not None else {"results": []}
        self.explode = explode
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.explode:
            raise IOError("no network")
        return FakeResponse(self.payload)


class Keyless(unittest.TestCase):
    """The provider that needs no account, so the app works before setup.

    Spotify supplies the whole tag set and needs a registered application to
    do it; this has to supply the same fields from an endpoint that needs
    nothing, or the no-setup path produces a library with no albums or years.
    """

    def provider(self, results=(ITUNES_ITEM,), explode=False):
        session = FakeSession({"results": list(results)}, explode=explode)
        return metadata.ITunesProvider(session=session), session

    def test_maps_a_result_into_the_shared_shape(self):
        p, _ = self.provider()
        track = p.search("gravity")[0]
        self.assertEqual(track["source"], "itunes")
        self.assertEqual(track["title"], "Gravity")
        self.assertEqual(track["artist"], "John Mayer")
        self.assertEqual(track["artists"], ["John Mayer"])
        self.assertEqual(track["album"], "Continuum")
        self.assertEqual(track["year"], "2006")
        self.assertEqual(track["duration_ms"], 245773)
        self.assertAlmostEqual(track["duration"], 245.773, places=2)

    def test_supplies_every_field_the_tagger_writes(self):
        p, _ = self.provider()
        info = p.track_info(p.search("gravity")[0])
        # Exactly the keys apply_metadata reads; a missing one silently
        # produces a file with no album or no track number.
        for key in ("name", "artists", "album", "album_artist", "cover_url",
                    "track_number", "disc_number", "release_date",
                    "duration_ms"):
            self.assertIn(key, info)
            self.assertIsNotNone(info[key], key)
        self.assertEqual(info["track_number"], 4)
        self.assertEqual(info["release_date"], "2006")

    def test_asks_for_artwork_bigger_than_a_thumbnail(self):
        p, _ = self.provider()
        info = p.track_info(p.search("gravity")[0])
        self.assertIn("600x600", info["cover_url"])

    def test_skips_results_that_are_not_tracks(self):
        p, _ = self.provider(results=[{"trackName": "No artist"},
                                      {"artistName": "No title"},
                                      ITUNES_ITEM])
        self.assertEqual(len(p.search("x")), 1)

    def test_repeat_searches_do_not_hit_the_network_again(self):
        p, session = self.provider()
        p.search("gravity")
        p.search("gravity")
        self.assertEqual(session.calls, 1)

    def test_an_empty_query_never_asks(self):
        p, session = self.provider()
        self.assertEqual(p.search("   "), [])
        self.assertEqual(session.calls, 0)

    def test_a_dead_network_is_an_empty_answer_not_a_crash(self):
        p, _ = self.provider(explode=True)
        self.assertEqual(p.search("gravity"), [])
        self.assertIsNone(p.lookup("gravity"))

    def test_spotify_items_reshape_to_the_same_thing(self):
        spotify_item = {
            "id": "abc", "name": "Gravity", "duration_ms": 245773,
            "artists": [{"name": "John Mayer"}],
            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
            "album": {"name": "Continuum", "release_date": "2006-09-12",
                      "images": [{"url": "big"}, {"url": "small"}]},
        }
        track = metadata.as_spotify_track(spotify_item)
        itunes, _ = self.provider()
        self.assertEqual(set(track) - {"source"},
                         set(itunes.search("gravity")[0])
                         - {"source", "track_number", "disc_number",
                            "album_artist"})


class KeylessDownload(unittest.TestCase):
    """process_track has to accept resolved metadata, not just a URL.

    An iTunes result has no Spotify URL to look up, so if the pipeline could
    only start from one, the no-account path would stop at the download.
    """

    def test_a_metadata_dict_skips_the_spotify_lookup(self):
        import shutil
        import tempfile

        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        meta = {"name": "Song", "artists": ["Band"], "album": "Album",
                "duration_ms": 180000}
        # Pre-create the file so the "already have it" path returns before
        # any network work, while still proving sp was never touched.
        open(os.path.join(out, "Band - Song.mp3"), "wb").close()

        class Exploding:
            def __getattr__(self, name):
                raise AssertionError("Spotify was consulted for a dict")

        result = downloader.process_track(Exploding(), meta, out,
                                          log_callback=lambda m: None)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIs(result["metadata"], meta)



class ReadableTint(unittest.TestCase):
    """Cover colours pushed until text on them is legible.

    Both directions matter and only one of them was ever exercised: the
    light-theme branch called a blend() this module does not have, and its
    only caller sat inside a try, so the two light themes silently got no
    page tint for weeks rather than an error. Running it over every palette
    is what catches that.
    """

    TARGET = 4.5

    def test_every_theme_gets_a_legible_tint(self):
        for name, theme in themes.THEMES.items():
            tint = ui_widgets.readable_tint(theme["accent"], theme["text"],
                                            theme["surface_hover"])
            self.assertGreaterEqual(
                round(themes.contrast(tint, theme["text"]), 2), self.TARGET,
                "%s: %s on %s" % (name, tint, theme["text"]))

    def test_it_moves_away_from_the_ink_not_always_darker(self):
        # Light ink -> the tint must end up darker; dark ink -> lighter.
        dark = ui_widgets.readable_tint("#d7827e", "#ffffff", "#000000")
        light = ui_widgets.readable_tint("#d7827e", "#575279", "#ffffff")
        self.assertLess(themes.luminance(dark), themes.luminance("#d7827e"))
        self.assertGreater(themes.luminance(light), themes.luminance("#d7827e"))

    def test_a_colour_that_already_reads_is_left_alone(self):
        self.assertEqual(ui_widgets.readable_tint("#000000", "#ffffff", "#111"),
                         "#000000")

    def test_missing_colour_falls_back(self):
        self.assertEqual(ui_widgets.readable_tint(None, "#ffffff", "#123456"),
                         "#123456")

    def test_luminance_helpers_run_both_ways(self):
        # Both were reachable only through readable_tint, and one of them
        # raised NameError the moment it was.
        self.assertIsInstance(ui_widgets.clamp_luminance("#ffffff"), str)
        self.assertIsInstance(ui_widgets.lift_luminance("#000000"), str)
        self.assertLess(themes.luminance(ui_widgets.clamp_luminance("#ffffff")),
                        1.0)
        self.assertGreater(themes.luminance(ui_widgets.lift_luminance("#000000")),
                           0.0)


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


def _sp_track(name, artist, ms=200000, extra=None):
    """The shape Spotify hands back inside a playlist item."""
    track = {
        "name": name,
        "type": "track",
        "artists": [{"name": a} for a in artist.split("|")],
        "album": {"name": "An Album", "release_date": "2011-05-17",
                  "images": [{"url": "http://art/1.jpg"}],
                  "artists": [{"name": artist.split("|")[0]}]},
        "track_number": 3,
        "disc_number": 1,
        "duration_ms": ms,
        "external_urls": {"spotify": "https://open.spotify.com/track/" + name},
    }
    track.update(extra or {})
    return track


def _item(track):
    """A playlist entry as /items returns it: the track under "item"."""
    return {"is_local": False, "item": track}


class FakeSpotify:
    """Just enough of a signed-in client to page through an account."""

    def __init__(self, playlists=(), items=None, liked=(), forbidden=(),
                 tracks_forbidden=True):
        self._playlists = list(playlists)
        self._items = items or {}
        self._liked = list(liked)
        # Spotify serves /items for playlists you own and 403s the rest.
        self.forbidden = set(forbidden)
        self.tracks_forbidden = tracks_forbidden
        self.calls = []

    def current_user(self):
        return {"id": "me", "display_name": "Me"}

    # Paging is done in two pages everywhere, so the loops are exercised
    # rather than assumed.
    def _page(self, rows, limit, kind):
        head, tail = rows[:1], rows[1:]
        return {"items": head, "next": ("more:%s" % kind) if tail else None,
                "total": len(rows), "_rest": tail}

    def current_user_playlists(self, limit=50):
        self.calls.append("playlists")
        return self._page(self._playlists, limit, "playlists")

    def current_user_saved_tracks(self, limit=50):
        self.calls.append("liked")
        if limit == 1:
            return {"items": [], "next": None, "total": len(self._liked)}
        return self._page(self._liked, limit, "liked")

    def playlist_items(self, playlist_id, limit=100, additional_types=None):
        """The old /tracks endpoint, which Spotify now answers 403 for."""
        self.calls.append("tracks:%s" % playlist_id)
        if self.tracks_forbidden:
            raise RuntimeError("http status: 403 - Forbidden")
        return self._page(self._items.get(playlist_id, []), limit, "items")

    def _get(self, path, **kwargs):
        """spotipy's escape hatch, which is how /items is reached."""
        self.calls.append("GET " + path)
        if path.endswith("/items"):
            playlist_id = path.split("/")[1]
            if playlist_id in self.forbidden:
                raise RuntimeError("http status: 403 - Forbidden")
            return self._page(self._items.get(playlist_id, []),
                              kwargs.get("limit", 100), "items")
        raise AssertionError("unexpected path %r" % path)

    def next(self, results):
        rest = results.get("_rest") or []
        return {"items": rest, "next": None, "_rest": []}


class OldFakeSpotify(FakeSpotify):
    """A client from before /items existed: no _get to call."""

    _get = None

    def __getattribute__(self, name):
        if name == "_get":
            raise AttributeError(name)
        return object.__getattribute__(self, name)


def _playlist(pid, name, owner="me", total=None, shape="items",
              collaborative=False):
    """One entry as /me/playlists returns it.

    Spotify renamed the count block from `tracks` to `items`; both shapes are
    built here because the code has to survive either.
    """
    out = {"id": pid, "name": name, "owner": {"id": owner},
           "collaborative": collaborative}
    if total is not None:
        out[shape] = {"href": "https://api/%s/items" % pid, "total": total}
    return out


class SpotifyApiShape(unittest.TestCase):
    """The four things that broke when Spotify moved the endpoint.

    Every playlist showed "0 tracks" and importing any of them would have
    produced an empty playlist, because /tracks began answering 403 and the
    count moved to a differently named field.
    """

    def test_the_count_is_read_from_its_new_name(self):
        sp = FakeSpotify(playlists=[_playlist("p", "Clubbin'", total=10)])
        found = spotify_import.list_playlists(sp)
        self.assertEqual([p["total"] for p in found if not p["liked"]], [10])

    def test_the_old_name_still_reads(self):
        sp = FakeSpotify(playlists=[_playlist("p", "Clubbin'", total=10,
                                              shape="tracks")])
        found = spotify_import.list_playlists(sp)
        self.assertEqual([p["total"] for p in found if not p["liked"]], [10])

    def test_a_playlist_with_no_count_at_all_is_not_a_crash(self):
        sp = FakeSpotify(playlists=[_playlist("p", "Clubbin'", total=None)])
        self.assertEqual(
            [p["total"] for p in spotify_import.list_playlists(sp)
             if not p["liked"]], [0])

    def test_contents_come_from_items_not_tracks(self):
        """/tracks is 403 now, for your own playlists too.

        spotipy's playlist_items() still calls it, so reading a playlist
        through the library alone gets nothing at all.
        """
        sp = FakeSpotify(items={"p": [_item(_sp_track("A", "X")),
                                      _item(_sp_track("B", "Y"))]})
        tracks = spotify_import.read_playlist(
            sp, {"id": "p", "name": "p", "liked": False})
        self.assertEqual([t["name"] for t in tracks], ["A", "B"])
        self.assertIn("GET playlists/p/items", sp.calls)
        self.assertNotIn("tracks:p", sp.calls)

    def test_a_client_without_get_falls_back_to_the_old_call(self):
        sp = OldFakeSpotify(items={"p": [{"track": _sp_track("A", "X")}]},
                            tracks_forbidden=False)
        tracks = spotify_import.read_playlist(
            sp, {"id": "p", "name": "p", "liked": False})
        self.assertEqual([t["name"] for t in tracks], ["A"])
        self.assertIn("tracks:p", sp.calls)

    def test_a_refusal_travels_rather_than_being_swallowed(self):
        """An empty playlist and a forbidden one must not look the same.

        The old endpoint is left able to answer here, so that falling back to
        it on a 403 -- rather than only on a missing method -- would quietly
        succeed and hide the refusal instead of reporting it.
        """
        sp = FakeSpotify(items={"p": [_item(_sp_track("A", "X"))]},
                         forbidden=("p",), tracks_forbidden=False)
        with self.assertRaises(RuntimeError):
            spotify_import.read_playlist(
                sp, {"id": "p", "name": "p", "liked": False})

    def test_playlists_you_only_follow_are_marked_unreadable(self):
        sp = FakeSpotify(playlists=[
            _playlist("mine", "Mine", owner="me", total=3),
            _playlist("theirs", "Theirs", owner="someone", total=99),
            _playlist("shared", "Shared", owner="someone", total=5,
                      collaborative=True),
        ])
        found = {p["name"]: p for p in spotify_import.list_playlists(sp)}
        self.assertTrue(found["Mine"]["readable"])
        self.assertTrue(found["Liked Songs"]["readable"])
        self.assertTrue(found["Shared"]["readable"])
        self.assertFalse(found["Theirs"]["readable"])

    def test_a_track_is_found_under_either_name(self):
        # Playlists say "item" now; Liked Songs still says "track".
        for wrapper in ("item", "track"):
            sp = FakeSpotify(items={"p": [{wrapper: _sp_track("A", "X")}]})
            tracks = spotify_import.read_playlist(
                sp, {"id": "p", "name": "p", "liked": False})
            self.assertEqual([t["name"] for t in tracks], ["A"], wrapper)


class SpotifyImportReading(unittest.TestCase):

    def test_liked_songs_leads_and_ownership_is_marked(self):
        sp = FakeSpotify(playlists=[
            {"id": "p1", "name": "Mine", "owner": {"id": "me"},
             "tracks": {"total": 4}},
            {"id": "p2", "name": "Theirs", "owner": {"id": "someone"},
             "tracks": {"total": 9}},
        ], liked=[1, 2, 3])
        found = spotify_import.list_playlists(sp)

        self.assertEqual([p["name"] for p in found],
                         ["Liked Songs", "Mine", "Theirs"])
        self.assertEqual(found[0]["id"], spotify_import.LIKED_ID)
        self.assertEqual([p["mine"] for p in found], [True, True, False])
        self.assertEqual([p["total"] for p in found], [3, 4, 9])

    def test_a_deleted_collaborative_playlist_is_a_null_item(self):
        # Spotify returns null in the list rather than leaving it out, which
        # used to be an AttributeError halfway through the import.
        sp = FakeSpotify(playlists=[
            None, {"id": "p", "name": "Real", "owner": {"id": "me"},
                   "tracks": {"total": 1}}])
        self.assertEqual([p["name"] for p in spotify_import.list_playlists(sp)],
                         ["Liked Songs", "Real"])

    def test_no_client_is_not_an_exception(self):
        self.assertEqual(spotify_import.list_playlists(None), [])

    def test_reading_skips_what_cannot_be_downloaded(self):
        sp = FakeSpotify(items={"p": [
            {"track": _sp_track("Real", "Artist")},
            {"track": _sp_track("Onmydisk", "Artist", extra={"is_local": True})},
            {"track": {"name": "An episode", "type": "episode",
                       "artists": [], "album": {}}},
            {"track": None},
            {"track": _sp_track("AlsoReal", "Artist")},
        ]})
        tracks = spotify_import.read_playlist(
            sp, {"id": "p", "name": "p", "liked": False})
        self.assertEqual([t["name"] for t in tracks], ["Real", "AlsoReal"])

    def test_liked_songs_read_from_their_own_endpoint(self):
        sp = FakeSpotify(liked=[{"track": _sp_track("A", "X")},
                                {"track": _sp_track("B", "Y")}])
        tracks = spotify_import.read_playlist(
            sp, {"id": spotify_import.LIKED_ID, "name": "Liked Songs",
                 "liked": True})
        self.assertEqual([t["name"] for t in tracks], ["A", "B"])
        self.assertIn("liked", sp.calls)

    def test_metadata_carries_everything_the_downloader_tags_with(self):
        meta = spotify_import.as_metadata(_sp_track("Gravity", "John Mayer|Ed"))
        self.assertEqual(meta["artists"], ["John Mayer", "Ed"])
        self.assertEqual(meta["album_artist"], "John Mayer")
        self.assertEqual(meta["release_date"], "2011")
        self.assertEqual(meta["cover_url"], "http://art/1.jpg")
        self.assertEqual(meta["duration_ms"], 200000)


class SpotifyImportMatching(unittest.TestCase):

    def _meta(self, name, artist, ms=200000):
        return spotify_import.as_metadata(_sp_track(name, artist, ms))

    def test_a_remaster_matches_the_copy_already_owned(self):
        owned = {("john mayer", "gravity"): [("C:/lib/g.mp3", 200.0)]}
        meta = self._meta("Gravity - Remastered 2011", "John Mayer")
        self.assertEqual(spotify_import.match(meta, owned), "C:/lib/g.mp3")

    def test_a_feature_credit_does_not_stop_a_match(self):
        owned = {("tom misch", "water baby"): [("C:/lib/w.mp3", 240.0)]}
        meta = self._meta("Water Baby", "Tom Misch|De La Soul")
        self.assertEqual(spotify_import.match(meta, owned), "C:/lib/w.mp3")

    def test_length_separates_a_live_cut_from_the_studio_one(self):
        # Both normalise to the same key on purpose; the only thing telling
        # them apart is how long they run.
        owned = {("john mayer", "gravity"): [("C:/lib/live.mp3", 480.0),
                                             ("C:/lib/studio.mp3", 247.0)]}
        meta = self._meta("Gravity", "John Mayer", ms=247000)
        self.assertEqual(spotify_import.match(meta, owned), "C:/lib/studio.mp3")

    def test_nothing_owned_is_no_match(self):
        self.assertIsNone(spotify_import.match(self._meta("X", "Y"), {}))


class SpotifyImportPlan(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-import-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _meta(self, name, artist="Artist", ms=200000):
        return spotify_import.as_metadata(_sp_track(name, artist, ms))

    def test_order_is_spotify_order_not_what_is_owned(self):
        """The whole list, in sequence.

        Position is the only thing carrying the playlist's identity once the
        tracks are just paths, and it is what gets handed to
        reorder_playlist. Spot-checking one slot is not enough: a reversed
        list still has the same track in the middle.
        """
        owned = {("artist", "b"): [("C:/lib/b.mp3", 200.0)]}
        tracks = [self._meta("A"), self._meta("B"), self._meta("C")]
        paths, missing = spotify_import.plan(tracks, owned, self.dir)

        self.assertEqual(paths, [
            spotify_import.predicted_path(tracks[0], self.dir),
            "C:/lib/b.mp3",
            spotify_import.predicted_path(tracks[2], self.dir),
        ])
        self.assertEqual([m["name"] for m in missing], ["A", "C"])

    def test_a_song_listed_twice_is_held_once(self):
        # A playlist may repeat a track; playlist_items is keyed on the path,
        # so a repeat would collide rather than appear twice.
        tracks = [self._meta("A"), self._meta("A"), self._meta("B")]
        paths, missing = spotify_import.plan(tracks, {}, self.dir)
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual([m["name"] for m in missing], ["A", "B"])

    def test_a_file_already_on_disk_is_not_queued_again(self):
        """The index is not the only witness to what you own.

        A file whose tags disagree with its name, or that the index has not
        caught up with, is on disk and unmatched -- and queueing it told the
        user a download was needed when process_track would just skip it.
        """
        meta = self._meta("Gravity", "John Mayer")
        open(spotify_import.predicted_path(meta, self.dir), "w").close()

        paths, missing = spotify_import.plan([meta], {}, self.dir)
        self.assertEqual(missing, [])
        self.assertEqual(paths, [spotify_import.predicted_path(meta, self.dir)])

    def test_the_predicted_path_is_where_the_download_actually_lands(self):
        """The load-bearing assumption of the whole import.

        A slot is reserved for a track before it is downloaded, and filled by
        checking whether that exact path now exists. If the downloader's
        naming and this prediction ever drift apart, every imported playlist
        quietly comes out short and nothing raises.
        """
        meta = self._meta("Gravity: Live/Remastered?", "John Mayer|Ed")
        predicted = spotify_import.predicted_path(meta, self.dir)

        open(predicted, "w").close()
        result = downloader.process_track(None, meta, self.dir,
                                          log_callback=lambda _m: None)

        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(os.path.normcase(result["path"]),
                         os.path.normcase(predicted))


class Fingerprints(unittest.TestCase):
    """The index side of the match."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-fp-")
        self.ix = LibraryIndex(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.ix.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _add(self, path, title, artist, duration=200.0):
        with self.ix._lock:
            self.ix._conn.execute(
                "INSERT INTO tracks(path, mtime, size, title, artist, duration,"
                " added) VALUES(?,1,1,?,?,?,0)", (path, title, artist, duration))
            self.ix._conn.commit()

    def test_variants_collapse_onto_one_key(self):
        self._add("a.mp3", "Gravity (Remastered 2011)", "John Mayer")
        self._add("b.mp3", "Gravity - Live", "John Mayer, Someone", 480.0)
        table = self.ix.fingerprints()

        self.assertEqual(list(table), [("john mayer", "gravity")])
        self.assertEqual(sorted(p for p, _d in table[("john mayer", "gravity")]),
                         ["a.mp3", "b.mp3"])

    def test_a_file_with_no_usable_title_is_left_out(self):
        self._add("c.mp3", "", "Nobody")
        self.assertEqual(self.ix.fingerprints(), {})

    def test_it_feeds_straight_into_a_match(self):
        self._add("g.mp3", "Gravity", "John Mayer", 247.0)
        meta = spotify_import.as_metadata(
            _sp_track("Gravity (Remastered)", "John Mayer", 247000))
        self.assertEqual(spotify_import.match(meta, self.ix.fingerprints()),
                         "g.mp3")


class LyricParsing(unittest.TestCase):
    """Everything the old inline parser dropped on the floor."""

    def test_one_line_can_carry_several_timestamps(self):
        # A compressed LRC gives the chorus once and lists when it recurs.
        # The old parser read the first stamp and left the rest sitting in
        # the text, so every repeat was lost and one line came out wrong.
        out, synced = lyrics.parse(
            "[00:12.00][01:45.30][02:58.10]We go again\n[00:20.00]A verse")
        self.assertTrue(synced)
        self.assertEqual(out, [(12.0, "We go again"), (20.0, "A verse"),
                               (105.3, "We go again"), (178.1, "We go again")])

    def test_the_legacy_hundredths_form_still_reads(self):
        # [mm:ss:xx] has three colon-separated parts, which the old parser
        # counted and skipped.
        out, _ = lyrics.parse("[01:02:50]Old form")
        self.assertEqual(out, [(62.5, "Old form")])

    def test_fractions_are_not_read_as_whole_seconds(self):
        out, _ = lyrics.parse("[00:01.5]Half\n[00:02.05]Also")
        self.assertEqual(out, [(1.5, "Half"), (2.05, "Also")])

    def test_metadata_tags_are_not_lyrics(self):
        out, _ = lyrics.parse("[ar:Someone]\n[ti:A Song]\n[length:03:24]\n"
                              "[offset:+500]\n[00:05.00]The only real line")
        self.assertEqual(out, [(5.0, "The only real line")])

    def test_an_instrumental_break_is_kept_as_a_gap(self):
        """A timed line with no words is where the singing stops.

        Dropping it left the last line before the break highlighted through
        all of it, and the closing one highlighted to the end of the song --
        which is what "the lyrics stop and it gets stuck" looks like.
        """
        out, _ = lyrics.parse("[00:05.00]Before\n[00:35.37] \n[01:46.28]After")
        self.assertEqual(out, [(5.0, "Before"), (35.37, ""),
                               (106.28, "After")])

    def test_lines_come_back_in_time_order(self):
        out, _ = lyrics.parse("[02:00.00]Third\n[00:10.00]First\n[01:00.00]Second")
        self.assertEqual([t for _s, t in out], ["First", "Second", "Third"])

    def test_a_repeated_line_at_one_timestamp_is_held_once(self):
        out, _ = lyrics.parse("[00:05.00]Same\n[00:05.00]Same\n[00:09.00]Different")
        self.assertEqual(out, [(5.0, "Same"), (9.0, "Different")])

    def test_nothing_in_nothing_out(self):
        self.assertEqual(lyrics.parse(""), ([], False))
        self.assertEqual(lyrics.parse(None), ([], False))


class PlainLyrics(unittest.TestCase):
    """Words with no timings are still words."""

    GENIUS = ("2 Contributors\n"
              "Daylily - Live at Studio 4 Lyrics\n"
              "\n"
              "Outside for the first time\n"
              "You might also like\n"
              "And you'll be just fine\n"
              "\n"
              "\n"
              "Shine onto me4Embed")

    def test_the_page_furniture_is_stripped(self):
        out, synced = lyrics.parse(self.GENIUS)
        self.assertFalse(synced)
        self.assertEqual([t for _s, t in out],
                         ["Outside for the first time",
                          "And you'll be just fine",
                          "",
                          "Shine onto me"])

    def test_untimed_lines_carry_no_time(self):
        out, _ = lyrics.parse("Just some words\nOn two lines")
        self.assertEqual(out, [(None, "Just some words"), (None, "On two lines")])

    def test_a_section_marker_survives(self):
        # [Chorus] is the only structure plain lyrics have, and it is not a
        # timestamp, so it must not be mistaken for one or thrown away.
        out, synced = lyrics.parse("[Chorus]\nSing along")
        self.assertFalse(synced)
        self.assertEqual([t for _s, t in out], ["[Chorus]", "Sing along"])


class LyricFetching(unittest.TestCase):
    """A timed transcript beats plain text, whoever answers first."""

    SYNCED = "[00:01.00]Timed line"
    PLAIN = "Just the words"

    def _search(self, log, synced=None, plain=None, raises=()):
        def search(query, synced_only=False, plain_only=False):
            log.append("synced" if synced_only else "plain")
            if synced_only:
                if "synced" in raises:
                    raise RuntimeError("provider down")
                return synced
            if "plain" in raises:
                raise RuntimeError("provider down")
            return plain
        return search

    def test_synced_is_asked_for_first_and_settles_it(self):
        """search() on its own returns whoever answered first.

        That is how a song with a good LRC on one provider came back as
        Genius plain text instead, differently on different runs.
        """
        log = []
        out, synced = lyrics.fetch(
            "q", self._search(log, synced=self.SYNCED, plain=self.PLAIN))
        self.assertTrue(synced)
        self.assertEqual(log, ["synced"])          # never had to ask for plain
        self.assertEqual(out, [(1.0, "Timed line")])

    def test_plain_is_used_rather_than_thrown_away(self):
        log = []
        out, synced = lyrics.fetch(
            "q", self._search(log, synced=None, plain=self.PLAIN))
        self.assertFalse(synced)
        self.assertEqual(log, ["synced", "plain"])
        self.assertEqual(out, [(None, "Just the words")])

    def test_a_provider_that_raises_is_not_the_end_of_it(self):
        log = []
        out, synced = lyrics.fetch(
            "q", self._search(log, plain=self.PLAIN, raises=("synced",)))
        self.assertFalse(synced)
        self.assertEqual([t for _s, t in out], ["Just the words"])

    def test_nothing_anywhere_is_reported_as_nothing(self):
        log = []
        self.assertEqual(lyrics.fetch("q", self._search(log)), ([], False))


class PeakHold(unittest.TestCase):
    """What makes a spectrum analyser look like one."""

    def test_a_peak_holds_above_the_bar_then_falls(self):
        peaks = visualizers._Peaks()
        peaks.update([1.0], 0.0)
        # Still held while the bar has dropped away underneath it.
        self.assertAlmostEqual(peaks.update([0.1], 0.2)[0], 1.0)
        # After the hold, falling but not yet down to the bar.
        after = peaks.update([0.1], 0.2 + peaks.HOLD + 0.1)[0]
        self.assertLess(after, 1.0)
        self.assertGreater(after, 0.1)

    def test_a_peak_never_falls_below_the_bar(self):
        """Within the single frame where the decay would overshoot it.

        A frame later the peak is picked back up by the bar anyway, so the
        only way to see this is to catch the one update that overshoots --
        which on screen is a cap flickering below the bar it belongs to.
        """
        peaks = visualizers._Peaks()
        peaks.update([1.0], 0.0)
        # Far enough past the hold that a full clamped step is taken, and the
        # bar is high enough that the step would carry the peak under it.
        held = peaks.update([0.9], peaks.HOLD + 0.5)[0]
        self.assertGreaterEqual(held, 0.9)

    def test_a_peak_does_come_down_to_meet_a_quiet_band(self):
        peaks = visualizers._Peaks()
        peaks.update([1.0], 0.0)
        now = peaks.HOLD + 0.1
        for _ in range(12):
            now += 0.2
            held = peaks.update([0.1], now)[0]
        self.assertLess(held, 0.2)

    def test_a_louder_band_takes_the_peak_immediately(self):
        peaks = visualizers._Peaks()
        peaks.update([0.2], 0.0)
        self.assertAlmostEqual(peaks.update([0.9], 0.05)[0], 0.9)

    def test_one_frame_cannot_drop_every_peak_to_the_floor(self):
        """A paused visualiser comes back to an enormous time delta.

        Unclamped, the first frame after it would subtract minutes of decay
        and every peak would land on its bar at once.
        """
        peaks = visualizers._Peaks()
        peaks.update([1.0], 0.0)
        peaks.update([1.0], 0.1)
        self.assertGreater(peaks.update([0.0], 600.0)[0], 0.4)

    def test_a_different_band_count_is_a_different_song(self):
        peaks = visualizers._Peaks()
        peaks.update([1.0, 1.0], 0.0)
        self.assertEqual(peaks.update([0.2, 0.2, 0.2], 0.01), [0.2, 0.2, 0.2])


class VisualiserChoice(unittest.TestCase):
    """The list was cut from 32 to 9, so every saved position moved."""

    def test_a_name_is_taken_as_it_is(self):
        for name in visualizers.names():
            self.assertEqual(visualizers.names()[visualizers.resolve(name)],
                             name)

    def test_an_old_index_finds_the_mode_it_meant(self):
        # 16 was Spectrum Ribbon and 23 was Tunnel, both of which survived.
        self.assertEqual(visualizers.names()[visualizers.resolve(16)],
                         "Spectrum Ribbon")
        self.assertEqual(visualizers.names()[visualizers.resolve(23)],
                         "Tunnel")

    def test_an_index_for_a_mode_that_is_gone_falls_back(self):
        # 25 was Grid Pulse, which no longer exists. Wrapping the index would
        # silently hand back some unrelated visualiser instead.
        self.assertEqual(visualizers.resolve(25), 0)
        self.assertEqual(visualizers.resolve(9), 0)

    def test_nonsense_is_the_first_one_rather_than_an_error(self):
        for saved in (None, "", "Fire", 99, -1, "12", object()):
            self.assertEqual(visualizers.resolve(saved), 0, repr(saved))

    def test_the_legacy_list_still_matches_what_it_describes(self):
        """It is the map from old positions, so its length is load-bearing."""
        self.assertEqual(len(visualizers.LEGACY_NAMES), 32)
        self.assertEqual(len(set(visualizers.LEGACY_NAMES)), 32)
        # Everything kept must appear in it, or that mode's old setting is
        # unreachable.
        for name in visualizers.names():
            self.assertIn(name, visualizers.LEGACY_NAMES)

    def test_no_two_modes_share_a_name(self):
        self.assertEqual(len(set(visualizers.names())),
                         len(visualizers.names()))


class BlockingWin32Calls(unittest.TestCase):
    """Nothing reached from a Tk callback may run a Windows modal loop.

    Dragging the title bar used to do the standard trick for moving a
    frameless window: ReleaseCapture, then SendMessage with
    WM_NCLBUTTONDOWN/HTCAPTION, which asks Windows to run the move for you
    and hands you edge snapping for free. It also aborted the interpreter the
    first time anyone clicked the title bar:

        Fatal Python error: PyEval_RestoreThread: the function must be
        called with the GIL held, but the GIL is released

    SendMessage does not return until the move is finished. Windows runs a
    modal loop meanwhile, and that loop dispatches messages straight back
    into Tk -- while ctypes is still holding the GIL released for the call it
    has not returned from. The first callback that reaches Python during the
    drag finds no thread state and the process dies. It is not an exception,
    so nothing catches it and no log records it.

    No behavioural test can reach this. SendMessage(WM_NCLBUTTONDOWN) returns
    immediately unless a mouse button is genuinely held down, so a
    synthesised click runs the same code and proves nothing; only a real
    hand on a real mouse reproduces it. What can be checked is that the call
    is not there, which is what this does -- by reading the source rather
    than importing it, so it needs no display.
    """

    # Every one of these runs a message loop of its own, and every one of
    # them is reachable from a binding.
    FORBIDDEN = ("SendMessageW", "SendMessageA", "SendMessage",
                 "DoDragDrop", "TrackPopupMenu", "MessageBoxW")

    def _module(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            return ast.parse(handle.read(), filename=name)

    def _called_names(self, tree):
        """Attribute and function names actually called, not mentioned.

        Parsed rather than grepped: the fix carries an explanation naming
        the call it removed, and a test that cannot tell the difference
        between doing a thing and describing it would fail on its own
        comment.
        """
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute):
                found.add(target.attr)
            elif isinstance(target, ast.Name):
                found.add(target.id)
        return found

    def test_the_window_never_hands_a_drag_to_windows(self):
        for name in ("gui.py", "ui_widgets.py", "media_keys.py"):
            called = self._called_names(self._module(name))
            for blocking in self.FORBIDDEN:
                self.assertNotIn(
                    blocking, called,
                    "%s calls %s. It does not return until Windows has "
                    "finished with it, and the loop it runs meanwhile "
                    "dispatches back into Tk with the GIL released -- which "
                    "kills the process rather than raising." % (name, blocking))

    def test_it_can_tell_a_call_from_a_mention(self):
        """The guard above is only worth having if it is not fooled."""
        described = ast.parse('"""We used to call user32.SendMessageW here."""\n'
                              'x = "SendMessageW"')
        self.assertNotIn("SendMessageW", self._called_names(described))
        actually = ast.parse("user32.SendMessageW(h, 1, 2, 3)")
        self.assertIn("SendMessageW", self._called_names(actually))


def _ffmpeg():
    """The bundled ffmpeg, if this machine has it. CI does not."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "bin", "ffmpeg.exe")
    return path if os.path.exists(path) else None


class Formats(unittest.TestCase):
    """The library indexed .mp3 and nothing else, and wrote only ID3.

    Anyone arriving with a collection already on disk -- which is most people
    who want a local music player -- could not see a note of it.
    """

    META = {"name": "A Title", "artists": ["An Artist", "Another"],
            "album": "An Album", "album_artist": "An Artist",
            "track_number": 4, "disc_number": 1, "release_date": "2011"}
    # A real, if tiny, JPEG.
    COVER = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffc200"
        "0b080001000101011100ffc40014000100000000000000000000000000000009"
        "ffda0008010100013f10")
    CODECS = (("flac", "flac"), ("m4a", "aac"), ("opus", "libopus"),
              ("ogg", "libvorbis"), ("mp3", "libmp3lame"))
    # WAV carries no tags through this interface, which is worth saying out
    # loud rather than reporting a success that wrote nothing.
    UNTAGGABLE = (("wav", "pcm_s16le"),)

    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = _ffmpeg()
        if not cls.ffmpeg:
            return
        import subprocess
        cls.dir = tempfile.mkdtemp(prefix="eg-fmt-")
        # One second of a tone is enough to be a real file of each kind.
        for ext, codec in cls.CODECS + cls.UNTAGGABLE:
            subprocess.run(
                [cls.ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=1", "-c:a", codec,
                 os.path.join(cls.dir, "sample." + ext)],
                check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "ffmpeg", None):
            shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        if not self.ffmpeg:
            self.skipTest("no bundled ffmpeg to make sample files with")

    def test_every_format_round_trips_tags_and_art(self):
        for ext, _codec in self.CODECS:
            path = os.path.join(self.dir, "sample." + ext)
            self.assertTrue(audio_files.write_tags(path, self.META,
                                                   cover=self.COVER), ext)
            tags = audio_files.read_tags(path)
            self.assertEqual(tags["title"], "A Title", ext)
            self.assertEqual(tags["artist"], "An Artist, Another", ext)
            self.assertEqual(tags["album"], "An Album", ext)
            self.assertEqual(tags["track_no"], 4, ext)
            self.assertEqual(tags["year"], "2011", ext)
            self.assertEqual(audio_files.cover_bytes(path), self.COVER, ext)
            self.assertTrue(tags["has_art"], ext)
            self.assertAlmostEqual(tags["duration"], 1.0, delta=0.2)

    def test_a_format_that_cannot_be_tagged_says_so(self):
        path = os.path.join(self.dir, "sample.wav")
        self.assertFalse(audio_files.write_tags(path, self.META))
        # And it is still indexable, by its filename.
        self.assertTrue(audio_files.is_audio(path))
        self.assertAlmostEqual(audio_files.read_tags(path)["duration"], 1.0,
                               delta=0.2)

    def test_each_format_describes_itself(self):
        """Shown in the queue and while playing, so it has to be right."""
        expected = {"flac": "FLAC 16/", "m4a": "AAC ", "opus": "Opus ",
                    "ogg": "Vorbis ", "mp3": "MP3 "}
        for ext, prefix in expected.items():
            said = audio_files.describe(os.path.join(self.dir,
                                                     "sample." + ext))
            self.assertTrue(said.startswith(prefix),
                            "%s described as %r" % (ext, said))
        self.assertIn("kHz", audio_files.describe(
            os.path.join(self.dir, "sample.flac")))
        self.assertIn("kbps", audio_files.describe(
            os.path.join(self.dir, "sample.mp3")))

    def test_a_lossless_file_reports_a_bitrate(self):
        """FLAC has no bitrate field; without one it sorts as though free."""
        path = os.path.join(self.dir, "sample.flac")
        self.assertGreater(audio_files.read_tags(path)["bitrate"] or 0, 0)


class FormatsWithoutFfmpeg(unittest.TestCase):
    """The parts that need no real audio to check."""

    def test_it_knows_what_it_will_index(self):
        for name in ("a.mp3", "A.FLAC", "b.m4a", "c.opus", "d.Ogg", "e.wav"):
            self.assertTrue(audio_files.is_audio(name), name)
        for name in ("cover.jpg", "notes.txt", "stream.webm", "a.mp3.part"):
            self.assertFalse(audio_files.is_audio(name), name)

    def test_an_untagged_file_still_reads_as_its_filename(self):
        directory = tempfile.mkdtemp(prefix="eg-untagged-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "Some Artist - Some Song.mp3")
        with open(path, "wb") as handle:
            handle.write(b"not really an mp3")
        tags = audio_files.read_tags(path)
        self.assertEqual(tags["artist"], "Some Artist")
        self.assertEqual(tags["title"], "Some Song")
        self.assertEqual(tags["has_art"], 0)


class LibraryFolders(unittest.TestCase):
    """One flat folder was the only thing the index understood."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="eg-roots-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.ix = LibraryIndex(os.path.join(self.home, "t.db"))
        self.addCleanup(self.ix.close)

    def _make(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x" * 32)
        return path

    def test_it_walks_into_subfolders(self):
        """A collection anyone has kept is in Artist/Album folders."""
        self._make("music", "Artist", "Album", "One - Song.mp3")
        self._make("music", "Artist", "Album", "Two - Song.flac")
        self._make("music", "loose.opus")
        added, _updated, _removed = self.ix.scan(
            [os.path.join(self.home, "music")])
        self.assertEqual(added, 3)

    def test_it_indexes_more_than_one_folder(self):
        self._make("a", "one.mp3")
        self._make("b", "two.flac")
        added, _u, _r = self.ix.scan([os.path.join(self.home, "a"),
                                      os.path.join(self.home, "b")])
        self.assertEqual(added, 2)

    def test_it_ignores_things_that_are_not_music(self):
        self._make("music", "song.mp3")
        self._make("music", "cover.jpg")
        self._make("music", "notes.txt")
        self._make("music", "node_modules", "pkg", "buried.mp3")
        added, _u, _r = self.ix.scan([os.path.join(self.home, "music")])
        self.assertEqual(added, 1)

    def test_an_unplugged_drive_does_not_empty_the_library(self):
        """Only files missing from a folder actually looked at are forgotten.

        Otherwise scanning with an external drive disconnected would delete
        every track on it, including its play counts and its place in every
        playlist.
        """
        self._make("a", "one.mp3")
        self._make("b", "two.mp3")
        roots = [os.path.join(self.home, "a"), os.path.join(self.home, "b")]
        self.ix.scan(roots)
        self.assertEqual(self.ix.count(), 2)

        _a, _u, removed = self.ix.scan([roots[0]])       # b is "unplugged"
        self.assertEqual(removed, 0)
        self.assertEqual(self.ix.count(), 2)

    def test_removing_a_folder_forgets_its_tracks(self):
        """The other side of that: what is removed has to actually go."""
        self._make("a", "one.mp3")
        self._make("b", "two.mp3")
        roots = [os.path.join(self.home, "a"), os.path.join(self.home, "b")]
        self.ix.scan(roots)
        self.assertEqual(self.ix.forget_outside([roots[0]]), 1)
        self.assertEqual(self.ix.count(), 1)

    def test_a_deleted_file_is_still_forgotten(self):
        path = self._make("a", "one.mp3")
        root = os.path.join(self.home, "a")
        self.ix.scan([root])
        os.remove(path)
        _a, _u, removed = self.ix.scan([root])
        self.assertEqual(removed, 1)
        self.assertEqual(self.ix.count(), 0)


class KeepingTheSourceFormat(unittest.TestCase):
    """Every download was re-encoded to MP3 at 192kbps.

    YouTube serves Opus at around 160, so that was a lossy source decoded
    and encoded again into a lossy target -- a copy strictly worse than what
    it was made from, and worse than what Spotify streams.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-keep-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _touch(self, name):
        path = os.path.join(self.dir, name)
        open(path, "wb").close()
        return path

    def test_the_default_download_is_never_re_encoded(self):
        """The whole of the change, in the one place it is decided.

        Nothing else can check this: it takes a real download to see, and by
        then every track fetched has already been made worse.
        """
        post = downloader.audio_postprocessor("original")
        self.assertEqual(post["preferredcodec"], "best")
        self.assertNotIn("preferredquality", post)
        for value in (None, "", "source", "best"):
            self.assertEqual(
                downloader.audio_postprocessor(value)["preferredcodec"],
                "best", repr(value))

    def test_asking_for_a_bitrate_still_gets_mp3(self):
        post = downloader.audio_postprocessor("320")
        self.assertEqual(post["preferredcodec"], "mp3")
        self.assertEqual(post["preferredquality"], "320")

    def test_original_is_the_default_and_a_bitrate_is_not(self):
        for value in (None, "", "original", "ORIGINAL", "best", "source"):
            self.assertTrue(downloader.keeps_original(value), repr(value))
        for value in ("192", "320", "128"):
            self.assertFalse(downloader.keeps_original(value), repr(value))

    def test_the_finished_file_is_found_whatever_its_extension(self):
        base = os.path.join(self.dir, "Artist - Title")
        self.assertIsNone(downloader.downloaded_file(base))
        path = self._touch("Artist - Title.opus")
        self.assertEqual(downloader.downloaded_file(base), path)

    def test_a_leftover_scrap_is_not_mistaken_for_the_download(self):
        base = os.path.join(self.dir, "Artist - Title")
        self._touch("Artist - Title.webm.part")
        self._touch("Artist - Title.ytdl")
        self.assertIsNone(downloader.downloaded_file(base))

    def test_cleanup_keeps_the_audio_and_removes_the_rest(self):
        base = os.path.join(self.dir, "Artist - Title")
        keep = self._touch("Artist - Title.m4a")
        self._touch("Artist - Title.webm.part")
        self._touch("Artist - Title.info.json")
        downloader._cleanup_partials(base)
        self.assertTrue(os.path.exists(keep))
        self.assertEqual(
            sorted(os.listdir(self.dir)), ["Artist - Title.m4a"])


class ImportSlotsFollowTheFormat(unittest.TestCase):
    """A playlist slot is reserved before the file exists.

    It was reserved as `<name>.mp3`, which is no longer what a download
    produces -- so every imported playlist would have come out missing every
    track it had to fetch.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-slot-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.meta = spotify_import.as_metadata(_sp_track("Song", "Artist"))

    def test_a_slot_resolves_to_whatever_landed(self):
        predicted = spotify_import.predicted_path(self.meta, self.dir)
        self.assertIsNone(spotify_import.resolve(predicted))

        real = os.path.splitext(predicted)[0] + ".opus"
        open(real, "wb").close()
        self.assertEqual(spotify_import.resolve(predicted), real)

    def test_a_track_owned_as_opus_is_not_queued_again(self):
        predicted = spotify_import.predicted_path(self.meta, self.dir)
        open(os.path.splitext(predicted)[0] + ".m4a", "wb").close()
        paths, missing = spotify_import.plan([self.meta], {}, self.dir)
        self.assertEqual(missing, [])
        self.assertTrue(paths[0].endswith(".m4a"))


def _sine(db, rate=48000, secs=4.0, freq=1000.0, channels=2):
    """A tone at a known level, which is what the standard calibrates on."""
    import numpy as np
    amp = 10.0 ** (db / 20.0)
    t = np.arange(int(rate * secs)) / float(rate)
    return np.repeat((amp * np.sin(2 * np.pi * freq * t))[:, None],
                     channels, axis=1)


class LoudnessMeasurement(unittest.TestCase):
    """ITU-R BS.1770, checked against the numbers the standard publishes.

    Worth checking rather than assuming: a plausible-looking filter design
    read 0.25dB low at every level, which is a kind of wrong that never
    announces itself -- everything still works, everything is quietly a
    quarter of a decibel off, forever.
    """

    # The coefficients BS.1770-4 tabulates for 48kHz.
    PUBLISHED = (
        [1.53512485958697, -2.69169618940638, 1.19839281085285],
        [1.0, -1.69065929318241, 0.73248077421585],
        [1.0, -2.0, 1.0],
        [1.0, -1.99004745483398, 0.99007225036621],
    )

    def test_the_filter_matches_the_published_coefficients(self):
        import numpy as np
        (b1, a1), (b2, a2) = loudness._k_weighting(48000)
        for mine, published in zip((b1, a1, b2, a2), self.PUBLISHED):
            self.assertLess(float(np.max(np.abs(np.asarray(mine)
                                                - np.asarray(published)))),
                            1e-12)

    def test_a_tone_reads_its_own_level(self):
        """The standard's calibration: 1kHz on both channels reads its dBFS."""
        for rate in (48000, 44100):
            for db in (-23.0, -20.0, -14.0):
                got = loudness.integrated_lufs(_sine(db, rate=rate), rate)
                self.assertAlmostEqual(got, db, delta=0.1,
                                       msg="%ddB at %dHz" % (db, rate))

    def test_silence_at_the_end_does_not_drag_it_down(self):
        """Gating is the whole reason this is not a plain average.

        Without it a track with a long quiet outro measures too quiet, and is
        then played too loud -- which is the opposite of the point.
        """
        import numpy as np
        tone = _sine(-20.0, secs=4.0)
        padded = np.concatenate([tone, np.zeros_like(tone)], axis=0)
        self.assertAlmostEqual(loudness.integrated_lufs(padded, 48000),
                               -20.0, delta=0.3)

    def test_a_quiet_passage_does_not_drag_it_down_either(self):
        """The relative gate, which the silence test does not reach.

        A quiet intro is above the absolute gate but well below the body of
        the track. Averaged in, it makes the track measure quieter than it
        is, and it is then played louder than everything else -- the exact
        problem this is here to fix, arriving through the fix itself.
        """
        import numpy as np
        loud = _sine(-14.0, secs=4.0)
        quiet = _sine(-40.0, secs=4.0)
        mixed = np.concatenate([quiet, loud], axis=0)
        self.assertAlmostEqual(loudness.integrated_lufs(mixed, 48000),
                               -14.0, delta=0.5)

    def test_mono_is_measured_too(self):
        import numpy as np
        mono = _sine(-20.0)[:, :1]
        self.assertIsNotNone(loudness.integrated_lufs(mono, 48000))

    def test_nothing_to_measure_is_not_a_crash(self):
        import numpy as np
        self.assertIsNone(loudness.integrated_lufs(None, 48000))
        self.assertIsNone(loudness.integrated_lufs(np.zeros((10, 2)), 48000))
        self.assertIsNone(loudness.integrated_lufs(_sine(-20.0), 0))
        self.assertIsNone(loudness.integrated_lufs(
            np.zeros((48000 * 2, 2)), 48000))     # digital silence


class LoudnessGain(unittest.TestCase):

    def test_a_quiet_track_is_brought_up_and_a_loud_one_down(self):
        quiet = loudness.gain_for(-24.0, 0.3)
        loud = loudness.gain_for(-6.0, 0.5)
        self.assertGreater(quiet, 1.0)
        self.assertLess(loud, 1.0)

    def test_it_will_not_push_a_track_into_clipping(self):
        """A track already mastered to full scale cannot be turned up.

        Turning it up anyway and letting it clip would be a worse sound than
        leaving it a little quiet.
        """
        gain = loudness.gain_for(-24.0, 1.0)
        self.assertLessEqual(gain * 1.0, 1.0)

    def test_a_very_quiet_recording_is_not_lifted_out_of_its_noise(self):
        self.assertLessEqual(
            loudness.gain_for(-60.0, 0.01),
            10.0 ** (loudness.MAX_GAIN_DB / 20.0) + 1e-9)

    def test_no_measurement_means_leave_it_alone(self):
        self.assertEqual(loudness.gain_for(None, 0.5), 1.0)

    def test_matching_actually_matches(self):
        """Two tones eight decibels apart end up on the same level."""
        import numpy as np
        levels = []
        for db in (-8.0, -16.0, -22.0):
            tone = _sine(db)
            lufs, peak = loudness.measure(tone, 48000)
            adjusted = np.clip(tone * loudness.gain_for(lufs, peak), -1, 1)
            levels.append(loudness.integrated_lufs(adjusted, 48000))
        self.assertLess(max(levels) - min(levels), 0.2)
        for level in levels:
            self.assertAlmostEqual(level, loudness.TARGET_LUFS, delta=0.2)


class EngineLoudness(unittest.TestCase):
    """How the engine decides what to apply, without an audio device."""

    def _engine(self):
        import player_engine
        cls = next(v for v in vars(player_engine).values()
                   if isinstance(v, type) and hasattr(v, "_gain_for"))
        engine = cls.__new__(cls)
        engine.normalise = True
        engine.loudness_for = None
        engine.on_loudness = None
        return engine

    def _data(self, db=-20.0):
        import numpy as np
        return (_sine(db, rate=48000, secs=1.0) * 32767).astype(np.int16)

    def test_it_measures_once_and_reports_what_it_found(self):
        engine = self._engine()
        seen = []
        engine.on_loudness = lambda p, l, k: seen.append((p, l, k))
        gain = engine._gain_for("a.mp3", self._data(), 48000)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0][1], -20.0, delta=0.3)
        self.assertGreater(gain, 1.0)

    def test_a_remembered_measurement_is_not_taken_again(self):
        engine = self._engine()
        engine.loudness_for = lambda path: (-8.0, 0.5)
        engine.on_loudness = lambda *a: self.fail("measured despite the cache")
        self.assertLess(engine._gain_for("a.mp3", self._data(), 48000), 1.0)

    def test_the_playback_thread_never_measures(self):
        """The gapless swap runs inside the audio callback.

        Half a second of measurement there is half a second of silence in
        the middle of a song, so with nothing cached it must decline.
        """
        engine = self._engine()
        engine.on_loudness = lambda *a: self.fail("measured on the audio path")
        self.assertEqual(
            engine._gain_for("a.mp3", self._data(), 48000, measure=False), 1.0)

    def test_switching_it_off_leaves_everything_alone(self):
        engine = self._engine()
        engine.normalise = False
        engine.on_loudness = lambda *a: self.fail("measured while switched off")
        self.assertEqual(engine._gain_for("a.mp3", self._data(), 48000), 1.0)

    def test_the_gain_reaches_the_audio(self):
        """Everything else here is arithmetic nobody hears unless this does."""
        import numpy as np
        engine = self._engine()
        engine.volume = 1.0
        engine.track_gain = 0.5
        chunk = np.full((64, 2), 0.4, dtype=np.float32)
        self.assertTrue(np.allclose(engine._level(chunk), 0.2, atol=1e-3))

        engine.track_gain = 1.0
        engine.volume = 0.25
        self.assertTrue(np.allclose(engine._level(chunk), 0.1, atol=1e-3))

    def test_the_output_is_still_kept_inside_full_scale(self):
        import numpy as np
        engine = self._engine()
        engine.volume = 1.0
        engine.track_gain = 4.0
        loud = np.full((64, 2), 0.9, dtype=np.float32)
        self.assertLessEqual(float(np.max(np.abs(engine._level(loud)))), 1.0)

    def test_nothing_loaded_is_not_a_crash(self):
        engine = self._engine()
        self.assertEqual(engine._gain_for("a.mp3", None, 48000), 1.0)


class PastedLinks(unittest.TestCase):
    """A link to audio is not a search term.

    Everything that was not a Spotify link went to Spotify as a search
    string, so pasting a Bandcamp album searched Spotify for the URL and
    found nothing. It is also the only route to lossless: YouTube has never
    served any, so a track sourced through it is capped around 170kbps
    however it is asked for.
    """

    def test_it_knows_a_link_from_a_search(self):
        for link in ("https://artist.bandcamp.com/album/x",
                     "https://archive.org/details/y",
                     "https://soundcloud.com/a/b",
                     "http://example.com/track.flac"):
            self.assertTrue(downloader.is_media_url(link), link)
        for other in ("https://open.spotify.com/track/x",
                      "https://spotify.com/album/y",
                      "spotify:track:x",
                      "the national bloodbuzz ohio", "", None):
            self.assertFalse(downloader.is_media_url(other), repr(other))

    def test_lossless_is_named_as_such(self):
        self.assertEqual(
            downloader.describe_format({"acodec": "flac", "ext": "flac"}),
            "FLAC, lossless")
        self.assertEqual(
            downloader.describe_format({"acodec": "alac", "abr": 900}),
            "ALAC, lossless")

    def test_a_lossy_format_is_named_with_its_bitrate(self):
        """The number worth knowing, and the one never shown."""
        self.assertEqual(
            downloader.describe_format({"acodec": "opus", "abr": 170.6}),
            "opus 171 kbps")
        self.assertEqual(
            downloader.describe_format({"ext": "m4a", "acodec": "none",
                                        "tbr": 128}),
            "m4a 128 kbps")
        self.assertEqual(downloader.describe_format({}), "")
        self.assertEqual(downloader.describe_format(None), "")

    def test_a_result_becomes_something_downloadable(self):
        meta = downloader.as_source_metadata({
            "track": "Turning", "artist": "Grateful Dead",
            "album": "Barton Hall", "thumbnail": "http://art/x.jpg",
            "duration": 40.5, "track_number": 3, "release_year": 1977,
            "webpage_url": "https://archive.org/x", "acodec": "flac",
        })
        self.assertEqual(meta["name"], "Turning")
        self.assertEqual(meta["artists"], ["Grateful Dead"])
        self.assertEqual(meta["source_url"], "https://archive.org/x")
        self.assertEqual(meta["source_format"], "FLAC, lossless")
        self.assertEqual(meta["duration_ms"], 40500)
        self.assertEqual(meta["release_date"], "1977")

    def test_an_artist_credit_is_kept_whole(self):
        """Splitting it on commas invents names that were never there."""
        meta = downloader.as_source_metadata(
            {"title": "X", "uploader": "Simon, Garfunkel & Friends",
             "webpage_url": "https://e.com/x"})
        self.assertEqual(meta["artists"], ["Simon, Garfunkel & Friends"])

    def test_something_with_no_audio_behind_it_is_skipped(self):
        self.assertIsNone(downloader.as_source_metadata(None))
        self.assertIsNone(downloader.as_source_metadata({"title": "no url"}))
        self.assertIsNone(downloader.as_source_metadata(
            {"webpage_url": "https://e.com/x"}))       # no title

    def test_an_album_falls_back_to_its_own_title(self):
        meta = downloader.as_source_metadata(
            {"title": "Track One", "webpage_url": "https://e.com/1"},
            fallback_album="The Album")
        self.assertEqual(meta["album"], "The Album")


class DownloadsGoWhereTheLinkPoints(unittest.TestCase):
    """A track that came from a link must not be looked for somewhere else.

    Searching YouTube for it would find something that sounds like it, at
    YouTube's quality -- which throws away both the exact recording and the
    only reason to have pasted the link.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-src-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.searched = []
        self.fetched = []

        def no_search(metadata, log_callback=print, num_results=5):
            self.searched.append(metadata)
            return "https://youtube.example/found"

        def fake_download(source_url, base, log_callback=print,
                          quality="original"):
            self.fetched.append(source_url)
            path = base + ".flac"
            open(path, "wb").close()
            return path

        for name, stub in (("pick_youtube_source", no_search),
                           ("download_audio", fake_download),
                           ("apply_metadata", lambda *a, **k: True)):
            original = getattr(downloader, name)
            self.addCleanup(setattr, downloader, name, original)
            setattr(downloader, name, stub)

    def test_a_link_is_fetched_directly(self):
        meta = {"name": "Turning", "artists": ["Grateful Dead"],
                "album": "Barton Hall",
                "source_url": "https://archive.org/x"}
        result = downloader.process_track(None, meta, self.dir,
                                          log_callback=lambda m: None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.fetched, ["https://archive.org/x"])
        self.assertEqual(self.searched, [],
                         "went looking for a track it had the address of")

    def test_a_spotify_lookup_still_has_to_go_and_find_the_audio(self):
        meta = {"name": "Gravity", "artists": ["John Mayer"], "album": "X"}
        downloader.process_track(None, meta, self.dir,
                                 log_callback=lambda m: None)
        self.assertEqual(self.fetched, ["https://youtube.example/found"])
        self.assertEqual(len(self.searched), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
