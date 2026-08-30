"""The library browser: Songs / Albums / Artists, drill-down, and rendering.

Pulled out of the App class, which had grown past a hundred methods and had
become the reason every change to this project arrived as a find-and-replace
script rather than an edit. This owns the scroll frame, the breadcrumb and
the row widgets; it talks to the rest of the app through the small callback
set passed to the constructor.
"""

import io
import os
import threading

import customtkinter as ctk
from PIL import Image
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC

CHUNK = 40  # rows rendered per event-loop turn


def fmt_time(seconds):
    seconds = int(max(0, seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


class LibraryView:
    def __init__(self, frame, crumb_bar, crumb_label, status_label,
                 index, theme, schedule, on_play, get_query):
        self.frame = frame
        self.crumb_bar = crumb_bar
        self.crumb_label = crumb_label
        self.status = status_label
        self.index = index
        self.theme = theme
        self.schedule = schedule        # (delay, fn, *args) -> id, tolerant of shutdown
        self.on_play = on_play
        self.get_query = get_query

        self.view = "Songs"
        self.sort = "Title"
        self.filter = None              # (kind, value, label)
        self.rows = []
        self.paths = []

        self._art_cache = {}
        self._signature = None
        self._token = 0

    # ------------------------------------------------------------ navigation

    def set_view(self, name):
        self.view = name
        self.filter = None
        self.render()

    def set_sort(self, name):
        self.sort = name
        self.render()

    def clear_filter(self):
        self.filter = None
        self.render()

    def open_album(self, album, artist):
        self.filter = ("album", album, f"{album} · {artist}")
        self.render()

    def open_artist(self, artist):
        self.filter = ("artist", artist, artist)
        self.render()

    def invalidate(self):
        """Force the next render to rebuild even if the row set looks identical."""
        self._signature = None

    # -------------------------------------------------------------- rendering

    def render(self):
        query = self.get_query() or ""

        if self.filter:
            kind, value, label = self.filter
            self.crumb_label.configure(text=label)
            self.crumb_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(0, 4))
            rows = self.index.tracks(
                search=query or None,
                sort="Album" if kind == "album" else self.sort,
                **{kind: value},
            )
            self._render(rows, self._track_row, ("tracks", kind, value, query), "tracks")
            return

        self.crumb_bar.grid_forget()
        if self.view == "Albums":
            rows = self.index.albums(search=query or None)
            self._render(rows, self._album_row, ("albums", query), "albums")
        elif self.view == "Artists":
            rows = self.index.artists(search=query or None)
            self._render(rows, self._artist_row, ("artists", query), "artists")
        else:
            rows = self.index.tracks(search=query or None, sort=self.sort)
            self._render(rows, self._track_row, ("tracks", self.sort, query), "tracks")

    def _render(self, rows, builder, key, noun):
        """Rebuild only when the row set changed, in responsive chunks.

        The original list destroyed and recreated every widget on each
        refresh, including on every keystroke in the search box.
        """
        signature = (key, tuple(
            r.get("path") or (r.get("album"), r.get("artist"), r.get("n"))
            for r in rows))
        if signature == self._signature:
            return
        self._signature = signature

        self._token += 1
        token = self._token
        for widget in self.frame.winfo_children():
            widget.destroy()

        self.rows = rows
        self.paths = [r["path"] for r in rows if r.get("path")]

        if not rows:
            ctk.CTkLabel(self.frame,
                         text="Nothing here yet. Use Download More to add music.",
                         text_color=self.theme["text_secondary"],
                         font=ctk.CTkFont(size=15)).pack(pady=40)
            self._set_status(f"{self.index.count()} tracks indexed")
            return

        def chunk(start):
            if token != self._token or not self.frame.winfo_exists():
                return
            for row in rows[start:start + CHUNK]:
                builder(row)
            if start + CHUNK < len(rows):
                self.schedule(1, chunk, start + CHUNK)

        chunk(0)
        self._set_status(f"{len(rows)} {noun}")

    def _set_status(self, text):
        try:
            self.status.configure(text=text)
        except Exception:
            pass

    # ------------------------------------------------------------------ rows

    def _row(self, height=56):
        row = ctk.CTkFrame(self.frame, fg_color="transparent",
                           corner_radius=10, height=height)
        row.pack(fill="x", padx=6, pady=3)
        row.pack_propagate(False)
        return row

    def _clickable(self, widget, handler):
        widget.bind("<Button-1>", lambda e: handler())
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._clickable(child, handler)

    def _track_row(self, track):
        row = self._row()
        title = track.get("title") or os.path.basename(track["path"])
        detail = " · ".join(
            x for x in (track.get("artist") or "", track.get("album") or "") if x)

        ctk.CTkLabel(row, text=title, anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=self.theme["text"]).pack(side="left", padx=(16, 8))
        ctk.CTkLabel(row, text=detail, anchor="w", font=ctk.CTkFont(size=12),
                     text_color=self.theme["text_secondary"]).pack(side="left", padx=4)
        ctk.CTkLabel(row, text=fmt_time(track.get("duration")), width=48, anchor="e",
                     font=ctk.CTkFont(size=12),
                     text_color=self.theme["text_secondary"]).pack(side="right", padx=16)
        self._clickable(row, lambda p=track["path"]: self.on_play(p))

    def _album_row(self, album):
        row = self._row(64)
        art = ctk.CTkLabel(row, text="", width=48, height=48)
        art.pack(side="left", padx=(12, 12))
        self.request_thumb(album.get("cover_path"), 48, art)

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=album["album"], anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=self.theme["text"]).pack(anchor="w")
        meta = (f"{album['artist']} · {album['n']} tracks"
                f" · {fmt_time(album['total'])}")
        if album.get("year"):
            meta += f" · {album['year']}"
        ctk.CTkLabel(box, text=meta, anchor="w", font=ctk.CTkFont(size=12),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")
        self._clickable(row, lambda a=album["album"], r=album["artist"]:
                        self.open_album(a, r))

    def _artist_row(self, artist):
        row = self._row(64)
        art = ctk.CTkLabel(row, text="", width=48, height=48)
        art.pack(side="left", padx=(12, 12))
        self.request_thumb(artist.get("cover_path"), 48, art)

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=artist["artist"], anchor="w",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=self.theme["text"]).pack(anchor="w")
        ctk.CTkLabel(box, text=f"{artist['n']} tracks · {artist['albums']} album(s)",
                     anchor="w", font=ctk.CTkFont(size=12),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")
        self._clickable(row, lambda a=artist["artist"]: self.open_artist(a))

    # ------------------------------------------------------------- cover art

    def request_thumb(self, path, size, label):
        """Load embedded cover art off the UI thread, cached by path and size."""
        if not path:
            return
        key = (path, size)
        cached = self._art_cache.get(key)
        if cached is not None:
            label.configure(image=cached)
            return

        def work():
            try:
                audio = MP3(path, ID3=ID3)
                data = next((t.data for t in (audio.tags or {}).values()
                             if isinstance(t, APIC)), None)
                if not data:
                    return
                img = Image.open(io.BytesIO(data)).convert("RGB").resize(
                    (size, size), Image.Resampling.LANCZOS)
            except Exception:
                return
            self.schedule(0, self._set_thumb, key, img, size, label)

        threading.Thread(target=work, daemon=True).start()

    def _set_thumb(self, key, img, size, label):
        if len(self._art_cache) > 300:
            self._art_cache.clear()
        image = ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        self._art_cache[key] = image
        try:
            if label.winfo_exists():
                label.configure(image=image)
        except Exception:
            pass
