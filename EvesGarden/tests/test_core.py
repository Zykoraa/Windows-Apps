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
import colorsys
import downloader
import smart_playlists
import themes


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
