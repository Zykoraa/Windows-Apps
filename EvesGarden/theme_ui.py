"""Typography and spacing.

The app used to force every single label, button and entry through
JetBrainsMono NF -- a coding face. Monospace is excellent for a log and wrong
for everything else: it gives even weight to every glyph, so nothing reads as
a heading, and it made the whole app look like a terminal emulator.

This replaces that with one type scale: a proper UI face for chrome, and
monospace kept only where fixed-width actually helps (the download log, and
elapsed/remaining times so the digits stop jittering).
"""

import customtkinter as ctk

# Windows 11's UI face, then older Windows, then whatever Tk falls back to.
UI_STACK = ["Segoe UI Variable Text", "Segoe UI", "Selawik", "Helvetica"]
DISPLAY_STACK = ["Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"]
MONO_STACK = ["Cascadia Mono", "Consolas", "JetBrainsMono NF", "Courier New"]

_resolved = {}
_cache = {}


def _pick(stack):
    """First family actually installed.

    tkinter.font.families() needs a live Tk interpreter. Asked too early it
    raises or answers with a stub list, so a failed lookup must not be
    cached -- otherwise one early call pins the app to a fallback face for
    the rest of the session.
    """
    key = tuple(stack)
    if key in _resolved:
        return _resolved[key]
    try:
        import tkinter.font as tkfont
        available = set(tkfont.families())
    except Exception:
        return stack[-1]           # no root yet; ask again later
    if len(available) < 20:
        return stack[-1]           # implausible list, treat as not ready
    choice = next((f for f in stack if f in available), stack[-1])
    _resolved[key] = choice
    return choice


def ui_family():
    return _pick(UI_STACK)


def display_family():
    return _pick(DISPLAY_STACK)


def mono_family():
    return _pick(MONO_STACK)


# One scale, so sizes stop being invented at each call site.
SCALE = {
    "display":  (26, "bold",   "display"),   # overlay headings
    "title":    (20, "bold",   "display"),   # panel headings
    "heading":  (15, "bold",   "display"),   # row titles, buttons
    "body":     (13, "normal", "ui"),        # default
    "body_med": (13, "bold",   "ui"),        # emphasised body
    "caption":  (12, "normal", "ui"),        # secondary metadata
    "small":    (11, "normal", "ui"),        # status lines
    "mono":     (12, "normal", "mono"),      # the log
    "time":     (12, "normal", "mono"),      # digits that must not jitter
}


def font(role="body", size=None, weight=None):
    """A cached CTkFont for a role in the scale."""
    base_size, base_weight, family_kind = SCALE.get(role, SCALE["body"])
    size = size or base_size
    weight = weight or base_weight
    key = (family_kind, size, weight)
    if key not in _cache:
        family = {"display": display_family, "ui": ui_family,
                  "mono": mono_family}[family_kind]()
        _cache[key] = ctk.CTkFont(family=family, size=size, weight=weight)
    return _cache[key]


def clear_cache():
    """Fonts belong to a Tk interpreter; drop them if one is torn down."""
    _cache.clear()


# ------------------------------------------------------------------ spacing

# A 4px rhythm, so padding stops being a different magic number every time.
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 14, 20, 32

ROW_H = 56           # track row
ROW_ART = 40         # thumbnail inside a track row
RADIUS = 10          # cards and rows
RADIUS_PILL = 18     # buttons and entries
SIDEBAR_W = 210
BOTTOM_H = 96
