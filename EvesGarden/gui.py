import colorgram
import subprocess
import webbrowser
import sys
import os
import traceback


def _log_dir():
    """A writable place for logs.

    These used to be opened in the process working directory, which for an
    installed .exe is often read-only -- and the open() ran at import time,
    so the app died before showing a window.
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "EvesGarden")
    else:
        path = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = os.path.expanduser("~")
    return path


LOG_DIR = _log_dir()
if getattr(sys, "frozen", False):
    # Only hijack the streams in the windowed build; running from source
    # should keep printing to the terminal the developer is watching.
    try:
        sys.stdout = open(os.path.join(LOG_DIR, "gui_stdout.log"), "w",
                          encoding="utf-8", buffering=1)
        sys.stderr = open(os.path.join(LOG_DIR, "gui_stderr.log"), "w",
                          encoding="utf-8", buffering=1)
    except OSError:
        pass

# --- Monkey-patch subprocess.Popen to suppress console window flashing on Windows ---
import os
import subprocess
_original_popen_init = subprocess.Popen.__init__
def _patched_popen_init(self, *args, **kwargs):
    if os.name == 'nt':
        startupinfo = kwargs.get('startupinfo')
        if startupinfo is None:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = startupinfo
    _original_popen_init(self, *args, **kwargs)
subprocess.Popen.__init__ = _patched_popen_init
# ----------------------------------------------------------------------------------
import customtkinter as ctk
import ctypes

import pystray
from PIL import Image, ImageDraw
import threading

import threading
import os
import glob
import math
import time
import random
import json
import syncedlyrics
import random
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC
from PIL import Image
import io
import tkinter as tk
from PIL import ImageTk
from library_index import LibraryIndex, SORTS
from library_view import LibraryView
import queue as thread_queue
import dialogs
import metadata
import palette
import motion
import ui_widgets
import recycle
from discover import Discover
from play_queue import PlayQueue
from settings import Settings
from download_manager import (
    DownloadManager, QUEUED, RUNNING, DONE, SKIPPED, FAILED, CANCELLED,
)
from media_keys import MediaKeys
import visualizers
import app_icon
from discord_presence import DiscordPresence
import credentials
import lyrics as lyrics_source
import spotify_auth
import spotify_import
from downloader import (
    setup_spotify, search_spotify_track, search_spotify_artist,
    get_artist_albums, get_spotify_album_tracks, get_spotify_playlist_tracks,
    get_spotify_album_tracks_info, process_track, download_many, get_config_dir,
    is_liked_songs, _base_ydl_opts, _score_candidate,
    get_related_tracks, repair_library, find_orphaned_downloads,
    SpotifyAuthError,
)

LIBRARY_DIR = os.path.join(os.path.expanduser("~"), "Music", "SpotifyDownloads")
CONFIG_DIR = LOG_DIR
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
INDEX_PATH = os.path.join(CONFIG_DIR, "library.db")
VIZ_MODES = visualizers.names()


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
from player_engine import PlayerEngine

# Eighteen hand-written tables of seven colours each disagreed with one
# another in ways that showed; they are derived from three authored colours
# apiece now. See themes.py for what was wrong and which rule replaced it.
import themes
from themes import THEMES

ctk.set_appearance_mode("Dark")
# The default family is set in App.__init__ instead: resolving it needs a
# live Tk interpreter, and there is none yet at import time.

# Monkeypatch CTkFont to ensure all explicit calls use the nerd font
import theme_ui

# Every CTkFont used to be forced to JetBrainsMono NF, a coding face, which
# is why the whole app read like a terminal. Default to the UI face instead
# and let call sites that genuinely want fixed-width ask for it.
_original_ctk_font = ctk.CTkFont


class _UIFont(_original_ctk_font):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("family", theme_ui.ui_family())
        super().__init__(*args, **kwargs)


ctk.CTkFont = _UIFont

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Both must exist before anything schedules work via _safe_after().
        self._closing = False
        self._ui_calls = thread_queue.Queue()

        # CustomTkinter builds plenty of its own labels -- the caption on a
        # scrollable frame, the text in an option menu, the segmented button --
        # and takes their family from here rather than from any font we pass
        # in. It was left on the coding face, so all of those rendered as
        # terminal text beside the UI face everything else was using.
        ctk.ThemeManager.theme["CTkFont"]["family"] = theme_ui.ui_family()

        self.settings = Settings(SETTINGS_PATH)
        self.index = LibraryIndex(INDEX_PATH)

        self.overrideredirect(True)  # Frameless window
        self._apply_window_icon()
        self.after(10, self.set_appwindow)
        self.title("Spotify Downloader & Player")
        self._restore_geometry()
        self.minsize(900, 620)

        self.current_theme_name = self.settings.get("theme")
        if self.current_theme_name not in THEMES:
            self.current_theme_name = "Spotify Classic"
        self.theme = THEMES[self.current_theme_name]

        # setup_spotify() now raises instead of falling back to credentials
        # baked into the binary, so a missing .env must not kill the player.
        self.spotify_error = None
        try:
            self.sp = setup_spotify()
        except SpotifyAuthError as e:
            self.sp = None
            self.spotify_error = str(e)
        except Exception as e:
            self.sp = None
            self.spotify_error = f"Could not reach Spotify: {e}"

        self.player = PlayerEngine()
        self._seeking = False
        self._viz_idle = False
        # Covers are kept as PIL images, not just CTkImages, so the next one
        # can be blended onto the one already showing.
        self._thumb_pil = None
        self._np_pil = None
        self._lyric_fonts = {}
        self._tray_icon = None
        self._lib_search_timer = None
        self._art_cache = {}
        self._row_signature = None
        self._render_token = 0

        self.downloads = DownloadManager(
            self.sp, process_track,
            on_change=lambda job: self._safe_after(0, self._on_job_change, job),
            on_log=self._gui_log,
        )

        self.visualizer_mode = int(self.settings.get("visualizer_mode") or 0)
        if not (0 <= self.visualizer_mode < len(VIZ_MODES)):
            self.visualizer_mode = 0
        # Best-effort: no application id, or Discord closed, simply means the
        # feature stays off. It must never affect playback.
        self.discord = DiscordPresence(
            client_id=(self.settings.get("discord_client_id")
                       or credentials.discord_client_id()),
            enabled=bool(self.settings.get("discord_presence", True)),
        )
        if self.discord.available:
            self.discord.start()
        self._discord_tick = 0

        # A signed-in client, created lazily: only playlists need one.
        self.user_sp = None
        self._refresh_user_client()

        self.queue = PlayQueue(
            on_change=lambda: self._safe_after(0, self._queue_changed))
        self.queue_visible = False
        self._queue_meta = {}

        # Needs no account, so search and download work before -- or
        # without -- any Spotify setup. Spotify stays preferred where it is
        # configured; this is the floor.
        self.catalogue = metadata.ITunesProvider()
        self.discover = Discover(self.sp, _base_ydl_opts, _score_candidate,
                                 fallback=self.catalogue)
        self.discover_results = []
        self._discover_timer = None
        self._streaming_track = None

        self.dl_visible = False
        self.dl_overlay = None
        self.visualizer_palette = self.settings.get("visualizer_palette") or "Accent"
        if self.visualizer_palette not in visualizers.palette_names():
            self.visualizer_palette = "Accent"
        self.visualizer_visible = False   # toggled on after build_ui

        # Deliberately not wiring visualizer_callback: the engine exposes
        # smoothed_bands and the UI samples it on its own clock.
        self.player.on_track_end_callback = self.on_track_end
        # Fires on the audio thread when the engine rolls into the preloaded
        # track by itself, so it has to be handed straight to the UI thread.
        self.player.on_track_advanced_callback = (
            lambda path: self._safe_after(0, self._on_gapless_advance, path))
        self.player.gapless = bool(self.settings.get("gapless", True))
        self.player.crossfade = float(self.settings.get("crossfade", 0.0) or 0.0)

        self.library_view = self.settings.get("library_view") or "Songs"
        self.library_sort = self.settings.get("library_sort") or "Title"
        self.library_filter = None          # ("album"|"artist", value)
        self.current_library_files = []
        self.current_rows = []
        self.current_playlist = []
        # (playlist_id, ordered paths) for imports whose downloads
        # have not finished yet.
        self._pending_imports = []
        # Re-entrancy guard for the masthead's own measuring pass.
        self._brand_busy = False
        self.current_index = -1
        self.shuffle = bool(self.settings.get("shuffle"))
        self.repeat = bool(self.settings.get("repeat"))

        self._setup_media_keys()
        self.parsed_lyrics = []
        self.current_lyric_index = -1

        # Build UI layout
        self.build_ui()
        self.apply_theme()
        self._bind_shortcuts()

        # Restore audio state, syncing the controls as well as the engine --
        # setting only the engine leaves the sliders showing their defaults.
        self._restore_audio_controls()

        if self.settings.get("visualizer_visible"):
            self.toggle_visualizer_visibility()

        # Start GUI update loops
        self._pump_ui_calls()
        self.update_progress_loop()
        self.update_visualizer_loop()
        self.setup_overlay = None
        if self.spotify_error and not self.settings.get("setup_seen"):
            # Offered once, not on every launch until credentials exist. That
            # was reasonable while the app could do nothing without them.
            self.settings.set("setup_seen", True)
            self._safe_after(450, lambda: self.open_setup(first_run=True))

        self._safe_after(300, self._resume_last_track)
        self._safe_after(120000, self._autosave_loop)


    def _safe_after(self, delay, callback=None, *args):
        """Schedule work on the Tk thread, from any thread.

        Tk's after() is only safe on the thread that owns the interpreter.
        Called from a worker it raises "main thread is not in main loop" --
        and this used to swallow that, so the work was simply dropped. It was
        not a rare shutdown case either: a cover decoded during startup, while
        the main thread was still finishing __init__, never reached the bar at
        all, which is why a resumed track opened with no artwork. Lyrics, list
        thumbnails and download progress all went through the same door.

        Work handed over from a worker now goes on a queue that the main loop
        drains, which is the only thread allowed to touch Tk.
        """
        if getattr(self, "_closing", False):
            return None
        if threading.current_thread() is threading.main_thread():
            try:
                return self.after(delay, callback, *args)
            except Exception:
                return None
        if callback is None:
            return None
        try:
            self._ui_calls.put((delay, callback, args))
        except Exception:
            pass
        # No cancel token: only main-thread callers schedule cancellable work.
        return None

    def _pump_ui_calls(self):
        """Run whatever the worker threads handed over, on the Tk thread."""
        while True:
            try:
                delay, callback, args = self._ui_calls.get_nowait()
            except thread_queue.Empty:
                break
            except Exception:
                break
            try:
                if delay:
                    self.after(delay, callback, *args)
                else:
                    callback(*args)
            except Exception:
                traceback.print_exc()
        if not getattr(self, "_closing", False):
            try:
                self.after(25, self._pump_ui_calls)
            except Exception:
                pass

    def _setup_media_keys(self):
        """Register only the media keys, not a system-wide keyboard hook.

        This used to run a pynput Listener, which sees every keystroke typed
        in any application. RegisterHotKey asks Windows for exactly the keys
        we handle and nothing else is ever delivered to us.
        """
        def dispatch(action):
            handler = {
                "play_pause": self.toggle_play_pause,
                "next": self.play_next,
                "prev": self.play_prev,
                "stop": self.stop_playback,
            }.get(action)
            if handler:
                self._safe_after(0, handler)

        self.media_keys = MediaKeys(dispatch)
        try:
            self.media_keys.start()
        except Exception as e:
            print(f"Media keys unavailable: {e}")

    def stop_playback(self):
        self.player.stop()
        self.player.clear_preload()
        self.play_btn.set_glyph("play")
        self.progress_slider.set(0.0)
        self.progress_slider.set_buffered(0.0)
        if getattr(self, "discord", None):
            self.discord.clear()

    def _restore_audio_controls(self):
        volume = float(self.settings.get("volume") or 1.0)
        self.volume_slider.set(volume)
        self.on_volume(volume)

        gains = list(self.settings.get("eq_gains") or [1.0] * 10)
        for slider, gain in zip(self.eq_sliders, gains):
            slider.set(gain)
        self.player.set_eq(gains)

        preset = self.settings.get("eq_preset")
        if preset in self.presets:
            self.preset_var.set(preset)

    def _bind_shortcuts(self):
        """Keyboard control, skipped while a text field has focus."""
        def guard(fn):
            def handler(event=None):
                # focus_get() raises when focus sits on a window Tkinter does
                # not own, and a focused CTkEntry reports as its inner
                # tk.Entry -- so check the Tk class, not the CTk wrapper.
                try:
                    widget = self.focus_get()
                    klass = widget.winfo_class() if widget is not None else ""
                except Exception:
                    klass = ""
                if klass in ("Entry", "TEntry", "Text"):
                    return None
                fn()
                return "break"
            return handler

        def nudge(delta):
            def move():
                duration = self.player.get_duration()
                if duration > 0:
                    self.player.set_progress(
                        (self.player.get_position() + delta) / duration)
            return move

        def volume(delta):
            def change():
                value = min(1.0, max(0.0, self.player.volume + delta))
                self.volume_slider.set(value)
                self.on_volume(value)
            return change

        for seq, fn in (
            ("<space>", self.toggle_play_pause),
            ("<Right>", nudge(5)), ("<Left>", nudge(-5)),
            ("<Control-Right>", self.play_next), ("<Control-Left>", self.play_prev),
            ("<Up>", volume(0.05)), ("<Down>", volume(-0.05)),
            ("<m>", self._toggle_mute),
            ("<v>", self.toggle_visualizer_visibility),
            ("<s>", self.toggle_shuffle), ("<r>", self.toggle_repeat),
            ("<n>", self.toggle_now_playing_overlay),
            ("<l>", self.like_now_playing),
        ):
            self.bind(seq, guard(fn))

        self.bind("<slash>", lambda e: (self.lib_search_entry.focus_set(), "break")[1])
        # Deliberately not wrapped in guard(): the palette has to be reachable
        # from inside the search box, which is where you notice you wanted it.
        self.bind("<Control-k>", self.toggle_palette)
        self.bind("<Control-K>", self.toggle_palette)
        self.bind("<Escape>", lambda e: self._escape())

    def toggle_palette(self, event=None):
        """One box for the library, Spotify and the app's own commands."""
        if getattr(self, "_palette", None) is None:
            self._palette = palette.CommandPalette(
                self, self.theme, self.index,
                getattr(self, "discover", None), self._safe_after,
                on_play=self.play_from_library,
                on_download=self.download_discovered,
                commands=self._palette_commands(),
                thumb_loader=getattr(self.library, "request_thumb", None))
        self._palette.toggle()
        return "break"

    def _palette_commands(self):
        """The parts of the app worth reaching without hunting for a button."""
        return [
            ("Now playing", "Full-screen cover, lyrics and queue",
             self.toggle_now_playing_overlay),
            ("Add music", "Search Spotify and download", self.open_downloader),
            ("Visualiser", "Toggle the spectrum view",
             self.toggle_visualizer_visibility),
            ("Up next", "Show the queue", self.toggle_queue),
            ("Shuffle", "Toggle shuffle", self.toggle_shuffle),
            ("Repeat", "Toggle repeat", self.toggle_repeat),
            ("Liked songs", "Show everything you have hearted",
             lambda: self._go_to_view("Liked")),
            ("Albums", "Browse by album", lambda: self._go_to_view("Albums")),
            ("Artists", "Browse by artist", lambda: self._go_to_view("Artists")),
            ("Playlists", "Your playlists", lambda: self._go_to_view("Playlists")),
            ("Duplicates", "Find and remove duplicate downloads",
             lambda: self._go_to_view("Duplicates")),
            ("Repair library", "Recover unconverted downloads",
             self.run_repair),
        ]

    def _go_to_view(self, name):
        self.set_library_view(name)

    def _toggle_mute(self):
        self._muted_at = getattr(self, "_muted_at", None)
        if self.player.volume > 0:
            self._muted_at = self.player.volume
            self.volume_slider.set(0.0)
            self.on_volume(0.0)
        else:
            restore = self._muted_at or 1.0
            self.volume_slider.set(restore)
            self.on_volume(restore)

    def _escape(self):
        if getattr(self, "_palette", None) is not None and self._palette.visible:
            self._palette.close()
        elif getattr(self, "setup_overlay", None) is not None:
            self.close_setup()
        elif getattr(self, "dl_visible", False):
            self.close_downloader()
        elif self.visualizer_visible:
            self.toggle_visualizer_visibility()
        elif getattr(self, "np_overlay_visible", False):
            self.toggle_now_playing_overlay()
        elif self.library_filter:
            self.clear_library_filter()
        return "break"

    def toggle_play_pause(self):
        if self.player.playing and not self.player.paused:
            self.player.pause()
            self.play_btn.set_glyph("play")
            self._push_discord(playing=False)
        else:
            self.player.play()
            self.play_btn.set_glyph("pause")
            self._push_discord(playing=True)

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self.settings.set("shuffle", self.shuffle)
        self.shuffle_btn.set_active(self.shuffle)

        # current_index is -1 until something plays, and the playlist can be
        # empty; indexing it unguarded raised IndexError on the first click.
        current_track = None
        if self.current_playlist and 0 <= self.current_index < len(self.current_playlist):
            current_track = self.current_playlist[self.current_index]

        if self.shuffle and self.current_playlist:
            random.shuffle(self.current_playlist)
            if current_track is not None:
                self.current_playlist.remove(current_track)
                self.current_playlist.insert(0, current_track)
                self.current_index = 0
        elif not self.shuffle and self.current_library_files:
            self.current_playlist = self.current_library_files.copy()
            try:
                self.current_index = self.current_playlist.index(current_track)
            except ValueError:
                self.current_index = -1 if current_track is None else 0

    def toggle_repeat(self):
        self.repeat = not self.repeat
        self.settings.set("repeat", self.repeat)
        self.repeat_btn.set_active(self.repeat)

    def play_next(self):
        """Anything queued by hand wins; otherwise continue through the list."""
        nxt = self._queue_next()
        if not nxt:
            self.player.stop()
            self.play_btn.set_glyph("play")
            return
        if nxt in self.current_playlist:
            self.current_index = self.current_playlist.index(nxt)
        self.play_file(nxt)

    def play_prev(self):
        if not self.current_playlist: return
        # If past 3 seconds, just restart track. The old expression called
        # len() on audio_data, which is None until a track has loaded.
        if self.player.get_position() > 3.0:
            self.player.set_progress(0.0)
            return

        self.current_index -= 1
        if self.current_index < 0:
            if self.repeat:
                self.current_index = len(self.current_playlist) - 1
            else:
                self.current_index = 0
        self.play_file(self.current_playlist[self.current_index])

    def on_track_end(self):
        self._safe_after(0, self.play_next)

    # ------------------------------------------------------------- gapless

    # Far enough ahead to absorb a slow decode, late enough that skipping
    # through a list does not decode a track per skip.
    PRELOAD_LEAD = 25.0

    def _queue_changed(self):
        self._render_queue()
        # Whatever was decoded ahead may no longer be what plays next.
        self._maybe_preload_next(force=True)

    def _current_path(self):
        if 0 <= self.current_index < len(self.current_playlist):
            return self.current_playlist[self.current_index]
        return None

    def _maybe_preload_next(self, force=False):
        """Decode the next track shortly before this one runs out.

        Not from the start of it: a decode costs a few hundred milliseconds
        and tens of megabytes, and most of that would be thrown away every
        time somebody skips through a list.
        """
        if not self.player.gapless or self.player.stream is not None:
            return
        duration = self.player.get_duration()
        if duration <= 0:
            return
        lead = max(self.PRELOAD_LEAD, self.player.crossfade + 10.0)
        if not force and duration - self.player.get_position() > lead:
            return
        nxt = self.queue.peek_next(shuffle=self.shuffle, repeat=self.repeat,
                                   current=self._current_path())
        if nxt:
            self.player.preload(nxt)

    def _on_gapless_advance(self, path):
        """The engine moved to the next track without stopping the stream.

        Everything play_file does apart from the loading, which has already
        happened -- calling play_file here would stop the stream and put back
        the gap this exists to remove. The queue still has to be consumed,
        and it hands back the same track because peek_next committed to it.
        """
        if not path:
            return
        self.queue.next_path(shuffle=self.shuffle, repeat=self.repeat,
                             current=self._current_path())
        if path in self.current_playlist:
            self.current_index = self.current_playlist.index(path)
        self._begin_track(path)

    def extract_album_art(self, file_path):
        """Decode cover art on a worker thread.

        Resizing a 640x640 JPEG and running colorgram over it took long enough
        to visibly stall the UI on every track change, because this ran inline
        on the Tk thread.
        """
        threading.Thread(
            target=self._extract_album_art_worker, args=(file_path,), daemon=True
        ).start()

    def _extract_album_art_worker(self, file_path):
        try:
            audio = MP3(file_path, ID3=ID3)
            tags = audio.tags.values() if audio.tags else []
            for tag in tags:
                if not isinstance(tag, APIC):
                    continue
                image = Image.open(io.BytesIO(tag.data)).convert("RGB")
                thumb = image.resize((64, 64), Image.Resampling.LANCZOS)
                np_art = image.resize((400, 400), Image.Resampling.LANCZOS)

                accent = None
                try:
                    # colorgram accepts a PIL image directly, so this no longer
                    # writes tmp_cover.jpg into the working directory (which
                    # two concurrent track changes would race over).
                    colors = colorgram.extract(image.resize((100, 100)), 2)
                    if colors:
                        c = colors[0].rgb
                        accent = f"#{c.r:02x}{c.g:02x}{c.b:02x}"
                except Exception:
                    pass

                self._safe_after(0, self._apply_album_art, thumb, np_art, accent)
                return
        except Exception as e:
            print(f"Error extracting art: {e}")
        self._safe_after(0, self._apply_album_art, None, None, None)

    def _apply_album_art(self, thumb, np_art, accent):
        if not self.winfo_exists():
            return
        if thumb is None:
            # A track with no embedded cover still gets a tile. Blanking the
            # label instead left a hole in the bottom bar, which was the most
            # visible flicker in the app.
            thumb = ui_widgets.placeholder_art(64, self.theme["surface"],
                                               self.theme["text"])
            # Lifted well off the floor colour: a tile built straight on
            # NP_FLOOR is near-black on a near-black backdrop, so a track with
            # no embedded cover showed nothing at all in the full-screen view.
            np_art = ui_widgets.placeholder_art(
                400, motion.blend(self.NP_FLOOR, "#ffffff", 0.13), "#ffffff")
            accent = None

        # Deliberately no placeholder in between: holding the outgoing cover
        # until the incoming one has decoded, then dissolving, reads better
        # than a blank frame every time the track changes.
        ui_widgets.crossfade(self.album_art_label, self._thumb_pil, thumb, 64)
        # The full-screen cover is not a label any more -- it is composited
        # into the backdrop, so it is redrawn rather than cross-faded.
        self._thumb_pil, self._np_pil = thumb, np_art

        self.dynamic_accent = accent or self.theme["accent"]
        # The full-screen view derives its own, much darker, pair: it is a
        # backdrop to read text off, not a panel colour.
        self._np_tint = (ui_widgets.clamp_luminance(accent, 0.11, self.NP_FLOOR)
                         if accent else self.NP_FLOOR)
        self._np_card = motion.blend(self._np_tint, "#ffffff", 0.11)
        fade = bool(getattr(self, "np_overlay_visible", False))
        for card in (getattr(self, "np_lyrics_card", None),
                     getattr(self, "np_queue_card", None),
                     getattr(self, "lyrics_scroll", None),
                     getattr(self, "np_queue_list", None)):
            if card is not None:
                card.configure(fg_color=self._np_card)
        self._np_redraw_stage(force=True, fade=fade)

    def build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # 1. Title Bar
        self.title_bar = ctk.CTkFrame(self, height=35, corner_radius=0, fg_color=self.theme["surface"])
        self.title_bar.grid(row=0, column=0, sticky="ew")
        self.title_bar.bind("<B1-Motion>", self.move_window)
        self.title_bar.bind("<Button-1>", self.get_pos)
        self.title_bar.bind("<Double-Button-1>", self.toggle_maximize)

        # No mark or title here any more. A frameless window that redraws
        # the system title bar and then fills it with the same icon and name
        # the system would have shown is just an imitation of the chrome it
        # went to the trouble of removing; the branding belongs inside the
        # app, where there is room to do it properly. See the masthead below.

        self.close_btn = ctk.CTkButton(self.title_bar, text=" ✕ ", width=40, height=35, corner_radius=0, fg_color="transparent", hover_color="#e81123", command=self.destroy)
        self.close_btn.pack(side="right")
        self.max_btn = ctk.CTkButton(self.title_bar, text=" ❐ ", width=40, height=35, corner_radius=0, fg_color="transparent", command=self.toggle_maximize)
        self.max_btn.pack(side="right")
        self.min_btn = ctk.CTkButton(self.title_bar, text=" 🗕 ", width=40, height=35, corner_radius=0, fg_color="transparent", command=self.minimize_to_tray)
        self.min_btn.pack(side="right")

        # The theme picker used to sit in the library header, where the tabs,
        # the search box, the sort menu and Add music already wanted more room
        # than the window has: it was the last thing packed, so it was the one
        # squeezed, down to a 20px sliver at the right edge. It is an
        # application setting rather than a library control, so the title bar
        # is where it belongs anyway.
        self.theme_dropdown = ctk.CTkOptionMenu(
            self.title_bar, values=list(THEMES.keys()),
            command=self.change_theme, corner_radius=13,
            width=148, height=26, font=theme_ui.font("small"))
        self.theme_dropdown.set(self.current_theme_name)
        self.theme_dropdown.pack(side="right", padx=(0, 14), pady=4)

        # 2. Main Area (Library)
        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color=self.theme["bg"])
        self.main_area.grid(row=1, column=0, sticky="nsew")
        self.main_area.grid_columnconfigure(0, weight=1)
        self.main_area.grid_rowconfigure(0, weight=0)   # header
        self.main_area.grid_rowconfigure(1, weight=0)   # breadcrumb
        self.main_area.grid_rowconfigure(2, weight=1)   # list
        self.main_area.grid_rowconfigure(3, weight=0)   # status

        self.library_header = ctk.CTkFrame(self.main_area, height=60, fg_color="transparent")
        self.library_header.grid(row=0, column=0, sticky="ew", padx=20, pady=10)
        self.library_header.bind("<Configure>", self._sync_brand)

        # Songs / Albums / Artists -- the library used to be a flat glob of
        # one folder, discarding the album and artist tags every download
        # writes.
        # The masthead: the app's mark and name at the top-left of its own
        # content, rather than pretending to be a Windows title bar.
        self.brand = ctk.CTkFrame(self.library_header, fg_color="transparent")
        self.brand.pack(side="left", padx=(2, 18))
        self.brand_mark = ui_widgets.glyph_canvas(
            self.brand, "leaf", size=34, colour=self.theme["accent"],
            background=self.theme["bg"], stroke=2.1, fill=0.88)
        self.brand_mark.pack(side="left", padx=(0, 9))
        self.brand_word = ctk.CTkLabel(
            self.brand, text="Eve's Garden",
            font=theme_ui.font("display", size=19),
            text_color=self.theme["text"])
        self.brand_word.pack(side="left")

        self.view_tabs = ctk.CTkSegmentedButton(
            self.library_header, values=["Songs", "Liked", "Recent", "Playlists", "Albums",
                    "Artists", "Duplicates"],
            command=self.set_library_view, corner_radius=theme_ui.RADIUS_PILL,
            height=36, font=theme_ui.font("body_med"))
        self.view_tabs.set(self.library_view)
        self.view_tabs.pack(side="left", padx=(0, 12))

        # Everything that acts on the library rides in one cluster, and the
        # cluster is packed before the search box. Pack hands out width in
        # packing order, so whatever goes last is what gets squeezed when the
        # row runs out -- and a shorter search box is a far better failure
        # than a button sliced in half, which is what this row did when the
        # search box was packed first.
        self.library_actions = ctk.CTkFrame(self.library_header,
                                            fg_color="transparent")
        self.library_actions.pack(side="right")

        self.sort_dropdown = ctk.CTkOptionMenu(
            self.library_actions, values=list(SORTS.keys()),
            command=self.set_library_sort, corner_radius=theme_ui.RADIUS_PILL,
            width=170, height=36, font=theme_ui.font("body"))
        self.sort_dropdown.set(self.library_sort)
        self.sort_dropdown.pack(side="left", padx=(0, 10))

        self.nav_dl_btn = ctk.CTkButton(
            self.library_actions, text="+  Add music", command=self.open_downloader,
            corner_radius=theme_ui.RADIUS_PILL, height=36, width=132,
            font=theme_ui.font("body_med"))
        self.nav_dl_btn.pack(side="left", padx=6)

        self.new_pl_btn = ctk.CTkButton(
            self.library_actions, text="+  New",
            command=self.prompt_new_playlist,
            corner_radius=theme_ui.RADIUS_PILL, height=36, width=0,
            font=theme_ui.font("body_med"))

        # Beside New playlist, because the Playlists tab is where somebody
        # goes looking for a playlist they already keep somewhere else.
        self.import_pl_btn = ctk.CTkButton(
            self.library_actions, text="Import from Spotify",
            command=self.import_from_spotify, fg_color="transparent",
            border_width=1, corner_radius=theme_ui.RADIUS_PILL,
            height=36, width=0, font=theme_ui.font("body_med"))

        self.dedupe_btn = ctk.CTkButton(
            self.library_actions, text="Move ticked to Recycle Bin",
            command=self.remove_duplicates, corner_radius=theme_ui.RADIUS_PILL,
            height=36, width=0, font=theme_ui.font("body_med"))

        # Only offered when there is actually something to recover.
        self.repair_btn = ctk.CTkButton(self.library_actions, text="Repair library",
                                        command=self.run_repair, corner_radius=20,
                                        font=ctk.CTkFont(weight="bold"))
        self.refresh_repair_button()

        # Requests little and expands into what is left, so the row degrades
        # by shortening the search box. Packed last, which is what makes that
        # sentence true rather than merely intended.
        self.lib_search_entry = ctk.CTkEntry(
            self.library_header, placeholder_text="Search title, artist or album",
            border_width=1, corner_radius=theme_ui.RADIUS_PILL, height=36,
            width=140, font=theme_ui.font("body"))
        self.lib_search_entry.pack(side="left", padx=(4, 10), fill="x",
                                   expand=True)
        self.lib_search_entry.bind("<KeyRelease>", self._on_library_search)

        # Shown only while drilled into one album or artist.
        self.crumb_bar = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.crumb_back = ctk.CTkButton(self.crumb_bar, text="←  Back", width=92,
                                        height=32, corner_radius=16,
                                        font=theme_ui.font("body_med"),
                                        command=self.clear_library_filter)
        # The bar itself now runs edge to edge so its tint does, so the
        # inset that used to come from the grid lives on the children.
        self.crumb_back.pack(side="left", padx=(24, 0))
        self.crumb_label = ctk.CTkLabel(self.crumb_bar, text="",
                                        font=theme_ui.font("title"))
        self.crumb_label.pack(side="left", padx=14)

        # Not "transparent": a scrollable frame keeps its own canvas, and a
        # transparent one paints it with whatever colour it detected when it
        # was built. After a theme change the 2px gaps between rows still
        # showed the previous theme, which read as a dark rule under every
        # row.
        self.library_frame = ctk.CTkScrollableFrame(
            self.main_area, fg_color=self.theme["bg"])
        self.library_frame.grid(row=2, column=0, sticky="nsew", padx=20, pady=(4, 6))

        self.library_status = ctk.CTkLabel(self.main_area, text="", anchor="w",
                                           font=ctk.CTkFont(size=11))
        self.library_status.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 8))

        # 3. Bottom Playback Bar
        self.bottom_bar = ctk.CTkFrame(self, height=90, corner_radius=0, fg_color=self.theme["surface"])
        self.bottom_bar.grid(row=2, column=0, sticky="ew")
        self.bottom_bar.grid_columnconfigure(0, weight=1)
        self.bottom_bar.grid_columnconfigure(1, weight=1)
        self.bottom_bar.grid_columnconfigure(2, weight=1)

        self.now_playing_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.now_playing_frame.grid(row=0, column=0, rowspan=2, sticky="w", padx=20)

        self.np_like_btn = ctk.CTkLabel(self.now_playing_frame, text="♡",
                                        width=30, cursor="hand2",
                                        font=theme_ui.font("body", size=18))
        self.np_like_btn.pack(side="right", padx=(10, 6))
        self.np_like_btn.bind("<Button-1>", lambda e: self.like_now_playing())

        self.album_art_label = ctk.CTkLabel(self.now_playing_frame, text="", width=64, height=64)
        self.album_art_label.pack(side="left", padx=(0, 10))
        self.album_art_label.bind("<Button-1>", self.toggle_now_playing_overlay)
        self.album_art_label.configure(cursor="hand2")

        # One flat "Artist - Title" string gave the title no prominence, so
        # split it: title leads, artist and album sit under it.
        np_text = ctk.CTkFrame(self.now_playing_frame, fg_color="transparent")
        np_text.pack(side="left", fill="y")
        self.now_playing_label = ctk.CTkLabel(np_text, text="No track selected",
                                              font=theme_ui.font("heading"),
                                              anchor="w", justify="left")
        self.now_playing_label.pack(anchor="w", pady=(16, 0))
        self.now_playing_sub = ctk.CTkLabel(np_text, text="",
                                            font=theme_ui.font("caption"),
                                            anchor="w", justify="left")
        self.now_playing_sub.pack(anchor="w")

        self.controls_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        # The seek bar keeps 20px above its track clear for the scrub
        # readout, so an evenly padded transport ends up sitting high with a
        # band of nothing under it. Pushed down and closed up against the bar.
        self.controls_frame.grid(row=0, column=1, pady=(12, 0))

        # These were text: U+1F500 SHUFFLE, U+23EE PREVIOUS TRACK and so on.
        # Windows has no font that covers that range, so each one rendered as
        # a boxed fallback and the transport read as four grey rectangles.
        for name, glyph, cmd, primary in (
            ("shuffle_btn", "shuffle", self.toggle_shuffle, False),
            ("prev_btn", "prev", self.play_prev, False),
            ("play_btn", "play", self.toggle_play_pause, True),
            ("next_btn", "next", self.play_next, False),
            ("repeat_btn", "repeat", self.toggle_repeat, False),
        ):
            button = ui_widgets.GlyphButton(
                self.controls_frame, self.theme, glyph, command=cmd,
                primary=primary, size=50 if primary else 40)
            button.pack(side="left", padx=5)
            setattr(self, name, button)
        self.shuffle_btn.set_active(self.shuffle)
        self.repeat_btn.set_active(self.repeat)

        self.progress_row = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.progress_row.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        self.progress_row.grid_columnconfigure(1, weight=1)

        # The times sit on the track, not on the middle of the widget: the
        # seek bar reserves a band above itself for the scrub readout.
        self.time_elapsed = ctk.CTkLabel(self.progress_row, text="0:00", width=44,
                                         font=theme_ui.font("time"))
        self.time_elapsed.grid(row=0, column=0, padx=(0, 8), sticky="s",
                               pady=(0, 1))

        # A stock CTkSlider showed the playhead and nothing else: no hover
        # affordance, and no way to see where a scrub would land short of
        # letting go and listening.
        self.progress_slider = ui_widgets.SeekBar(
            self.progress_row, self.theme, command=self._commit_seek,
            formatter=self._scrub_label, on_scrub=self._on_scrub)
        self.progress_slider.grid(row=0, column=1, sticky="ew")
        self.progress_slider.set_buffered(0.0)

        self.time_total = ctk.CTkLabel(self.progress_row, text="0:00", width=44,
                                       font=theme_ui.font("time"))
        self.time_total.grid(row=0, column=2, padx=(8, 0), sticky="s",
                             pady=(0, 1))

        self.eq_toggle_btn = ctk.CTkButton(self.bottom_bar, text="EQ", width=40, height=40, corner_radius=20, command=self.toggle_eq)
        self.eq_toggle_btn.grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.viz_toggle_btn = ctk.CTkButton(self.bottom_bar, text="VIZ", width=40, height=40, corner_radius=20, command=self.toggle_visualizer_visibility)
        self.viz_toggle_btn.grid(row=0, column=3, sticky="e", padx=(10, 20))

        # Volume: the engine had no gain stage at all, so the only way to turn
        # the app down was the system mixer.
        self.queue_btn = ctk.CTkButton(
            self.bottom_bar, text="☰", width=40, height=40,
            corner_radius=20, command=self.toggle_queue,
            font=theme_ui.font("body", size=15))
        self.queue_btn.grid(row=0, column=4, sticky="e", padx=(0, 20))

        self.volume_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.volume_frame.grid(row=1, column=2, columnspan=2, sticky="e",
                               padx=(10, 20), pady=(0, 10))
        # Clicking the speaker now mutes -- it was a static label, and mute
        # was only reachable from the keyboard.
        self.volume_icon = ui_widgets.GlyphButton(
            self.volume_frame, self.theme, "volume_high", size=28,
            command=self._toggle_mute, glyph_scale=0.68, stroke=1.8)
        self.volume_icon.pack(side="left", padx=(0, 6))
        self.volume_slider = ui_widgets.SeekBar(
            self.volume_frame, self.theme, command=self.on_volume,
            on_drag=self.on_volume, wheel_step=0.05, width=96)
        self.volume_slider.set(1.0)
        self.volume_slider.pack(side="left")

        self.library = LibraryView(
            self.library_frame, self.crumb_bar, self.crumb_label,
            self.library_status, self.index, self.theme,
            self._safe_after, self.play_from_library,
            lambda: self.lib_search_entry.get().strip(),
        )
        self.library.view = self.library_view
        self.library.sort = self.library_sort
        self.library.on_like = self.toggle_like
        self.library.on_menu = self.show_track_menu

        self._build_resize_grips()

        # Build Overlays
        self.build_now_playing_overlay()
        self.build_viz_overlay()

        self.load_library()

    def get_pos(self, event):
        self.xwin = event.x
        self.ywin = event.y

    def move_window(self, event):
        self.geometry(f'+{event.x_root - self.xwin}+{event.y_root - self.ywin}')

    def toggle_now_playing_overlay(self, event=None):
        if getattr(self, "dl_visible", False):
            self.close_downloader()
        if hasattr(self, 'np_overlay_visible') and self.np_overlay_visible:
            self._slide_out(self.np_overlay)
            self.np_overlay_visible = False
        else:
            self.np_overlay_visible = True
            self._np_place()
            self._np_redraw_stage()
            self._render_np_queue()
            self._slide_in(self.np_overlay)

    # --------------------------------------------------------- overlay motion

    @staticmethod
    def _overlay_rely(overlay):
        """Where an overlay currently sits, so an interrupted slide resumes
        from there instead of jumping back to the edge."""
        try:
            info = overlay.place_info()
            return float(info.get("rely", 1.0)) if info else 1.0
        except Exception:
            return 1.0

    def _slide_in(self, overlay, duration=motion.BASE):
        """Bring a full-screen panel up from the bottom edge.

        Every overlay in the app used to appear the instant it was place()d
        and vanish the instant it was forgotten. Nothing about that was
        broken, but it made the app feel like it was cutting between slides.
        """
        start = self._overlay_rely(overlay) if overlay.winfo_ismapped() else 1.0
        overlay.place(relx=0, rely=start, relwidth=1, relheight=1)
        overlay.lift()
        motion.animate(overlay, duration,
                       lambda t: overlay.place_configure(rely=start * (1 - t)),
                       name="slide")

    def _slide_out(self, overlay, duration=motion.FAST):
        """Drop a panel back off the bottom edge, faster than it came in."""
        if not overlay.winfo_ismapped():
            return
        start = self._overlay_rely(overlay)
        motion.animate(
            overlay, duration,
            lambda t: overlay.place_configure(rely=start + (1 - start) * t),
            done=overlay.place_forget,
            easing=motion.ease_in_out_cubic, name="slide")

    def open_downloader(self):
        """Show the downloader panel, building it the first time."""
        if getattr(self, "dl_overlay", None) is None:
            self.dl_overlay = ctk.CTkFrame(self.main_area, corner_radius=0,
                                           fg_color=self.theme["bg"])
            self.build_dl_view(self.dl_overlay)

        if self.dl_visible:
            self.close_downloader()
            return

        # One overlay at a time, so panels never stack on each other.
        if self.visualizer_visible:
            self.toggle_visualizer_visibility()
        if getattr(self, "np_overlay_visible", False):
            self.toggle_now_playing_overlay()

        self._slide_in(self.dl_overlay)
        self.dl_visible = True
        self._sync_download_buttons()
        self._rebuild_job_rows()
        self.url_entry.focus_set()

    def close_downloader(self):
        if getattr(self, "dl_overlay", None) is not None:
            self._slide_out(self.dl_overlay)
        self.dl_visible = False
        self.suggestions_frame.place_forget()

    def build_viz_overlay(self):
        self.viz_overlay = ctk.CTkFrame(self.main_area, corner_radius=0, fg_color=self.theme["bg"])
        # Canvas
        self.canvas = ctk.CTkCanvas(self.viz_overlay, highlightthickness=0, bg=self.theme["bg"])
        self.canvas.pack(fill="both", expand=True)

        self.viz_dropdown = ctk.CTkOptionMenu(
            self.viz_overlay,
            values=VIZ_MODES,
            command=self.set_visualizer_mode_by_name,
            fg_color=self.theme["surface"],
            button_color=self.theme["surface"],
            button_hover_color=self.theme["surface_hover"],
            dropdown_fg_color=self.theme["surface"],
            dropdown_hover_color=self.theme["surface_hover"],
            text_color=self.theme["text"]
        )
        self.viz_dropdown.place(relx=0.98, rely=0.05, anchor="ne")

        # Colour is a separate axis from the mode, so every visualiser can be
        # rendered as a single hue, a spectrum or a gradient.
        self.viz_palette_dropdown = ctk.CTkOptionMenu(
            self.viz_overlay,
            values=visualizers.palette_names(),
            command=self.set_visualizer_palette,
            fg_color=self.theme["surface"],
            button_color=self.theme["surface"],
            button_hover_color=self.theme["surface_hover"],
            dropdown_fg_color=self.theme["surface"],
            dropdown_hover_color=self.theme["surface_hover"],
            text_color=self.theme["text"]
        )
        self.viz_palette_dropdown.set(self.visualizer_palette)
        self.viz_palette_dropdown.place(relx=0.98, rely=0.13, anchor="ne")


        # EQ Frame (Overlay/Hidden)
        self.eq_frame = ctk.CTkFrame(self.main_area, corner_radius=15)
        self.eq_header = ctk.CTkFrame(self.eq_frame, fg_color="transparent")
        self.eq_header.pack(fill="x", padx=15, pady=10)
        ctk.CTkLabel(self.eq_header, text="10-Band EQ", font=ctk.CTkFont(weight="bold")).pack(side="left")

        self.presets = {
            "Flat": [1.0]*10,
            "Bass Boost": [2.5, 2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            "Electronic": [2.0, 1.8, 1.2, 1.0, 1.0, 1.5, 1.2, 1.5, 1.8, 2.0],
            "Rock": [1.8, 1.5, 1.2, 1.0, 0.9, 1.0, 1.2, 1.5, 1.6, 1.8]
        }
        self.preset_var = ctk.StringVar(value="Flat")
        self.preset_dropdown = ctk.CTkOptionMenu(self.eq_header, variable=self.preset_var, values=list(self.presets.keys()), command=self.on_eq_preset, corner_radius=10)
        self.preset_dropdown.pack(side="right")
        self.eq_sliders_frame = ctk.CTkFrame(self.eq_frame, fg_color="transparent")
        self.eq_sliders_frame.pack(fill="x", padx=10, pady=10)

        self.eq_sliders = []
        labels = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
        for lbl in labels:
            col = ctk.CTkFrame(self.eq_sliders_frame, fg_color="transparent")
            col.pack(side="left", expand=True)
            slider = ctk.CTkSlider(col, from_=0.0, to=3.0, orientation="vertical", height=100, command=self.on_eq_change)
            slider.set(1.0)
            slider.pack(pady=5)
            ctk.CTkLabel(col, text=lbl, font=ctk.CTkFont(size=10)).pack()
            self.eq_sliders.append(slider)

        # Audio settings belong together, and this is the only panel that is
        # about how playback sounds rather than what is playing.
        fade_row = ctk.CTkFrame(self.eq_frame, fg_color="transparent")
        fade_row.pack(fill="x", padx=18, pady=(2, 14))
        self.crossfade_lbl = ctk.CTkLabel(
            fade_row, text="Crossfade", anchor="w",
            font=theme_ui.font("caption"),
            text_color=self.theme["text_secondary"])
        self.crossfade_lbl.pack(side="left")
        self.crossfade_value = ctk.CTkLabel(
            fade_row, text="off", width=44, anchor="e",
            font=theme_ui.font("time"),
            text_color=self.theme["text_secondary"])
        self.crossfade_value.pack(side="right")
        self.crossfade_slider = ui_widgets.SeekBar(
            fade_row, self.theme, command=self.on_crossfade,
            on_drag=self._show_crossfade, wheel_step=1 / 12.0, width=150)
        self.crossfade_slider.pack(side="right", padx=10)
        self.crossfade_slider.set(self.player.crossfade / self.MAX_CROSSFADE)
        self._show_crossfade(self.crossfade_slider.get())

    # The full-screen view is always a dark room, whatever the app theme is.
    # A cover blurred out to fill the screen only works as a ground if it is
    # dark, and half the palettes here are not; deriving the ink from the
    # theme would have meant near-white text on a pale card over a dark
    # backdrop in exactly the themes where it looks worst.
    NP_INK = "#f2f2f5"
    NP_DIM = "#a6a6b2"
    NP_FLOOR = "#0a0a0d"
    NP_MARGIN = 52

    def _np_theme(self):
        """The app palette, overridden with the dark-room ink.

        The full-screen view deliberately ignores the theme, so any widget
        living on it has to be handed the substitute rather than the real one
        or it paints itself for a background that is not there.
        """
        palette = dict(self.theme)
        palette.update(text=self.NP_INK, text_secondary=self.NP_DIM,
                       surface_hover="#4c4c58")
        return palette

    def build_now_playing_overlay(self):
        """The full-screen now playing: cover, lyrics and the queue together.

        All three already existed and none of them shared a screen. The cover
        and the lyrics sat in two flat columns; the queue was a slide-out on
        the far side of the app, so seeing what was coming up meant leaving
        the view you had opened to look at the track.

        The ground is the cover itself, blown up and blurred down to nothing
        but colour, with the real cover floating on it under a drop shadow.
        That compositing has to happen in PIL and arrive as a single image:
        Tk cannot blend one widget over another, so a shadow can only exist
        if whatever it falls on is part of the same picture. Hence a canvas
        here rather than the usual stack of frames.
        """
        t = self.theme
        self.np_overlay = ctk.CTkFrame(self.main_area, corner_radius=0,
                                       fg_color=self.NP_FLOOR)

        self.np_canvas = tk.Canvas(self.np_overlay, highlightthickness=0,
                                   bd=0, bg=self.NP_FLOOR, takefocus=0)
        self.np_canvas.pack(fill="both", expand=True)

        self._np_stage_id = self.np_canvas.create_image(0, 0, anchor="nw")
        self._np_stage_photo = None      # Tk holds no reference of its own
        self._np_stage_pil = None        # kept so the next one can dissolve in
        self._np_stage_key = None
        self._np_tint = self.NP_FLOOR
        self._np_card = "#17171c"
        self._np_resize_job = None

        self._np_title_id = self.np_canvas.create_text(
            0, 0, text="", anchor="nw", fill=self.NP_INK, justify="left",
            font=theme_ui.font("display", size=32))
        self._np_artist_id = self.np_canvas.create_text(
            0, 0, text="", anchor="nw", fill=self.NP_INK, justify="left",
            font=theme_ui.font("body", size=17))
        self._np_meta_id = self.np_canvas.create_text(
            0, 0, text="", anchor="nw", fill=self.NP_DIM, justify="left",
            font=theme_ui.font("small"))

        # Both scrolling panes are opaque cards. Real frosted glass would need
        # the backdrop behind every pixel of the pane, and a scrollable frame
        # cannot carry a background image.
        self.np_lyrics_card = ctk.CTkFrame(self.np_canvas, corner_radius=18,
                                           width=400, height=400,
                                           fg_color=self._np_card)
        self.np_lyrics_card.pack_propagate(False)
        self.np_lyrics_head = ctk.CTkLabel(
            self.np_lyrics_card, text="LYRICS", anchor="w",
            font=theme_ui.font("small"), text_color=self.NP_DIM)
        self.np_lyrics_head.pack(anchor="w", padx=24, pady=(16, 2))
        # Not "transparent": a scrollable frame keeps its own canvas, which
        # goes on painting the default dark background regardless.
        self.lyrics_scroll = ctk.CTkScrollableFrame(self.np_lyrics_card,
                                                    fg_color=self._np_card)
        self.lyrics_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 14))
        self.lyrics_scroll.grid_columnconfigure(0, weight=1)
        self.lyrics_scroll.bind("<Configure>", self._on_lyrics_resize)

        self.np_queue_card = ctk.CTkFrame(self.np_canvas, corner_radius=18,
                                          width=292, height=400,
                                          fg_color=self._np_card)
        self.np_queue_card.pack_propagate(False)
        self.np_queue_head = ctk.CTkLabel(
            self.np_queue_card, text="NEXT UP", anchor="w",
            font=theme_ui.font("small"), text_color=self.NP_DIM)
        self.np_queue_head.pack(anchor="w", padx=24, pady=(16, 2))
        self.np_queue_list = ctk.CTkScrollableFrame(self.np_queue_card,
                                                    fg_color=self._np_card)
        self.np_queue_list.pack(fill="both", expand=True, padx=8, pady=(0, 14))

        self.np_close_btn = ui_widgets.GlyphButton(
            self.np_canvas, self._np_theme(), "close", size=38,
            command=self.toggle_now_playing_overlay, background=self.NP_FLOOR)

        self.lyrics_labels = []
        self._lyrics_wrap = 0
        self._lyric_spacers = []

        self.np_canvas.bind("<Configure>", self._np_on_resize)

    # ------------------------------------------------- full-screen layout

    def _np_layout(self):
        """Geometry for the canvas size we actually have.

        Falls back to the parent's size because the canvas reports 1x1 until
        Tk has mapped it, and the stage is composed before the slide starts --
        without this the overlay animated in as a black rectangle and only
        acquired its backdrop once the first <Configure> landed.
        """
        w = self.np_canvas.winfo_width()
        h = self.np_canvas.winfo_height()
        if w <= 1 or h <= 1:
            w = max(w, self.main_area.winfo_width())
            h = max(h, self.main_area.winfo_height())
        w, h = max(1, w), max(1, h)
        m = self.NP_MARGIN
        cover = int(min(340, max(160, h - 320), (w - m * 2) * 0.30))
        top = m
        # The queue is the first thing to go when there is no room for three
        # columns; the lyrics then take the whole right-hand side.
        show_queue = w >= 1080
        queue_w = 292 if show_queue else 0
        gap = 26
        lyr_x = m + cover + 54
        lyr_w = w - lyr_x - m - (queue_w + gap if show_queue else 0)
        return {
            "w": w, "h": h, "cover": cover, "cover_xy": (m, top),
            "text_x": m, "text_y": top + cover + 28,
            "lyrics": (lyr_x, top, max(200, lyr_w), max(120, h - top - m)),
            "queue": (w - m - queue_w, top, queue_w, max(120, h - top - m)),
            "show_queue": show_queue,
        }

    def _np_place(self):
        """Position everything that is cheap to move, without re-rendering."""
        if getattr(self, "np_canvas", None) is None:
            return
        L = self._np_layout()
        c = self.np_canvas
        wrap = L["cover"] + 30

        c.coords(self._np_title_id, L["text_x"], L["text_y"])
        c.itemconfigure(self._np_title_id, width=wrap)
        box = c.bbox(self._np_title_id)
        y = (box[3] if box else L["text_y"] + 36) + 6

        c.coords(self._np_artist_id, L["text_x"], y)
        c.itemconfigure(self._np_artist_id, width=wrap)
        box = c.bbox(self._np_artist_id)
        y = (box[3] if box else y + 22) + 8

        c.coords(self._np_meta_id, L["text_x"], y)
        c.itemconfigure(self._np_meta_id, width=wrap)

        lx, ly, lw, lh = L["lyrics"]
        self.np_lyrics_card.configure(width=lw, height=lh)
        self.np_lyrics_card.place(x=lx, y=ly)

        if L["show_queue"]:
            qx, qy, qw, qh = L["queue"]
            self.np_queue_card.configure(width=qw, height=qh)
            self.np_queue_card.place(x=qx, y=qy)
        else:
            self.np_queue_card.place_forget()

        self.np_close_btn.place(x=L["w"] - 50, y=12)

    def _paint_stage(self, image):
        self._np_stage_photo = ImageTk.PhotoImage(image)
        self.np_canvas.itemconfigure(self._np_stage_id,
                                     image=self._np_stage_photo)
        self.np_canvas.tag_lower(self._np_stage_id)

    def _show_stage(self, stage, fade=False):
        """Put a composed stage on the canvas, dissolving from the last one.

        Two saturated covers cutting straight to each other mid-song is the
        one moment this view looks cheap. Deliberately few frames: each one
        is a full-screen blend plus a PhotoImage, which is not cheap, and a
        slow dissolve reads as intentional where a stuttering fast one does
        not.
        """
        old = self._np_stage_pil
        self._np_stage_pil = stage
        if not fade or old is None or old.size != stage.size:
            self._paint_stage(stage)
            return
        motion.animate(self.np_canvas, motion.SLOW,
                       lambda t: self._paint_stage(Image.blend(old, stage, t)),
                       easing=motion.linear, fps=26, name="stage")

    def _np_redraw_stage(self, force=False, fade=False):
        """Recompose the backdrop and cover into one image.

        Keyed on the size and the cover so a resize that changes nothing (and
        Tk sends plenty of those) does not pay for the compositing again.
        """
        if getattr(self, "np_canvas", None) is None:
            return
        L = self._np_layout()
        if L["w"] < 60 or L["h"] < 60:
            return
        art = self._np_pil
        key = (id(art), L["w"], L["h"], L["cover"], self._np_tint)
        if key == self._np_stage_key and not force:
            return
        self._np_stage_key = key

        try:
            if art is None:
                stage = Image.new("RGB", (L["w"], L["h"]),
                                  ui_widgets._rgb(self.NP_FLOOR))
            else:
                stage = ui_widgets.compose_stage(
                    art, L["w"], L["h"], L["cover"], L["cover_xy"],
                    tint=self._np_tint)
            self._show_stage(stage, fade=fade)
            # The close control is a canvas widget, so it cannot be
            # transparent -- but the backdrop is smooth where it sits, so
            # sampling one pixel there hides the seam completely.
            try:
                spot = stage.getpixel((min(L["w"] - 1, L["w"] - 31), 31))
                self.np_close_btn.set_palette(
                    self._np_theme(), background="#%02x%02x%02x" % spot[:3])
            except Exception:
                pass
        except Exception as e:
            print(f"now playing stage: {e}")

    def _np_on_resize(self, _event=None):
        """Reflow immediately, recomposite once the drag settles."""
        self._np_place()
        if getattr(self, "_np_resize_job", None):
            try:
                self.after_cancel(self._np_resize_job)
            except Exception:
                pass
        self._np_resize_job = self._safe_after(140, self._np_redraw_stage)

    def _np_set_track(self, title, artist, meta):
        if getattr(self, "np_canvas", None) is None:
            return
        self.np_canvas.itemconfigure(self._np_title_id, text=title or "")
        self.np_canvas.itemconfigure(self._np_artist_id, text=artist or "")
        self.np_canvas.itemconfigure(self._np_meta_id, text=meta or "")
        self._np_place()

    def _np_row_hover(self):
        return motion.blend(self._np_card, "#ffffff", 0.07)

    def _render_np_queue(self):
        """The up-next list, inside the full-screen view."""
        if getattr(self, "np_queue_list", None) is None:
            return
        if not getattr(self, "np_overlay_visible", False):
            return
        for widget in self.np_queue_list.winfo_children():
            widget.destroy()

        upcoming = self.queue.upcoming
        following = self.queue.context_after(25)
        wanted = set(upcoming) | set(following)
        self._queue_meta = {tr["path"]: tr for tr in self.index.tracks()
                            if tr["path"] in wanted}

        if upcoming:
            ctk.CTkLabel(self.np_queue_list, text="QUEUED", anchor="w",
                         font=theme_ui.font("small"),
                         text_color=self.theme["accent"]).pack(
                             anchor="w", padx=10, pady=(2, 4))
            for path in upcoming:
                self._queue_entry(self.np_queue_list, path, True,
                                  ink=self.NP_INK, dim=self.NP_DIM,
                                  hover=self._np_row_hover())

        if following:
            ctk.CTkLabel(self.np_queue_list, text="THEN FROM THIS LIST",
                         anchor="w", font=theme_ui.font("small"),
                         text_color=self.NP_DIM).pack(
                             anchor="w", padx=10, pady=(14, 4))
            for path in following:
                self._queue_entry(self.np_queue_list, path, False,
                                  ink=self.NP_INK, dim=self.NP_DIM,
                                  hover=self._np_row_hover())

        if not upcoming and not following:
            ctk.CTkLabel(self.np_queue_list,
                         text="Nothing lined up.\nPlay from a list, or queue a\ntrack from its right-click menu.",
                         justify="left", anchor="w",
                         font=theme_ui.font("body"),
                         text_color=self.NP_DIM).pack(anchor="w", padx=12,
                                                      pady=18)

    def _lyric_wrap_width(self):
        width = self.lyrics_scroll.winfo_width()
        return max(220, width - 64) if width > 1 else 520

    def _on_lyrics_resize(self, _event=None):
        wrap = self._lyric_wrap_width()
        if abs(wrap - self._lyrics_wrap) < 12:
            return
        self._lyrics_wrap = wrap
        for label in self.lyrics_labels:
            try:
                label.configure(wraplength=wrap)
            except Exception:
                pass

    @staticmethod
    def _blend(a, b, t):
        ca = tuple(int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        cb = tuple(int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        return "#%02x%02x%02x" % tuple(
            int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))

    def _lyric_style(self, index, state):
        """Paint one line. States: 'active', 'past', 'next'.

        The active line used to jump from 21pt to 28pt, which re-flowed every
        line below it and made the pane lurch on each change. Size is fixed
        and emphasis comes from colour plus a highlight pill.

        Colours are blended against the card rather than the theme background,
        because the lyrics now sit on a card floating over the cover.
        """
        if not (0 <= index < len(self.lyrics_labels)):
            return
        card = getattr(self, "_np_card", self.theme["bg"])
        label = self.lyrics_labels[index]
        try:
            # An instrumental break has no words to light up, and a
            # highlight pill around nothing looks like a rendering fault.
            if not (label.cget("text") or "").strip():
                label.configure(fg_color="transparent")
                return
        except Exception:
            pass
        try:
            if state == "active":
                label.configure(
                    text_color=self.NP_INK,
                    fg_color=self._blend(card, self.theme["accent"], 0.34))
            elif state == "past":
                label.configure(text_color=self._blend(card, self.NP_DIM, 0.62),
                                fg_color="transparent")
            else:
                label.configure(text_color=self.NP_DIM, fg_color="transparent")
        except Exception:
            pass

    def _seek_to_lyric(self, index):
        """Jump the playhead to a line the user clicked."""
        if not (0 <= index < len(self.parsed_lyrics)):
            return
        duration = self.player.get_duration() or 0
        if duration <= 0:
            return
        # A shade early, so the first syllable of the line is not clipped.
        target = max(0.0, self.parsed_lyrics[index][0] - 0.25)
        self.player.set_progress(min(1.0, target / duration))

    def fetch_lyrics(self, query):
        def _fetch():
            try:
                found = lyrics_source.fetch(query, syncedlyrics.search)
                self._safe_after(0, self.setup_lyrics, found)
            except Exception as e:
                print(f"Lyrics error: {e}")
                self._safe_after(0, self.setup_lyrics, ([], False))

        # Clear existing
        for lbl in self.lyrics_labels:
            lbl.destroy()
        self.lyrics_labels.clear()

        loading_lbl = ctk.CTkLabel(self.lyrics_scroll,
                                   text="Looking for lyrics\u2026",
                                   font=theme_ui.font("title"),
                                   text_color=self.NP_DIM)
        loading_lbl.grid(row=0, column=0, pady=28)
        self.lyrics_labels.append(loading_lbl)

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def setup_lyrics(self, found):
        """Render whatever came back: a timed transcript, or just the words.

        Plain lyrics used to be answered with "no synced timings for this
        track" and nothing else, while the words themselves sat in hand and
        went in the bin. They are worth showing; they just do not follow
        along, so the pane says so rather than looking stuck.
        """
        lines, synced = found
        for lbl in self.lyrics_labels:
            lbl.destroy()
        for spacer in getattr(self, "_lyric_spacers", []):
            spacer.destroy()
        self.lyrics_labels.clear()
        self._lyric_spacers = []
        self.parsed_lyrics.clear()
        self.current_lyric_index = -1

        def message(text):
            lbl = ctk.CTkLabel(self.lyrics_scroll, text=text,
                               font=theme_ui.font("body", size=16),
                               text_color=self.NP_DIM, justify="left",
                               fg_color="transparent", anchor="w",
                               wraplength=self._lyric_wrap_width())
            lbl.grid(row=0, column=0, padx=16, pady=24, sticky="ew")
            self.lyrics_labels.append(lbl)

        if not lines:
            message("No lyrics found for this track.\n\nNot every release has a "
                    "transcript; the cover and the queue still work.")
            return

        wrap = self._lyric_wrap_width()
        self._lyrics_wrap = wrap
        row = 0

        if not synced:
            note = ctk.CTkLabel(
                self.lyrics_scroll,
                text="No timings for this one \u2014 the words will not follow along.",
                font=theme_ui.font("caption"), text_color=self.NP_DIM,
                fg_color="transparent", anchor="w", justify="left",
                wraplength=wrap)
            note.grid(row=row, column=0, sticky="ew", padx=16, pady=(6, 14))
            # A spacer rather than a lyric: lyrics_labels is indexed by line.
            self._lyric_spacers.append(note)
            row += 1

        # Spacers top and bottom so the first and last lines can still sit in
        # the middle of the pane when the active line is centred. Untimed
        # lyrics are never centred, so they do not need the room.
        top = ctk.CTkFrame(self.lyrics_scroll, fg_color="transparent",
                           height=140 if synced else 8)
        top.grid(row=row, column=0, sticky="ew")
        self._lyric_spacers.append(top)
        row += 1

        for i, (at, text) in enumerate(lines):
            if synced:
                self.parsed_lyrics.append((at, text))
            lbl = ctk.CTkLabel(self.lyrics_scroll, text=text,
                               font=self._lyric_font(21),
                               text_color=self.NP_DIM,
                               fg_color="transparent", corner_radius=12,
                               wraplength=wrap, justify="left", anchor="w")
            # An empty line is an instrumental break, and reads as one only
            # if it is a gap rather than a full-height blank row.
            lbl.grid(row=row, column=0, sticky="ew", padx=2, pady=3,
                     ipadx=14, ipady=9 if text else 1)
            if synced and text:
                # Clicking a line jumps to it. The timings are already
                # parsed, so the lyrics may as well be a second scrub bar.
                lbl.configure(cursor="hand2")
                lbl.bind("<Button-1>", lambda _e, n=i: self._seek_to_lyric(n))
            self.lyrics_labels.append(lbl)
            row += 1

        bottom = ctk.CTkFrame(self.lyrics_scroll, fg_color="transparent",
                              height=200 if synced else 24)
        bottom.grid(row=row, column=0, sticky="ew")
        self._lyric_spacers.append(bottom)

    def build_dl_view(self, parent):
        self.dl_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.dl_frame.pack(fill="both", expand=True)
        self.dl_frame.grid_columnconfigure(0, weight=1)
        # Results deserve the room; the log is a footnote.
        self.dl_frame.grid_rowconfigure(3, weight=1)
        self.dl_frame.grid_rowconfigure(4, weight=0)

        header = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=36, pady=(22, 6))
        self.dl_heading = ctk.CTkLabel(header, text="Discover",
                                       font=theme_ui.font("display"))
        self.dl_heading.pack(side="left")
        self.dl_close_btn = ctk.CTkButton(
            header, text="Back to library", width=150, height=36,
            corner_radius=theme_ui.RADIUS_PILL,
            font=theme_ui.font("body_med"), command=self.close_downloader)
        self.dl_close_btn.pack(side="right")
        self.dl_hint = ctk.CTkLabel(
            header,
            text="Search to preview anything, or paste a Spotify link to download",
            font=theme_ui.font("caption"))
        self.dl_hint.pack(side="right", padx=16)

        # The search row carries the glyph the palette uses, so the two
        # search surfaces read as the same idea.
        self.dl_search_row = ctk.CTkFrame(self.dl_frame, height=52,
                                          corner_radius=theme_ui.RADIUS_PILL,
                                          fg_color=self.theme["surface"])
        self.dl_search_row.grid(row=1, column=0, padx=36, pady=(6, 10),
                                sticky="ew")
        self.dl_search_row.pack_propagate(False)
        search_row = self.dl_search_row
        self.dl_search_icon = ui_widgets.glyph_canvas(
            search_row, "search", size=24,
            colour=self.theme["text_secondary"],
            background=self.theme["surface"], stroke=1.8)
        self.dl_search_icon.pack(side="left", padx=(20, 10))
        self.url_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Search an artist, album or song, or paste a Spotify link",
            height=44, border_width=0, fg_color="transparent",
            font=theme_ui.font("body", size=16))
        self.url_entry.pack(side="left", fill="both", expand=True,
                            padx=(0, 20))
        self.url_entry.bind("<KeyRelease>", self._on_discover_key)
        self.url_entry.bind("<FocusIn>", lambda e: self.on_key_release(None))
        self.url_entry.bind("<Return>", lambda e: self.start_download())

        self.search_timer = None
        self.suggestions_frame = ctk.CTkScrollableFrame(
            self.dl_frame, height=210, corner_radius=15,
            fg_color=self.theme["surface"])

        buttons = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        buttons.grid(row=2, column=0, padx=36, pady=(2, 8), sticky="ew")

        self.download_button = ctk.CTkButton(
            buttons, text="Start download", height=44, width=176,
            corner_radius=theme_ui.RADIUS_PILL,
            font=theme_ui.font("heading"), command=self.start_download)
        self.download_button.pack(side="left")

        # A long album run used to be uninterruptible.
        self.cancel_button = ctk.CTkButton(
            buttons, text="Cancel", height=44, width=110,
            corner_radius=theme_ui.RADIUS_PILL, command=self.cancel_downloads,
            fg_color="transparent", border_width=1,
            font=theme_ui.font("body_med"))
        self.retry_button = ctk.CTkButton(
            buttons, text="Retry failed", height=44, width=140,
            corner_radius=theme_ui.RADIUS_PILL, command=self.retry_failed,
            font=theme_ui.font("body_med"))
        # Playlists need a signed-in user; everything else works app-only.
        self.signin_btn = ctk.CTkButton(
            buttons, text="Sign in to Spotify", height=44, width=170,
            corner_radius=theme_ui.RADIUS_PILL, fg_color="transparent",
            border_width=1, font=theme_ui.font("body_med"),
            command=self.spotify_sign_in)
        self.signin_btn.pack(side="left", padx=8)

        self.dl_progress_lbl = ctk.CTkLabel(buttons, text="",
                                            font=theme_ui.font("caption"))
        self.dl_progress_lbl.pack(side="right")
        self._sync_signin_button()

        # Search results: preview straight from here, download only what you
        # decide to keep.
        self.results_frame = ctk.CTkScrollableFrame(
            self.dl_frame, corner_radius=theme_ui.RADIUS, label_text="Results",
            fg_color=self.theme["surface"])
        self.results_frame.grid(row=3, column=0, padx=36, pady=(6, 6),
                                sticky="nsew")

        # Per-track state, so a failure no longer scrolls out of the log and
        # disappears with no record of what broke.
        self.jobs_frame = ctk.CTkScrollableFrame(self.dl_frame, corner_radius=15,
                                                 label_text="Queue",
                                                 fg_color=self.theme["surface"])
        self.job_rows = {}

        self.log_box = ctk.CTkTextbox(self.dl_frame, corner_radius=15, height=84,
                                      font=theme_ui.font("mono"),
                                      fg_color=self.theme["surface"])
        self.log_box.configure(state="disabled")
        self._log_shown = False

        self._sync_download_buttons()
        self._rebuild_job_rows()
        # Otherwise the screen opens as two empty boxes filling most of it.
        self._render_discover_message(
            "Search for an artist, album or song.\n\n"
            "Anything you find can be previewed before you decide to keep it, "
            "and a Spotify link pasted above downloads straight away.")

        if self.spotify_error:
            # Searching and downloading no longer need an account, so this is
            # an invitation rather than a wall. It used to disable the search
            # box and the download button, which left the whole screen inert.
            self.dl_hint.configure(
                text="Searching %s. Connect Spotify for your own playlists."
                     % self.catalogue.name)
            self.setup_prompt_btn = ctk.CTkButton(
                self.dl_frame, text="Set up Spotify access", height=40,
                corner_radius=theme_ui.RADIUS_PILL,
                font=theme_ui.font("body_med"), command=self.open_setup)
            self.setup_prompt_btn.grid(row=2, column=0, padx=36, pady=(0, 8),
                                       sticky="e")

    STATE_STYLE = {
        QUEUED:    ("\u25cb", "text_secondary"),
        RUNNING:   ("\u25cf", "accent"),
        DONE:      ("\u2713", "accent"),
        SKIPPED:   ("=", "text_secondary"),
        FAILED:    ("\u2717", "text"),
        CANCELLED: ("\u2013", "text_secondary"),
    }

    def _rebuild_job_rows(self):
        """Redraw the queue list from scratch (called when a batch starts)."""
        if not self._dl_alive():
            return
        for widget in self.jobs_frame.winfo_children():
            widget.destroy()
        self.job_rows = {}
        for job in self.downloads.jobs:
            self._make_job_row(job)

    def _make_job_row(self, job):
        row = ctk.CTkFrame(self.jobs_frame, fg_color="transparent", height=26)
        row.pack(fill="x", padx=4, pady=1)
        icon = ctk.CTkLabel(row, text="", width=18, font=ctk.CTkFont(size=13))
        icon.pack(side="left")
        label = ctk.CTkLabel(row, text=job.label, anchor="w", font=ctk.CTkFont(size=12))
        label.pack(side="left", fill="x", expand=True, padx=6)
        note = ctk.CTkLabel(row, text="", anchor="e", font=ctk.CTkFont(size=11))
        note.pack(side="right", padx=6)
        self.job_rows[id(job)] = (icon, label, note)
        self._paint_job_row(job)

    def _paint_job_row(self, job):
        widgets = self.job_rows.get(id(job))
        if not widgets:
            return
        icon, label, note = widgets
        glyph, colour = self.STATE_STYLE.get(job.state, ("\u25cb", "text_secondary"))
        try:
            icon.configure(text=glyph, text_color=self.theme[colour])
            label.configure(text=job.label, text_color=self.theme[
                "text" if job.state in (RUNNING, DONE) else "text_secondary"])
            note.configure(
                text=(job.error or "")[:60] if job.state == FAILED else
                     ("already have it" if job.state == SKIPPED else ""),
                text_color=self.theme["text_secondary"])
        except Exception:
            pass

    def _dl_alive(self):
        """Whether the downloader panel exists and can be written to."""
        panel = getattr(self, "dl_overlay", None)
        try:
            return panel is not None and panel.winfo_exists()
        except Exception:
            return False

    def _on_job_change(self, job):
        if not self._dl_alive():
            return
        if id(job) not in self.job_rows:
            self._make_job_row(job)
        else:
            self._paint_job_row(job)
        self._sync_download_buttons()

    def _sync_download_buttons(self):
        if not self._dl_alive():
            return
        running = self.downloads.running
        self.download_button.configure(state="disabled" if running else "normal")
        if running:
            self.cancel_button.pack(side="left", padx=8)
        else:
            self.cancel_button.pack_forget()
        if self.downloads.failed_jobs() and not running:
            self.retry_button.pack(side="left", padx=8)
        else:
            self.retry_button.pack_forget()

        counts = self.downloads.summary()
        if counts["total"]:
            finished = counts[DONE] + counts[SKIPPED] + counts[FAILED] + counts[CANCELLED]
            parts = [f"{finished}/{counts['total']} done"]
            if counts[FAILED]:
                parts.append(f"{counts[FAILED]} failed")
            self.dl_progress_lbl.configure(text="  \u00b7  ".join(parts))
        else:
            self.dl_progress_lbl.configure(text="")

    def cancel_downloads(self):
        self.downloads.cancel()
        self._gui_log("Cancelling -- downloads already in flight will finish.")

    def retry_failed(self):
        if self.downloads.retry_failed(LIBRARY_DIR,
                                       jobs=int(self.settings.get("download_jobs") or 3),
                                       quality=self.settings.get("download_quality")):
            self._sync_download_buttons()
            self._watch_downloads()

    # Twelve seconds is past the point where it stops being a transition and
    # starts being a mashup.
    MAX_CROSSFADE = 12.0

    def _show_crossfade(self, value):
        seconds = round(float(value) * self.MAX_CROSSFADE)
        self.crossfade_value.configure(
            text="off" if seconds <= 0 else "%ds" % seconds)
        return seconds

    def on_crossfade(self, value):
        """Seconds of overlap between tracks; zero leaves playback gapless."""
        seconds = self._show_crossfade(value)
        # Keep the control in step when something other than a drag sets it.
        if getattr(self, "crossfade_slider", None) is not None:
            self.crossfade_slider.set(float(value))
        self.player.crossfade = float(seconds)
        self.settings.set("crossfade", float(seconds))
        # A crossfade needs the next track decoded well before the overlap
        # starts, so widen the lead to match.
        self._maybe_preload_next(force=seconds > 0)

    def toggle_eq(self):
        if self.eq_frame.winfo_ismapped():
            self.eq_frame.place_forget()
            self.eq_toggle_btn.configure(fg_color=self.theme["surface_hover"], text_color=self.theme["text"])
        else:
            self.eq_frame.place(relx=0.5, rely=0.9, anchor="s")
            self.eq_toggle_btn.configure(fg_color=self.theme["accent"], text_color=self.theme["bg"])

    def on_eq_preset(self, choice):
        gains = self.presets.get(choice, [1.0] * 10)
        for slider, gain in zip(self.eq_sliders, gains):
            slider.set(gain)
        self.player.set_eq(gains)
        self.settings.update(eq_gains=list(gains), eq_preset=choice)

    def on_eq_change(self, _):
        gains = [s.get() for s in self.eq_sliders]
        self.player.set_eq(gains)
        self.settings.set("eq_gains", gains)

    def change_theme(self, choice):
        if choice not in THEMES:
            return
        self.current_theme_name = choice
        self.theme = THEMES[choice]
        self.settings.set("theme", choice)
        # The dropdown updates itself when the user picks from it, but not
        # when something else calls this, which left it naming a theme that
        # was no longer on screen.
        if getattr(self, "theme_dropdown", None) is not None:
            self.theme_dropdown.set(choice)
        self.apply_theme()

    def apply_theme(self):
        """Repaint every themed widget.

        This only touched six widgets before, so switching themes left the
        title bar, transport buttons, overlays and sliders on the old palette.
        """
        t = self.theme
        # CustomTkinter picks its own defaults from the appearance mode, and
        # any widget we do not colour explicitly falls back to them. Pinned to
        # "Dark", the two light themes drew a dark frame behind every
        # transparent widget -- every row in the library had a black box round
        # it.
        ctk.set_appearance_mode(
            "Light" if themes.luminance(t["bg"]) > 0.5 else "Dark")
        self.configure(fg_color=t["bg"])

        for name, key in (
            ("main_area", "bg"), ("title_bar", "surface"), ("bottom_bar", "surface"),
            ("eq_frame", "surface"), ("viz_overlay", "bg"),
            # Scrolling frames have to be repainted by name: they own a canvas
            # that keeps whatever colour it was built with.
            ("library_frame", "bg"), ("queue_list", "surface"),
            ("queue_panel", "surface"), ("suggestions_frame", "surface"),
            ("results_frame", "surface"), ("jobs_frame", "surface"),
            ("dl_search_row", "surface"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t[key])

        if hasattr(self, 'canvas'):
            self.canvas.configure(bg=t["bg"])
            self._viz_idle = False        # the idle text needs repainting
        if getattr(self, "brand_word", None) is not None:
            self.brand_word.configure(text_color=t["text"])
        if getattr(self, "brand_mark", None) is not None:
            ui_widgets.repaint_glyph(self.brand_mark, t["accent"], t["bg"])
        if hasattr(self, 'lib_search_entry'):
            self.lib_search_entry.configure(fg_color=t["bg"], text_color=t["text"])

        if getattr(self, "view_tabs", None) is not None:
            # A segmented button has one text colour for both states, so the
            # selected fill has to stay readable against it -- readable_tint
            # moves the accent away from the ink until it does, which is the
            # same rule the album page headers use.
            self.view_tabs.configure(
                fg_color=t["surface"],
                selected_color=ui_widgets.readable_tint(
                    t["accent"], t["text"], t["surface_hover"]),
                selected_hover_color=t["surface_hover"],
                unselected_color=t["surface"],
                unselected_hover_color=t["surface_hover"],
                text_color=t["text"])
        if getattr(self, "lib_search_entry", None) is not None:
            self.lib_search_entry.configure(border_color=t["surface_hover"],
                                            placeholder_text_color=t["text_secondary"])

        for name in ("theme_dropdown", "viz_dropdown", "preset_dropdown",
                     "sort_dropdown"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["surface"], button_color=t["surface"],
                                 button_hover_color=t["surface_hover"],
                                 dropdown_fg_color=t["surface"],
                                 dropdown_hover_color=t["surface_hover"],
                                 text_color=t["text"])

        for name in ("nav_dl_btn", "eq_toggle_btn", "viz_toggle_btn",
                     "repair_btn", "dedupe_btn", "new_pl_btn", "queue_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["accent"], hover_color=t["accent_hover"],
                                 text_color=t["bg"])

        if getattr(self, "min_btn", None) is not None:
            self.min_btn.configure(hover_color=t["surface_hover"],
                                   text_color=t["text"])

        # The transport paints itself, so a theme change is a repaint rather
        # than a text-colour swap.
        for name in ("shuffle_btn", "prev_btn", "play_btn", "next_btn",
                     "repeat_btn", "volume_icon"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.set_palette(t)
        if getattr(self, "shuffle_btn", None) is not None:
            self.shuffle_btn.set_active(self.shuffle)
        if getattr(self, "repeat_btn", None) is not None:
            self.repeat_btn.set_active(self.repeat)

        for name in ("progress_slider", "volume_slider"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.set_palette(t)

        if getattr(self, "now_playing_sub", None) is not None:
            self.now_playing_sub.configure(text_color=t["text_secondary"])
        for name in ("crossfade_lbl", "crossfade_value"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text_color=t["text_secondary"])
        if getattr(self, "crossfade_slider", None) is not None:
            self.crossfade_slider.set_palette(t)

        for name in ("now_playing_label", "time_elapsed", "time_total"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text_color=t["text"])

        if not getattr(self, 'dynamic_accent', None):
            self.dynamic_accent = t["accent"]

        for name in ("dl_overlay",):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["bg"])
        for name in ("dl_heading",):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text_color=t["text"])
        for name in ("dl_hint", "dl_progress_lbl"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text_color=t["text_secondary"])
        if getattr(self, "dl_search_icon", None) is not None:
            ui_widgets.repaint_glyph(self.dl_search_icon, t["text_secondary"],
                                     t["surface"])
        if getattr(self, "log_box", None) is not None:
            self.log_box.configure(fg_color=t["surface"])
        for name in ("download_button", "retry_button", "dl_close_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["accent"], hover_color=t["accent_hover"],
                                 text_color=t["bg"])
        # Outlined buttons: the accent is the edge rather than the fill, so
        # they sit beside a solid one without competing with it.
        for name in ("signin_btn", "cancel_button", "import_pl_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(border_color=t["accent"],
                                 hover_color=t["surface_hover"],
                                 text_color=t["text"])
        # The full-screen view keeps its own dark palette on purpose, so a
        # theme change only has to reach its one accent-coloured control.
        if getattr(self, "np_close_btn", None) is not None:
            self.np_close_btn.set_palette(self._np_theme(),
                                          background=self.NP_FLOOR)
        if getattr(self, "viz_palette_dropdown", None) is not None:
            self.viz_palette_dropdown.configure(
                fg_color=t["surface"], button_color=t["surface"],
                button_hover_color=t["surface_hover"],
                dropdown_fg_color=t["surface"],
                dropdown_hover_color=t["surface_hover"], text_color=t["text"])
        for i in range(len(getattr(self, "lyrics_labels", []))):
            self._lyric_style(i, "active" if i == self.current_lyric_index else "next")

        if getattr(self, "_palette", None) is not None:
            self._palette.set_theme(t)

        view = getattr(self, "library", None)
        if view is not None:
            # Rows are built with literal theme colours, so a theme change
            # has to rebuild them rather than just recolour the containers.
            view.theme = t
            view.invalidate()
            view.render()

    def toggle_visualizer_visibility(self, event=None):
        if getattr(self, "dl_visible", False):
            self.close_downloader()
        self.visualizer_visible = not self.visualizer_visible
        # Skip the per-chunk FFT entirely while the overlay is hidden.
        self.player.visualizer_enabled = self.visualizer_visible
        self.settings.set("visualizer_visible", self.visualizer_visible)
        self._viz_idle = False
        if self.visualizer_visible:
            self._slide_in(self.viz_overlay)
            self.viz_dropdown.set(VIZ_MODES[self.visualizer_mode])
        else:
            self._slide_out(self.viz_overlay)

    def toggle_visualizer_mode(self, event=None):
        self.set_visualizer_mode(self.visualizer_mode + 1)

    def set_visualizer_mode(self, index):
        self.visualizer_mode = index % len(VIZ_MODES)
        self.settings.set("visualizer_mode", self.visualizer_mode)
        if hasattr(self, "viz_dropdown"):
            self.viz_dropdown.set(VIZ_MODES[self.visualizer_mode])

    def set_visualizer_mode_by_name(self, name):
        try:
            self.set_visualizer_mode(VIZ_MODES.index(name))
        except ValueError:
            self.set_visualizer_mode(0)

    def _draw_bands(self, bands):
        """Delegate to the visualiser registry.

        This was a 180-line if/elif chain over sixteen inlined renderers.
        """
        if not hasattr(self, "canvas") or not self.canvas.winfo_exists():
            return
        if not self.visualizer_visible:
            return
        palette = self.visualizer_palette
        if palette == "Album art":
            base = getattr(self, "dynamic_accent", None) or self.theme["accent"]
        else:
            base = self.theme["accent"]
        visualizers.draw(
            self.canvas, self.visualizer_mode, bands, base,
            self.canvas.winfo_width(), self.canvas.winfo_height(), time.time(),
            palette=palette,
        )

    def set_visualizer_palette(self, name):
        self.visualizer_palette = name
        self.settings.set("visualizer_palette", name)
        # Keep the control in step when the palette is set from code, the
        # same way set_visualizer_mode does for the mode dropdown.
        if getattr(self, "viz_palette_dropdown", None) is not None:
            self.viz_palette_dropdown.set(name)

    def update_visualizer(self, bands):
        """Kept for callers that push bands in explicitly."""
        self._safe_after(0, self._draw_bands, bands)

    def update_visualizer_loop(self):
        """Redraw at ~30 fps from the Tk thread, reading the engine's state."""
        if self.visualizer_visible:
            if self.player.playing and not self.player.paused:
                self._viz_idle = False
                self._draw_bands(self.player.smoothed_bands.tolist())
            elif not self._viz_idle:
                self._viz_idle = True
                self._draw_idle_visualizer()
        self._safe_after(33, self.update_visualizer_loop)

    def _draw_idle_visualizer(self):
        """Something to look at when there are no bands to draw.

        The loop only drew while audio was running, so opening the visualiser
        with nothing playing left the last frame frozen on screen, or on a
        fresh launch a black rectangle with two dropdowns floating in the
        corner and no indication of what it was for.
        """
        canvas = getattr(self, "canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        canvas.delete("all")
        canvas.create_text(width / 2, height / 2 - 14, text="Nothing playing",
                           fill=self.theme["text"],
                           font=theme_ui.font("title"))
        canvas.create_text(width / 2, height / 2 + 16,
                           text="Start a track and the spectrum appears here.",
                           fill=self.theme["text_secondary"],
                           font=theme_ui.font("body"))

    def set_appwindow(self):
        hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        style = style & ~0x00000080
        style = style | 0x00040000
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
        self.withdraw()
        self.deiconify()

    def _apply_window_icon(self):
        """Set the window/taskbar icon from the icon baked into the source.

        wm_iconphoto takes an image rather than a path, so nothing has to be
        shipped alongside the executable for this to work.
        """
        try:
            self._icon_photo = app_icon.icon_photo(64)
            self.wm_iconphoto(True, self._icon_photo)
        except Exception as e:
            print(f"Could not set window icon: {e}")

    def _restore_geometry(self):
        """Reopen where you left off, but only if it is still on-screen."""
        win = self.settings.get("window") or {}
        w = max(900, int(win.get("w") or 1100))
        h = max(620, int(win.get("h") or 800))
        x, y = win.get("x"), win.get("y")
        if x is None or y is None:
            self.geometry(f"{w}x{h}")
            return
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if -50 <= int(x) <= sw - 200 and -10 <= int(y) <= sh - 120:
            self.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        else:
            self.geometry(f"{w}x{h}")

    def _build_resize_grips(self):
        """Restore resizing that overrideredirect(True) takes away.

        A frameless window loses the OS border, so the window was stuck at a
        fixed size with no way to resize or maximise. These thin edge strips
        reimplement drag-to-resize while keeping the custom chrome.
        """
        # CustomTkinter wants width/height on the constructor, not on place().
        edges = {
            "e":  (dict(width=6),            dict(relx=1.0, rely=0.0, anchor="ne", relheight=1.0)),
            "w":  (dict(width=6),            dict(relx=0.0, rely=0.0, anchor="nw", relheight=1.0)),
            "s":  (dict(height=6),           dict(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0)),
            "se": (dict(width=16, height=16), dict(relx=1.0, rely=1.0, anchor="se")),
            "sw": (dict(width=16, height=16), dict(relx=0.0, rely=1.0, anchor="sw")),
        }
        cursors = {"e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
                   "s": "sb_v_double_arrow", "se": "size_nw_se", "sw": "size_ne_sw"}
        self._grips = {}
        for edge, (size, place) in edges.items():
            grip = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0,
                                cursor=cursors[edge], **size)
            grip.place(**place)
            grip.bind("<Button-1>", lambda e, s=edge: self._resize_start(e, s))
            grip.bind("<B1-Motion>", lambda e, s=edge: self._resize_drag(e, s))
            self._grips[edge] = grip

    def _resize_start(self, event, edge):
        self._resize_origin = (
            event.x_root, event.y_root,
            self.winfo_width(), self.winfo_height(),
            self.winfo_x(), self.winfo_y(),
        )

    def _resize_drag(self, event, edge):
        if not getattr(self, "_resize_origin", None):
            return
        x0, y0, w0, h0, wx, wy = self._resize_origin
        dx, dy = event.x_root - x0, event.y_root - y0
        w, h, x, y = w0, h0, wx, wy

        if "e" in edge:
            w = max(900, w0 + dx)
        if "w" in edge:
            w = max(900, w0 - dx)
            x = wx + (w0 - w)
        if "s" in edge:
            h = max(620, h0 + dy)
        self.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")

    def toggle_maximize(self, event=None):
        """Frameless windows get no maximise button, so emulate one."""
        if getattr(self, "_pre_max", None):
            w, h, x, y = self._pre_max
            self._pre_max = None
            self.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self._pre_max = (self.winfo_width(), self.winfo_height(),
                             self.winfo_x(), self.winfo_y())
            self.geometry(f"{self.winfo_screenwidth()}x"
                          f"{self.winfo_screenheight() - 48}+0+0")

    def minimize_to_tray(self):
        self.withdraw()

        # Each minimise used to build a brand-new Icon and thread, so the tray
        # accumulated a dead icon every time the window was restored.
        if self._tray_icon is not None:
            return

        # Was a glyph drawn with ImageDraw.text onto a black square, which
        # came out as an unreadable smudge at tray size.
        image = app_icon.icon_image(64)

        def restore(icon=None, item=None):
            self._tray_icon = None
            if icon is not None:
                icon.stop()
            self._safe_after(0, self.deiconify)

        def quit_app(icon, item):
            self._tray_icon = None
            icon.stop()
            self._safe_after(0, self.on_close)

        menu = pystray.Menu(pystray.MenuItem('Show', restore, default=True),
                            pystray.MenuItem('Exit', quit_app))
        self._tray_icon = pystray.Icon("EvesGarden", image, "Eve\'s Garden", menu)
        self.tray_icon = self._tray_icon  # kept for backwards compatibility
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def on_close(self):
        """Persist state, then shut the background machinery down."""
        self._closing = True
        self._save_state()
        for stop in (
            lambda: self._tray_icon and self._tray_icon.stop(),
            lambda: self.discover.close(),
            lambda: self.discord.stop(),
            lambda: self.media_keys.stop(),
            lambda: self.downloads.cancel(),
            lambda: self.player.close(),
            lambda: self.index.close(),
        ):
            try:
                stop()
            except Exception:
                pass
        self._tray_icon = None
        self.destroy()

    def _save_state(self):
        try:
            if not getattr(self, "_pre_max", None):
                self.settings.set("window", {
                    "x": self.winfo_x(), "y": self.winfo_y(),
                    "w": self.winfo_width(), "h": self.winfo_height(),
                })
            track = None
            if 0 <= self.current_index < len(self.current_playlist):
                track = self.current_playlist[self.current_index]
            self.settings.update(
                last_track=track,
                last_position=self.player.get_position(),
                library_view=self.library_view,
                library_sort=self.library_sort,
            )
            self.settings.save()
        except Exception as e:
            print(f"Could not save settings: {e}")

    def _autosave_loop(self):
        """Checkpoint periodically so a crash does not lose your place."""
        self._save_state()
        self._safe_after(120000, self._autosave_loop)

    def _resume_last_track(self):
        """Reopen on the track you left, paused at the same position."""
        if not self.settings.get("resume_on_launch"):
            return
        path = self.settings.get("last_track")
        position = float(self.settings.get("last_position") or 0)
        if not path or not os.path.exists(path):
            return

        self.current_playlist = list(self.current_library_files)
        try:
            self.current_index = self.current_playlist.index(path)
        except ValueError:
            self.current_playlist = [path]
            self.current_index = 0

        try:
            row = next((t for t in self.index.tracks() if t["path"] == path),
                       None)
        except Exception:
            row = None
        self._present_track(path, row)

        def load():
            if self.player.load_track(path):
                duration = self.player.get_duration()
                if duration > 0 and position < duration - 2:
                    self.player.set_progress(position / duration)
                self._safe_after(0, self._show_resumed)

        threading.Thread(target=load, daemon=True).start()

    def _show_resumed(self):
        self.play_btn.set_glyph("play")          # paused: press play to continue
        self.progress_slider.set(self.player.get_progress())
        self.time_elapsed.configure(text=fmt_time(self.player.get_position()))
        self.time_total.configure(text=fmt_time(self.player.get_duration()))

    def _sync_playlist_button(self):
        for name in ("new_pl_btn", "import_pl_btn"):
            button = getattr(self, name, None)
            if button is None:
                continue
            if self.library_view == "Playlists":
                button.pack(side="left", padx=6)
            else:
                button.pack_forget()

    # What the search box keeps before the wordmark is asked to go. 180px
    # still shows most of its placeholder; below that it stops reading as a
    # search box at all.
    SEARCH_FLOOR = 180

    def _sync_brand(self, _event=None):
        """Give up the wordmark before the search box becomes unusable.

        This row carries a brand, seven tabs and up to four controls, and at
        the size the app opens at they do not all fit -- they never did; the
        overflow used to land on whichever button was packed last, which came
        out sliced in half. Something has to yield, and the wordmark is the
        only part of the row that is decoration rather than a control, so it
        goes first and takes the leaf with it if that is still not enough.
        """
        # A <Configure> can land while the row is still being built, so
        # every part of it is checked before any of it is measured.
        head = getattr(self, "library_header", None)
        word = getattr(self, "brand_word", None)
        mark = getattr(self, "brand_mark", None)
        tabs = getattr(self, "view_tabs", None)
        actions = getattr(self, "library_actions", None)
        if None in (head, word, mark, tabs, actions):
            return
        if self._brand_busy:
            return          # update_idletasks below can land us back here
        available = head.winfo_width()
        if available <= 1:
            return          # not laid out yet; Configure will call again

        self._brand_busy = True
        try:
            # A control that has just been packed, or had its label changed,
            # has not worked out how wide it wants to be yet -- the Duplicates
            # button reported almost nothing until Tk caught up, so the row
            # kept its masthead and squeezed the search box to 62px instead.
            actions.update_idletasks()
            self._place_brand(head, tabs, actions, word, mark, available)
        finally:
            self._brand_busy = False

    def _place_brand(self, head, tabs, actions, word, mark, available):
        # Summed over the cluster's own children rather than asking the
        # cluster: a frame's requested width is only recomputed once the
        # geometry manager next runs, so straight after a button is packed or
        # forgotten it still reports the width it had before -- which had the
        # wide Playlists row keeping the wordmark while the narrow Songs row
        # gave it up. A child's own requested width does not move.
        cluster = sum(w.winfo_reqwidth() + 12
                      for w in actions.winfo_children()
                      if w.winfo_manager() == "pack")
        needed = tabs.winfo_reqwidth() + 12 + cluster + self.SEARCH_FLOOR + 14
        spare = available - needed
        mark_w = mark.winfo_reqwidth() + 20      # the leaf and the padding
        want_mark = spare >= mark_w
        want_word = want_mark and spare >= mark_w + word.winfo_reqwidth() + 9

        # The frame goes, not just its contents: an emptied CTkFrame still
        # holds 43px of the row, which is most of what the leaf was meant to
        # be giving back.
        for widget, wanted, kwargs in (
                (self.brand, want_mark,
                 {"side": "left", "padx": (2, 18), "before": tabs}),
                (word, want_word, {"side": "left"})):
            if wanted == (widget.winfo_manager() == "pack"):
                continue
            if wanted:
                widget.pack(**kwargs)
            else:
                widget.pack_forget()

    def _sync_sort_control(self):
        """Offer the sort only where it changes anything.

        Five of the seven tabs ignore it outright: Liked is always ordered by
        when you liked it, Recent by when you played it, Duplicates by what
        deleting them would reclaim, Albums and Artists by name, and a
        playlist by its own order. The dropdown sat over all of them looking
        live, and answering to nothing.
        """
        widget = getattr(self, "sort_dropdown", None)
        if widget is None:
            return
        wanted = (self.library_view == "Songs"
                  or getattr(self.library, "filter", None) is not None)
        if wanted == (widget.winfo_manager() == "pack"):
            return
        if wanted:
            # before=, or it would come back at the far end of the cluster.
            widget.pack(side="left", padx=(0, 10), before=self.nav_dl_btn)
        else:
            widget.pack_forget()

    def _sync_dedupe_button(self):
        """Only offer the delete action while the Duplicates view is open."""
        button = getattr(self, "dedupe_btn", None)
        if button is None:
            return
        if self.library_view == "Duplicates" and getattr(self, "library", None):
            marked = len(self.library.marked_duplicates())
            button.configure(text=f"Move {marked} to Recycle Bin",
                             state="normal" if marked else "disabled")
            button.pack(side="left", padx=6)
        else:
            button.pack_forget()

    def remove_duplicates(self):
        """Recycle the ticked copies, then re-scan.

        These go to the Recycle Bin rather than being deleted outright, so a
        wrong call is undoable from Explorer.
        """
        paths = self.library.marked_duplicates()
        if not paths:
            return
        self.dedupe_btn.configure(state="disabled", text="Removing...")

        def work():
            recycled, failed = recycle.send_to_recycle_bin(paths)
            for path in recycled:
                try:
                    self.index.forget(path)
                except Exception:
                    pass
            self._safe_after(0, self._finish_dedupe, len(recycled), failed)

        threading.Thread(target=work, daemon=True).start()

    def _finish_dedupe(self, removed, failed):
        note = f"Moved {removed} file{'' if removed == 1 else 's'} to the Recycle Bin"
        if failed:
            note += f"; {len(failed)} could not be removed"
        self.library_status.configure(text=note)
        self.library.invalidate()
        self.render_library()
        self._sync_dedupe_button()

    def refresh_repair_button(self):
        """Show the repair action only when orphaned raw downloads exist."""
        try:
            orphans = find_orphaned_downloads(LIBRARY_DIR)
        except Exception:
            orphans = []
        if orphans:
            size_mb = sum(os.path.getsize(p) for p in orphans) / 1e6
            noun = "file" if len(orphans) == 1 else "files"
            self.repair_btn.configure(
                text=f"Repair {len(orphans)} {noun}  ({size_mb:.0f} MB)")
            self.repair_btn.pack(side="left", padx=10)
        else:
            self.repair_btn.pack_forget()

    def run_repair(self):
        """Convert leftover raw downloads from failed conversions into MP3s."""
        self.repair_btn.configure(state="disabled", text="Repairing...")

        def work():
            try:
                repair_library(self.sp, LIBRARY_DIR, log_callback=lambda m: print(m))
            except Exception as e:
                print(f"Repair failed: {e}")
            self._safe_after(0, self.load_library)
            self._safe_after(0, lambda: self.repair_btn.configure(state="normal"))
            self._safe_after(0, self.refresh_repair_button)

        threading.Thread(target=work, daemon=True).start()

    def load_library(self):
        """Refresh the index in the background, then re-render."""
        if hasattr(self, "repair_btn"):
            self.refresh_repair_button()

        def work():
            try:
                added, _updated, removed = self.index.scan(LIBRARY_DIR)
            except Exception as e:
                print(f"Library scan failed: {e}")
                return
            if added or removed:
                self._safe_after(0, self.library.invalidate)
            self._safe_after(0, self.render_library)

        threading.Thread(target=work, daemon=True).start()
        self.render_library()

    # -- thin delegates onto LibraryView ---------------------------------

    def toggle_queue(self):
        """Slide the up-next panel in and out."""
        if getattr(self, "queue_panel", None) is None:
            self._build_queue_panel()
        self.queue_visible = not self.queue_visible
        if self.queue_visible:
            # CustomTkinter wants width on the constructor, not on place().
            self.queue_panel.place(relx=1.0, rely=0, anchor="ne", relheight=1.0)
            self.queue_panel.lift()
            self._render_queue()
        else:
            self.queue_panel.place_forget()

    def _build_queue_panel(self):
        t = self.theme
        self.queue_panel = ctk.CTkFrame(self.main_area, corner_radius=0,
                                        width=330, fg_color=t["surface"])
        self.queue_panel.pack_propagate(False)
        header = ctk.CTkFrame(self.queue_panel, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(header, text="Up next", font=theme_ui.font("title"),
                     text_color=t["text"]).pack(side="left")
        ctk.CTkButton(header, text="Clear", width=64, height=28,
                      corner_radius=14, font=theme_ui.font("small"),
                      fg_color="transparent", border_width=1,
                      command=self.queue.clear).pack(side="right")
        self.queue_list = ctk.CTkScrollableFrame(self.queue_panel,
                                                 fg_color=t["surface"])
        self.queue_list.pack(fill="both", expand=True, padx=8, pady=(0, 12))

    def _queue_entry(self, parent, path, queued, ink=None, dim=None,
                     hover=None):
        """One up-next row. Used by the side panel and the full-screen view,
        which sit on different backgrounds and so pass their own colours."""
        ink = ink or self.theme["text"]
        dim = dim or self.theme["text_secondary"]
        hover = hover or self.theme["surface_hover"]
        row = ctk.CTkFrame(parent, fg_color="transparent", height=44,
                           corner_radius=8)
        row.pack(fill="x", padx=4, pady=1)
        row.pack_propagate(False)
        meta = self._queue_meta.get(path, {})
        title = meta.get("title") or os.path.basename(path)

        def fit(text, n=30):
            text = text or ""
            return text if len(text) <= n else text[:n - 1].rstrip() + "…"

        if queued:
            ctk.CTkButton(row, text="✕", width=26, height=26,
                          corner_radius=13, fg_color="transparent",
                          font=theme_ui.font("small"),
                          command=lambda p=path: self.queue.remove(p)
                          ).pack(side="right", padx=4)

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=fit(title), anchor="w",
                     font=theme_ui.font("body_med"),
                     text_color=ink).pack(anchor="w", pady=(5, 0))
        ctk.CTkLabel(box, text=fit(meta.get("artist"), 34), anchor="w",
                     font=theme_ui.font("small"),
                     text_color=dim).pack(anchor="w")

        # The queue was a read-only list; clicking a row now jumps to it,
        # which is the obvious thing to try and did nothing before.
        def enter(_e=None):
            if row.winfo_exists():
                row.configure(fg_color=hover)

        def leave(_e=None):
            if row.winfo_exists():
                row.configure(fg_color="transparent")

        def go(_e=None):
            self._play_from_queue(path)

        for widget in (row, box, *box.winfo_children()):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
            widget.bind("<Button-1>", go, add="+")
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _play_from_queue(self, path):
        """Play a row from the up-next list, dropping it out of the queue."""
        self.queue.remove(path)
        if path in self.current_playlist:
            self.current_index = self.current_playlist.index(path)
        self.play_file(path)

    def _render_queue(self):
        self._render_np_queue()
        if getattr(self, "queue_panel", None) is None or not self.queue_visible:
            return
        for widget in self.queue_list.winfo_children():
            widget.destroy()

        upcoming = self.queue.upcoming
        following = self.queue.context_after(15)
        # One index lookup for the whole panel rather than one per row.
        wanted = set(upcoming) | set(following)
        self._queue_meta = {t["path"]: t for t in self.index.tracks()
                            if t["path"] in wanted}

        if upcoming:
            ctk.CTkLabel(self.queue_list, text="QUEUED", anchor="w",
                         font=theme_ui.font("small"),
                         text_color=self.theme["accent"]).pack(
                             anchor="w", padx=8, pady=(4, 2))
            for path in upcoming:
                self._queue_entry(self.queue_list, path, True)

        if following:
            ctk.CTkLabel(self.queue_list, text="THEN FROM THIS LIST", anchor="w",
                         font=theme_ui.font("small"),
                         text_color=self.theme["text_secondary"]).pack(
                             anchor="w", padx=8, pady=(12, 2))
            for path in following:
                self._queue_entry(self.queue_list, path, False)

        if not upcoming and not following:
            ctk.CTkLabel(self.queue_list, text="Nothing queued.",
                         font=theme_ui.font("body"),
                         text_color=self.theme["text_secondary"]).pack(pady=20)

    def show_track_menu(self, path, x, y):
        """Right-click actions for one track."""
        menu = tk.Menu(self, tearoff=0,
                       bg=self.theme["surface"], fg=self.theme["text"],
                       activebackground=self.theme["accent"],
                       activeforeground=self.theme["bg"],
                       borderwidth=0, font=(theme_ui.ui_family(), 10))
        menu.add_command(label="Play", command=lambda: self.play_from_library(path))
        menu.add_command(label="Play next",
                         command=lambda: self.queue.add(path, next_up=True))
        menu.add_command(label="Add to queue", command=lambda: self.queue.add(path))
        menu.add_separator()

        liked = self.index.is_liked(path)
        menu.add_command(label="Remove from Liked" if liked else "Add to Liked",
                         command=lambda: self._like_from_menu(path))

        playlists = self.index.playlists()
        if playlists:
            submenu = tk.Menu(menu, tearoff=0, bg=self.theme["surface"],
                              fg=self.theme["text"],
                              activebackground=self.theme["accent"],
                              activeforeground=self.theme["bg"], borderwidth=0)
            for playlist in playlists:
                submenu.add_command(
                    label=playlist["name"],
                    command=lambda pid=playlist["id"]: self._add_to_playlist(pid, path))
            menu.add_cascade(label="Add to playlist", menu=submenu)
        menu.add_command(label="New playlist with this...",
                         command=lambda: self.prompt_new_playlist([path]))

        if self.library.view == "Playlists" and self.library.playlist_id:
            menu.add_separator()
            menu.add_command(label="Remove from this playlist",
                             command=lambda: self._remove_from_playlist(path))

        menu.add_separator()
        menu.add_command(label="Show in Explorer",
                         command=lambda: self._reveal(path))
        try:
            menu.tk_popup(int(x), int(y))
        finally:
            menu.grab_release()

    def _like_from_menu(self, path):
        liked = self.toggle_like(path)
        self.library.set_heart(path, liked)

    def _add_to_playlist(self, playlist_id, path):
        added = self.index.add_to_playlist(playlist_id, path)
        self.library_status.configure(
            text="Added to playlist" if added else "Already in that playlist")

    def _remove_from_playlist(self, path):
        self.index.remove_from_playlist(self.library.playlist_id, path)
        self.library.invalidate()
        self.render_library()

    def _reveal(self, path):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            print(f"Could not open Explorer: {e}")

    def prompt_new_playlist(self, paths=()):
        """Ask for a name, create the playlist, and drop any tracks in."""
        name = dialogs.prompt_text(
            self, self.theme, "New playlist", "Name your playlist",
            placeholder="Late night, Gym, Everything Sob Rock...",
            confirm="Create")
        if not name:
            return
        playlist_id = self.index.create_playlist(name)
        if paths:
            self.index.add_to_playlist(playlist_id, list(paths))
        self.library_view = "Playlists"
        self.view_tabs.set("Playlists")
        self.library.playlist_id = None
        self.library.set_view("Playlists")
        self.render_library()

    # ------------------------------------------- importing from Spotify

    def _import_status(self, message):
        """Status belongs beside the library, not in the download log.

        The log lives on the Add music screen; an import starts from the
        Playlists tab, and reporting into a panel the user is not looking at
        is the same as not reporting at all.
        """
        if getattr(self, "library_status", None) is not None:
            self.library_status.configure(text=message)

    def import_from_spotify(self):
        """Bring the account's playlists across as playlists here."""
        if not self.sp:
            self._import_status("Connect Spotify first: Add music, then "
                                "Set up Spotify access.")
            return
        if self.user_sp is None and not self._refresh_user_client():
            # Reading playlists needs a signed-in user -- public ones
            # included -- and that button lives on the Add music screen.
            self._import_status("Sign in to Spotify first.")
            self.open_downloader()
            return
        if self.downloads.running:
            self._import_status("A download is already running. Try again "
                                "once it has finished.")
            return

        self.import_pl_btn.configure(state="disabled")
        self._import_status("Reading your playlists...")

        def work():
            try:
                found = spotify_import.list_playlists(self.user_sp)
            except Exception as e:
                self._safe_after(0, self._import_failed,
                                 "Could not read your playlists: %s" % e)
                return
            self._safe_after(0, self._choose_playlists, found)

        threading.Thread(target=work, daemon=True).start()

    def _import_failed(self, message):
        self.import_pl_btn.configure(state="normal")
        self._import_status(message)
        self._gui_log(message)

    def _choose_playlists(self, playlists):
        self.import_pl_btn.configure(state="normal")
        self._import_status("")
        chosen = dialogs.pick_playlists(self, self.theme, playlists)
        if not chosen:
            return
        self.import_pl_btn.configure(state="disabled")
        self._import_status("Reading %d playlist%s from Spotify..."
                            % (len(chosen), "" if len(chosen) == 1 else "s"))
        threading.Thread(target=self._import_worker, args=(chosen,),
                         daemon=True).start()

    def _import_worker(self, chosen):
        """Read each chosen playlist and work out what is already here.

        Off the main thread: a long account is a lot of paging, and the whole
        library index is walked once to match against.
        """
        owned = self.index.fingerprints()
        plans, missing, labels, meta = [], [], {}, {}
        for playlist in chosen:
            try:
                tracks = spotify_import.read_playlist(self.user_sp, playlist)
            except Exception as e:
                self._gui_log("Could not read %s: %s" % (playlist["name"], e))
                continue
            paths, gaps = spotify_import.plan(tracks, owned, LIBRARY_DIR)
            plans.append((playlist["name"], paths))
            for track in gaps:
                # Keyed so a song sitting on three playlists is downloaded
                # once rather than three times.
                key = (track.get("spotify_url")
                       or spotify_import.predicted_path(track, LIBRARY_DIR))
                if key in meta:
                    continue
                meta[key] = track
                labels[key] = "%s - %s" % (", ".join(track["artists"]),
                                           track["name"])
                missing.append(key)
        self._safe_after(0, self._finish_import, plans, missing, labels, meta)

    def _playlist_named(self, name):
        """Reuse a playlist of this name, or make one.

        Importing the same account twice should top the playlists up rather
        than leaving two of everything.
        """
        wanted = (name or "").strip().lower()
        for row in self.index.playlists():
            if (row["name"] or "").strip().lower() == wanted:
                return row["id"]
        return self.index.create_playlist(name)

    def _finish_import(self, plans, missing, labels, meta):
        self.import_pl_btn.configure(state="normal")
        if not plans:
            self._import_status("Nothing could be read from Spotify.")
            return

        here = 0
        for name, paths in plans:
            playlist_id = self._playlist_named(name)
            have = [p for p in paths if os.path.exists(p)]
            if have:
                self.index.add_to_playlist(playlist_id, have)
                self.index.reorder_playlist(playlist_id, have)
            here += len(have)
            # The rest are filed once they land, and the whole playlist is
            # put back into Spotify's order then -- so a track that took
            # longest does not end up at the bottom because of it.
            if len(have) < len(paths):
                self._pending_imports.append((playlist_id, paths))

        self.library_view = "Playlists"
        self.view_tabs.set("Playlists")
        self.library.playlist_id = None
        self.library.set_view("Playlists")
        self.library.invalidate()
        self.render_library()

        note = "%d playlist%s imported, %d track%s already here" % (
            len(plans), "" if len(plans) == 1 else "s",
            here, "" if here == 1 else "s")
        if missing:
            self._import_status("%s. Downloading the other %d."
                                % (note, len(missing)))
            self._start_batch(missing, labels=labels, meta=meta)
        else:
            self._import_status(note + ". Nothing left to download.")

    def _reconcile_imports(self):
        """File newly downloaded tracks into the playlists waiting for them."""
        if not self._pending_imports:
            return
        pending, self._pending_imports = self._pending_imports, []
        for playlist_id, paths in pending:
            have = [p for p in paths if os.path.exists(p)]
            if not have:
                continue
            self.index.add_to_playlist(playlist_id, have)
            self.index.reorder_playlist(playlist_id, have)
            # A failed download can still be retried, so the plan is kept
            # until every track is either here or given up on.
            if len(have) < len(paths) and self.downloads.failed_jobs():
                self._pending_imports.append((playlist_id, paths))
        if getattr(self, "library", None) is not None:
            self.library.invalidate()

    def _queue_next(self):
        current = None
        if 0 <= self.current_index < len(self.current_playlist):
            current = self.current_playlist[self.current_index]
        return self.queue.next_path(shuffle=self.shuffle, repeat=self.repeat,
                                    current=current)

    def toggle_like(self, path):
        """Flip a track's heart and keep the bottom bar in step."""
        liked = self.index.toggle_liked(path)
        if getattr(self, "_now_playing_row", None) and                 self._now_playing_row.get("path") == path:
            self._sync_now_playing_heart(liked)
        return liked

    def _sync_now_playing_heart(self, liked=None):
        button = getattr(self, "np_like_btn", None)
        if button is None:
            return
        row = getattr(self, "_now_playing_row", None)
        path = (row or {}).get("path")
        if liked is None:
            liked = bool(path and self.index.is_liked(path))
        button.configure(text="♥" if liked else "♡",
                         text_color=(self.theme["accent"] if liked
                                     else self.theme["text_secondary"]))

    def like_now_playing(self):
        row = getattr(self, "_now_playing_row", None)
        path = (row or {}).get("path")
        if not path:
            return
        liked = self.toggle_like(path)
        view = getattr(self, "library", None)
        if view is not None:
            view.set_heart(path, liked)

    def render_library(self):
        self.library.render()
        self.current_rows = self.library.rows
        self.current_library_files = self.library.paths
        self._sync_dedupe_button()
        self._sync_playlist_button()
        self._sync_sort_control()
        self._sync_brand()

    def set_library_view(self, name):
        self.library_view = name
        self.library.playlist_id = None
        self.library.smart = None
        self.settings.set("library_view", name)
        # The segmented button updates itself when the user clicks it, but not
        # when anything else switches view -- so the palette, a restored
        # setting or a new playlist all left the tab strip highlighting a view
        # that was no longer on screen.
        if getattr(self, "view_tabs", None) is not None:
            self.view_tabs.set(name)
        self.library.set_view(name)
        self.render_library()

    def set_library_sort(self, name):
        self.library_sort = name
        self.settings.set("library_sort", name)
        self.library.set_sort(name)
        self.render_library()

    def clear_library_filter(self):
        if self.library.view == "Playlists" and (self.library.playlist_id
                                                 or self.library.smart):
            self.library.close_playlist()
            self.render_library()
            return
        # Back steps up one level rather than always jumping to the top.
        self.library.go_back()
        self.render_library()

    def open_album(self, album, artist):
        self.library.open_album(album, artist)
        self.render_library()

    def open_artist(self, artist):
        self.library.open_artist(artist)
        self.render_library()


    # -- thin delegates onto LibraryView ---------------------------------







    # ------------------------------------------------------- library views















    def _on_library_search(self, _event=None):
        """Debounce the library filter.

        This rebuilt every row on each keystroke, which for a large library
        meant destroying and recreating hundreds of widgets while typing.
        """
        if getattr(self, "_lib_search_timer", None):
            try:
                self.after_cancel(self._lib_search_timer)
            except Exception:
                pass
        self._lib_search_timer = self._safe_after(250, self.render_library)

    def play_from_library(self, file_path):
        self.current_playlist = list(self.current_library_files)
        if self.shuffle:
            random.shuffle(self.current_playlist)
            if file_path in self.current_playlist:
                self.current_playlist.remove(file_path)
                self.current_playlist.insert(0, file_path)

        try:
            self.current_index = self.current_playlist.index(file_path)
        except ValueError:
            self.current_playlist = [file_path]
            self.current_index = 0

        # Whatever list you played from becomes what plays after the queue.
        self.queue.set_context(self.current_playlist, start=file_path)
        self.play_file(file_path)

    def play_file(self, file_path):
        """Show a track and start decoding it."""
        self._begin_track(file_path)
        threading.Thread(target=self._load_and_play, args=(file_path,),
                         daemon=True).start()

    def _begin_track(self, file_path):
        """Everything about starting a track except the audio.

        Split out because gapless advance needs all of this and none of the
        loading: the engine has already switched buffers by the time it says
        so.
        """
        full_name = os.path.splitext(os.path.basename(file_path))[0]
        try:
            matches = [t for t in self.index.tracks() if t["path"] == file_path]
            row = matches[0] if matches else None
        except Exception:
            row = None
        self.time_total.configure(text="0:00")
        self.time_elapsed.configure(text="0:00")
        # A local file is available end to end the moment it opens, so the
        # whole rail is "buffered". The streaming preview path is what makes
        # the distinction worth drawing.
        self.progress_slider.set(0.0)
        self.progress_slider.set_buffered(1.0)

        recent_file = os.path.join(LIBRARY_DIR, "recent.json")
        recent_list = []
        if os.path.exists(recent_file):
            try:
                with open(recent_file, 'r') as rf:
                    recent_list = json.load(rf)
            except: pass

        if file_path in recent_list:
            recent_list.remove(file_path)
        recent_list.insert(0, file_path)
        recent_list = recent_list[:50]

        try:
            with open(recent_file, 'w') as rf:
                json.dump(recent_list, rf)
        except: pass

        try:
            self.index.record_play(file_path)
        except Exception:
            pass
        self.queue.note_playing(file_path)
        self._render_queue()
        view = getattr(self, "library", None)
        if view is not None:
            view.mark_playing(file_path)
        self._present_track(file_path, row)
        self._push_discord(playing=True)
        self._ensure_cover_url(file_path, row)
        self.play_btn.set_glyph("pause")
        # Search lyrics with the full name -- `filename` is truncated to 50
        # characters with an ellipsis for display, which never matched.
        self.fetch_lyrics(full_name)

    def _present_track(self, path, row):
        """Put one track's metadata on every surface that shows it.

        The bottom bar, the full-screen view and the heart were each filled in
        separately, in two different places -- so resuming on launch produced
        a bar showing the raw filename, no artist line, no artwork and a heart
        left over from the previous session. The heart was also being synced
        before _now_playing_row was updated, so it answered for the track
        before this one.
        """
        full_name = os.path.splitext(os.path.basename(path))[0]
        display = (row or {}).get("title") or full_name
        artist = " \u00b7 ".join(x for x in ((row or {}).get("artist"),
                                             (row or {}).get("album")) if x)

        self.now_playing_label.configure(
            text=display if len(display) <= 46 else display[:43] + "\u2026")
        if hasattr(self, "now_playing_sub"):
            self.now_playing_sub.configure(text=artist)

        self._now_playing_row = row or {"path": path}
        self._sync_now_playing_heart()

        bits = []
        if (row or {}).get("year"):
            bits.append(str(row["year"]))
        if (row or {}).get("duration"):
            bits.append(fmt_time(row["duration"]))
        self._np_set_track(display, artist, "  \u00b7  ".join(bits))

        self.extract_album_art(path)

    def _config_dir(self):
        return get_config_dir()

    def _refresh_user_client(self):
        """Pick up a cached sign-in without opening a browser."""
        try:
            self.user_sp = spotify_auth.get_client(
                os.getenv("SPOTIPY_CLIENT_ID", ""),
                os.getenv("SPOTIPY_CLIENT_SECRET", ""),
                self._config_dir(),
            )
        except Exception:
            self.user_sp = None
        return self.user_sp is not None

    def spotify_sign_in(self):
        """Open the browser so Spotify can authorise reading playlists."""
        if not self.sp:
            self.log("Set up your Spotify credentials first.")
            return
        self.signin_btn.configure(state="disabled", text="Check your browser...")
        self.log("Opening your browser to sign in to Spotify...")
        self.log(f"If nothing opens, the redirect URI "
                 f"{spotify_auth.redirect_uri()} must be listed in your Spotify "
                 f"app's settings.")

        def work():
            ok, message = spotify_auth.sign_in(
                os.getenv("SPOTIPY_CLIENT_ID", ""),
                os.getenv("SPOTIPY_CLIENT_SECRET", ""),
                self._config_dir(),
            )
            self._safe_after(0, self._finish_sign_in, ok, message)

        threading.Thread(target=work, daemon=True).start()

    def _finish_sign_in(self, ok, message):
        self._refresh_user_client()
        self.log(message)
        self._sync_signin_button()
        if ok:
            self.log("Playlists will work now -- paste the link again.")

    def spotify_sign_out(self):
        spotify_auth.sign_out(self._config_dir())
        self.user_sp = None
        self.log("Signed out of Spotify.")
        self._sync_signin_button()

    def _sync_signin_button(self):
        button = getattr(self, "signin_btn", None)
        if button is None:
            return
        if self.user_sp is not None:
            button.configure(state="normal", text="Signed in \u2713",
                             command=self.spotify_sign_out)
        else:
            button.configure(state="normal", text="Sign in to Spotify",
                             command=self.spotify_sign_in)

    def open_setup(self, first_run=False):
        """Ask for Spotify credentials in the app.

        Sharing the build should not mean sharing keys: a client secret
        compiled into an executable can be read straight back out of it, so
        each person supplies their own free credentials here, once.
        """
        if getattr(self, "setup_overlay", None) is not None:
            self.setup_overlay.destroy()

        t = self.theme
        self.setup_overlay = ctk.CTkFrame(self.main_area, corner_radius=0,
                                          fg_color=t["bg"])
        self.setup_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.setup_overlay.lift()

        card = ctk.CTkFrame(self.setup_overlay, corner_radius=18,
                            fg_color=t["surface"])
        card.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(card, text="Connect Spotify  (optional)",
                     font=theme_ui.font("display", size=24),
                     text_color=t["text"]).pack(padx=44, pady=(32, 6))
        # This used to say the app "needs" a Spotify developer app. It did,
        # once: the downloader was disabled without one. It now searches and
        # downloads through a source that needs no account, so saying so would
        # be untrue -- and putting a wall of setup steps in front of somebody
        # who does not need them is worse than untrue.
        ctk.CTkLabel(
            card, justify="left", wraplength=520, text_color=t["text_secondary"],
            font=theme_ui.font("body"),
            text=("Searching and downloading already work. Track details come "
                  "from " + getattr(getattr(self, "catalogue", None), "name",
                                    "Apple") + ", which needs no account.\n\n"
                  "Connecting Spotify adds your own library on top: your "
                  "playlists and your liked songs. It is a free developer app "
                  "of your own, takes about a minute, and needs no card.\n\n"
                  "1.  Open the dashboard below and sign in.\n"
                  "2.  Create app  ->  give it any name, tick the terms.\n"
                  "3.  In the app's Settings, add this Redirect URI:\n"
                  "        " + spotify_auth.redirect_uri() + "\n"
                  "     (needed to download your playlists)\n"
                  "4.  Copy the Client ID and Client Secret into the boxes.")
        ).pack(padx=44, pady=(0, 14), anchor="w")

        ctk.CTkButton(card, text="Open the Spotify dashboard", height=36,
                      corner_radius=18, fg_color="transparent", border_width=2,
                      border_color=t["accent"], text_color=t["text"],
                      hover_color=t["surface_hover"],
                      command=lambda: webbrowser.open(
                          "https://developer.spotify.com/dashboard")
                      ).pack(padx=44, pady=(0, 18), fill="x")

        self.setup_id_entry = ctk.CTkEntry(card, placeholder_text="Client ID",
                                           height=42, corner_radius=21, width=520)
        self.setup_id_entry.pack(padx=44, pady=(0, 10))
        self.setup_secret_entry = ctk.CTkEntry(card, placeholder_text="Client Secret",
                                               height=42, corner_radius=21,
                                               width=520, show="\u2022")
        self.setup_secret_entry.pack(padx=44, pady=(0, 10))

        self.setup_redirect_entry = ctk.CTkEntry(
            card, placeholder_text="Redirect URI (must match your Spotify app)",
            height=42, corner_radius=21, width=520)
        self.setup_redirect_entry.pack(padx=44, pady=(0, 10))

        self.setup_status = ctk.CTkLabel(card, text="", wraplength=520,
                                         justify="left",
                                         font=ctk.CTkFont(size=12),
                                         text_color=t["text_secondary"])
        self.setup_status.pack(padx=44, pady=(0, 8), anchor="w")

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(padx=44, pady=(0, 32), fill="x")
        self.setup_save_btn = ctk.CTkButton(
            buttons, text="Verify and save", height=42, corner_radius=21,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=t["accent"], hover_color=t["accent_hover"],
            text_color=t["bg"], command=self._save_setup)
        self.setup_save_btn.pack(side="left")
        ctk.CTkButton(buttons, text="Skip for now", height=42, width=130,
                      corner_radius=21, fg_color="transparent", border_width=2,
                      border_color=t["surface_hover"], text_color=t["text"],
                      hover_color=t["surface_hover"],
                      command=self.close_setup).pack(side="right")

        existing_id = os.getenv("SPOTIPY_CLIENT_ID", "")
        if existing_id:
            self.setup_id_entry.insert(0, existing_id)
        self.setup_redirect_entry.insert(0, spotify_auth.redirect_uri())
        self.setup_id_entry.focus_set()

    def close_setup(self):
        if getattr(self, "setup_overlay", None) is not None:
            self.setup_overlay.destroy()
            self.setup_overlay = None

    def _save_setup(self):
        client_id = self.setup_id_entry.get().strip()
        secret = self.setup_secret_entry.get().strip()
        redirect = self.setup_redirect_entry.get().strip()
        self.setup_save_btn.configure(state="disabled", text="Checking...")
        self.setup_status.configure(text="Asking Spotify to confirm those keys...",
                                    text_color=self.theme["text_secondary"])

        def work():
            ok, message = credentials.verify(client_id, secret)
            self._safe_after(0, self._finish_setup, ok, message, client_id, secret,
                             redirect)

        threading.Thread(target=work, daemon=True).start()

    def _finish_setup(self, ok, message, client_id, secret, redirect=None):
        self.setup_save_btn.configure(state="normal", text="Verify and save")
        if not ok:
            self.setup_status.configure(text=message, text_color=self.theme["text"])
            return

        where = credentials.save(client_id, secret, redirect_uri=redirect)
        self._refresh_user_client()
        try:
            self.sp = setup_spotify()
            self.spotify_error = None
            self.downloads._sp = self.sp
        except Exception as e:
            self.setup_status.configure(text=f"Saved, but could not start: {e}",
                                        text_color=self.theme["text"])
            return

        self.setup_status.configure(text=f"Saved to {where}",
                                    text_color=self.theme["text_secondary"])
        if self._dl_alive():
            self.url_entry.configure(state="normal")
            self.download_button.configure(state="normal")
            if getattr(self, "setup_prompt_btn", None) is not None:
                self.setup_prompt_btn.destroy()
                self.setup_prompt_btn = None
            self.log("Spotify connected. You can download now.")
        self._safe_after(900, self.close_setup)

    def _push_discord(self, playing=None):
        """Send the current track to Discord. Silent when not configured."""
        if not getattr(self, "discord", None) or not self.discord.available:
            return
        row = getattr(self, "_now_playing_row", None)
        if not row:
            return
        if playing is None:
            playing = self.player.playing and not self.player.paused
        stem = os.path.splitext(os.path.basename(row.get("path") or ""))[0]
        self.discord.update(
            title=row.get("title") or stem or "Preview",
            artist=row.get("artist") or "",
            album=row.get("album") or "",
            cover_url=row.get("cover_url"),
            position=self.player.get_position(),
            duration=self.player.get_duration(),
            playing=playing,
        )

    def _ensure_cover_url(self, file_path, row):
        if not file_path:
            return
        """Resolve a remote cover once per track, for Discord's large image.

        The embedded APIC art cannot be handed to Discord -- it needs a URL --
        so the Spotify cover is looked up once and cached in the index.
        """
        if not self.sp or not getattr(self, "discord", None):
            return
        if not self.discord.available or (row or {}).get("cover_url"):
            return

        title = (row or {}).get("title")
        artist = (row or {}).get("artist")
        if not title:
            return

        def work():
            try:
                query = f"track:{title} artist:{artist}" if artist else title
                res = self.sp.search(q=query, limit=1, type="track")
                items = res["tracks"]["items"]
                if not items:
                    return
                images = items[0]["album"].get("images") or []
                if not images:
                    return
                url = images[0]["url"]
                self.index.set_cover_url(file_path, url)
                if (getattr(self, "_now_playing_row", {}) or {}).get("path") == file_path:
                    self._now_playing_row["cover_url"] = url
                    self._safe_after(0, self._push_discord)
            except Exception as e:
                print(f"Cover lookup failed: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _load_and_play(self, file_path):
        if self.player.load_track(file_path):
            self.player.play()
        elif self.player.last_error:
            self._safe_after(0, self.now_playing_label.configure,
                       {"text": "Could not play this file"})
            print(self.player.last_error)

    def _on_scrub(self, active):
        """The seek bar reports when a drag starts and ends.

        The 100 ms refresh has to stop pushing the playhead into the bar for
        the duration, or the knob snaps back out from under the cursor.
        """
        self._seeking = active

    def _commit_seek(self, value):
        # Only on release. Committing on every drag pixel reset the EQ filter
        # state dozens of times a second.
        self.player.set_progress(value)

    def _scrub_label(self, value):
        """The time under the cursor, for the seek bar's readout bubble."""
        duration = self.player.get_duration() or 0
        if duration <= 0:
            return ""
        return fmt_time(value * duration)

    def on_volume(self, value):
        self.player.set_volume(value)
        self.settings.set("volume", float(value))
        self.volume_icon.set_glyph(
            "mute" if value < 0.01
            else ("volume_low" if value < 0.5 else "volume_high"))

    def _lyric_font(self, size):
        """Cache fonts: building a CTkFont per tick leaked Tk font objects."""
        if size not in self._lyric_fonts:
            self._lyric_fonts[size] = ctk.CTkFont(size=size, weight="bold")
        return self._lyric_fonts[size]

    def _scroll_lyric_into_view(self, index):
        """Centre the active line using its real position.

        The old ratio (index - 3) / line-count assumed every line was the
        same height, but wraplength makes long lines taller, so the active
        line drifted further off-centre the wordier the song was.
        """
        try:
            label = self.lyrics_labels[index]
            canvas = self.lyrics_scroll._parent_canvas
            content = self.lyrics_scroll.winfo_height()
            viewport = canvas.winfo_height()
            if content <= viewport or content <= 1:
                return
            centre = label.winfo_y() + label.winfo_height() / 2
            target = (centre - viewport / 2) / (content - viewport)
            canvas.yview_moveto(min(1.0, max(0.0, target)))
        except Exception:
            pass

    def update_progress_loop(self):
        if self.player.playing and not self.player.paused:
            if not self._seeking:
                self.progress_slider.set(self.player.get_progress())

            current_time = self.player.get_position()
            self.time_elapsed.configure(text=fmt_time(current_time))
            self._maybe_preload_next()

            # Re-push every ~15s so Discord's progress bar tracks seeks.
            self._discord_tick += 1
            if self._discord_tick % 150 == 0:
                self._push_discord()
            self.time_total.configure(text=fmt_time(self.player.get_duration()))

            if self.parsed_lyrics:
                new_idx = -1
                for i in range(len(self.parsed_lyrics) - 1, -1, -1):
                    if current_time >= self.parsed_lyrics[i][0]:
                        new_idx = i
                        break

                if new_idx != self.current_lyric_index and new_idx != -1:
                    previous = self.current_lyric_index
                    self.current_lyric_index = new_idx
                    if previous > new_idx:
                        # Seeking backwards: everything after the new line is
                        # upcoming again, not already sung.
                        for i in range(new_idx + 1, min(previous + 1,
                                                        len(self.lyrics_labels))):
                            self._lyric_style(i, "next")
                    else:
                        for i in range(max(0, previous), new_idx):
                            self._lyric_style(i, "past")
                    self._lyric_style(new_idx, "active")
                    self._scroll_lyric_into_view(new_idx)

        self._safe_after(100, self.update_progress_loop)  # faster updates for smooth lyrics

    # ================= DOWNLOADER TAB =================

    def log(self, message):
        if not self._dl_alive():
            print(message)
            return
        # The log only takes up room once it has something to say; an empty
        # black panel across the bottom of the screen said nothing.
        if not getattr(self, "_log_shown", False):
            self.log_box.grid(row=4, column=0, padx=36, pady=(6, 22),
                              sticky="nsew")
            self._log_shown = True
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_discover_key(self, event=None):
        """Debounce typing, then search Spotify."""
        if self._discover_timer:
            try:
                self.after_cancel(self._discover_timer)
            except Exception:
                pass
        self._discover_timer = self._safe_after(350, self.run_discover_search)

    def run_discover_search(self):
        query = self.url_entry.get().strip()
        if not query or "http" in query:
            return
        self._render_discover_message("Searching...")

        def work():
            try:
                results = self.discover.search(query)
            except Exception as e:
                self._safe_after(0, self._render_discover_message,
                                 f"Search failed: {e}")
                return
            self._safe_after(0, self._render_discover, results)
            # Warm the top few so pressing play does not wait on YouTube.
            for track in results[:4]:
                self.discover.prefetch(track)

        threading.Thread(target=work, daemon=True).start()

    def _render_discover_message(self, text):
        if not self._dl_alive():
            return
        for widget in self.results_frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(self.results_frame, text=text, justify="center",
                     wraplength=460, font=theme_ui.font("body"),
                     text_color=self.theme["text_secondary"]).pack(
                         pady=64, padx=30)

    def _render_discover(self, results):
        if not self._dl_alive():
            return
        self.discover_results = results
        for widget in self.results_frame.winfo_children():
            widget.destroy()
        if not results:
            self._render_discover_message("Nothing found.")
            return
        for track in results:
            self._discover_row(track)

    def _discover_row(self, track):
        row = ctk.CTkFrame(self.results_frame, fg_color="transparent",
                           corner_radius=theme_ui.RADIUS, height=58)
        row.pack(fill="x", padx=4, pady=2)
        row.pack_propagate(False)

        art = ctk.CTkLabel(row, text="", width=42, height=42, corner_radius=5)
        art.pack(side="left", padx=(8, 12))
        self.discover.fetch_cover(
            track["cover_url"], 42,
            lambda img, lbl=art: self._safe_after(0, self._set_discover_art, lbl, img))

        download = ctk.CTkButton(row, text="Download", width=94, height=30,
                                 corner_radius=15, font=theme_ui.font("small"),
                                 command=lambda t=track: self.download_discovered(t))
        download.pack(side="right", padx=(6, 10))

        preview = ctk.CTkButton(row, text="\u25b6  Preview", width=100, height=30,
                                corner_radius=15, font=theme_ui.font("small"),
                                fg_color="transparent", border_width=1,
                                command=lambda t=track, b=None: self.preview_track(t))
        preview.pack(side="right", padx=4)
        track["_preview_btn"] = preview

        ctk.CTkLabel(row, text=fmt_time(track["duration"]), width=48, anchor="e",
                     font=theme_ui.font("time"),
                     text_color=self.theme["text_secondary"]).pack(side="right")

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=track["title"], anchor="w", justify="left",
                     font=theme_ui.font("body_med"),
                     text_color=self.theme["text"]).pack(anchor="w", pady=(10, 0))
        detail = "  ·  ".join(x for x in (track["artist"], track["album"],
                                          track["year"]) if x)
        ctk.CTkLabel(box, text=detail, anchor="w", justify="left",
                     font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")

    def _set_discover_art(self, label, image):
        try:
            if not label.winfo_exists():
                return
            size = image.size[0]
            label._art = ctk.CTkImage(light_image=image, dark_image=image,
                                      size=(size, size))
            label.configure(image=label._art)
        except Exception:
            pass

    # ------------------------------------------------------------- preview

    def preview_track(self, track):
        """Stream a search result without saving it to disk."""
        button = track.get("_preview_btn")
        if button is not None:
            try:
                button.configure(text="Loading...", state="disabled")
            except Exception:
                pass
        self._streaming_track = track
        self.now_playing_label.configure(text=track["title"])
        self.now_playing_sub.configure(
            text=f"{track['artist']}  ·  preview")

        def work():
            try:
                resolved = self.discover.stream_url(track)
            except Exception as e:
                self._safe_after(0, self._preview_failed, track, str(e))
                return
            self._safe_after(0, self._start_preview, track, resolved)

        threading.Thread(target=work, daemon=True).start()

    def _start_preview(self, track, resolved):
        ok = self.player.load_stream(resolved["url"],
                                     duration=resolved.get("duration") or track["duration"],
                                     title=track["title"])
        button = track.get("_preview_btn")
        if not ok:
            self._preview_failed(track, self.player.last_error or "stream failed")
            return
        self.player.play()
        self.play_btn.set_glyph("pause")
        if button is not None:
            try:
                button.configure(text="\u25b6  Preview", state="normal")
            except Exception:
                pass
        # A stream is not in the library, so nothing to highlight or like.
        self._now_playing_row = {"path": None, "title": track["title"],
                                 "artist": track["artist"],
                                 "album": track["album"],
                                 "cover_url": track.get("cover_large")}
        self._push_discord(playing=True)
        self.log(f"Previewing {track['artist']} - {track['title']} (not downloaded)")

    def _preview_failed(self, track, message):
        button = track.get("_preview_btn")
        if button is not None:
            try:
                button.configure(text="\u25b6  Preview", state="normal")
            except Exception:
                pass
        self.log(f"Could not preview {track['title']}: {message}")

    def download_discovered(self, track):
        """Keep a previewed track: download and tag it properly."""
        label = f"{track['artist']} - {track['title']}"
        if track.get("source") == "itunes":
            # There is no Spotify URL to look up, so the metadata that came
            # back with the search result goes down the pipeline instead. The
            # key is only an identity for the job list.
            key = track.get("url") or track["id"]
            self._start_batch(
                [key], labels={key: label},
                meta={key: metadata.ITunesProvider.track_info(track)})
            return
        self._start_batch([track["url"]], labels={track["url"]: label})

    def on_key_release(self, event):
        if self.search_timer:
            self.after_cancel(self.search_timer)
        self.search_timer = self._safe_after(400, self.perform_search)

    def perform_search(self):
        query = self.url_entry.get().strip()
        if not query:
            # If search is empty, fetch recommendations based on now playing
            threading.Thread(target=self.fetch_recommendations, daemon=True).start()
            return

        if "http" in query:
            self.suggestions_frame.place_forget()
            return

        threading.Thread(target=self.fetch_suggestions, args=(query,), daemon=True).start()

    def fetch_recommendations(self):
        try:
            if not self.sp or not self.current_playlist or self.current_index < 0:
                self._safe_after(0, self.suggestions_frame.place_forget)
                return

            file_path = self.current_playlist[self.current_index]
            audio = MP3(file_path, ID3=ID3)
            tags = audio.tags
            title = tags.get("TIT2").text[0] if tags and tags.get("TIT2") else None
            artist = tags.get("TPE1").text[0] if tags and tags.get("TPE1") else None

            if not title or not artist:
                self._safe_after(0, self.suggestions_frame.place_forget)
                return

            # Find the Spotify Track ID for the local file
            search_res = self.sp.search(q=f"track:{title} artist:{artist}", limit=1, type='track')
            if not search_res['tracks']['items']:
                self._safe_after(0, self.suggestions_frame.place_forget)
                return
            track_id = search_res['tracks']['items'][0]['id']

            # sp.recommendations() now returns HTTP 404: Spotify retired
            # /v1/recommendations for newly-registered apps, so this feature
            # was dead. Derive suggestions from artist top tracks instead.
            tracks = get_related_tracks(self.sp, track_id, limit=5)
            if not tracks:
                self._safe_after(0, self.suggestions_frame.place_forget)
                return

            # Named for what it can actually deliver. Spotify closed the
            # endpoints that made real recommendations possible, so calling
            # these "suggested for you" would be overselling a search.
            self._safe_after(0, self.show_suggestions, None, tracks,
                             f"More from {artist}")
        except Exception as e:
            print(f"Recommendation error: {e}")
            self._safe_after(0, self.suggestions_frame.place_forget)

    def fetch_suggestions(self, query):
        if not self.sp:
            return
        try:
            artists = search_spotify_artist(self.sp, query)
            results = self.sp.search(q=query, limit=3, type='track')
            tracks = results['tracks']['items']
            self._safe_after(0, self.show_suggestions, artists, tracks)
        except Exception:
            pass

    def show_suggestions(self, artists, tracks, tracks_title="Tracks"):
        for widget in self.suggestions_frame.winfo_children():
            widget.destroy()

        if not artists and not tracks:
            self.suggestions_frame.place_forget()
            return

        if tracks:
            ctk.CTkLabel(self.suggestions_frame, text=tracks_title, font=ctk.CTkFont(weight="bold"), text_color=self.theme["text_secondary"]).pack(anchor="w", padx=10, pady=(5,0))
            for track in tracks:
                name = track['name']
                art = ", ".join(a['name'] for a in track['artists'])
                url = track['external_urls']['spotify']
                btn_text = f"{name}  ·  {art}"
                btn = ctk.CTkButton(self.suggestions_frame, text=btn_text, anchor="w", fg_color="transparent",
                                    text_color=self.theme["text"], hover_color=self.theme["surface_hover"], corner_radius=8,
                                    command=lambda u=url, t=btn_text: self.select_suggestion(u, t))
                btn.pack(fill="x", padx=5, pady=2)

        if artists:
            ctk.CTkLabel(self.suggestions_frame, text="Artists", font=ctk.CTkFont(weight="bold"), text_color=self.theme["text_secondary"]).pack(anchor="w", padx=10, pady=(10,0))
            for artist in artists[:3]: # Only show top 3 artists to save space
                name = artist['name']
                url = artist['external_urls']['spotify']
                btn = ctk.CTkButton(self.suggestions_frame, text=name, anchor="w", fg_color="transparent",
                                    text_color=self.theme["text"], hover_color=self.theme["surface_hover"], corner_radius=8,
                                    command=lambda u=url, t=name, aid=artist['id']: self.select_artist(u, t, aid))
                btn.pack(fill="x", padx=5, pady=2)

        self.suggestions_frame.configure(width=self.url_entry.winfo_width())
        self.suggestions_frame.place(x=self.url_entry.winfo_x(), y=self.url_entry.winfo_y() + self.url_entry.winfo_height() + 5)
        self.suggestions_frame.lift()

    def select_suggestion(self, url, text):
        self.suggestions_frame.place_forget()
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)

    def select_artist(self, url, name, artist_id):
        self.suggestions_frame.place_forget()
        self.log(f"Fetching albums for {name}...")
        threading.Thread(target=self.prompt_artist_albums, args=(name, artist_id), daemon=True).start()

    def prompt_artist_albums(self, artist_name, artist_id):
        try:
            albums = get_artist_albums(self.sp, artist_id)

            # Pre-fetch thumbnails so we don't freeze the UI
            import requests
            from PIL import Image
            import io

            for album in albums:
                try:
                    if album.get('images'):
                        # Grab the smallest image to save bandwidth (usually the last one, 64x64)
                        img_url = album['images'][-1]['url']
                        r = requests.get(img_url, timeout=5)
                        if r.status_code == 200:
                            img = Image.open(io.BytesIO(r.content))
                            album['ctk_image'] = ctk.CTkImage(img, size=(40, 40))
                except:
                    pass

            self._safe_after(0, self.show_album_selector, artist_name, albums)
        except Exception as e:
            self._safe_after(0, self.log, f"Failed to fetch albums: {e}")

    def show_album_selector(self, artist_name, albums):
        dialog = dialogs.ModalDialog(
            self, self.theme, f"Albums by {artist_name}", size=(520, 660),
            body_pad=(18, 16))
        # Not modal: the downloads it starts report into the window behind it.
        panel = dialog.body

        ctk.CTkLabel(panel, text="Select albums or tracks to download",
                     font=theme_ui.font("title"), anchor="w",
                     text_color=self.theme["text"]).pack(fill="x", pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(panel, fg_color=self.theme["surface"],
                                        corner_radius=theme_ui.RADIUS)
        scroll.pack(fill="both", expand=True)

        vars_dict = {}

        for album in albums:
            album_url = album['external_urls']['spotify']
            var = ctk.BooleanVar(value=False)
            vars_dict[album_url] = var

            # Container for the album and its tracks
            album_container = ctk.CTkFrame(scroll, fg_color="transparent")
            album_container.pack(fill="x", pady=2)

            row = ctk.CTkFrame(album_container, fg_color="transparent")
            row.pack(fill="x", pady=5, padx=10)

            cb = ctk.CTkCheckBox(row, text="", variable=var, width=28,
                                 fg_color=self.theme["accent"], hover_color=self.theme["accent_hover"])
            cb.pack(side="left", padx=(0, 5))

            if 'ctk_image' in album:
                img_lbl = ctk.CTkLabel(row, text="", image=album['ctk_image'])
                img_lbl.pack(side="left", padx=(0, 10))

            # Without a wraplength these ran off the edge of the dialog, so a
            # title like "Where the Light Is: John Mayer Live In Los Angeles"
            # was cut off mid-word.
            text_lbl = ctk.CTkLabel(row, text=album['name'], text_color=self.theme["text"],
                                    font=ctk.CTkFont(size=14, weight="bold"),
                                    justify="left", anchor="w", wraplength=300)
            text_lbl.pack(side="left", fill="x", expand=True)

            tracks_frame = ctk.CTkFrame(album_container, fg_color="transparent")

            def toggle_expand(a_url=album_url, tf=tracks_frame):
                if tf.winfo_ismapped():
                    tf.pack_forget()
                else:
                    tf.pack(fill="x", padx=(40, 10))
                    # If empty, fetch tracks
                    if not tf.winfo_children():
                        ctk.CTkLabel(tf, text="Loading tracks...", text_color=self.theme["text_secondary"]).pack(pady=5)
                        threading.Thread(target=self._fetch_and_show_tracks, args=(a_url, tf, vars_dict), daemon=True).start()

            expand_btn = ctk.CTkButton(row, text="▼", width=30, height=30, fg_color="transparent", hover_color=self.theme["surface_hover"],
                                       text_color=self.theme["text"], command=toggle_expand)
            expand_btn.pack(side="right")

        def download_selected():
            selected_urls = [url for url, var in vars_dict.items() if var.get()]
            dialog.close()
            if selected_urls:
                threading.Thread(target=self.download_selected_items, args=(selected_urls,), daemon=True).start()

        ctk.CTkButton(panel, text="Download selected", height=44,
                      corner_radius=theme_ui.RADIUS_PILL,
                      font=theme_ui.font("body_med"),
                      fg_color=self.theme["accent"],
                      text_color=self.theme["bg"],
                      hover_color=self.theme["accent_hover"],
                      command=download_selected).pack(fill="x", pady=(14, 0))
        dialog.present()

    def _fetch_and_show_tracks(self, album_url, frame, vars_dict):
        try:
            tracks_info = get_spotify_album_tracks_info(self.sp, album_url)
            self._safe_after(0, self._render_album_tracks, frame, tracks_info, vars_dict)
        except Exception as e:
            self._safe_after(0, lambda: ctk.CTkLabel(frame, text="Failed to load tracks", text_color="red").pack())

    def _render_album_tracks(self, frame, tracks_info, vars_dict):
        for widget in frame.winfo_children():
            widget.destroy()

        for i, track in enumerate(tracks_info):
            track_url = track['url']
            var = ctk.BooleanVar(value=False)
            vars_dict[track_url] = var
            cb = ctk.CTkCheckBox(frame, text=f"{i+1}. {track['name']}", variable=var,
                                 fg_color=self.theme["accent"], hover_color=self.theme["accent_hover"], text_color=self.theme["text_secondary"],
                                 font=ctk.CTkFont(size=12))
            cb.pack(anchor="w", pady=2)

    def _gui_log(self, message):
        self._safe_after(0, self.log, message)

    def download_selected_items(self, urls):
        """Expand albums to tracks, dedupe, then hand the batch to the manager."""
        os.makedirs(LIBRARY_DIR, exist_ok=True)

        track_urls, seen = [], set()
        for url in urls:
            try:
                expanded = (get_spotify_album_tracks(self.sp, url)
                            if 'album' in url else [url] if 'track' in url else [])
            except Exception as e:
                self._gui_log(f"Error reading {url}: {e}")
                continue
            for t in expanded:
                if t not in seen:
                    seen.add(t)
                    track_urls.append(t)

        self._start_batch(track_urls)

    def _start_batch(self, track_urls, labels=None, meta=None):
        if not track_urls:
            self._gui_log("Nothing to download.")
            return
        started = self.downloads.start(
            track_urls, LIBRARY_DIR,
            jobs=int(self.settings.get("download_jobs") or 3),
            quality=self.settings.get("download_quality"),
            labels=labels, meta=meta,
        )
        if not started:
            self._gui_log("A download is already running.")
            return
        self._safe_after(0, self._rebuild_job_rows)
        self._safe_after(0, self._sync_download_buttons)
        self._watch_downloads()

    def _watch_downloads(self):
        """Refresh the library once the batch finishes."""
        if self.downloads.running:
            self._safe_after(500, self._watch_downloads)
            return
        self._sync_download_buttons()
        self._reconcile_imports()
        self.load_library()


    def _spotify_required(self, what):
        """Tell the user what is missing instead of doing nothing."""
        self._gui_log("%s needs a Spotify connection. %s"
                      % (what, self.spotify_error or
                         "Use Set up Spotify access."))

    def start_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self.log("Please enter a URL.")
            return
        if not self.sp:
            self.log(self.spotify_error or "Spotify is not configured.")
            return
        if self.downloads.running:
            self.log("A download is already running.")
            return

        os.makedirs(LIBRARY_DIR, exist_ok=True)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        threading.Thread(target=self.download_thread,
                         args=(url, LIBRARY_DIR), daemon=True).start()

    def download_thread(self, url, out_dir):
        try:
            if is_liked_songs(url):
                # "Liked Songs" has no playlist id -- its URL is
                # open.spotify.com/collection/tracks, so the plain
                # "playlist" check below never matched it.
                self._gui_log("Fetching your Liked Songs...")
                track_urls = get_spotify_playlist_tracks(self.sp, url,
                                                         user_sp=self.user_sp)
            elif "track" in url:
                track_urls = [url]
            elif "playlist" in url:
                self._gui_log("Fetching playlist tracks...")
                track_urls = get_spotify_playlist_tracks(self.sp, url,
                                                         user_sp=self.user_sp)
            elif "album" in url:
                self._gui_log("Fetching album tracks...")
                track_urls = get_spotify_album_tracks(self.sp, url)
            else:
                self._gui_log(f"Searching Spotify for '{url}'...")
                found = search_spotify_track(self.sp, url)
                if not found:
                    self._gui_log(f"Could not find any track matching '{url}'.")
                    return
                track_urls = [found]
        except SpotifyAuthError as e:
            # e.g. a private or Spotify-curated playlist, which app-only
            # credentials cannot read -- previously surfaced as a raw 401.
            self._gui_log(str(e))
            return
        except Exception as e:
            self._gui_log(f"An error occurred: {e}")
            return

        self._safe_after(0, self._start_batch, track_urls)

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.close_btn.configure(command=app.on_close)
    app.mainloop()
