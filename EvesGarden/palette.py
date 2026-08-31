"""One search box for everything: the library, Spotify, and the app itself.

Searching used to mean knowing where to look first. The library filter sat in
the header and only matched what had already been downloaded; finding
something you did not own meant opening the downloader and typing the same
words into a second box. Two fields, two result lists, two mental models for
one question -- "where is this song".

This is the single entry point. Local matches resolve instantly off the SQLite
index and are listed first, because a track you already own is almost always
the one you meant; Spotify is queried in the background and appended when it
answers. Commands are matched too, so the palette also reaches the parts of
the app that have no button.
"""

import threading

import customtkinter as ctk

import motion
import theme_ui
import ui_widgets

CARD_W = 680
CARD_H = 530
LIST_H = 400
# Clear of the library header, so the palette reads as floating over the app
# rather than colliding with the controls behind it.
CARD_Y = 84

# Deliberately small. Seven local matches filled the visible list on their own,
# which pushed the Spotify group below the fold -- and a unified search that
# hides half its results is just the library filter with extra steps.
LOCAL_LIMIT = 4
REMOTE_LIMIT = 5
COMMAND_LIMIT = 4
DEBOUNCE_MS = 320
ART = 34


def _fits(query, text):
    """Every word of the query appears somewhere in the text."""
    text = (text or "").lower()
    return all(word in text for word in query.lower().split())


class CommandPalette:
    """A frameless finder over the whole window.

    Owns no application state: everything it can do arrives as a callback, the
    same arrangement LibraryView uses, so this file stays testable and gui.py
    stays out of it.
    """

    def __init__(self, parent, theme, index, discover, schedule,
                 on_play, on_download, commands=(), thumb_loader=None):
        self.parent = parent
        self.theme = theme
        self.index = index
        self.discover = discover
        self.schedule = schedule            # (delay, fn, *args), thread-safe
        self.on_play = on_play
        self.on_download = on_download
        self.commands = list(commands)      # (label, hint, callable)
        self.thumb_loader = thumb_loader

        self.visible = False
        self._items = []
        self._rows = []
        self._selected = 0
        self._token = 0                     # drops results for stale queries
        self._debounce = None
        self._remote = {}                   # query -> spotify results
        self._built = False

    # ------------------------------------------------------------- building

    def _build(self):
        t = self.theme
        # CustomTkinter only accepts width/height on the constructor, so the
        # card is sized here and place() only ever positions it.
        self.card = ctk.CTkFrame(self.parent, corner_radius=16,
                                 width=CARD_W, height=CARD_H,
                                 fg_color=t["surface"],
                                 border_width=1,
                                 border_color=t["surface_hover"])
        self.card.pack_propagate(False)

        head = ctk.CTkFrame(self.card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 8))

        self.icon = ui_widgets.glyph_canvas(
            head, "search", size=22, colour=t["text_secondary"],
            background=t["surface"], stroke=1.8)
        self.icon.pack(side="left", padx=(2, 12))

        self.entry = ctk.CTkEntry(
            head, placeholder_text="Search your library, Spotify, or type a command",
            border_width=0, fg_color="transparent", height=34,
            font=theme_ui.font("body", size=17), text_color=t["text"])
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", self._on_typed)
        for seq, fn in (("<Down>", lambda e: self._move(1)),
                        ("<Up>", lambda e: self._move(-1)),
                        ("<Return>", lambda e: self._activate()),
                        ("<Escape>", lambda e: self.close())):
            self.entry.bind(seq, fn)

        self.rule = ctk.CTkFrame(self.card, height=1, corner_radius=0,
                                 fg_color=t["surface_hover"])
        self.rule.pack(fill="x", padx=1)

        self.list = ctk.CTkScrollableFrame(self.card, fg_color=t["surface"],
                                           height=LIST_H)
        self.list.pack(fill="both", expand=True, padx=6, pady=(6, 4))

        self.footer = ctk.CTkLabel(
            self.card, anchor="w", font=theme_ui.font("small"),
            text_color=t["text_secondary"],
            text="↑↓  move      ↵  select      esc  close")
        self.footer.pack(fill="x", padx=20, pady=(0, 12))
        self._built = True

    # -------------------------------------------------------------- opening

    def toggle(self):
        self.close() if self.visible else self.open()

    def open(self):
        if not self._built:
            self._build()
        if self.visible:
            self.entry.focus_set()
            return
        self.visible = True
        self.card.place(relx=0.5, y=CARD_Y, anchor="n")
        self.card.lift()
        # Drops in from slightly above rather than appearing, which is the
        # difference between a panel and a thing that was already there.
        motion.animate(self.card, motion.FAST,
                       lambda p: self.card.place_configure(
                           y=int(CARD_Y - 30 + 30 * p)),
                       name="drop")
        self.entry.delete(0, "end")
        self.entry.focus_set()
        self._refresh()

    def close(self):
        if not self.visible:
            return
        self.visible = False
        motion.cancel(self.card, "drop")
        self.card.place_forget()
        try:
            self.parent.focus_set()
        except Exception:
            pass
        return "break"

    def set_theme(self, theme):
        self.theme = theme
        if not self._built:
            return
        t = theme
        self.card.configure(fg_color=t["surface"], border_color=t["surface_hover"])
        self.rule.configure(fg_color=t["surface_hover"])
        self.list.configure(fg_color=t["surface"])
        self.entry.configure(text_color=t["text"])
        self.icon.configure(bg=t["surface"])
        self.footer.configure(text_color=t["text_secondary"])
        if self.visible:
            self._refresh()

    # ------------------------------------------------------------ searching

    def _on_typed(self, event=None):
        if event is not None and event.keysym in (
                "Up", "Down", "Return", "Escape"):
            return
        self._refresh()

        query = self.entry.get().strip()
        if len(query) < 2 or self.discover is None:
            return
        if query in self._remote:
            return
        # Spotify is a network round trip, so it waits for a pause in typing.
        if self._debounce:
            try:
                self.parent.after_cancel(self._debounce)
            except Exception:
                pass
        self._debounce = self.schedule(DEBOUNCE_MS, self._search_remote, query)

    def _search_remote(self, query):
        token = self._token

        def work():
            try:
                found = self.discover.search(query, limit=REMOTE_LIMIT)
            except Exception:
                found = []
            self.schedule(0, self._remote_ready, query, found, token)

        threading.Thread(target=work, daemon=True).start()

    def _remote_ready(self, query, found, token):
        if token != self._token or not self.visible:
            return                      # the query moved on while we waited
        if len(self._remote) > 40:
            self._remote.clear()
        self._remote[query] = found
        if self.entry.get().strip() == query:
            self._refresh(keep_selection=True)

    # ------------------------------------------------------------- results

    def _collect(self, query):
        items = []
        if not query:
            for label, hint, action in self.commands[:COMMAND_LIMIT]:
                items.append({"kind": "command", "group": "Actions",
                              "title": label, "sub": hint, "hint": "↵",
                              "action": action})
            for row in self.index.tracks(sort="Recently played",
                                         played_only=True)[:LOCAL_LIMIT]:
                items.append(self._local_item(row, group="Recent"))
            return items

        matched = [c for c in self.commands if _fits(query, c[0])]
        for label, hint, action in matched[:COMMAND_LIMIT]:
            items.append({"kind": "command", "group": "Actions",
                          "title": label, "sub": hint, "hint": "↵",
                          "action": action})

        try:
            local = self.index.tracks(search=query)[:LOCAL_LIMIT]
        except Exception:
            local = []
        for row in local:
            items.append(self._local_item(row))

        owned = {(r.get("title") or "").lower() + "|" +
                 (r.get("artist") or "").lower() for r in local}
        for track in self._remote.get(query) or []:
            # No point offering to download something already sitting in the
            # library three rows further up.
            key = track["title"].lower() + "|" + track["artist"].lower()
            if key in owned:
                continue
            items.append({
                "kind": "remote", "group": "On Spotify",
                "title": track["title"],
                "sub": "  ·  ".join(x for x in (track["artist"],
                                                     track["album"]) if x),
                "hint": "↵ download", "track": track,
                "cover_url": track.get("cover_url"),
            })
        return items

    def _local_item(self, row, group="In your library"):
        return {
            "kind": "local", "group": group,
            "title": row.get("title") or row["path"].rsplit("\\", 1)[-1],
            "sub": "  ·  ".join(x for x in ((row.get("artist") or ""),
                                                 (row.get("album") or "")) if x),
            "hint": "↵ play", "path": row["path"],
        }

    def _refresh(self, keep_selection=False):
        if not self._built:
            return
        query = self.entry.get().strip()
        self._token += 1
        previous = self._selected
        self._items = self._collect(query)
        self._selected = min(previous, len(self._items) - 1) if keep_selection else 0
        self._selected = max(0, self._selected)
        self._render()

    def _render(self):
        for widget in self.list.winfo_children():
            widget.destroy()
        self._rows = []

        if not self._items:
            ctk.CTkLabel(self.list, text="Nothing matches that.",
                         font=theme_ui.font("body"),
                         text_color=self.theme["text_secondary"]).pack(pady=26)
            return

        group = None
        for i, item in enumerate(self._items):
            if item["group"] != group:
                group = item["group"]
                ctk.CTkLabel(self.list, text=group.upper(), anchor="w",
                             font=theme_ui.font("small"),
                             text_color=self.theme["text_secondary"]).pack(
                                 anchor="w", padx=14, pady=(10, 2))
            self._rows.append(self._row(i, item))
        self._paint_selection()

    def _row(self, index, item):
        t = self.theme
        row = ctk.CTkFrame(self.list, fg_color="transparent", height=48,
                           corner_radius=10)
        row.pack(fill="x", padx=6, pady=1)
        row.pack_propagate(False)

        art = ctk.CTkLabel(row, text="", width=ART, height=ART)
        art.pack(side="left", padx=(10, 12))
        art.configure(image=ui_widgets.placeholder_ctk(ART, t["surface"],
                                                       t["text"]))
        if item["kind"] == "local" and self.thumb_loader:
            self.thumb_loader(item["path"], ART, art)
        elif item["kind"] == "remote" and item.get("cover_url"):
            self._load_remote_art(item["cover_url"], art)

        hint = ctk.CTkLabel(row, text=item["hint"], anchor="e",
                            font=theme_ui.font("small"),
                            text_color=t["text_secondary"])
        hint.pack(side="right", padx=(8, 14))

        box = ctk.CTkFrame(row, fg_color="transparent")
        box.pack(side="left", fill="both", expand=True)
        title = ctk.CTkLabel(box, text=item["title"], anchor="w",
                             font=theme_ui.font("body_med"),
                             text_color=t["text"])
        title.pack(anchor="w", pady=(6, 0))
        sub = ctk.CTkLabel(box, text=item["sub"], anchor="w",
                           font=theme_ui.font("small"),
                           text_color=t["text_secondary"])
        sub.pack(anchor="w")

        def enter(_e=None, n=index):
            self._selected = n
            self._paint_selection()

        def click(_e=None, n=index):
            self._selected = n
            self._activate()

        for widget in (row, box, title, sub, art, hint):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Button-1>", click, add="+")
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass
        row._parts = (title, sub, hint)
        return row

    def _load_remote_art(self, url, label):
        def done(image):
            self.schedule(0, self._set_art, label, image)
        try:
            self.discover.fetch_cover(url, ART, done)
        except Exception:
            pass

    def _set_art(self, label, image):
        try:
            if not label.winfo_exists():
                return
            photo = ctk.CTkImage(light_image=image, dark_image=image,
                                 size=(ART, ART))
            label._art = photo
            label.configure(image=photo)
        except Exception:
            pass

    # ----------------------------------------------------------- selection

    def _paint_selection(self):
        t = self.theme
        for i, row in enumerate(self._rows):
            if not row.winfo_exists():
                continue
            chosen = (i == self._selected)
            row.configure(fg_color=t["surface_hover"] if chosen else "transparent")
            title, sub, hint = row._parts
            title.configure(text_color=t["text"])
            sub.configure(text_color=t["text_secondary"])
            hint.configure(text_color=t["accent"] if chosen
                           else t["text_secondary"])

    def _move(self, delta):
        if not self._items:
            return "break"
        self._selected = (self._selected + delta) % len(self._items)
        self._paint_selection()
        self._scroll_into_view()
        return "break"

    def _scroll_into_view(self):
        try:
            row = self._rows[self._selected]
            canvas = self.list._parent_canvas
            content = self.list.winfo_height()
            view = canvas.winfo_height()
            if content <= view or content <= 1:
                return
            centre = row.winfo_y() + row.winfo_height() / 2
            canvas.yview_moveto(
                min(1.0, max(0.0, (centre - view / 2) / (content - view))))
        except Exception:
            pass

    def _activate(self):
        if not (0 <= self._selected < len(self._items)):
            return "break"
        item = self._items[self._selected]
        self.close()
        try:
            if item["kind"] == "command":
                item["action"]()
            elif item["kind"] == "local":
                self.on_play(item["path"])
            elif item["kind"] == "remote":
                self.on_download(item["track"])
        except Exception as e:
            print(f"palette: {e}")
        return "break"
