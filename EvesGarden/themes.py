"""The colour palettes, and the rules that keep them consistent.

These used to be eighteen hand-written tables of seven colours each, and they
disagreed with one another in ways that showed:

* In eleven of the eighteen, `accent_hover` was not a lighter accent at all
  but a second, unrelated accent. Kanagawa paired blue with gold 179 degrees
  away, Synthwave pink with cyan, Tokyo Night blue with green. Every button in
  the app changed colour family under the cursor, which reads as a rendering
  fault rather than a hover state.
* `surface` was on the wrong side of `bg` in Catppuccin Mocha, and close
  enough to be invisible in Spotify Classic, Rose Pine and Ayu Mirage -- so
  the bottom bar, the cards and the page all sat at the same apparent depth.
* `text_secondary` was sometimes a different hue entirely (Dracula's was
  cyan) and sometimes barely dimmer than `text` (Nord), so the same label
  carried a different amount of emphasis depending on the theme.

So a theme is now authored as the three colours that actually carry its
identity -- background, accent, text -- and everything else is derived by one
set of rules. Consistency stops being something each palette has to remember,
and fixing a rule fixes all eighteen at once.
"""

# How far each derived colour travels, as a fraction toward its target.
ELEVATION = 0.075          # surface, lifted off the background toward the ink
ELEVATION_HOVER = 0.145    # surface_hover, one step further
ACCENT_LIFT = 0.22         # accent_hover, toward the ink rather than a new hue

# text_secondary is faded toward the background until it lands on a contrast
# ratio, not by a fixed fraction. How far you can fade depends on how much
# contrast the pair started with, so one fixed fade gave the same label 8:1 in
# Spotify Classic and 3:1 in Rose Pine Dawn -- the emphasis a reader saw
# depended on the theme.
SECONDARY_TARGET = 5.2
SECONDARY_CEILING = 0.5


def _rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def mix(a, b, t):
    """Blend two "#rrggbb" colours; t=0 is a, t=1 is b."""
    ca, cb = _rgb(a), _rgb(b)
    return _hex(ca[i] + (cb[i] - ca[i]) * t for i in range(3))


def luminance(value):
    """WCAG relative luminance."""
    def channel(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a, b):
    """WCAG contrast ratio between two colours, 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def fade_to_contrast(text, bg, target=SECONDARY_TARGET,
                     ceiling=SECONDARY_CEILING):
    """Fade `text` toward `bg` as far as `target` contrast allows."""
    best, low, high = text, 0.0, ceiling
    for _ in range(24):           # contrast falls monotonically with the fade
        mid = (low + high) / 2
        candidate = mix(text, bg, mid)
        if contrast(candidate, bg) >= target:
            best, low = candidate, mid
        else:
            high = mid
    return best


def build(bg, accent, text):
    """A full palette from the three colours that carry a theme's identity."""
    return {
        "bg": bg,
        "surface": mix(bg, text, ELEVATION),
        "surface_hover": mix(bg, text, ELEVATION_HOVER),
        "accent": accent,
        # Toward the theme's own ink, so it brightens on a dark theme and
        # deepens on a light one, and never changes hue.
        "accent_hover": mix(accent, text, ACCENT_LIFT),
        "text": text,
        "text_secondary": fade_to_contrast(text, bg),
    }


# name -> (background, accent, text). Everything else follows.
SEEDS = {
    "Spotify Classic":  ("#121212", "#1db954", "#ffffff"),
    "Catppuccin Mocha": ("#1e1e2e", "#cba6f7", "#cdd6f4"),
    "Osaka Forest":     ("#2b3339", "#a7c080", "#d3c6aa"),
    "Dracula":          ("#282a36", "#bd93f9", "#f8f8f2"),
    "Nord":             ("#2e3440", "#88c0d0", "#eceff4"),
    "Tokyo Night":      ("#1a1b26", "#7aa2f7", "#c0caf5"),
    "Gruvbox":          ("#282828", "#fabd2f", "#ebdbb2"),
    "Rose Pine":        ("#191724", "#ebbcba", "#e0def4"),
    "Everforest":       ("#272e33", "#a7c080", "#d3c6aa"),
    "Solarized":        ("#002b36", "#2aa198", "#eee8d5"),
    "Synthwave":        ("#1a1327", "#ff2e97", "#f7f2ff"),
    "Monokai":          ("#221f22", "#a9dc76", "#fcfcfa"),
    "Kanagawa":         ("#1f1f28", "#7e9cd8", "#dcd7ba"),
    "Ayu Mirage":       ("#1f2430", "#ffcc66", "#cbccc6"),
    "Deep Ocean":       ("#0b1622", "#4dd0e1", "#e3f2fd"),
    "Ember":            ("#1c1614", "#ff7043", "#f5ece7"),
    "Rose Pine Dawn":   ("#faf4ed", "#d7827e", "#575279"),
    "Nordic Light":     ("#eceff4", "#5e81ac", "#2e3440"),
}

THEMES = {name: build(*seed) for name, seed in SEEDS.items()}

KEYS = ("bg", "surface", "surface_hover", "accent", "accent_hover",
        "text", "text_secondary")


def problems(theme):
    """Everything wrong with one palette, as a list of strings.

    Used by the tests. The thresholds are the ones the old hand-written
    tables actually failed.
    """
    found = []
    missing = [k for k in KEYS if k not in theme]
    if missing:
        return ["missing keys: %s" % ", ".join(missing)]

    if contrast(theme["text"], theme["bg"]) < 4.5:
        found.append("text on bg is below 4.5:1")
    if contrast(theme["text_secondary"], theme["bg"]) < 3.0:
        found.append("secondary text on bg is below 3:1")
    if contrast(theme["text"], theme["surface"]) < 4.5:
        found.append("text on surface is below 4.5:1")

    # Elevation has to be visible, and has to go the same way at both steps.
    ink_is_light = luminance(theme["text"]) > luminance(theme["bg"])
    for higher, lower in (("surface", "bg"), ("surface_hover", "surface")):
        delta = luminance(theme[higher]) - luminance(theme[lower])
        if (delta <= 0) if ink_is_light else (delta >= 0):
            found.append("%s is on the wrong side of %s" % (higher, lower))
        if max(abs(a - b) for a, b in zip(_rgb(theme[higher]),
                                          _rgb(theme[lower]))) < 6:
            found.append("%s is indistinguishable from %s" % (higher, lower))

    # The hover accent must be the same colour, brighter -- not another one.
    ar, ag, ab = _rgb(theme["accent"])
    hr, hg, hb = _rgb(theme["accent_hover"])
    if max(abs(ar - hr), abs(ag - hg), abs(ab - hb)) < 4:
        found.append("accent_hover is not distinguishable from accent")
    span = max(ar, ag, ab) - min(ar, ag, ab)
    hover_span = max(hr, hg, hb) - min(hr, hg, hb)
    if span > 30 and hover_span < span * 0.35:
        found.append("accent_hover has lost the accent's hue")
    return found
