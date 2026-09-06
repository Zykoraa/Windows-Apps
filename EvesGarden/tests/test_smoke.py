"""Launch the real window and walk every surface in it.

Every regression this project has had was a widget that crashed or drew
nothing, and every one of them was found by a person opening the app and
noticing. The unit tests never would have: they cover the index, the queue and
the filename rules, and not one of them creates a window.

Worse, Tk hides exactly this class of failure. An exception raised inside a
callback goes to Tkinter's report_callback_exception, which prints a traceback
and carries on -- so a broken panel leaves the app running and looking almost
right. That is how a CTkFrame sized in place(), a cross-fade onto a label that
no longer existed, and a cover placeholder drawn in near-black on near-black
all reached the point of being screenshotted.

So this walks the app the way a person would and fails on anything that
reaches that handler, or that prints a traceback from the worker-callback
pump. It runs against a temporary config directory and an empty library, so
it touches nothing of the user's and every surface is exercised in its empty
state -- which is where these crashes tend to live.
"""

import io
import os
import shutil
import sys
import tempfile
import traceback
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WALK_TIMEOUT_MS = 90_000


class OfflineCatalogue:
    """Stands in for the keyless metadata provider.

    The palette searches a provider on a worker thread, and since that gained
    a fallback needing no account it became a live network call -- which made
    this test depend on Apple's servers being up, and land its results at an
    unpredictable moment. Canned results exercise the same code path without
    either problem. No cover_url, so no artwork fetch either.
    """

    name = "Offline"

    @staticmethod
    def search(query, limit=25):
        return [{"source": "offline", "id": "offline:%d" % i,
                 "title": "Result %d for %s" % (i, query),
                 "artists": ["Someone"], "artist": "Someone",
                 "album": "An Album", "year": "2020", "duration": 180.0,
                 "duration_ms": 180000, "url": "", "cover_url": None,
                 "cover_large": None}
                for i in range(min(3, limit))]

    @staticmethod
    def search_artists(query, limit=6):
        return [{"source": "offline", "id": "offline:artist:%d" % i,
                 "name": "Artist %d for %s" % (i, query), "url": "",
                 "image_url": None, "genres": ["Rock"]}
                for i in range(min(2, limit))]

    @staticmethod
    def artist_albums(artist):
        return [{"source": "offline", "id": "offline:album:%d" % i,
                 "name": "Album %d" % i, "artist": artist["name"],
                 "year": "200%d" % i,
                 "album_type": ("album", "single", "compilation")[i],
                 "total_tracks": 3, "url": "", "cover_url": None,
                 "cover_large": None}
                for i in range(3)]

    @staticmethod
    def search_albums(name, artist="", limit=10):
        return [{"source": "offline", "id": "offline:album:0", "name": name,
                 "artist": artist or "Someone", "year": "2006",
                 "album_type": "album", "total_tracks": 3, "url": "",
                 "cover_url": None, "cover_large": None}]

    @staticmethod
    def album_tracks(album):
        return [{"source": "offline", "id": "%s:t%d" % (album["id"], i),
                 "title": "Track %d" % i, "artists": ["Someone"],
                 "artist": "Someone", "album": album["name"],
                 "year": album["year"], "duration": 180.0,
                 "duration_ms": 180000, "url": "", "cover_url": None,
                 "cover_large": None, "track_number": i + 1,
                 "disc_number": 1, "album_artist": "Someone"}
                for i in range(3)]


def _has_display():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


HAS_DISPLAY = _has_display()


class FakeEvent:
    """Enough of a Tk event for the window-drag handlers."""

    def __init__(self, x=0, y=0, x_root=0, y_root=0):
        self.x, self.y = x, y
        self.x_root, self.y_root = x_root, y_root


class SurfaceWalk:
    """Drives one App through every screen, collecting anything that broke."""

    def __init__(self, gui, app):
        self.gui = gui
        self.app = app
        self.errors = []
        self.visited = []

    # ------------------------------------------------------------ capture

    def watch(self):
        def report(exc, value, tb):
            self.errors.append("callback exception in %s:\n%s" % (
                self.visited[-1] if self.visited else "startup",
                "".join(traceback.format_exception(exc, value, tb))))
        self.app.report_callback_exception = report

    # --------------------------------------------------------------- walk

    def steps(self):
        app = self.app
        app.player.set_volume(0.0)
        yield 1200

        # A fresh config directory means no Spotify credentials, so the
        # first-run setup opens over everything. Close it and carry on.
        if getattr(app, "setup_overlay", None) is not None:
            self.mark("close first-run setup")
            app.close_setup()
            yield 250

        for view in ("Songs", "Liked", "Recent", "Playlists", "Albums",
                     "Artists", "Duplicates"):
            self.mark("library view: %s" % view)
            app.set_library_view(view)
            yield 160
            assert app.view_tabs.get() == view, "tab strip did not follow"

        self.mark("sort order")
        app.set_library_sort(list(self.gui.SORTS)[1])
        yield 160
        app.set_library_view("Songs")
        yield 160

        for label, opener, closer in (
            ("downloader", app.open_downloader, app.close_downloader),
            ("now playing", app.toggle_now_playing_overlay,
             app.toggle_now_playing_overlay),
            ("queue panel", app.toggle_queue, app.toggle_queue),
            ("equaliser", app.toggle_eq, app.toggle_eq),
        ):
            self.mark("open %s" % label)
            opener()
            yield 420
            self.mark("close %s" % label)
            closer()
            yield 320

        # Searching an artist's name used to answer with their loose tracks
        # only, so there was no way through to an album. Walk the whole path:
        # the results, the discography, one album opened, and back again.
        self.mark("open downloader for search")
        app.open_downloader()
        yield 400

        self.mark("only one search surface in the downloader")
        entries = [w for w in self._widgets(app.dl_frame)
                   if isinstance(w, self.gui.ctk.CTkEntry)]
        assert len(entries) == 1, (
            "the downloader is showing %d search boxes; the old suggestions "
            "panel used to float a second one under the real one whenever "
            "Spotify was connected" % len(entries))
        assert not hasattr(app, "suggestions_frame"), (
            "the floating suggestions panel is back")

        self.mark("search results list artists as well as songs")
        app.url_entry.delete(0, "end")
        app.url_entry.insert(0, "someone")
        app.run_discover_search()
        yield from self._until("the search never answered",
                               lambda: bool(app.discover_artists))
        headings = self._labels(app.results_frame)
        assert "Artists" in headings and "Songs" in headings, (
            "results panel showed %s, not both artists and songs" % headings)

        self.mark("open a discography")
        app.open_artist(app.discover_artists[0])
        yield from self._until(
            "the discography never rendered",
            lambda: self._button(app.results_frame, "Back to results") is not None)
        assert self._button(app.results_frame, "Download album") is not None, (
            "a discography with no way to take an album")
        headings = self._labels(app.results_frame)
        assert "Albums" in headings and "Singles & EPs" in headings, (
            "releases were not grouped by kind: %s" % headings)

        self.mark("expand an album")
        toggle = self._button(app.results_frame, "\u25bc")
        assert toggle is not None, "an album that cannot be opened"
        toggle.invoke()
        yield from self._until(
            "the album never opened",
            lambda: "Track 1" in self._labels(app.results_frame))

        self.mark("collapse it again")
        self._button(app.results_frame, "\u25b2").invoke()
        yield from self._until(
            "collapsing an album left its tracks on screen",
            lambda: "Track 1" not in self._labels(app.results_frame))

        self.mark("back to the results")
        self._button(app.results_frame, "Back to results").invoke()
        yield from self._until(
            "going back did not restore the search results",
            lambda: "Songs" in self._labels(app.results_frame))

        # The transport used to be a dead end: the two things written in it
        # are the two places you would want to go next.
        self.mark("the transport title opens the album")
        app._now_playing_row = {"path": None, "title": "Gravity",
                                "artist": "John Mayer, Tom Misch",
                                "album": "Continuum", "cover_url": None}
        app._browse_playing_album()
        yield from self._until(
            "the album never opened from the transport",
            lambda: "Continuum" in self._labels(app.results_frame))
        assert self._button(app.results_frame, "Download album") is not None, (
            "the album opened with no way to take it")
        assert self._button(app.results_frame, "Back to results") is not None, (
            "the album opened with no way back out")

        self.mark("the transport credit opens the artist")
        app._browse_playing_artist()
        yield from self._until(
            "the discography never opened from the transport",
            lambda: self._button(app.results_frame, "Back to results") is not None
                    and "Albums" in self._labels(app.results_frame))

        self.mark("a track with no album says so rather than guessing")
        app._now_playing_row = {"path": None, "title": "Something",
                                "artist": "Someone", "album": "",
                                "cover_url": None}
        app._browse_playing_album()
        yield 200

        self.mark("close downloader after search")
        app.close_downloader()
        yield 320

        # The lyrics pane has three shapes and none of them can be reached
        # without the network, so they are handed straight to the renderer.
        app.toggle_now_playing_overlay()
        yield 340

        self.mark("lyrics: timed, with an instrumental gap")
        app.setup_lyrics(([(0.0, "The first line"), (12.5, "The second line"),
                           (35.4, ""), (48.0, "After the break")], True))
        yield 260
        assert len(app.parsed_lyrics) == 4, "timed lines did not reach the loop"
        assert len(app.lyrics_labels) == 4, (
            "labels and timings must stay index-aligned, or the highlight "
            "lands on the wrong line")
        app._lyric_style(1, "active")
        app._lyric_style(2, "active")      # the gap: no pill around nothing
        app._lyric_style(0, "past")
        app._scroll_lyric_into_view(3)
        yield 220

        # A long song, because the bug this covers only shows once the words
        # outgrow the pane: CTkScrollableFrame keeps the canvas scrollregion
        # up to date from its own <Configure> binding, and binding over that
        # without add="+" left the canvas believing the whole song already
        # fit. Nothing scrolled -- not the follow, not the wheel.
        self.mark("lyrics: a song longer than the pane")
        app.setup_lyrics(([(float(i * 3),
                            "Line %d of a song with enough words to wrap" % i)
                           for i in range(60)], True))
        yield 400
        canvas = app.lyrics_scroll._parent_canvas
        assert canvas.cget("scrollregion"), (
            "the lyrics canvas has no scrollregion, so it cannot scroll at all")
        first, last = canvas.yview()
        assert last - first < 0.9, (
            "the pane thinks a 60-line song already fits: yview=%s"
            % (canvas.yview(),))

        self.mark("lyrics: the song scrolls itself")
        app._lyrics_touched_at = 0.0
        app._scroll_lyric_into_view(50, smooth=False)
        yield 260
        moved = canvas.yview()[0]
        assert moved > 0.5, (
            "following the song did not move the pane: yview=%s" % (canvas.yview(),))

        self.mark("lyrics: a hand on the pane wins")
        app._lyrics_user_took_over()
        app._scroll_lyric_into_view(0, smooth=False)
        yield 160
        assert canvas.yview()[0] == moved, (
            "the song pulled the pane back while it was being read")

        self.mark("lyrics: and it takes it back afterwards")
        app._lyrics_touched_at = 0.0
        app._scroll_lyric_into_view(0, smooth=False)
        yield 160
        assert canvas.yview()[0] < 0.1, (
            "the song never took the pane back after the reader let go")

        self.mark("lyrics: words with no timings")
        app.setup_lyrics(([(None, "Just the words"), (None, ""),
                           (None, "On three lines")], False))
        yield 260
        assert app.parsed_lyrics == [], (
            "untimed lines must not drive the highlight loop -- it compares "
            "them against the playhead")
        assert len(app.lyrics_labels) == 3

        self.mark("lyrics: nothing found")
        app.setup_lyrics(([], False))
        yield 220
        app.toggle_now_playing_overlay()
        yield 300

        self.mark("visualiser draws behind now playing")
        app.toggle_now_playing_overlay()
        yield 420
        canvas = app.np_canvas
        keep = set(canvas.find_all()) - set(canvas.find_withtag(self.gui.visualizers.TAG))
        assert keep, "the now playing canvas was empty before the visualiser"
        app._draw_bands([0.5] * 64)
        yield 220
        viz = canvas.find_withtag(self.gui.visualizers.TAG)
        assert viz, "the visualiser drew nothing behind now playing"
        assert keep <= set(canvas.find_all()), (
            "drawing the visualiser wiped the cover or the titles off the "
            "canvas it shares with them")

        self.mark("the backdrop stays in its band")
        height = canvas.winfo_height()
        floor = height * (1.0 - app.NP_VIZ_BAND) - 4
        highest = min(canvas.bbox(item)[1] for item in viz)
        assert highest >= floor, (
            "the backdrop reached %dpx up a %dpx canvas, past the %dpx band"
            % (height - highest, height, height * app.NP_VIZ_BAND))

        self.mark("the pickers are clear of the cards")
        def box(widget):
            return (widget.winfo_rootx(), widget.winfo_rooty(),
                    widget.winfo_rootx() + widget.winfo_width(),
                    widget.winfo_rooty() + widget.winfo_height())
        for picker in (app.viz_dropdown, app.viz_palette_dropdown):
            for card, name in ((app.np_lyrics_card, "lyrics"),
                               (app.np_queue_card, "queue")):
                if not (picker.winfo_ismapped() and card.winfo_ismapped()):
                    continue
                ax0, ay0, ax1, ay1 = box(picker)
                bx0, by0, bx1, by1 = box(card)
                assert not (ax0 < bx1 and bx0 < ax1
                            and ay0 < by1 and by0 < ay1), (
                    "a visualiser picker is sitting on top of the %s card, "
                    "covering its heading" % name)

        self.mark("the titles stay on top of it")
        order = canvas.find_all()
        assert order.index(app._np_title_id) > order.index(viz[-1]), (
            "the title is underneath the backdrop")

        self.mark("closing now playing clears the backdrop")
        app.toggle_now_playing_overlay()
        yield 420
        assert not canvas.find_withtag(self.gui.visualizers.TAG), (
            "the last frame was left on the canvas after closing")

        self.mark("command palette")
        app.toggle_palette()
        yield 320
        palette = app._palette
        palette.entry.insert(0, "a")
        palette._on_typed()
        yield 220
        palette._move(1)
        palette._move(-1)
        yield 120
        self.mark("close palette")
        palette.close()
        yield 200

        # The header row is over-subscribed at the size the app opens at,
        # so the masthead stands down in stages to pay for it. What must
        # never happen again is a control being sliced instead: "New
        # playlist" came out 32px wide and the import button did not appear
        # at all, which reads as a rendering bug rather than a layout one.
        self.mark("header at the widths it has to survive")
        # 900 is the narrowest the window can be dragged to, so it is the one
        # that has to hold.
        for width in (1600, 1340, 1100, 900):
            app.geometry("%dx700+20+20" % width)
            yield 260
            shape = None
            for view in ("Songs", "Playlists", "Duplicates"):
                app.set_library_view(view)
                yield 200
                # What the header carries must depend on the window width and
                # nothing else. Sizing it for the view on screen meant the row
                # rearranged as you moved between tabs -- and at 1100px the
                # tab strip you had just clicked was what disappeared.
                here = (app.view_tabs.winfo_manager() == "pack",
                        app.brand.winfo_manager() == "pack",
                        app.brand_word.winfo_manager() == "pack",
                        app.nav_dl_btn.cget("text"))
                assert shape is None or here == shape, (
                    "header rearranged on %s at %dpx: %r then %r"
                    % (view, width, shape, here))
                shape = here
                # Exactly one way to change view, always.
                assert (app.view_tabs.winfo_manager() == "pack") !=                        (app.view_menu.winfo_manager() == "pack"), (
                    "tab strip and view dropdown must not both be %s at %d"
                    % ("hidden" if app.view_tabs.winfo_manager() != "pack"
                       else "shown", width))
                # Re-packing sends a widget to the end of its side, so the
                # control that changes view can come back to the right of
                # the search box it is supposed to sit before.
                showing = (app.view_tabs
                           if app.view_tabs.winfo_manager() == "pack"
                           else app.view_menu)
                assert showing.winfo_rootx() < app.lib_search_entry.winfo_rootx(), (
                    "view control ended up right of the search box at %d on %s"
                    % (width, view))
                # The search box is the one thing the row never gives up.
                assert app.lib_search_entry.winfo_width() >= 100, (
                    "search box down to %dpx at %d on %s"
                    % (app.lib_search_entry.winfo_width(), width, view))
                # The bottom bar has the same problem and had it worse: all
                # three of its columns stretched, so when it ran out of room
                # the EQ button was drawn as a 15px sliver of a pill.
                app._set_now_playing_text(
                    "A Track With A Rather Long Name On It",
                    "Some Artist · An Album With A Long Name")
                for child in (app.eq_toggle_btn, app.viz_toggle_btn,
                              app.queue_btn, app.volume_icon):
                    assert child.winfo_width() >= child.winfo_reqwidth() - 1, (
                        "%s squeezed to %dpx of %dpx at %d"
                        % (child.__class__.__name__, child.winfo_width(),
                           child.winfo_reqwidth(), width))
                assert app.bottom_bar.winfo_height() <= 140, (
                    "bottom bar grew to %dpx at %d -- a fixed-size frame with "
                    "no height asked for defaults to 200"
                    % (app.bottom_bar.winfo_height(), width))
                for child in app.library_actions.winfo_children():
                    if child.winfo_manager() != "pack":
                        continue
                    try:
                        name = child.cget("text")
                    except Exception:
                        name = child.__class__.__name__
                    assert child.winfo_width() >= child.winfo_reqwidth(), (
                        "%r squeezed to %dpx of %dpx at %d on %s"
                        % (name, child.winfo_width(), child.winfo_reqwidth(),
                           width, view))
                if app.brand.winfo_manager() == "pack":
                    # Re-packing sends a widget to the end of its side unless
                    # it says otherwise, so a masthead that stood down and
                    # came back can reappear in the middle of the row.
                    ahead = (app.view_tabs
                             if app.view_tabs.winfo_manager() == "pack"
                             else app.view_menu)
                    assert app.brand.winfo_rootx() < ahead.winfo_rootx(), (
                        "masthead came back right of the tabs at %d on %s"
                        % (width, view))
        app.geometry("1280x800+40+40")
        yield 260
        app.set_library_view("Songs")
        yield 200

        # The import picker is the one surface that needs a Spotify account
        # to reach, so a walk of the app can never open it. Build it here
        # instead: it is a dialog full of freshly constructed widgets, which
        # is precisely where this app's crashes have lived.
        self.mark("spotify import picker")
        picker = self.gui.dialogs.PlaylistPicker(app, app.theme, [
            {"id": "liked-songs", "name": "Liked Songs", "owner": "you",
             "total": 412, "mine": True, "readable": True, "liked": True},
            {"id": "p1", "name": "Late night", "owner": "you",
             "total": 1, "mine": True, "readable": True, "liked": False},
            {"id": "p2", "name": "Shared with you", "owner": "Ada",
             "total": 90, "mine": False, "readable": True, "liked": False},
            # Spotify refuses this one, so the row must be dead and say why.
            {"id": "p3", "name": "One you only follow", "owner": "Mason",
             "total": 87, "mine": False, "readable": False, "liked": False},
        ])
        picker.present()          # not show(): that blocks on its own loop
        yield 320
        picker._set_all(True)
        yield 120
        assert len(picker._chosen()) == 3, (
            "All ticked %d of 4 -- a playlist Spotify will not serve must "
            "not be selectable" % len(picker._chosen()))
        picker._set_all(False)
        yield 120
        self.mark("close spotify import picker")
        picker.close()
        yield 260

        # What Spotify took. Neither of these can be reached without an
        # account and a playlist that has actually lost something, so both
        # are built here with the losses they are meant to describe.
        self.mark("what changed on Spotify")
        report = [
            {"name": "Late night", "error": None, "returned": 0,
             "losses": [
                 {"title": "A Song", "artist": "An Artist",
                  "reason": "unavailable", "kept": True},
                 {"title": "Another", "artist": "Someone Else",
                  "reason": "removed", "kept": False},
             ],
             "added": [{"name": "New One", "artists": ["Third"],
                        "spotify_url": "u/new"}]},
            {"name": "Unreadable", "error": "403 Forbidden"},
        ]
        changes = self.gui.dialogs.PlaylistChanges(app, app.theme, report)
        changes.present()
        yield 320
        self.mark("close what changed")
        changes.close()
        yield 260

        # And the same thing where it lives afterwards: a playlist of what is
        # gone, with the ones still on disk playable and the rest dimmed.
        self.mark("gone from Spotify")
        playlist = app.index.create_playlist("Late night")
        app.index.watch_playlist(playlist, "spotify", "abc", "Late night")
        app.index.record_seen(playlist, [
            {"url": "u/a", "title": "A Song", "artist": "An Artist",
             "path": None},
            {"url": "u/b", "title": "Another", "artist": "Someone Else",
             "path": None},
        ])
        app.index.mark_gone(playlist, ["u/a"], "unavailable")
        app.index.mark_gone(playlist, ["u/b"], "removed")
        app.library.invalidate()
        app.set_library_view("Playlists")
        yield 260
        app.library.open_vanished()
        yield 320
        assert len(app.library.rows) == 2, (
            "two tracks have gone; the list shows %d" % len(app.library.rows))
        self.mark("back out of gone from Spotify")
        app.clear_library_filter()
        yield 200
        assert not app.library.vanished, "Back did not leave the list"
        app.index.delete_playlist(playlist)
        app.library.invalidate()
        app.set_library_view("Songs")
        yield 200

        # The library folder picker, which is the only way in for anybody
        # who already owns music.
        self.mark("music folders")
        folders = self.gui.dialogs.FolderPicker(
            app, app.theme, [self.gui.LIBRARY_DIR, r"D:\Music\Archive"],
            fixed=(self.gui.LIBRARY_DIR,))
        folders.present()
        yield 300
        # The download folder cannot be removed; the other one can.
        folders._remove(r"D:\Music\Archive")
        yield 120
        assert folders.roots == [self.gui.LIBRARY_DIR], folders.roots
        self.mark("close music folders")
        folders.close()
        yield 260

        # An account with no playlists at all still has to render something.
        self.mark("import picker with nothing on the account")
        empty = self.gui.dialogs.PlaylistPicker(app, app.theme, [])
        empty.present()
        yield 260
        empty.close()
        yield 260

        # No credentials in a fresh config directory, so this takes the
        # "tell them what is missing" path rather than opening anything.
        self.mark("import with no Spotify connected")
        app.set_library_view("Playlists")
        yield 160
        app.import_from_spotify()
        yield 200
        app.set_library_view("Songs")
        yield 160

        # Hover the seek bar first. Its knob is a cached image, and a theme
        # change drops that cache -- but only a bar that has been hovered has
        # ever assigned one, so without this the walk switched themes with
        # nothing to invalidate and sailed past a real crash.
        self.mark("hover the seek bar and leave again")
        bar = app.progress_slider
        bar._hover = True
        bar._hover_x = 40
        bar._redraw()          # assigns the knob sprite
        yield 120
        bar._hover = False
        bar._hover_x = None
        bar._redraw()          # hides it again, still holding the reference
        yield 120

        # Both directions: the light themes are the ones that break, and
        # switching back has its own repaint path.
        for name in ("Rose Pine Dawn", "Nordic Light", "Spotify Classic",
                     "Tokyo Night"):
            self.mark("theme: %s" % name)
            app.change_theme(name)
            yield 260

        # Every mode, drawn on the canvas it now shares with the cover and
        # the cards. A mode that forgets to tag what it draws would clear
        # them, and a mode that throws would take the whole backdrop down.
        self.mark("visualiser modes")
        app.toggle_now_playing_overlay()
        yield 300
        protected = (set(app.np_canvas.find_all())
                     - set(app.np_canvas.find_withtag(self.gui.visualizers.TAG)))
        for index in range(len(self.gui.VIZ_MODES)):
            app.set_visualizer_mode(index)
            app._draw_bands([0.4 + 0.3 * ((i * 7) % 11) / 11.0
                             for i in range(64)])
            yield 30
            assert protected <= set(app.np_canvas.find_all()), (
                "mode %r wiped the cover or the titles off the shared canvas"
                % self.gui.VIZ_MODES[index])
        for name in self.gui.visualizers.palette_names():
            app.set_visualizer_palette(name)
            app._draw_bands([0.5] * 64)
            yield 20
        app.toggle_now_playing_overlay()
        yield 240

        # Windows decides whether it will snap or tile a window from its
        # style bits alone. A frameless window is a WS_POPUP and gets neither
        # unless it also says it is sizable and can be maximised -- without
        # these, Win+Left does nothing, dragging to an edge does nothing, and
        # a tiling manager will not take the window at all.
        self.mark("window can still be snapped")
        if sys.platform == "win32":
            import ctypes
            hwnd = app._hwnd()
            assert hwnd, "no top-level window handle"
            style = ctypes.windll.user32.GetWindowLongW(hwnd, app.GWL_STYLE)
            for bit, name in ((app.WS_THICKFRAME, "WS_THICKFRAME"),
                              (app.WS_MAXIMIZEBOX, "WS_MAXIMIZEBOX"),
                              (app.WS_MINIMIZEBOX, "WS_MINIMIZEBOX")):
                assert style & bit, (
                    "%s is not set, so Windows will not snap this window"
                    % name)
        yield 80

        # Dragging the title bar handed the whole move to Windows for one
        # release -- ReleaseCapture, then SendMessage(WM_NCLBUTTONDOWN),
        # which is the usual recipe for edge snapping on a frameless window.
        # It aborted the interpreter the first time anybody clicked the title
        # bar: the modal move loop dispatches back into Tk while ctypes still
        # has the GIL released for the call it has not returned from.
        #
        #     Fatal Python error: PyEval_RestoreThread: the function must be
        #     called with the GIL held, but the GIL is released
        #
        # That is not an exception and cannot be caught, so this step passes
        # by the process still being here afterwards.
        self.mark("drag the title bar and let go")
        before = app.geometry()
        app.get_pos(FakeEvent(x=40, y=10, x_root=500, y_root=300))
        yield 60
        app.move_window(FakeEvent(x=40, y=10, x_root=560, y_root=340))
        yield 60
        app.end_drag(FakeEvent(x=40, y=10, x_root=560, y_root=340))
        yield 60
        assert not app._dragging, "the drag never ended"

        # Let go against the left edge and the window takes that half --
        # unless half is narrower than the window is allowed to be, which is
        # the case on a 1024px CI runner, and there it must decline rather
        # than wedge itself against its own minimum.
        self.mark("drop the window on the left edge")
        left, top, right, bottom = app._work_area()
        assert right > left and bottom > top, "no usable work area"
        half = (right - left) // 2
        was = app.winfo_width()
        app.get_pos(FakeEvent(x=40, y=10, x_root=500, y_root=300))
        yield 60
        app.end_drag(FakeEvent(x=1, y=10, x_root=left, y_root=top + 300))
        yield 200
        if half >= app.MIN_W:
            assert app.winfo_width() <= half + 4, (
                "dropping on the left edge did not take half the screen: "
                "%dpx of %d" % (app.winfo_width(), right - left))
        else:
            assert app.winfo_width() == was, (
                "snapped to %dpx when half the screen is %dpx and the window "
                "may not go below %dpx"
                % (app.winfo_width(), half, app.MIN_W))
        app.geometry(before)
        yield 200

        self.mark("transport with nothing loaded")
        app.toggle_play_pause()
        yield 120
        app.toggle_shuffle()
        app.toggle_repeat()
        app._toggle_mute()
        yield 200

    def _until(self, what, ready, budget=8000, step=100):
        """Yield until `ready()` holds, rather than guessing a delay.

        A render handed over from a worker thread lands whenever the UI pump
        next runs, and how soon that is depends on what else the app has in
        flight -- so a walk that waits a fixed number of milliseconds passes
        or fails by luck.
        """
        waited = 0
        while waited < budget:
            if ready():
                return
            yield step
            waited += step
        raise AssertionError("%s within %dms" % (what, budget))

    def _widgets(self, parent):
        found = [parent]
        for child in parent.winfo_children():
            found.extend(self._widgets(child))
        return found

    def _labels(self, parent):
        """Every piece of text actually on screen in a panel.

        Mapped only: collapsing an album unmaps its tracks but keeps them, so
        that re-opening it is instant -- and a walk that asked which widgets
        exist rather than which are visible could not tell the difference.
        """
        out = []
        for widget in self._widgets(parent):
            try:
                if not widget.winfo_ismapped():
                    continue
                text = widget.cget("text")
            except Exception:
                continue
            if isinstance(text, str) and text:
                out.append(text)
        return out

    def _button(self, parent, text):
        """The first button whose label contains `text`.

        Contains rather than equals: these labels carry a leading glyph, so
        the back button reads "\u2190  Back to results" rather than the words
        on their own.
        """
        for widget in self._widgets(parent):
            if not isinstance(widget, self.gui.ctk.CTkButton):
                continue
            try:
                label = widget.cget("text")
            except Exception:
                continue
            if isinstance(label, str) and text in label:
                return widget
        return None

    def mark(self, what):
        self.visited.append(what)

    def run(self):
        self.watch()
        steps = self.steps()

        def tick():
            try:
                delay = next(steps)
            except StopIteration:
                self.finish()
                return
            except Exception:
                self.errors.append("walk failed at %s:\n%s" % (
                    self.visited[-1] if self.visited else "start",
                    traceback.format_exc()))
                self.finish()
                return
            self.app.after(delay, tick)

        def give_up():
            if self.app.winfo_exists():
                self.errors.append("walk did not finish within %ds (reached %s)"
                                   % (WALK_TIMEOUT_MS // 1000,
                                      self.visited[-1] if self.visited else "start"))
                self.finish()

        self.app.after(200, tick)
        self.app.after(WALK_TIMEOUT_MS, give_up)
        try:
            self.app.mainloop()
        except Exception:
            self.errors.append("mainloop raised:\n%s" % traceback.format_exc())

    def finish(self):
        try:
            self.app._closing = True
            self.app.destroy()
        except Exception:
            pass


@unittest.skipUnless(HAS_DISPLAY, "needs a display")
class Smoke(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="eg-smoke-")
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_every_surface_opens_without_error(self):
        import gui

        library = os.path.join(self.home, "library")
        os.makedirs(library, exist_ok=True)
        # Redirect everything the app writes, so a test run cannot disturb the
        # real settings, index or music folder.
        for name, value in (("LOG_DIR", self.home),
                            ("CONFIG_DIR", self.home),
                            ("LIBRARY_DIR", library),
                            ("SETTINGS_PATH",
                             os.path.join(self.home, "settings.json")),
                            ("INDEX_PATH",
                             os.path.join(self.home, "library.db"))):
            self.addCleanup(setattr, gui, name, getattr(gui, name))
            setattr(gui, name, value)

        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            app = gui.App()
            app.geometry("1280x800+40+40")
            # No Spotify client to prefer over the canned results, and no
            # preview warming -- resolving a YouTube URL is a real network
            # call, and four of them behind every search starve the UI pump
            # the walk waits on.
            app.discover.sp = None
            app.discover.fallback = OfflineCatalogue()
            app.discover.prefetch = lambda track: None
            app.catalogue = OfflineCatalogue()
            walk = SurfaceWalk(gui, app)
            walk.run()
            noise = sys.stderr.getvalue()
        finally:
            sys.stderr = stderr

        # The worker-callback pump reports through traceback.print_exc rather
        # than through the Tk handler, so stderr has to be read as well.
        if "Traceback (most recent call last)" in noise:
            walk.errors.append("traceback on stderr:\n%s" % noise[-2000:])

        self.assertEqual(walk.errors, [],
                         "\n\n".join(walk.errors)
                         + "\n\nvisited: " + ", ".join(walk.visited))
        self.assertGreater(len(walk.visited), 20,
                           "walk stopped early: %s" % walk.visited)


if __name__ == "__main__":
    unittest.main(verbosity=2)
