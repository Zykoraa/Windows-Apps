"""Frameless modal dialogs, matching the main window.

The app deliberately sheds Windows chrome: the main window is
overrideredirect with a title bar it draws itself. The dialogs never got that
treatment. Asking for a playlist name opened a stock CTkInputDialog and the
artist album picker a bare CTkToplevel, so both arrived with a system frame, a
system title bar, square corners and a different close button -- sitting on
top of an app that had gone to some trouble not to have any of that.

These are the same shape as the main window: no system frame, a title bar
carrying the title and one close control, drag to move, Escape to cancel.
Because a toplevel is a real window, the open and close transitions can use
-alpha, which is a true opacity fade rather than the slide the in-window
overlays have to settle for.
"""

import customtkinter as ctk

import motion
import theme_ui


class ModalDialog(ctk.CTkToplevel):
    """A frameless dialog centred on its parent.

    Call show() for a blocking dialog that returns a result, or present() for
    one that stays up while work carries on behind it.
    """

    def __init__(self, parent, theme, title, size=None, body_pad=(24, 20)):
        super().__init__(parent)
        self.theme = theme
        self.result = None
        self._closing = False
        self._origin = (0, 0)

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self._set_alpha(0.0)
        if size:
            self.geometry("%dx%d" % size)

        # The outer window is the border: one pixel of it shows around the
        # shell, which is the only edge a frameless window gets.
        self.configure(fg_color=theme["surface_hover"])
        shell = ctk.CTkFrame(self, corner_radius=0, fg_color=theme["bg"])
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        self.title_bar = ctk.CTkFrame(shell, height=42, corner_radius=0,
                                      fg_color=theme["surface"])
        self.title_bar.pack(fill="x")
        self.title_bar.pack_propagate(False)

        self.title_label = ctk.CTkLabel(self.title_bar, text=title,
                                        font=theme_ui.font("heading"),
                                        text_color=theme["text"])
        self.title_label.pack(side="left", padx=16)

        self.close_btn = ctk.CTkButton(
            self.title_bar, text="✕", width=42, height=42,
            corner_radius=0, fg_color="transparent",
            hover_color="#e81123", text_color=theme["text_secondary"],
            command=self.close)
        self.close_btn.pack(side="right")

        for widget in (self.title_bar, self.title_label):
            widget.bind("<Button-1>", self._grab_origin)
            widget.bind("<B1-Motion>", self._drag)

        self.body = ctk.CTkFrame(shell, fg_color="transparent")
        self.body.pack(fill="both", expand=True,
                       padx=body_pad[0], pady=body_pad[1])

        self.bind("<Escape>", lambda _e: self.close())

    # ------------------------------------------------------------ chrome

    def _grab_origin(self, event):
        self._origin = (event.x, event.y)

    def _drag(self, event):
        self.geometry("+%d+%d" % (event.x_root - self._origin[0],
                                  event.y_root - self._origin[1]))

    def _set_alpha(self, value):
        try:
            self.attributes("-alpha", max(0.0, min(1.0, value)))
        except Exception:
            pass

    def centre_on_parent(self):
        self.update_idletasks()
        parent = self.master
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except Exception:
            return
        x = px + (pw - self.winfo_width()) // 2
        # A third of the way down, not halfway: a dialog pinned to the exact
        # centre of a tall window sits lower than the eye expects.
        y = py + max(0, (ph - self.winfo_height()) // 3)
        self.geometry("+%d+%d" % (max(0, x), max(0, y)))

    # ------------------------------------------------------------- shown

    def _appear(self, modal):
        self.update_idletasks()
        self.centre_on_parent()
        try:
            self.wait_visibility()
            if modal:
                self.grab_set()
        except Exception:
            pass
        motion.animate(self, motion.FAST,
                       lambda t: self._set_alpha(t), name="fade")

    def present(self):
        """Fade in and return immediately; the dialog stays up."""
        self._appear(modal=False)
        return self

    def show(self):
        """Fade in, block until closed, and return whatever close() was given."""
        self._appear(modal=True)
        self.wait_window()
        return self.result

    def close(self, result=None):
        if self._closing:
            return
        self._closing = True
        self.result = result
        try:
            self.grab_release()
        except Exception:
            pass
        start = 1.0
        motion.animate(self, motion.FAST,
                       lambda t: self._set_alpha(start - t),
                       done=self.destroy, name="fade")


class TextPrompt(ModalDialog):
    """One line of input, a confirm and a cancel."""

    def __init__(self, parent, theme, title, prompt, placeholder="",
                 initial="", confirm="Save"):
        super().__init__(parent, theme, title)
        t = theme

        ctk.CTkLabel(self.body, text=prompt, anchor="w",
                     font=theme_ui.font("body"),
                     text_color=t["text_secondary"]).pack(anchor="w",
                                                          pady=(0, 8))

        self.entry = ctk.CTkEntry(self.body, width=360, height=38,
                                  placeholder_text=placeholder,
                                  corner_radius=theme_ui.RADIUS,
                                  border_width=1, font=theme_ui.font("body"),
                                  fg_color=t["surface"], text_color=t["text"])
        self.entry.pack(fill="x")
        if initial:
            self.entry.insert(0, initial)
        self.entry.bind("<Return>", lambda _e: self._submit())

        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(row, text="Cancel", width=96, height=36,
                      corner_radius=theme_ui.RADIUS_PILL, border_width=1,
                      fg_color="transparent", border_color=t["surface_hover"],
                      hover_color=t["surface_hover"], text_color=t["text"],
                      font=theme_ui.font("body_med"),
                      command=self.close).pack(side="right")
        ctk.CTkButton(row, text=confirm, width=110, height=36,
                      corner_radius=theme_ui.RADIUS_PILL,
                      fg_color=t["accent"], hover_color=t["accent_hover"],
                      text_color=t["bg"], font=theme_ui.font("body_med"),
                      command=self._submit).pack(side="right", padx=(0, 10))

        self.after(60, self.entry.focus_set)

    def _submit(self):
        self.close(self.entry.get().strip() or None)


def prompt_text(parent, theme, title, prompt, placeholder="", initial="",
                confirm="Save"):
    """Ask for one line of text. Returns the string, or None if cancelled."""
    return TextPrompt(parent, theme, title, prompt, placeholder, initial,
                      confirm).show()


class PlaylistPicker(ModalDialog):
    """Choose which Spotify playlists to bring across.

    A checklist rather than one playlist at a time: somebody importing an
    account has ten or thirty of these, and confirming the same dialog thirty
    times is not a feature.

    Nothing starts out ticked. Ticking everything by default would be one
    click away from queueing several thousand downloads, which is not a
    decision to make on somebody's behalf -- so the running total sits above
    the list and updates as they choose.
    """

    def __init__(self, parent, theme, playlists):
        super().__init__(parent, theme, "Import from Spotify", size=(620, 640))
        t = theme
        self._rows = []

        ctk.CTkLabel(
            self.body, justify="left", wraplength=560, anchor="w",
            font=theme_ui.font("body"), text_color=t["text_secondary"],
            text=("Each one becomes a playlist here, in the same order. "
                  "Tracks you already have go in straight away, and the rest "
                  "are downloaded and slotted into place as they arrive.")
        ).pack(fill="x", pady=(0, 16))

        head = ctk.CTkFrame(self.body, fg_color="transparent")
        head.pack(fill="x")
        self.count_label = ctk.CTkLabel(head, text="", anchor="w",
                                        font=theme_ui.font("body_med"),
                                        text_color=t["text"])
        self.count_label.pack(side="left")

        for label, value in (("None", False), ("All", True)):
            ctk.CTkButton(
                head, text=label, width=62, height=28,
                corner_radius=theme_ui.RADIUS_PILL, border_width=1,
                fg_color="transparent", border_color=t["surface_hover"],
                hover_color=t["surface_hover"], text_color=t["text_secondary"],
                font=theme_ui.font("caption"),
                command=lambda v=value: self._set_all(v)).pack(side="right",
                                                               padx=(6, 0))

        self.list = ctk.CTkScrollableFrame(
            self.body, fg_color=t["surface"], corner_radius=theme_ui.RADIUS,
            height=356)
        self.list.pack(fill="both", expand=True, pady=(10, 0))

        for playlist in playlists:
            self._add_row(playlist)
        blocked = [p for p in playlists if not p.get("readable", True)]
        if blocked:
            ctk.CTkLabel(
                self.body, justify="left", wraplength=560, anchor="w",
                font=theme_ui.font("caption"), text_color=t["text_secondary"],
                text=("%d playlist%s you follow but do not own cannot be "
                      "read: Spotify closed that to apps in 2024. Copying one "
                      "into a playlist of your own makes it importable."
                      % (len(blocked), "" if len(blocked) == 1 else "s"))
            ).pack(fill="x", pady=(10, 0))
        if not playlists:
            ctk.CTkLabel(self.list, font=theme_ui.font("body"),
                         text_color=t["text_secondary"],
                         text="No playlists on this account.").pack(pady=40)

        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=(18, 0))
        ctk.CTkButton(row, text="Cancel", width=96, height=36,
                      corner_radius=theme_ui.RADIUS_PILL, border_width=1,
                      fg_color="transparent", border_color=t["surface_hover"],
                      hover_color=t["surface_hover"], text_color=t["text"],
                      font=theme_ui.font("body_med"),
                      command=self.close).pack(side="right")
        self.import_btn = ctk.CTkButton(
            row, text="Import", width=150, height=36,
            corner_radius=theme_ui.RADIUS_PILL, fg_color=t["accent"],
            hover_color=t["accent_hover"], text_color=t["bg"],
            font=theme_ui.font("body_med"), command=self._submit)
        self.import_btn.pack(side="right", padx=(0, 10))

        self._sync()

    def _add_row(self, playlist):
        t = self.theme
        # Spotify serves the contents of playlists you own or collaborate on,
        # and answers 403 for the rest however public they are. Offering one
        # of those would import a playlist with nothing in it.
        readable = playlist.get("readable", True)

        row = ctk.CTkFrame(self.list, fg_color="transparent", height=36)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)

        var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            row, text=playlist["name"], variable=var, command=self._sync,
            state="normal" if readable else "disabled",
            font=theme_ui.font("body"),
            text_color=t["text"] if readable else t["text_secondary"],
            fg_color=t["accent"], hover_color=t["accent_hover"],
            checkmark_color=t["bg"], border_color=t["surface_hover"],
            checkbox_width=19, checkbox_height=19, border_width=2,
            corner_radius=6).pack(side="left", padx=(12, 8))

        total = playlist.get("total") or 0
        if not readable:
            detail = "Spotify won't share this one"
        else:
            detail = "%d track%s" % (total, "" if total == 1 else "s")
            if not playlist.get("mine") and playlist.get("owner"):
                detail = "%s  by %s" % (detail, playlist["owner"])
        ctk.CTkLabel(row, text=detail, anchor="e",
                     font=theme_ui.font("caption"),
                     text_color=t["text_secondary"]).pack(side="right",
                                                          padx=(8, 14))
        # Only selectable rows are tracked, so All cannot tick a dead one.
        if readable:
            self._rows.append((playlist, var))

    # ------------------------------------------------------------ state

    def _chosen(self):
        return [p for p, var in self._rows if var.get()]

    def _set_all(self, value):
        for _playlist, var in self._rows:
            var.set(value)
        self._sync()

    def _sync(self):
        chosen = self._chosen()
        tracks = sum(p.get("total") or 0 for p in chosen)
        if chosen:
            self.count_label.configure(
                text="%d selected  ·  %s tracks" % (len(chosen),
                                                    "{:,}".format(tracks)))
        else:
            self.count_label.configure(text="Nothing selected")
        # Disabled rather than hidden, so the button does not move about.
        self.import_btn.configure(
            state="normal" if chosen else "disabled",
            text="Import" if not chosen else "Import %d" % len(chosen))

    def _submit(self):
        chosen = self._chosen()
        if chosen:
            self.close(chosen)


def pick_playlists(parent, theme, playlists):
    """Ask which playlists to import. Returns a list, or None if cancelled."""
    return PlaylistPicker(parent, theme, playlists).show()
