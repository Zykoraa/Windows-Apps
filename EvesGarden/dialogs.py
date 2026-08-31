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
