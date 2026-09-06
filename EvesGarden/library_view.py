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
import time
from concurrent.futures import ThreadPoolExecutor

import customtkinter as ctk
import motion
import smart_playlists
import theme_ui
import ui_widgets

HEART_FULL = "♥"
HEART_EMPTY = "♡"
from PIL import Image

import audio_files

# Rows rendered per turn of the event loop. This is the click latency of a
# view switch: the first chunk is built before anything is drawn, and a track
# row costs about 2ms to build. At 40 that was a 290ms freeze on the Songs
# tab; at 12 it is under 100ms, and the rest of the list still fills in well
# inside the time it takes to look at it.
CHUNK = 12
ROW_H = theme_ui.ROW_H
ART = theme_ui.ROW_ART

# Rows built above and below the part of the list you can actually see, so
# a flick of the wheel lands on something already drawn rather than on a
# gap that fills in afterwards.
BUFFER_ROWS = 6

# The gap a row leaves around itself, matching the pady it used to pack with.
STRIDE_PAD = 2

# How often the scroll position is looked at. A CustomTkinter scroll frame is
# driven from a scrollbar, two mousewheel bindings and the keyboard, and
# wrapping every one of those is more fragile than asking where we are: the
# check is one Tk call and does nothing at all unless the answer moved.
SCROLL_POLL_MS = 60
FADE_H = 14  # the header tint's fade-out into the list below it


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def ellipsize(text, limit):
    """Trim to a length with a proper ellipsis.

    Tk labels do not clip: a long album title given a fixed width simply
    overruns its neighbour instead of being cut off.
    """
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def fmt_time(seconds):
    seconds = int(max(0, seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


class LibraryView:
    def __init__(self, frame, crumb_bar, crumb_label, status_label,
                 index, theme, schedule, on_play, get_query):
        self.frame = frame
        # Only the rows you can see are built. See _sync_visible: a track row
        # costs about 9ms in CustomTkinter widgets, so a 483 track library
        # spent 4.3 seconds building rows to show eleven of them, and it grew
        # with the library rather than staying put.
        self._virtual = None
        self._place_y = None          # set while a row is being placed
        self._last_row = None         # the row _row() just made
        self._last_row_h = ROW_H
        self._scroll_at = None
        self._polling = False
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
        # Rows detached from the list and waiting to be destroyed.
        self._discarding = set()
        # Rows keyed by path so the playing track can be highlighted without
        # re-rendering the list.
        self._rows_by_path = {}
        self._locked = set()
        self.playing_path = None
        # path -> BooleanVar, for the duplicate review checkboxes
        self.dup_marks = {}
        self._hearts = {}
        self.on_like = None          # set by the app; toggles and re-paints
        self.on_menu = None          # right-click: (path, x, y)
        self.playlist_id = None      # set while viewing one playlist
        self.playlist_name = None
        self.smart = None            # set while viewing one smart playlist
        self.vanished = False        # set while viewing what has gone
        self._art_pool = ThreadPoolExecutor(max_workers=4,
                                            thread_name_prefix="cover")

        # The album-colour wash behind the breadcrumb.
        self._tint_band = None
        self._tint_key = None
        self._tint_colour = None
        self._tint_cache = {}
        self.crumb_bar.bind("<Configure>", lambda _e: self._redraw_tint())

    # ------------------------------------------------------------ navigation

    def set_view(self, name):
        self.view = name
        self.filter = None
        self.smart = None
        self.vanished = False
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

    def open_album_of(self, album, artist):
        """An album reached from inside an artist, so Back returns there."""
        self.filter = ("album", album, f"{artist} › {album}", artist)
        self.render()

    def go_back(self):
        """Up one level: album -> its artist, artist -> the full list."""
        if self.filter and self.filter[0] == "album" and len(self.filter) > 3:
            self.open_artist(self.filter[3])
        else:
            self.clear_filter()

    def invalidate(self):
        """Force the next render to rebuild even if the row set looks identical."""
        self._signature = None

    # -------------------------------------------------------------- rendering

    def render(self):
        query = self.get_query() or ""

        if self.filter:
            kind, value, label = self.filter[0], self.filter[1], self.filter[2]
            self.crumb_label.configure(text=label)
            self._show_crumb_bar()

            if kind == "artist":
                # An artist page is a shelf of albums, not 25 loose tracks.
                albums = [a for a in self.index.albums(search=query or None)
                          if a["artist"] == value]
                if albums:
                    self._page_tint((kind, value),
                                    albums[0].get("cover_path"))
                    self._render(albums, self._artist_album_row,
                                 ("artist-albums", value, query), "albums")
                    return

            rows = self.index.tracks(
                search=query or None,
                sort="Album" if kind == "album" else self.sort,
                **{kind: value},
            )
            self._page_tint((kind, value),
                            rows[0].get("path") if rows else None)
            self._render(rows, self._track_row, ("tracks", kind, value, query), "tracks")
            return

        self.crumb_bar.grid_forget()
        self._page_tint(None, None)
        if self.view == "Playlists":
            if self.smart is not None:
                rows = self.index.smart_tracks(self.smart)
                self.crumb_label.configure(text=self.smart.name)
                self.crumb_bar.grid(row=1, column=0, sticky="ew",
                                    padx=24, pady=(2, 6))
                self._render(rows, self._track_row,
                             ("smart", self.smart.key, len(rows), query),
                             "track")
                return
            if self.playlist_id is not None:
                rows = self.index.playlist_tracks(self.playlist_id)
                self.crumb_label.configure(text=self.playlist_name or "Playlist")
                self._show_crumb_bar()
                self._page_tint(("playlist", self.playlist_id),
                                rows[0].get("path") if rows else None)
                self._render(rows, self._track_row,
                             ("playlist", self.playlist_id, query), "track")
                return
            if self.vanished:
                rows = self.index.vanished()
                self.crumb_label.configure(text="Gone from Spotify")
                self._show_crumb_bar()
                self._render(rows, self._vanished_row,
                             ("vanished", len(rows), query), "track")
                return
            # Smart playlists lead: they are the ones whose contents move.
            rows = []
            for rule in smart_playlists.RULES:
                summary = self.index.smart_summary(rule)
                if not summary["n"]:
                    continue        # an empty question is just noise
                rows.append({"smart": rule, "id": rule.key, "name": rule.name,
                             "hint": rule.hint, "n": summary["n"],
                             "total": summary["total"],
                             "cover_path": summary["cover_path"]})
            # What has gone sits with the playlists it went from, because
            # that is where somebody would look for it.
            missing = self.index.vanished()
            if missing:
                kept = sum(1 for row in missing
                           if row["path"] and os.path.exists(row["path"]))
                rows.append({
                    "gone": True, "id": "vanished",
                    "name": "Gone from Spotify",
                    "hint": ("you still have %d of them" % kept if kept
                             else "none of them were downloaded"),
                    "n": len(missing), "total": 0, "cover_path": None,
                })
            rows.extend(self.index.playlists())
            self._render(rows, self._playlist_row, ("playlists", query),
                         "playlist")
            return
        if self.view == "Liked":
            rows = self.index.tracks(search=query or None, liked_only=True,
                                     sort="Recently liked")
            self._render(rows, self._track_row, ("liked", query), "liked song")
            return
        if self.view == "Recent":
            rows = self.index.tracks(search=query or None, played_only=True,
                                     sort="Recently played")
            self._render(rows, self._track_row, ("recent", query), "track")
            return
        if self.view == "Duplicates":
            groups = self.index.duplicates()
            self.dup_marks = {}
            self._render(groups, self._duplicate_group,
                         ("dupes", len(groups)), "duplicate group")
            return
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
            r.get("path") or r.get("album")
            or (r.get("title"), r.get("artist"), len(r.get("extra") or []))
            for r in rows))
        if signature == self._signature:
            return
        self._signature = signature

        self._token += 1
        token = self._token
        # Detaching is one cheap call per row; destroying is not, and doing
        # it here meant every view switch paid for the view being left before
        # the new one could be drawn.
        stale = [w for w in self.frame.winfo_children()
                 if w not in self._discarding]
        for widget in stale:
            self._detach(widget)
        self._discard(stale)
        self._virtual = None
        self._resize_to_fit()   # release the height a previous list pinned

        self.rows = rows
        self.paths = [r["path"] for r in rows if r.get("path")]
        self._rows_by_path = {}
        self._hearts = {}
        self._locked = set()

        if not rows:
            ctk.CTkLabel(self.frame,
                         text=({"Duplicates": "No duplicates found — your library is clean.",
                                "Liked": "No liked songs yet. Tap the heart on any track.",
                                "Recent": "Nothing played yet.",
                                "Playlists": "No playlists yet. Use New playlist to make one."}.get(self.view)
                               or "Nothing here yet. Use Add music to "
                                  "get started, or Ctrl+K \u2192 Music "
                                  "folders to point it at music you already "
                                  "have."),
                         text_color=self.theme["text_secondary"],
                         font=ctk.CTkFont(size=15)).pack(pady=40)
            self._set_status(f"{self.index.count()} tracks indexed")
            return

        self._virtual = {"rows": rows, "builder": builder, "token": token,
                         "stride": ROW_H + STRIDE_PAD, "built": {}}
        self._sync_visible()
        self._start_polling()
        self._set_status(plural(len(rows), noun.rstrip("s")))

    # ------------------------------------------------------------ virtual

    def _detach(self, widget):
        for forget in (widget.place_forget, widget.pack_forget):
            try:
                forget()
            except Exception:
                pass

    def _viewport(self):
        """Where the list has been scrolled to, and how tall the window is.

        None when there is no canvas to ask -- a bare frame in a test, say --
        which _sync_visible reads as "build the lot", since without a
        viewport there is no way to know what is off screen.
        """
        canvas = getattr(self.frame, "_parent_canvas", None)
        if canvas is None:
            return None
        try:
            if not canvas.winfo_exists():
                return None
            return (float(canvas.canvasy(0)), int(canvas.winfo_height()),
                    int(canvas.winfo_width()))
        except Exception:
            return None

    def visible_range(self, top, height, stride, count):
        """The rows worth having built, given where we are. Pure, so tested."""
        if stride <= 0 or count <= 0:
            return 0, -1
        first = int(top // stride) - BUFFER_ROWS
        last = int((top + max(height, 0)) // stride) + BUFFER_ROWS
        # Clamped to the list at both ends, so scrolling past the bottom asks
        # for rows that exist rather than for index 500 of 483.
        first = max(0, min(first, count - 1))
        last = max(first, min(last, count - 1))
        return first, last

    def _sync_visible(self):
        v = self._virtual
        if not v or v["token"] != self._token:
            return
        try:
            if not self.frame.winfo_exists():
                return
        except Exception:
            return

        rows, built, stride = v["rows"], v["built"], v["stride"]
        seen = self._viewport()
        if seen is None:
            first, last = 0, len(rows) - 1
        else:
            first, last = self.visible_range(seen[0], seen[1], stride,
                                             len(rows))

        for index in [i for i in built if i < first or i > last]:
            widget = built.pop(index)
            self._detach(widget)
            try:
                widget.destroy()
            except Exception:
                pass

        fresh = []
        for index in range(first, last + 1):
            if index in built:
                continue
            row = self._build_at(index)
            if row is not None:
                built[index] = row
                fresh.append(row)
                if index == 0 and self._last_row_h + STRIDE_PAD != stride:
                    # The first row of this view says how tall its kind is --
                    # an album row is taller than a track row -- and row 0
                    # sits at the same y whatever the answer, so learning it
                    # here costs nothing.
                    stride = v["stride"] = self._last_row_h + STRIDE_PAD

        if seen is not None:
            width = max(1, seen[2] - 12)
            # Every row when the window has been resized, otherwise only the
            # ones just made: configure() on a CTk frame forces a redraw, and
            # doing it to thirty rows on every scroll tick is exactly the
            # cost this whole change exists to avoid.
            for row in (built.values() if width != v.get("width") else fresh):
                try:
                    row.configure(width=width)
                except Exception:
                    pass
            v["width"] = width
        self._resize_to_fit()

    def _build_at(self, index):
        v = self._virtual
        self._place_y = index * v["stride"] + STRIDE_PAD // 2
        self._last_row = None
        try:
            v["builder"](v["rows"][index])
        finally:
            self._place_y = None
        return self._last_row

    def _resize_to_fit(self):
        """Give the scrollbar the whole list to scroll, not just what is built.

        Placed children never enlarge their parent, so the frame has to be
        told how tall the list is. Not with configure(height=): a
        CTkScrollableFrame forwards that to the outer container and the
        scrolling frame inside never hears about it. The frame is a window
        item on the canvas, so the item is what carries the height -- the
        same lever CustomTkinter uses itself to pin the width.
        """
        v = self._virtual
        canvas = getattr(self.frame, "_parent_canvas", None)
        item = getattr(self.frame, "_create_window_id", None)
        if canvas is None or item is None:
            return
        try:
            if not v:
                canvas.itemconfigure(item, height=0)   # back to its own size
                return
            total = max(1, len(v["rows"]) * v["stride"])
            canvas.itemconfigure(item, height=total)
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), total))
        except Exception:
            pass

    def _start_polling(self):
        if self._polling:
            return
        self._polling = True
        self._scroll_at = None
        self._poll_scroll()

    def _poll_scroll(self):
        if self._virtual is None:
            self._polling = False
            return
        seen = self._viewport()
        if seen is not None and seen != self._scroll_at:
            self._scroll_at = seen
            self._sync_visible()
        self.schedule(SCROLL_POLL_MS, self._poll_scroll)

    # Long enough to be worth a turn of the loop, short enough that a frame
    # is never missed for it.
    DISCARD_SLICE = 0.008

    def _discard(self, widgets):
        """Tear down rows in the background instead of all at once.

        A CustomTkinter widget costs about 0.8ms to destroy, and a track row
        is eight of them, so clearing a 125-track list took very nearly a
        second -- spent before the view being switched to had drawn anything.
        That second was the whole of "switching views lags". The rows are
        detached first, so they are already gone from the screen, and what
        is left is bookkeeping nobody is waiting on.
        """
        if not widgets:
            return
        self._discarding.update(widgets)
        pending = list(widgets)

        def drain():
            deadline = time.perf_counter() + self.DISCARD_SLICE
            while pending:
                widget = pending.pop()
                self._discarding.discard(widget)
                try:
                    widget.destroy()
                except Exception:
                    pass
                if time.perf_counter() >= deadline:
                    break
            if pending:
                self.schedule(1, drain)

        self.schedule(1, drain)

    def _toggle_like(self, path, label):
        if not self.on_like:
            return
        liked = self.on_like(path)
        # The row this belongs to may be destroyed and built again when it
        # scrolls out and back, and it is built from this data -- so without
        # writing the change down here, the heart would empty itself again.
        for row in self.rows or []:
            if row.get("path") == path:
                row["liked"] = liked
        try:
            label.configure(text=HEART_FULL if liked else HEART_EMPTY,
                            text_color=(self.theme["accent"] if liked
                                        else self.theme["text_secondary"]))
        except Exception:
            pass
        if self.view == "Liked" and not liked:
            self.invalidate()
            self.render()

    def set_heart(self, path, liked):
        """Repaint one row's heart, for when it is liked from elsewhere."""
        label = self._hearts.get(path)
        if label is not None and label.winfo_exists():
            label.configure(text=HEART_FULL if liked else HEART_EMPTY,
                            text_color=(self.theme["accent"] if liked
                                        else self.theme["text_secondary"]))

    def mark_playing(self, path):
        """Tint whichever row is playing, and clear the previous one."""
        previous = self._rows_by_path.get(self.playing_path)
        if previous is not None and previous.winfo_exists():
            self._locked.discard(previous)
            try:
                previous.configure(fg_color="transparent")
            except Exception:
                pass
        self.playing_path = path
        row = self._rows_by_path.get(path)
        if row is not None and row.winfo_exists():
            self._paint_playing(row)

    def _paint_playing(self, row):
        self._locked.add(row)
        try:
            row.configure(fg_color=self.theme.get("surface", "transparent"))
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.status.configure(text=text)
        except Exception:
            pass

    # ------------------------------------------------------------------ rows

    def _row(self, height=ROW_H):
        row = ctk.CTkFrame(self.frame, fg_color="transparent",
                           corner_radius=theme_ui.RADIUS, height=height)
        # Placed rather than packed when the list is virtual: pack stacks
        # whatever exists, which is the wrong answer when row 300 is on
        # screen and rows 0-299 are not built. y is arithmetic instead.
        if self._place_y is None:
            row.pack(fill="x", padx=6, pady=1)
        else:
            # CustomTkinter refuses width/height on place -- they belong to
            # the constructor -- so the height came from there and the width
            # is applied by _sync_visible, which is the thing that knows how
            # wide the viewport currently is.
            row.place(x=6, y=self._place_y)
        row.pack_propagate(False)
        # Builders do not return their row, and there are six of them; this
        # is how the caller gets hold of one without touching any of them.
        self._last_row, self._last_row_h = row, height
        # Rows gave no feedback at all before; a hover tint makes it obvious
        # what is about to be clicked in a long list.
        self._hoverable(row)
        return row

    def _hoverable(self, row):
        tint = self.theme.get("surface_hover", self.theme["surface"])

        def enter(_e=None):
            if row.winfo_exists() and row not in self._locked:
                row.configure(fg_color=tint)

        def leave(_e=None):
            if row.winfo_exists() and row not in self._locked:
                row.configure(fg_color="transparent")

        for widget in (row,):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
        row._hover_enter, row._hover_leave = enter, leave

    def _menuable(self, widget, path):
        """Right-click opens the track actions menu, on the row and children."""
        def popup(event, p=path):
            if self.on_menu:
                self.on_menu(p, event.x_root, event.y_root)
            return "break"
        widget.bind("<Button-3>", popup)
        for child in widget.winfo_children():
            child.bind("<Button-3>", popup)
            for grandchild in child.winfo_children():
                grandchild.bind("<Button-3>", popup)

    def _clickable(self, widget, handler):
        # The heart owns its own click; the row must not also start playback.
        if getattr(widget, "_no_row_click", False):
            return
        widget.bind("<Button-1>", lambda e: handler())
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._clickable(child, handler)

    def _track_row(self, track):
        row = self._row()
        art = ctk.CTkLabel(row, text="", width=ART, height=ART,
                           corner_radius=6)
        art.pack(side="left", padx=(10, 12))
        self.request_thumb(track.get("path"), ART, art)

        # Duration packs before the text block so it keeps its column when a
        # long title would otherwise push it off the edge.
        ctk.CTkLabel(row, text=fmt_time(track.get("duration")), width=52,
                     anchor="e", font=theme_ui.font("time"),
                     text_color=self.theme["text_secondary"]
                     ).pack(side="right", padx=(8, 16))

        liked = bool(track.get("liked"))
        heart = ctk.CTkLabel(row, text=HEART_FULL if liked else HEART_EMPTY,
                             width=26, cursor="hand2",
                             font=theme_ui.font("body", size=16),
                             text_color=(self.theme["accent"] if liked
                                         else self.theme["text_secondary"]))
        heart._no_row_click = True
        heart.pack(side="right", padx=(2, 4))
        heart.bind("<Button-1>", lambda e, pth=track["path"], lbl=heart:
                   self._toggle_like(pth, lbl))
        self._hearts[track["path"]] = heart

        # The album gets its own column. With only a title block on the left
        # and a duration on the right, the whole middle of every row was
        # empty; it is also the field you scan for when you are looking for
        # one track off a particular record. Suppressed inside an album, where
        # it would repeat the same string down the page.
        if not (self.filter and self.filter[0] == "album"):
            ctk.CTkLabel(row, text=ellipsize(track.get("album"), 34),
                         width=250, anchor="w", font=theme_ui.font("caption"),
                         text_color=self.theme["text_secondary"]
                         ).pack(side="right", padx=(14, 22))


        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        title = ellipsize(track.get("title") or os.path.splitext(
            os.path.basename(track["path"]))[0], 52)
        ctk.CTkLabel(box, text=title, anchor="w", justify="left",
                     font=theme_ui.font("heading"),
                     text_color=self.theme["text"]).pack(anchor="w", pady=(9, 0))
        detail = ellipsize(track.get("artist"), 44)
        ctk.CTkLabel(box, text=detail, anchor="w", justify="left",
                     font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")

        row._track_path = track["path"]
        self._rows_by_path[track["path"]] = row
        self._clickable(row, lambda p=track["path"]: self.on_play(p))
        self._menuable(row, track["path"])
        if self.playing_path == track["path"]:
            self._paint_playing(row)

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
        meta = (f"{album['artist']} · {plural(album['n'], 'track')}"
                f" · {fmt_time(album['total'])}")
        if album.get("year"):
            meta += f" · {album['year']}"
        ctk.CTkLabel(box, text=meta, anchor="w", font=ctk.CTkFont(size=12),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")
        self._clickable(row, lambda a=album["album"], r=album["artist"]:
                        self.open_album(a, r))

    def _duplicate_group(self, group):
        """One suspected duplicate: the copy to keep, and the ones to drop."""
        card = ctk.CTkFrame(self.frame, fg_color=self.theme["surface"],
                            corner_radius=theme_ui.RADIUS)
        card.pack(fill="x", padx=6, pady=6)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text=f"{group['artist']} — {group['title']}",
                     anchor="w", font=theme_ui.font("heading"),
                     text_color=self.theme["text"]).pack(side="left")
        ctk.CTkLabel(head,
                     text=f"frees {group['reclaim'] / 1e6:.0f} MB",
                     anchor="e", font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(side="right")

        def describe(row):
            bits = []
            if row.get("bitrate"):
                bits.append(f"{row['bitrate'] // 1000} kbps")
            if row.get("duration"):
                bits.append(fmt_time(row["duration"]))
            if row.get("size"):
                bits.append(f"{row['size'] / 1e6:.0f} MB")
            return "  ·  ".join(bits)

        keep = group["keep"]
        keep_row = ctk.CTkFrame(card, fg_color="transparent")
        keep_row.pack(fill="x", padx=14, pady=2)
        ctk.CTkLabel(keep_row, text="KEEP", width=54, anchor="w",
                     font=theme_ui.font("small"),
                     text_color=self.theme["accent"]).pack(side="left")
        ctk.CTkLabel(keep_row, text=os.path.basename(keep["path"]), anchor="w",
                     font=theme_ui.font("caption"),
                     text_color=self.theme["text"]).pack(side="left", fill="x",
                                                         expand=True)
        ctk.CTkLabel(keep_row, text=describe(keep), anchor="e",
                     font=theme_ui.font("small"),
                     text_color=self.theme["text_secondary"]).pack(side="right")

        for extra in group["extra"]:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=2)
            var = ctk.BooleanVar(value=True)
            self.dup_marks[extra["path"]] = var
            ctk.CTkCheckBox(row, text="", variable=var, width=54,
                            checkbox_width=18, checkbox_height=18,
                            fg_color=self.theme["accent"],
                            hover_color=self.theme["accent_hover"]).pack(side="left")
            ctk.CTkLabel(row, text=os.path.basename(extra["path"]), anchor="w",
                         font=theme_ui.font("caption"),
                         text_color=self.theme["text_secondary"]).pack(
                             side="left", fill="x", expand=True)
            ctk.CTkLabel(row, text=describe(extra), anchor="e",
                         font=theme_ui.font("small"),
                         text_color=self.theme["text_secondary"]).pack(side="right")

        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _playlist_row(self, playlist):
        row = self._row(72)
        art = ctk.CTkLabel(row, text="", width=56, height=56, corner_radius=6)
        art.pack(side="left", padx=(10, 14))
        self.request_thumb(playlist.get("cover_path"), 56, art)

        ctk.CTkLabel(row, text=fmt_time(playlist.get("total")), width=56,
                     anchor="e", font=theme_ui.font("time"),
                     text_color=self.theme["text_secondary"]
                     ).pack(side="right", padx=(8, 16))

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=playlist["name"], anchor="w",
                     font=theme_ui.font("heading"),
                     text_color=self.theme["text"]).pack(anchor="w", pady=(14, 0))
        detail = playlist.get("hint") or plural(playlist["n"], "track")
        if playlist.get("smart"):
            detail = "%s  ·  %s" % (plural(playlist["n"], "track"), detail)
        ctk.CTkLabel(box, text=detail, anchor="w",
                     font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")

        if playlist.get("gone"):
            self._clickable(row, self.open_vanished)
            return
        rule = playlist.get("smart")
        if rule is not None:
            self._clickable(row, lambda r=rule: self.open_smart(r))
        else:
            self._clickable(row, lambda pid=playlist["id"],
                            name=playlist["name"]: self.open_playlist(pid, name))

    def _vanished_row(self, entry):
        """One track that is no longer on Spotify.

        Playable if it was downloaded before it went, which is the entire
        point of noticing; dimmed and unplayable if it was not, because
        saying so is more use than leaving a row that does nothing.
        """
        path = entry.get("path")
        have = bool(path and os.path.exists(path))
        row = self._row()

        art = ctk.CTkLabel(row, text="", width=ART, height=ART)
        art.pack(side="left", padx=(10, 12))
        self.request_thumb(path if have else None, ART, art)

        ctk.CTkLabel(row, text="kept" if have else "lost", width=44,
                     anchor="e", font=theme_ui.font("caption"),
                     text_color=(self.theme["accent"] if have
                                 else self.theme["text_secondary"])).pack(
                                     side="right", padx=(6, 14))

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=entry.get("title") or "Unknown track",
                     anchor="w", font=theme_ui.font("heading"),
                     text_color=(self.theme["text"] if have
                                 else self.theme["text_secondary"])).pack(
                                     anchor="w", pady=(8, 0))
        went = ("went grey on Spotify" if entry.get("reason") == "unavailable"
                else "no longer in the playlist")
        ctk.CTkLabel(box, text="%s  \u00b7  %s  \u00b7  %s"
                              % (entry.get("artist") or "", went,
                                 entry.get("playlist") or ""),
                     anchor="w", font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")

        if have:
            self._rows_by_path[path] = row
            self._clickable(row, lambda p=path: self.on_play(p))
            self._menuable(row, path)

    def open_vanished(self):
        self.vanished = True
        self.playlist_id = None
        self.smart = None
        self.invalidate()
        self.render()

    def open_playlist(self, playlist_id, name):
        self.playlist_id = playlist_id
        self.playlist_name = name
        self.invalidate()
        self.render()

    def open_smart(self, rule):
        self.smart = rule
        self.playlist_id = None
        self.invalidate()
        self.render()

    def close_playlist(self):
        self.playlist_id = None
        self.playlist_name = None
        self.vanished = False
        self.smart = None
        self.invalidate()
        self.render()

    def _artist_album_row(self, album):
        """One album on an artist's page: large cover, title, year, length."""
        row = self._row(76)
        art = ctk.CTkLabel(row, text="", width=60, height=60, corner_radius=6)
        art.pack(side="left", padx=(10, 14))
        self.request_thumb(album.get("cover_path"), 60, art)

        ctk.CTkLabel(row, text=fmt_time(album.get("total")), width=56, anchor="e",
                     font=theme_ui.font("time"),
                     text_color=self.theme["text_secondary"]
                     ).pack(side="right", padx=(8, 16))

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(box, text=album["album"], anchor="w", justify="left",
                     font=theme_ui.font("heading"),
                     text_color=self.theme["text"]).pack(anchor="w", pady=(16, 0))
        bits = [plural(album["n"], "track")]
        if album.get("year"):
            bits.insert(0, str(album["year"]))
        ctk.CTkLabel(box, text="  ·  ".join(bits), anchor="w",
                     font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")

        self._clickable(row, lambda a=album["album"], r=album["artist"]:
                        self.open_album_of(a, r))

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
        ctk.CTkLabel(box,
                     text=f"{plural(artist['n'], 'track')} · "
                          f"{plural(artist['albums'], 'album')}",
                     anchor="w", font=theme_ui.font("caption"),
                     text_color=self.theme["text_secondary"]).pack(anchor="w")
        self._clickable(row, lambda a=artist["artist"]: self.open_artist(a))

    # ------------------------------------------------------------- cover art

    def request_thumb(self, path, size, label):
        """Load embedded cover art off the UI thread, cached by path and size.

        The placeholder tile goes down first, unconditionally. Art is decoded
        on a worker, so every row used to be an empty box that filled in a
        moment later, and scrolling a long list was a wall of pop-in.
        """
        label.configure(image=ui_widgets.placeholder_ctk(
            size, self.theme["surface"], self.theme["text"]))
        if not path:
            return
        key = (path, size)
        cached = self._art_cache.get(key)
        if cached is not None:
            label.configure(image=cached)
            return

        def work():
            try:
                data = audio_files.cover_bytes(path)
                if not data:
                    return
                img = Image.open(io.BytesIO(data)).convert("RGB").resize(
                    (size, size), Image.Resampling.LANCZOS)
            except Exception:
                return
            self.schedule(0, self._set_thumb, key, img, size, label)

        # A hundred-row list used to spawn a hundred threads at once, which
        # is why covers trickled in. Four workers keep the disk busy without
        # the churn.
        self._art_pool.submit(work)

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


    # ------------------------------------------------------------ page tint

    def _show_crumb_bar(self):
        """The breadcrumb, sized and positioned as a page header.

        Edge to edge rather than inset, because it now carries the album's
        colour and an inset band of colour reads as a stray box.
        """
        self.crumb_bar.grid(row=1, column=0, sticky="ew", padx=0,
                            pady=(0, 8), ipady=14)

    def _page_tint(self, key, cover_path):
        """Wash the page header with the dominant colour of its cover.

        Every page in the library looked identical apart from its text. One
        band of colour taken from the artwork is enough to make an album page
        feel like that album's page.
        """
        if key == self._tint_key:
            return
        self._tint_key = key
        if key is None:
            self._set_tint(None)
            return

        if key in self._tint_cache:
            self._set_tint(self._tint_cache[key])
            return

        self._set_tint(None)
        if not cover_path:
            self._tint_cache[key] = None
            return

        def work():
            colour = None
            try:
                data = audio_files.cover_bytes(cover_path)
                if data:
                    colour = ui_widgets.dominant_colour(
                        Image.open(io.BytesIO(data)).convert("RGB"))
            except Exception:
                pass
            self.schedule(0, self._tint_ready, key, colour)

        self._art_pool.submit(work)

    def _tint_ready(self, key, colour):
        self._tint_cache[key] = colour
        # The user may have navigated on while colorgram was working.
        if key == self._tint_key:
            self._set_tint(colour)

    def _set_tint(self, colour):
        """Ease the header from whatever it is now to the new colour."""
        target = (ui_widgets.readable_tint(colour, self.theme["text"],
                                           self.theme["bg"])
                  if colour else self.theme["bg"])
        start = self._tint_colour or self.theme["bg"]
        self._tint_colour = target

        def step(t):
            shade = motion.blend(start, target, t)
            self.crumb_bar.configure(fg_color=shade)
            self._redraw_tint(shade)

        motion.animate(self.crumb_bar, motion.SLOW, step, name="tint")

    def _redraw_tint(self, shade=None):
        """The strip that fades the header band down into the track list.

        Drawn as an image rather than as a widget background because Tk has
        no gradients, and kept clear of the breadcrumb's own children -- a
        CustomTkinter button paints its corners with its parent's flat colour,
        so anything sitting over the gradient would show a box around itself.
        """
        shade = shade or self._tint_colour or self.theme["bg"]
        band = self._tint_band
        if band is None:
            # CustomTkinter only accepts width/height on the constructor, so
            # the strip is sized here rather than in the place() call.
            band = self._tint_band = ctk.CTkLabel(
                self.crumb_bar, text="", height=FADE_H, corner_radius=0)
        if not band.winfo_exists():
            return

        width = self.crumb_bar.winfo_width()
        if shade == self.theme["bg"] or width <= 1:
            band.place_forget()
            return

        image = ui_widgets.gradient_image(width, FADE_H, shade,
                                          self.theme["bg"])
        self._tint_image = ctk.CTkImage(light_image=image, dark_image=image,
                                        size=(width, FADE_H))
        band.configure(image=self._tint_image)
        band.place(x=0, rely=1.0, anchor="sw", relwidth=1)
        band.lower()

    def marked_duplicates(self):
        """Paths the user has ticked for removal in the Duplicates view."""
        return [p for p, var in self.dup_marks.items() if var.get()]
