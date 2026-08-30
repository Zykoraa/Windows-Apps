import colorgram
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
from library_index import LibraryIndex, SORTS
from library_view import LibraryView
from settings import Settings
from download_manager import (
    DownloadManager, QUEUED, RUNNING, DONE, SKIPPED, FAILED, CANCELLED,
)
from media_keys import MediaKeys
import visualizers
import app_icon
from discord_presence import DiscordPresence
import credentials
from downloader import (
    setup_spotify, search_spotify_track, search_spotify_artist,
    get_artist_albums, get_spotify_album_tracks, get_spotify_playlist_tracks,
    get_spotify_album_tracks_info, process_track, download_many,
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

THEMES = {
    "Spotify Classic": {"bg": "#121212", "surface": "#181818", "surface_hover": "#282828", "accent": "#1DB954", "accent_hover": "#1ED760", "text": "#FFFFFF", "text_secondary": "#B3B3B3"},
    "Catppuccin Mocha": {"bg": "#1e1e2e", "surface": "#181825", "surface_hover": "#313244", "accent": "#cba6f7", "accent_hover": "#b4befe", "text": "#cdd6f4", "text_secondary": "#bac2de"},
    "Osaka Forest": {"bg": "#2b3339", "surface": "#323d43", "surface_hover": "#3a454a", "accent": "#a7c080", "accent_hover": "#b8d090", "text": "#d3c6aa", "text_secondary": "#9da9a0"},
    "Dracula": {"bg": "#282a36", "surface": "#44475a", "surface_hover": "#6272a4", "accent": "#bd93f9", "accent_hover": "#ff79c6", "text": "#f8f8f2", "text_secondary": "#8be9fd"},
    "Nord": {"bg": "#2e3440", "surface": "#3b4252", "surface_hover": "#434c5e", "accent": "#88c0d0", "accent_hover": "#8fbcbb", "text": "#eceff4", "text_secondary": "#d8dee9"},
    "Tokyo Night": {"bg": "#1a1b26", "surface": "#24283b", "surface_hover": "#2f3549", "accent": "#7aa2f7", "accent_hover": "#9ece6a", "text": "#c0caf5", "text_secondary": "#787c99"},
    "Gruvbox": {"bg": "#282828", "surface": "#3c3836", "surface_hover": "#504945", "accent": "#fabd2f", "accent_hover": "#fe8019", "text": "#ebdbb2", "text_secondary": "#a89984"},
    "Rose Pine": {"bg": "#191724", "surface": "#1f1d2e", "surface_hover": "#26233a", "accent": "#ebbcba", "accent_hover": "#f6c177", "text": "#e0def4", "text_secondary": "#908caa"},
    "Everforest": {"bg": "#272e33", "surface": "#2e383c", "surface_hover": "#374145", "accent": "#a7c080", "accent_hover": "#dbbc7f", "text": "#d3c6aa", "text_secondary": "#859289"},
    "Solarized": {"bg": "#002b36", "surface": "#073642", "surface_hover": "#0d4a5a", "accent": "#2aa198", "accent_hover": "#b58900", "text": "#eee8d5", "text_secondary": "#93a1a1"},
    "Synthwave": {"bg": "#1a1327", "surface": "#241b35", "surface_hover": "#32264a", "accent": "#ff2e97", "accent_hover": "#00e5ff", "text": "#f7f2ff", "text_secondary": "#a08cc4"},
    "Monokai": {"bg": "#221f22", "surface": "#2d2a2e", "surface_hover": "#403e41", "accent": "#a9dc76", "accent_hover": "#ffd866", "text": "#fcfcfa", "text_secondary": "#939293"},
    "Kanagawa": {"bg": "#1f1f28", "surface": "#2a2a37", "surface_hover": "#363646", "accent": "#7e9cd8", "accent_hover": "#e6c384", "text": "#dcd7ba", "text_secondary": "#727169"},
    "Ayu Mirage": {"bg": "#1f2430", "surface": "#232834", "surface_hover": "#2d3441", "accent": "#ffcc66", "accent_hover": "#73d0ff", "text": "#cbccc6", "text_secondary": "#707a8c"},
    "Deep Ocean": {"bg": "#0b1622", "surface": "#12212e", "surface_hover": "#1b2f40", "accent": "#4dd0e1", "accent_hover": "#80deea", "text": "#e3f2fd", "text_secondary": "#7292a8"},
    "Ember": {"bg": "#1c1614", "surface": "#26201d", "surface_hover": "#342b26", "accent": "#ff7043", "accent_hover": "#ffab40", "text": "#f5ece7", "text_secondary": "#a38d81"},
    "Rose Pine Dawn": {"bg": "#faf4ed", "surface": "#fffaf3", "surface_hover": "#f2e9e1", "accent": "#d7827e", "accent_hover": "#ea9d34", "text": "#575279", "text_secondary": "#797593"},
    "Nordic Light": {"bg": "#eceff4", "surface": "#e5e9f0", "surface_hover": "#d8dee9", "accent": "#5e81ac", "accent_hover": "#81a1c1", "text": "#2e3440", "text_secondary": "#4c566a"}
}

ctk.set_appearance_mode("Dark")
ctk.ThemeManager.theme["CTkFont"]["family"] = "JetBrainsMono NF"

# Monkeypatch CTkFont to ensure all explicit calls use the nerd font
_original_ctk_font = ctk.CTkFont
class JetBrainsFont(_original_ctk_font):
    def __init__(self, *args, **kwargs):
        kwargs["family"] = "JetBrainsMono NF"
        super().__init__(*args, **kwargs)
ctk.CTkFont = JetBrainsFont

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Must exist before anything schedules work via _safe_after().
        self._closing = False

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

        self.dl_visible = False
        self.dl_overlay = None
        self.visualizer_palette = self.settings.get("visualizer_palette") or "Accent"
        if self.visualizer_palette not in visualizers.palette_names():
            self.visualizer_palette = "Accent"
        self.visualizer_visible = False   # toggled on after build_ui

        # Deliberately not wiring visualizer_callback: the engine exposes
        # smoothed_bands and the UI samples it on its own clock.
        self.player.on_track_end_callback = self.on_track_end

        self.library_view = self.settings.get("library_view") or "Songs"
        self.library_sort = self.settings.get("library_sort") or "Title"
        self.library_filter = None          # ("album"|"artist", value)
        self.current_library_files = []
        self.current_rows = []
        self.current_playlist = []
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
        self.update_progress_loop()
        self.update_visualizer_loop()
        self.setup_overlay = None
        if self.spotify_error:
            # Nothing configured yet: walk the user through it rather than
            # leaving a dead Download button and an error in a log file.
            self._safe_after(450, lambda: self.open_setup(first_run=True))

        self._safe_after(300, self._resume_last_track)
        self._safe_after(120000, self._autosave_loop)


    def _safe_after(self, delay, callback=None, *args):
        """Schedule work on the Tk thread from a worker thread.

        Download, lyric and album-art threads outlive the window during
        shutdown; calling after() on a destroyed widget raises
        "main thread is not in main loop" inside those threads.
        """
        if getattr(self, "_closing", False):
            return None
        try:
            return self.after(delay, callback, *args)
        except Exception:
            return None

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
        self.play_btn.configure(text="\u25b6")
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
        ):
            self.bind(seq, guard(fn))

        self.bind("<slash>", lambda e: (self.lib_search_entry.focus_set(), "break")[1])
        self.bind("<Escape>", lambda e: self._escape())

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
        if getattr(self, "setup_overlay", None) is not None:
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
            self.play_btn.configure(text="▶")
            self._push_discord(playing=False)
        else:
            self.player.play()
            self.play_btn.configure(text="⏸")
            self._push_discord(playing=True)

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self.settings.set("shuffle", self.shuffle)
        color = self.theme["accent"] if self.shuffle else self.theme["text"]
        self.shuffle_btn.configure(text_color=color)

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
        color = self.theme["accent"] if self.repeat else self.theme["text"]
        self.repeat_btn.configure(text_color=color)

    def play_next(self):
        if not self.current_playlist: return
        self.current_index += 1
        if self.current_index >= len(self.current_playlist):
            if self.repeat:
                self.current_index = 0
            else:
                self.current_index -= 1
                self.player.stop()
                self.play_btn.configure(text="▶")
                return
        self.play_file(self.current_playlist[self.current_index])

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
            self.album_art_label.configure(image="")
            self.np_art_label.configure(image="")
            self.dynamic_accent = self.theme["accent"]
            self.np_overlay.configure(fg_color=self.theme["bg"])
            return

        self._thumb_img = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=(64, 64))
        self._np_img = ctk.CTkImage(light_image=np_art, dark_image=np_art, size=(400, 400))
        self.album_art_label.configure(image=self._thumb_img)
        self.np_art_label.configure(image=self._np_img)

        self.dynamic_accent = accent or self.theme["accent"]
        self.np_overlay.configure(fg_color=self._readable_bg(accent))

    @staticmethod
    def _luminance(hex_color):
        r = int(hex_color[1:3], 16) / 255
        g = int(hex_color[3:5], 16) / 255
        b = int(hex_color[5:7], 16) / 255
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def _readable_bg(self, accent):
        """Darken a pale cover colour so the light-on-dark text stays legible.

        The dominant album colour was applied to the overlay as-is, so a pale
        sleeve gave near-white text on a near-white background.
        """
        if not accent:
            return self.theme["bg"]
        try:
            lum = self._luminance(accent)
            if lum <= 0.35:
                return accent
            # Scale toward black until it clears the contrast threshold.
            factor = 0.35 / lum
            r = int(int(accent[1:3], 16) * factor)
            g = int(int(accent[3:5], 16) * factor)
            b = int(int(accent[5:7], 16) * factor)
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return self.theme["bg"]

    def build_ui(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # 1. Title Bar
        self.title_bar = ctk.CTkFrame(self, height=35, corner_radius=0, fg_color=self.theme["surface"])
        self.title_bar.grid(row=0, column=0, sticky="ew")
        self.title_bar.bind("<B1-Motion>", self.move_window)
        self.title_bar.bind("<Button-1>", self.get_pos)
        self.title_bar.bind("<Double-Button-1>", self.toggle_maximize)

        # The mark used to be a bare U+2721 glyph, which rendered differently
        # (or not at all) depending on the installed font.
        try:
            art = app_icon.icon_image(22)
            self._title_icon_img = ctk.CTkImage(light_image=art, dark_image=art,
                                                size=(22, 22))
            self.title_icon = ctk.CTkLabel(self.title_bar, text="",
                                           image=self._title_icon_img)
            self.title_icon.pack(side="left", padx=(14, 9))
            self.title_icon.bind("<B1-Motion>", self.move_window)
            self.title_icon.bind("<Button-1>", self.get_pos)
            self.title_icon.bind("<Double-Button-1>", self.toggle_maximize)
        except Exception as e:
            print(f"Could not draw title icon: {e}")

        self.title_lbl = ctk.CTkLabel(self.title_bar, text="Eve's Garden",
                                      font=ctk.CTkFont(weight="bold", size=14))
        self.title_lbl.pack(side="left", padx=(0, 15))
        self.title_lbl.bind("<B1-Motion>", self.move_window)
        self.title_lbl.bind("<Button-1>", self.get_pos)
        self.title_lbl.bind("<Double-Button-1>", self.toggle_maximize)

        self.close_btn = ctk.CTkButton(self.title_bar, text=" ✕ ", width=40, height=35, corner_radius=0, fg_color="transparent", hover_color="#e81123", command=self.destroy)
        self.close_btn.pack(side="right")
        self.max_btn = ctk.CTkButton(self.title_bar, text=" ❐ ", width=40, height=35, corner_radius=0, fg_color="transparent", command=self.toggle_maximize)
        self.max_btn.pack(side="right")
        self.min_btn = ctk.CTkButton(self.title_bar, text=" 🗕 ", width=40, height=35, corner_radius=0, fg_color="transparent", command=self.minimize_to_tray)
        self.min_btn.pack(side="right")

        # 2. Main Area (Library)
        self.main_area = ctk.CTkFrame(self, corner_radius=0, fg_color=self.theme["bg"])
        self.main_area.grid(row=1, column=0, sticky="nsew")
        self.main_area.grid_rowconfigure(1, weight=1)
        self.main_area.grid_columnconfigure(0, weight=1)
        self.main_area.grid_rowconfigure(2, weight=0)

        self.library_header = ctk.CTkFrame(self.main_area, height=60, fg_color="transparent")
        self.library_header.grid(row=0, column=0, sticky="ew", padx=20, pady=10)

        # Songs / Albums / Artists -- the library used to be a flat glob of
        # one folder, discarding the album and artist tags every download
        # writes.
        self.view_tabs = ctk.CTkSegmentedButton(
            self.library_header, values=["Songs", "Albums", "Artists"],
            command=self.set_library_view, corner_radius=15)
        self.view_tabs.set(self.library_view)
        self.view_tabs.pack(side="left", padx=(0, 12))

        self.lib_search_entry = ctk.CTkEntry(self.library_header, placeholder_text="Search title, artist or album...", border_width=0, corner_radius=15, height=35, width=280)
        self.lib_search_entry.pack(side="left", padx=10)
        self.lib_search_entry.bind("<KeyRelease>", self._on_library_search)

        self.sort_dropdown = ctk.CTkOptionMenu(
            self.library_header, values=list(SORTS.keys()),
            command=self.set_library_sort, corner_radius=15, width=150)
        self.sort_dropdown.set(self.library_sort)
        self.sort_dropdown.pack(side="left", padx=10)

        self.nav_dl_btn = ctk.CTkButton(self.library_header, text="Download More", command=self.open_downloader, corner_radius=20, font=ctk.CTkFont(weight="bold"))
        self.nav_dl_btn.pack(side="left", padx=10)

        self.theme_dropdown = ctk.CTkOptionMenu(self.library_header, values=list(THEMES.keys()), command=self.change_theme, corner_radius=15)
        self.theme_dropdown.set(self.current_theme_name)
        self.theme_dropdown.pack(side="right", padx=10)

        # Only offered when there is actually something to recover.
        self.repair_btn = ctk.CTkButton(self.library_header, text="Repair library",
                                        command=self.run_repair, corner_radius=20,
                                        font=ctk.CTkFont(weight="bold"))
        self.refresh_repair_button()

        # Shown only while drilled into one album or artist.
        self.crumb_bar = ctk.CTkFrame(self.main_area, fg_color="transparent")
        self.crumb_back = ctk.CTkButton(self.crumb_bar, text="←  All", width=80,
                                        corner_radius=15, command=self.clear_library_filter)
        self.crumb_back.pack(side="left")
        self.crumb_label = ctk.CTkLabel(self.crumb_bar, text="",
                                        font=ctk.CTkFont(size=18, weight="bold"))
        self.crumb_label.pack(side="left", padx=14)

        self.library_frame = ctk.CTkScrollableFrame(self.main_area, fg_color="transparent")
        self.library_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)

        self.library_status = ctk.CTkLabel(self.main_area, text="", anchor="w",
                                           font=ctk.CTkFont(size=11))
        self.library_status.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 6))

        # 3. Bottom Playback Bar
        self.bottom_bar = ctk.CTkFrame(self, height=90, corner_radius=0, fg_color=self.theme["surface"])
        self.bottom_bar.grid(row=2, column=0, sticky="ew")
        self.bottom_bar.grid_columnconfigure(0, weight=1)
        self.bottom_bar.grid_columnconfigure(1, weight=1)
        self.bottom_bar.grid_columnconfigure(2, weight=1)

        self.now_playing_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.now_playing_frame.grid(row=0, column=0, rowspan=2, sticky="w", padx=20)

        self.album_art_label = ctk.CTkLabel(self.now_playing_frame, text="", width=64, height=64)
        self.album_art_label.pack(side="left", padx=(0, 10))
        self.album_art_label.bind("<Button-1>", self.toggle_now_playing_overlay)
        self.album_art_label.configure(cursor="hand2")

        self.now_playing_label = ctk.CTkLabel(self.now_playing_frame, text="No track selected", font=ctk.CTkFont(size=14, weight="bold"))
        self.now_playing_label.pack(side="left")

        self.controls_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.controls_frame.grid(row=0, column=1, pady=5)

        self.shuffle_btn = ctk.CTkButton(self.controls_frame, text="🔀", width=40, height=40, corner_radius=20, command=self.toggle_shuffle, fg_color="transparent", text_color=self.theme["text"])
        self.shuffle_btn.pack(side="left", padx=5)
        self.prev_btn = ctk.CTkButton(self.controls_frame, text="⏮", width=40, height=40, corner_radius=20, command=self.play_prev, fg_color="transparent", hover_color=self.theme["surface_hover"])
        self.prev_btn.pack(side="left", padx=5)
        self.play_btn = ctk.CTkButton(self.controls_frame, text="▶", width=50, height=50, corner_radius=25, command=self.toggle_play_pause, font=ctk.CTkFont(size=18))
        self.play_btn.pack(side="left", padx=5)
        self.next_btn = ctk.CTkButton(self.controls_frame, text="⏭", width=40, height=40, corner_radius=20, command=self.play_next, fg_color="transparent", hover_color=self.theme["surface_hover"])
        self.next_btn.pack(side="left", padx=5)
        self.repeat_btn = ctk.CTkButton(self.controls_frame, text="🔁", width=40, height=40, corner_radius=20, command=self.toggle_repeat, fg_color="transparent", text_color=self.theme["text"])
        self.repeat_btn.pack(side="left", padx=5)

        self.progress_row = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.progress_row.grid(row=1, column=1, sticky="ew", pady=(0, 10))
        self.progress_row.grid_columnconfigure(1, weight=1)

        self.time_elapsed = ctk.CTkLabel(self.progress_row, text="0:00", width=42,
                                         font=ctk.CTkFont(size=11))
        self.time_elapsed.grid(row=0, column=0, padx=(0, 8))

        self.progress_slider = ctk.CTkSlider(self.progress_row, from_=0.0, to=1.0, command=self.on_seek, height=12)
        self.progress_slider.set(0.0)
        self.progress_slider.grid(row=0, column=1, sticky="ew")
        # While the user drags, the 100 ms refresh must stop yanking the knob
        # back to the playhead.
        self.progress_slider.bind("<Button-1>", self._seek_begin)
        self.progress_slider.bind("<ButtonRelease-1>", self._seek_end)

        self.time_total = ctk.CTkLabel(self.progress_row, text="0:00", width=42,
                                       font=ctk.CTkFont(size=11))
        self.time_total.grid(row=0, column=2, padx=(8, 0))

        self.eq_toggle_btn = ctk.CTkButton(self.bottom_bar, text="EQ", width=40, height=40, corner_radius=20, command=self.toggle_eq)
        self.eq_toggle_btn.grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.viz_toggle_btn = ctk.CTkButton(self.bottom_bar, text="VIZ", width=40, height=40, corner_radius=20, command=self.toggle_visualizer_visibility)
        self.viz_toggle_btn.grid(row=0, column=3, sticky="e", padx=(10, 20))

        # Volume: the engine had no gain stage at all, so the only way to turn
        # the app down was the system mixer.
        self.volume_frame = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        self.volume_frame.grid(row=1, column=2, columnspan=2, sticky="e",
                               padx=(10, 20), pady=(0, 10))
        self.volume_icon = ctk.CTkLabel(self.volume_frame, text="\U0001F50A",
                                        font=ctk.CTkFont(size=13), width=18)
        self.volume_icon.pack(side="left", padx=(0, 6))
        self.volume_slider = ctk.CTkSlider(self.volume_frame, from_=0.0, to=1.0,
                                           width=90, height=12, command=self.on_volume)
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
            self.np_overlay.place_forget()
            self.np_overlay_visible = False
        else:
            self.np_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.np_overlay_visible = True

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

        self.dl_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.dl_overlay.lift()
        self.dl_visible = True
        self._sync_download_buttons()
        self._rebuild_job_rows()
        self.url_entry.focus_set()

    def close_downloader(self):
        if getattr(self, "dl_overlay", None) is not None:
            self.dl_overlay.place_forget()
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

    def build_now_playing_overlay(self):
        self.np_overlay = ctk.CTkFrame(self.main_area, corner_radius=0,
                                       fg_color=self.theme["bg"])
        # The art column is fixed and the lyrics take whatever is left. Both
        # columns used to carry weight=1, which pinned the lyrics pane to half
        # the window no matter how wide it got.
        self.np_overlay.grid_columnconfigure(0, weight=0)
        self.np_overlay.grid_columnconfigure(1, weight=1)
        self.np_overlay.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self.np_overlay, fg_color="transparent")
        left.grid(row=0, column=0, padx=(44, 24), pady=40, sticky="n")

        self.np_art_label = ctk.CTkLabel(left, text="", width=400, height=400)
        self.np_art_label.pack()
        self.np_title_lbl = ctk.CTkLabel(left, text="", wraplength=400,
                                         justify="center",
                                         font=ctk.CTkFont(size=20, weight="bold"))
        self.np_title_lbl.pack(pady=(20, 4))
        self.np_artist_lbl = ctk.CTkLabel(left, text="", wraplength=400,
                                          justify="center",
                                          font=ctk.CTkFont(size=14))
        self.np_artist_lbl.pack()
        self.np_close_btn = ctk.CTkButton(left, text="Close", width=120, height=32,
                                          corner_radius=16,
                                          command=self.toggle_now_playing_overlay)
        self.np_close_btn.pack(pady=(24, 0))

        right = ctk.CTkFrame(self.np_overlay, fg_color=self.theme["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 40), pady=40)
        self.np_lyrics_pane = right
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.lyrics_scroll = ctk.CTkScrollableFrame(right, fg_color=self.theme["bg"])
        self.lyrics_scroll.grid(row=0, column=0, sticky="nsew")
        self.lyrics_scroll.grid_columnconfigure(0, weight=1)
        # wraplength was hardcoded at 700px against a column that measured
        # 434px, so every long line ran off the edge.
        self.lyrics_scroll.bind("<Configure>", self._on_lyrics_resize)

        self.lyrics_labels = []
        self._lyrics_wrap = 0
        self._lyric_spacers = []

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
        line below it and made the pane lurch on each change. Size is now
        fixed and emphasis comes from colour plus a highlight pill.
        """
        if not (0 <= index < len(self.lyrics_labels)):
            return
        t = self.theme
        label = self.lyrics_labels[index]
        try:
            if state == "active":
                label.configure(text_color=t["text"],
                                fg_color=self._blend(t["bg"], t["accent"], 0.32))
            elif state == "past":
                label.configure(text_color=self._blend(t["bg"], t["text_secondary"], 0.55),
                                fg_color="transparent")
            else:
                label.configure(text_color=t["text_secondary"], fg_color="transparent")
        except Exception:
            pass

    def fetch_lyrics(self, query):
        def _fetch():
            try:
                lrc = syncedlyrics.search(query)
                self._safe_after(0, self.setup_lyrics, lrc)
            except Exception as e:
                print(f"Lyrics error: {e}")
                self._safe_after(0, self.setup_lyrics, None)

        # Clear existing
        for lbl in self.lyrics_labels:
            lbl.destroy()
        self.lyrics_labels.clear()

        loading_lbl = ctk.CTkLabel(self.lyrics_scroll, text="Searching for lyrics...", font=ctk.CTkFont(size=24, weight="bold"), text_color=self.theme["text_secondary"])
        loading_lbl.grid(row=0, column=0, pady=20)
        self.lyrics_labels.append(loading_lbl)

        import threading
        threading.Thread(target=_fetch, daemon=True).start()

    def setup_lyrics(self, lrc):
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
                               font=ctk.CTkFont(size=20, weight="bold"),
                               text_color=self.theme["text_secondary"],
                               fg_color="transparent",
                               wraplength=self._lyric_wrap_width())
            lbl.grid(row=0, column=0, pady=24, sticky="ew")
            self.lyrics_labels.append(lbl)

        if not lrc:
            message("No lyrics found for this track.")
            return

        for line in lrc.split("\n"):
            if not (line.startswith("[") and "]" in line):
                continue
            time_str, text = line[1:].split("]", 1)
            text = text.strip()
            if not text:
                continue
            try:
                parts = time_str.split(":")
                if len(parts) == 2:
                    self.parsed_lyrics.append(
                        (float(parts[0]) * 60 + float(parts[1]), text))
            except ValueError:
                pass

        self.parsed_lyrics.sort(key=lambda x: x[0])

        if not self.parsed_lyrics:
            message("Found lyrics, but no synced timings for this track.")
            return

        wrap = self._lyric_wrap_width()
        self._lyrics_wrap = wrap

        # Spacers top and bottom so the first and last lines can still sit in
        # the middle of the pane when the active line is centred.
        top = ctk.CTkFrame(self.lyrics_scroll, fg_color="transparent", height=140)
        top.grid(row=0, column=0, sticky="ew")
        self._lyric_spacers.append(top)

        for i, (_t, text) in enumerate(self.parsed_lyrics):
            lbl = ctk.CTkLabel(self.lyrics_scroll, text=text,
                               font=self._lyric_font(21),
                               text_color=self.theme["text_secondary"],
                               fg_color="transparent", corner_radius=12,
                               wraplength=wrap, justify="left", anchor="w")
            lbl.grid(row=i + 1, column=0, sticky="ew", padx=2, pady=3,
                     ipadx=14, ipady=9)
            self.lyrics_labels.append(lbl)

        bottom = ctk.CTkFrame(self.lyrics_scroll, fg_color="transparent", height=200)
        bottom.grid(row=len(self.parsed_lyrics) + 1, column=0, sticky="ew")
        self._lyric_spacers.append(bottom)

    def build_dl_view(self, parent):
        self.dl_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.dl_frame.pack(fill="both", expand=True)
        self.dl_frame.grid_columnconfigure(0, weight=1)
        self.dl_frame.grid_rowconfigure(3, weight=3)
        self.dl_frame.grid_rowconfigure(4, weight=1)

        header = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=36, pady=(22, 6))
        self.dl_heading = ctk.CTkLabel(header, text="Add music",
                                       font=ctk.CTkFont(size=22, weight="bold"))
        self.dl_heading.pack(side="left")
        self.dl_close_btn = ctk.CTkButton(header, text="Back to library", width=150,
                                          height=34, corner_radius=17,
                                          command=self.close_downloader)
        self.dl_close_btn.pack(side="right")
        self.dl_hint = ctk.CTkLabel(
            header, text="Search, or paste a Spotify track / album / playlist link",
            font=ctk.CTkFont(size=12))
        self.dl_hint.pack(side="right", padx=16)

        self.url_entry = ctk.CTkEntry(
            self.dl_frame, placeholder_text="Search artist or song, or paste a Spotify URL...",
            height=52, corner_radius=26, font=ctk.CTkFont(size=16))
        self.url_entry.grid(row=1, column=0, padx=36, pady=(6, 10), sticky="ew")
        self.url_entry.bind("<KeyRelease>", self.on_key_release)
        self.url_entry.bind("<FocusIn>", lambda e: self.on_key_release(None))
        self.url_entry.bind("<Return>", lambda e: self.start_download())

        self.search_timer = None
        self.suggestions_frame = ctk.CTkScrollableFrame(self.dl_frame, height=210,
                                                        corner_radius=15)

        buttons = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        buttons.grid(row=2, column=0, padx=36, pady=(2, 8), sticky="ew")

        self.download_button = ctk.CTkButton(
            buttons, text="Start download", height=46, width=180, corner_radius=23,
            font=ctk.CTkFont(size=15, weight="bold"), command=self.start_download)
        self.download_button.pack(side="left")

        # A long album run used to be uninterruptible.
        self.cancel_button = ctk.CTkButton(
            buttons, text="Cancel", height=46, width=110, corner_radius=23,
            command=self.cancel_downloads, fg_color="transparent", border_width=2,
            font=ctk.CTkFont(size=14, weight="bold"))
        self.retry_button = ctk.CTkButton(
            buttons, text="Retry failed", height=46, width=140, corner_radius=23,
            command=self.retry_failed, font=ctk.CTkFont(size=14, weight="bold"))
        self.dl_progress_lbl = ctk.CTkLabel(buttons, text="",
                                            font=ctk.CTkFont(size=13))
        self.dl_progress_lbl.pack(side="right")

        # Per-track state, so a failure no longer scrolls out of the log and
        # disappears with no record of what broke.
        self.jobs_frame = ctk.CTkScrollableFrame(self.dl_frame, corner_radius=15,
                                                 label_text="Queue")
        self.jobs_frame.grid(row=3, column=0, padx=36, pady=(6, 6), sticky="nsew")
        self.job_rows = {}

        self.log_box = ctk.CTkTextbox(self.dl_frame, corner_radius=15, height=120,
                                      font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=4, column=0, padx=36, pady=(6, 22), sticky="nsew")
        self.log_box.configure(state="disabled")

        self._sync_download_buttons()
        self._rebuild_job_rows()

        if self.spotify_error:
            self.url_entry.configure(state="disabled")
            self.download_button.configure(state="disabled")
            self.log(self.spotify_error)
            self.setup_prompt_btn = ctk.CTkButton(
                self.dl_frame, text="Set up Spotify access", height=42,
                corner_radius=21, font=ctk.CTkFont(size=14, weight="bold"),
                command=self.open_setup)
            self.setup_prompt_btn.grid(row=2, column=0, padx=36, pady=(0, 8))

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
        self.current_theme_name = choice
        self.theme = THEMES[choice]
        self.settings.set("theme", choice)
        self.apply_theme()

    def apply_theme(self):
        """Repaint every themed widget.

        This only touched six widgets before, so switching themes left the
        title bar, transport buttons, overlays and sliders on the old palette.
        """
        t = self.theme
        self.configure(fg_color=t["bg"])

        for name, key in (
            ("main_area", "bg"), ("title_bar", "surface"), ("bottom_bar", "surface"),
            ("eq_frame", "surface"), ("np_overlay", "bg"), ("viz_overlay", "bg"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t[key])

        if hasattr(self, 'canvas'):
            self.canvas.configure(bg=t["bg"])
        if hasattr(self, 'title_lbl'):
            self.title_lbl.configure(text_color=t["text"])
        if hasattr(self, 'lib_search_entry'):
            self.lib_search_entry.configure(fg_color=t["bg"], text_color=t["text"])

        for name in ("theme_dropdown", "viz_dropdown", "preset_dropdown"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["surface"], button_color=t["surface"],
                                 button_hover_color=t["surface_hover"],
                                 dropdown_fg_color=t["surface"],
                                 dropdown_hover_color=t["surface_hover"],
                                 text_color=t["text"])

        for name in ("nav_dl_btn", "play_btn", "eq_toggle_btn", "viz_toggle_btn",
                     "repair_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["accent"], hover_color=t["accent_hover"],
                                 text_color=t["bg"])

        for name in ("prev_btn", "next_btn", "min_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(hover_color=t["surface_hover"], text_color=t["text"])

        if hasattr(self, 'shuffle_btn'):
            self.shuffle_btn.configure(
                hover_color=t["surface_hover"],
                text_color=t["accent"] if self.shuffle else t["text"])
        if hasattr(self, 'repeat_btn'):
            self.repeat_btn.configure(
                hover_color=t["surface_hover"],
                text_color=t["accent"] if self.repeat else t["text"])

        for name in ("progress_slider", "volume_slider"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(button_color=t["accent"],
                                 button_hover_color=t["accent_hover"],
                                 progress_color=t["accent"])

        for name in ("now_playing_label", "time_elapsed", "time_total", "volume_icon"):
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
        for name in ("download_button", "retry_button", "dl_close_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["accent"], hover_color=t["accent_hover"],
                                 text_color=t["bg"])
        if getattr(self, "cancel_button", None) is not None:
            self.cancel_button.configure(border_color=t["accent"],
                                         hover_color=t["surface_hover"],
                                         text_color=t["text"])
        for name in ("np_lyrics_pane", "lyrics_scroll"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(fg_color=t["bg"])
        for name in ("np_title_lbl", "np_artist_lbl"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(text_color=t["text"] if name.endswith("title_lbl")
                                 else t["text_secondary"])
        if getattr(self, "np_close_btn", None) is not None:
            self.np_close_btn.configure(fg_color=t["accent"],
                                        hover_color=t["accent_hover"],
                                        text_color=t["bg"])
        if getattr(self, "viz_palette_dropdown", None) is not None:
            self.viz_palette_dropdown.configure(
                fg_color=t["surface"], button_color=t["surface"],
                button_hover_color=t["surface_hover"],
                dropdown_fg_color=t["surface"],
                dropdown_hover_color=t["surface_hover"], text_color=t["text"])
        for i in range(len(getattr(self, "lyrics_labels", []))):
            self._lyric_style(i, "active" if i == self.current_lyric_index else "next")

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
        if self.visualizer_visible:
            self.viz_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.viz_dropdown.set(VIZ_MODES[self.visualizer_mode])
        else:
            self.viz_overlay.place_forget()

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
        if self.visualizer_visible and self.player.playing and not self.player.paused:
            self._draw_bands(self.player.smoothed_bands.tolist())
        self._safe_after(33, self.update_visualizer_loop)

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

        full_name = os.path.splitext(os.path.basename(path))[0]
        self.now_playing_label.configure(
            text=full_name if len(full_name) <= 50 else full_name[:47] + "...")
        self.extract_album_art(path)

        def load():
            if self.player.load_track(path):
                duration = self.player.get_duration()
                if duration > 0 and position < duration - 2:
                    self.player.set_progress(position / duration)
                self._safe_after(0, self._show_resumed)

        threading.Thread(target=load, daemon=True).start()

    def _show_resumed(self):
        self.play_btn.configure(text="\u25b6")   # paused: press play to continue
        self.progress_slider.set(self.player.get_progress())
        self.time_elapsed.configure(text=fmt_time(self.player.get_position()))
        self.time_total.configure(text=fmt_time(self.player.get_duration()))

    def refresh_repair_button(self):
        """Show the repair action only when orphaned raw downloads exist."""
        try:
            orphans = find_orphaned_downloads(LIBRARY_DIR)
        except Exception:
            orphans = []
        if orphans:
            size_mb = sum(os.path.getsize(p) for p in orphans) / 1e6
            self.repair_btn.configure(
                text=f"Repair {len(orphans)} file(s) \u00b7 {size_mb:.0f} MB")
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

    def render_library(self):
        self.library.render()
        self.current_rows = self.library.rows
        self.current_library_files = self.library.paths

    def set_library_view(self, name):
        self.library_view = name
        self.settings.set("library_view", name)
        self.library.set_view(name)
        self.render_library()

    def set_library_sort(self, name):
        self.library_sort = name
        self.settings.set("library_sort", name)
        self.library.set_sort(name)
        self.render_library()

    def clear_library_filter(self):
        self.library.clear_filter()
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

        self.play_file(file_path)

    def play_file(self, file_path):
        full_name = os.path.splitext(os.path.basename(file_path))[0]
        filename = full_name if len(full_name) <= 50 else full_name[:47] + "..."
        self.now_playing_label.configure(text=filename)
        self.time_total.configure(text="0:00")
        self.time_elapsed.configure(text="0:00")

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

        row = None
        try:
            matches = [t for t in self.index.tracks() if t["path"] == file_path]
            row = matches[0] if matches else None
        except Exception:
            pass
        if hasattr(self, "np_title_lbl"):
            self.np_title_lbl.configure(
                text=(row or {}).get("title") or full_name)
            self.np_artist_lbl.configure(
                text=" \u00b7 ".join(x for x in ((row or {}).get("artist"),
                                                 (row or {}).get("album")) if x))

        self._now_playing_row = row or {"path": file_path}
        self._push_discord(playing=True)
        self._ensure_cover_url(file_path, row)

        self.extract_album_art(file_path)
        self.play_btn.configure(text="⏸")
        threading.Thread(target=self._load_and_play, args=(file_path,), daemon=True).start()
        # Search lyrics with the full name -- `filename` is truncated to 50
        # characters with an ellipsis for display, which never matched.
        self.fetch_lyrics(full_name)

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

        ctk.CTkLabel(card, text="Connect Spotify",
                     font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=t["text"]).pack(padx=44, pady=(32, 6))
        ctk.CTkLabel(
            card, justify="left", wraplength=520, text_color=t["text_secondary"],
            font=ctk.CTkFont(size=13),
            text=("Eve's Garden reads track and album details from Spotify. "
                  "It needs a free developer app of your own -- takes about a "
                  "minute, and no card is required.\n\n"
                  "1.  Open the dashboard below and sign in.\n"
                  "2.  Create app  ->  give it any name, tick the terms.\n"
                  "3.  Copy the Client ID and Client Secret into the boxes.")
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
        self.setup_id_entry.focus_set()

    def close_setup(self):
        if getattr(self, "setup_overlay", None) is not None:
            self.setup_overlay.destroy()
            self.setup_overlay = None

    def _save_setup(self):
        client_id = self.setup_id_entry.get().strip()
        secret = self.setup_secret_entry.get().strip()
        self.setup_save_btn.configure(state="disabled", text="Checking...")
        self.setup_status.configure(text="Asking Spotify to confirm those keys...",
                                    text_color=self.theme["text_secondary"])

        def work():
            ok, message = credentials.verify(client_id, secret)
            self._safe_after(0, self._finish_setup, ok, message, client_id, secret)

        threading.Thread(target=work, daemon=True).start()

    def _finish_setup(self, ok, message, client_id, secret):
        self.setup_save_btn.configure(state="normal", text="Verify and save")
        if not ok:
            self.setup_status.configure(text=message, text_color=self.theme["text"])
            return

        where = credentials.save(client_id, secret)
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
        stem = os.path.splitext(os.path.basename(row.get("path", "")))[0]
        self.discord.update(
            title=row.get("title") or stem,
            artist=row.get("artist") or "",
            album=row.get("album") or "",
            cover_url=row.get("cover_url"),
            position=self.player.get_position(),
            duration=self.player.get_duration(),
            playing=playing,
        )

    def _ensure_cover_url(self, file_path, row):
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

    def _seek_begin(self, _event=None):
        self._seeking = True

    def _seek_end(self, _event=None):
        self.player.set_progress(self.progress_slider.get())
        self._seeking = False

    def on_seek(self, value):
        # Only commit the seek on release; committing on every drag pixel
        # reset the EQ filter state dozens of times a second.
        if not self._seeking:
            self.player.set_progress(value)

    def on_volume(self, value):
        self.player.set_volume(value)
        self.settings.set("volume", float(value))
        self.volume_icon.configure(
            text="\U0001F507" if value < 0.01 else ("\U0001F509" if value < 0.5 else "\U0001F50A")
        )

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
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

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

            self._safe_after(0, self.show_suggestions, None, tracks, "Suggested Based on Now Playing")
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
                btn_text = f"🎵 {name} - {art}"
                btn = ctk.CTkButton(self.suggestions_frame, text=btn_text, anchor="w", fg_color="transparent",
                                    text_color=self.theme["text"], hover_color=self.theme["surface_hover"], corner_radius=8,
                                    command=lambda u=url, t=btn_text: self.select_suggestion(u, t))
                btn.pack(fill="x", padx=5, pady=2)

        if artists:
            ctk.CTkLabel(self.suggestions_frame, text="Artists", font=ctk.CTkFont(weight="bold"), text_color=self.theme["text_secondary"]).pack(anchor="w", padx=10, pady=(10,0))
            for artist in artists[:3]: # Only show top 3 artists to save space
                name = artist['name']
                url = artist['external_urls']['spotify']
                btn = ctk.CTkButton(self.suggestions_frame, text=f"👤 {name}", anchor="w", fg_color="transparent",
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
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Albums by {artist_name}")
        dialog.geometry("500x650")
        dialog.attributes("-topmost", True)
        dialog.configure(fg_color=self.theme["bg"])

        ctk.CTkLabel(dialog, text=f"Select Albums or Tracks to Download", font=ctk.CTkFont(size=20, weight="bold"), text_color=self.theme["text"]).pack(pady=(20, 10))

        scroll = ctk.CTkScrollableFrame(dialog, fg_color=self.theme["surface"], corner_radius=15)
        scroll.pack(fill="both", expand=True, padx=20, pady=10)

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

            text_lbl = ctk.CTkLabel(row, text=album['name'], text_color=self.theme["text"], font=ctk.CTkFont(size=14, weight="bold"), justify="left")
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
            dialog.destroy()
            if selected_urls:
                threading.Thread(target=self.download_selected_items, args=(selected_urls,), daemon=True).start()

        ctk.CTkButton(dialog, text="Download Selected", height=45, corner_radius=25,
                      fg_color=self.theme["accent"], text_color=self.theme["bg"], hover_color=self.theme["accent_hover"],
                      command=download_selected).pack(pady=20, padx=20, fill="x")

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

    def _start_batch(self, track_urls, labels=None):
        if not track_urls:
            self._gui_log("Nothing to download.")
            return
        started = self.downloads.start(
            track_urls, LIBRARY_DIR,
            jobs=int(self.settings.get("download_jobs") or 3),
            quality=self.settings.get("download_quality"),
            labels=labels,
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
        self.load_library()


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
            if "track" in url:
                track_urls = [url]
            elif "playlist" in url:
                self._gui_log("Fetching playlist tracks...")
                track_urls = get_spotify_playlist_tracks(self.sp, url)
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
