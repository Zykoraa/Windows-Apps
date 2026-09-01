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


def _has_display():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


HAS_DISPLAY = _has_display()


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
            ("visualiser", app.toggle_visualizer_visibility,
             app.toggle_visualizer_visibility),
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

        self.mark("visualiser modes")
        app.toggle_visualizer_visibility()
        yield 260
        for index in range(len(self.gui.VIZ_MODES)):
            app.set_visualizer_mode(index)
            app.update_visualizer([0.4] * 24)
            yield 20
        app.toggle_visualizer_visibility()
        yield 200

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

        self.mark("transport with nothing loaded")
        app.toggle_play_pause()
        yield 120
        app.toggle_shuffle()
        app.toggle_repeat()
        app._toggle_mute()
        yield 200

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
            app.discover.fallback = OfflineCatalogue()
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
