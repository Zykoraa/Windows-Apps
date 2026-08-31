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
