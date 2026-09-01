"""Canvas visualiser modes, as a registry rather than an if/elif chain.

These used to live in a single 180-line `_draw_bands` method, with the list
of mode names repeated in three separate places -- so adding a mode meant
three coordinated edits and renaming one silently broke the dropdown. Now a
mode is one decorated function and the name list is derived from the registry.

Colour comes from a palette rather than a single accent, so every mode can
render as a gradient, a spectrum or a single hue without each one having to
know how that works.
"""

import colorsys
import math
import random

_REGISTRY = []


def visualizer(name):
    def register(fn):
        _REGISTRY.append((name, fn))
        return fn
    return register


# The thirty-two names in the order they used to be registered, kept for one
# reason: a mode was saved as its position in this list, and every position
# has moved. Without it, anyone who had picked a visualiser would open the
# app to a different one, or to none at all.
LEGACY_NAMES = (
    "Standard Bars", "Mirrored Bars", "Circular", "Waveform", "Particles",
    "Pulse", "Radar", "Starburst", "Galaxy", "Fire", "Matrix", "Hexagon",
    "Hyperspace", "Infinity", "EKG", "Vortex", "Spectrum Ribbon",
    "Bar Reflection", "Orbit Rings", "DNA Helix", "Kaleidoscope", "Rainfall",
    "Constellation", "Tunnel", "Bloom", "Grid Pulse", "Ripple", "Aurora",
    "Lissajous", "Comet Trail", "Equaliser Wall", "Sunburst",
)


def resolve(saved):
    """The index for whatever is in settings -- a name now, an index once.

    Anything unrecognised, including a mode that has since been removed,
    comes back as the first one rather than as an error or a wrap-around to
    something arbitrary.
    """
    available = names()
    if isinstance(saved, str):
        return available.index(saved) if saved in available else 0
    try:
        index = int(saved)
    except (TypeError, ValueError):
        return 0
    if 0 <= index < len(LEGACY_NAMES):
        legacy = LEGACY_NAMES[index]
        return available.index(legacy) if legacy in available else 0
    return 0


def names():
    """Mode names in registration order; the single source of truth."""
    return [name for name, _ in _REGISTRY]


# ---------------------------------------------------------------- palettes

def _hex(r, g, b):
    return f"#{max(0, min(255, int(r))):02x}{max(0, min(255, int(g))):02x}{max(0, min(255, int(b))):02x}"


def _parse(colour):
    colour = colour.lstrip("#")
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _hsv(h, s, v):
    return _hex(*(c * 255 for c in colorsys.hsv_to_rgb(h % 1.0, s, v)))


def _shift(base, hue_delta, sat=None, val=None):
    """Rotate a base colour around the wheel, keeping it recognisable."""
    r, g, b = (c / 255 for c in _parse(base))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return _hsv(h + hue_delta, sat if sat is not None else s,
                val if val is not None else max(0.35, v))


def _ramp(t, stops):
    """Sample a list of hex stops at 0..1."""
    t = max(0.0, min(1.0, t))
    span = len(stops) - 1
    i = min(int(t * span), span - 1)
    f = t * span - i
    a, b = _parse(stops[i]), _parse(stops[i + 1])
    return _hex(*(a[c] + (b[c] - a[c]) * f for c in range(3)))


PALETTES = {
    # Every entry maps (position 0..1, base accent, time) -> hex.
    "Accent":     lambda t, base, now: base,
    "Album art":  lambda t, base, now: base,          # base is the cover colour
    "Accent Glow": lambda t, base, now: _shift(base, 0.0, val=0.45 + 0.55 * t),
    "Spectrum":   lambda t, base, now: _hsv(t, 0.85, 1.0),
    "Rainbow Flow": lambda t, base, now: _hsv(t + now * 0.12, 0.82, 1.0),
    "Sunset":     lambda t, base, now: _ramp(t, ["#FFD166", "#F77F00", "#D62828", "#8E2157"]),
    "Ocean":      lambda t, base, now: _ramp(t, ["#7BF1E1", "#22A6B3", "#1B5299", "#0B1E45"]),
    "Neon":       lambda t, base, now: _ramp(t, ["#00F5D4", "#00BBF9", "#9B5DE5", "#F15BB5"]),
    "Ember":      lambda t, base, now: _ramp(t, ["#FFF3B0", "#FFB703", "#FB5607", "#6A040F"]),
    "Forest":     lambda t, base, now: _ramp(t, ["#D8F3DC", "#74C69D", "#2D6A4F", "#081C15"]),
    "Ultraviolet": lambda t, base, now: _ramp(t, ["#F1E4FF", "#B388EB", "#7B2CBF", "#240046"]),
    "Ice":        lambda t, base, now: _ramp(t, ["#FFFFFF", "#CAF0F8", "#48CAE4", "#0077B6"]),
    "Mono":       lambda t, base, now: _hex(*(60 + 195 * t,) * 3),
}


def palette_names():
    return list(PALETTES)


class _Peaks:
    """Per-band peak hold, which is what makes an analyser read as one.

    Nothing here kept any state between frames, so a "peak marker" could only
    ever be drawn relative to the bar it sat above -- the old one was a fixed
    offset with a sine wobble on it, which tracked the bar exactly and
    jittered while it did. A real peak holds the loudest the band has been,
    waits, and then falls, so a transient leaves a mark still visible after
    the bar under it has dropped away.
    """

    HOLD = 0.32          # seconds a peak stays put before it starts falling
    FALL = 1.15          # and then how much of full scale it loses a second

    def __init__(self):
        self.values = []
        self._until = []
        self._at = None

    def update(self, bands, now):
        n = len(bands)
        # A different band count, or time going backwards, means whatever was
        # held belongs to something else.
        if len(self.values) != n or self._at is None or now < self._at:
            self.values = list(bands)
            self._until = [now + self.HOLD] * n
            self._at = now
            return self.values

        # Clamped: a paused visualiser can come back to a huge delta, and one
        # frame should never drop every peak to the floor.
        step = max(0.0, min(0.25, now - self._at)) * self.FALL
        self._at = now
        for i, energy in enumerate(bands):
            if energy >= self.values[i]:
                self.values[i] = energy
                self._until[i] = now + self.HOLD
            elif now >= self._until[i]:
                self.values[i] = max(energy, self.values[i] - step)
        return self.values


_peaks = _Peaks()


class Frame:
    """Everything a mode needs to draw one frame."""

    __slots__ = ("canvas", "bands", "accent", "width", "height",
                 "cx", "cy", "n", "avg", "now", "bar_width", "_palette",
                 "_ring", "peaks")

    def __init__(self, canvas, bands, accent, width, height, now, palette,
                 peaks=()):
        self.canvas = canvas
        self.bands = bands
        self.accent = accent
        self.width = width
        self.height = height
        self.cx = width / 2
        self.cy = height / 2
        self.n = len(bands)
        self.avg = sum(bands) / self.n if self.n else 0.0
        self.now = now
        self.bar_width = width / self.n if self.n else width
        self._palette = PALETTES.get(palette, PALETTES["Accent"])
        self._ring = None
        # Held peaks for this frame, one per band. Worked out once for the
        # frame rather than per mode, so a mode reading them cannot advance
        # the decay by reading them twice.
        self.peaks = peaks or bands

    def colour(self, t=0.0):
        """Colour at position t (0..1) through the palette."""
        try:
            return self._palette(t, self.accent, self.now)
        except Exception:
            return self.accent

    def band_colour(self, i):
        return self.colour(i / max(1, self.n - 1))

    # ------------------------------------------------------ radial helpers

    @property
    def radius(self):
        """Usable radius for anything drawn around the centre.

        Modes used to hard-code radii -- 30, 50, 100 -- picked against the
        full-screen canvas. On any other size they either collapsed into a
        blob at the middle or ran off the edge.
        """
        return min(self.width, self.height) / 2 * 0.88

    @property
    def ring(self):
        """The spectrum mirrored, for anything drawn round a circle.

        Mapping band index straight onto angle gave every radial mode a
        lopsided fan: the low bands are loud and the high bands quiet, so one
        side was long spikes and the other side nothing. Mirroring makes the
        figure symmetric, which is what these were drawn to look like, and it
        puts the bass at the top and bottom rather than all down one side.
        """
        if self._ring is None:
            half = list(self.bands)
            self._ring = half + half[::-1]
        return self._ring

    def ring_colour(self, i):
        """Palette position for a mirrored ring, symmetric about the centre."""
        n = len(self.ring)
        return self.colour(1 - abs(1 - 2 * i / max(1, n - 1)))

    @staticmethod
    def px(value, floor=1):
        """A canvas width or radius as a sane integer."""
        return max(floor, int(round(value)))

    # ------------------------------------------------------------- shapes

    CAP_H = 3
    # A gentle round, not a dome: taking the radius from the bar's own height
    # turned every quiet band into a ball.
    BAR_RADIUS = 4

    def bar(self, x0, x1, top, bottom, colour, dome="top"):
        """One bar of the analyser, rounded at the end it grows towards.

        A Tk rectangle has hard corners and no way to round them, so the
        dome is half an oval laid over the end -- one more item per bar, and
        the difference between the row looking drawn and looking stamped.
        """
        if bottom < top:
            top, bottom = bottom, top
        radius = min((x1 - x0) / 2.0, self.BAR_RADIUS)
        if dome and radius >= 1.5 and (bottom - top) >= radius:
            if dome == "top":
                self.canvas.create_oval(x0, top, x1, top + radius * 2,
                                        fill=colour, outline="")
                top += radius
            else:
                self.canvas.create_oval(x0, bottom - radius * 2, x1, bottom,
                                        fill=colour, outline="")
                bottom -= radius
        if bottom > top:
            self.canvas.create_rectangle(x0, top, x1, bottom, fill=colour,
                                         outline="")

    def cap(self, x0, x1, y, below=False):
        """The peak marker, sitting just clear of where the bar reached."""
        y0 = y if below else y - self.CAP_H
        self.canvas.create_rectangle(x0, y0, x1, y0 + self.CAP_H,
                                     fill=self.colour(0.98), outline="")


def draw(canvas, mode, bands, accent, width, height, now, palette="Accent"):
    """Render one frame. `mode` is an index; out-of-range falls back to 0."""
    if not bands or width <= 1 or height <= 1:
        return 0
    if not (0 <= mode < len(_REGISTRY)):
        mode = 0
    canvas.delete("all")
    _REGISTRY[mode][1](Frame(canvas, bands, accent, width, height, now,
                             palette, _peaks.update(bands, now)))
    return len(_REGISTRY)


# ------------------------------------------------------------------- modes
#
# There were thirty-two of these. Most were a shape that moved rather than a
# picture of the sound: scattered squares, blobs that were meant to be fire,
# a wobbling circle with a line through it. They were not bad renderings of
# anything -- they were not renderings of anything. What is left is the ones
# that read as the music, drawn properly.


@visualizer("Standard Bars")
def _standard(f):
    """The classic analyser: a bar per band under a peak that holds.

    The peak marker in the old bar modes was a fixed offset above the bar
    with a sine wobble added, which is not a peak at all -- it tracked the
    bar exactly and jittered while it did. A real one holds the loudest the
    band has been and falls back slowly, so a transient leaves a mark that is
    still there after the bar has dropped away from it.
    """
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        x1 = x0 + f.bar_width - 2
        f.bar(x0, x1, f.height - energy * f.height, f.height, f.band_colour(i))
        f.cap(x0, x1, f.height - f.peaks[i] * f.height)


@visualizer("Mirrored Bars")
def _mirrored(f):
    """The same, opening from the middle in both directions."""
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        x1 = x0 + f.bar_width - 2
        reach = energy * f.cy
        colour = f.band_colour(i)
        f.bar(x0, x1, f.cy - reach, f.cy, colour)
        f.bar(x0, x1, f.cy, f.cy + reach, colour, dome="bottom")
        held = f.peaks[i] * f.cy
        f.cap(x0, x1, f.cy - held)
        f.cap(x0, x1, f.cy + held, below=True)


@visualizer("Bar Reflection")
def _reflection(f):
    """Bars standing on a mirrored, faded copy of themselves."""
    floor = f.height * 0.62
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 3
        x1 = x0 + f.bar_width - 6
        h = energy * floor * 0.95
        f.bar(x0, x1, floor - h, floor, f.band_colour(i))
        f.bar(x0, x1, floor + 3, floor + 3 + h * 0.42, f.colour(0.15),
              dome="bottom")
        f.cap(x0, x1, floor - f.peaks[i] * floor * 0.95)


@visualizer("Waveform")
def _waveform(f):
    """An oscilloscope trace, opened out symmetrically."""
    for sign in (-1, 1):
        points = []
        for i, energy in enumerate(f.bands):
            points.extend([i * f.bar_width, f.cy + sign * energy * f.cy])
        if points:
            f.canvas.create_line(*points, fill=f.colour(0.5), width=3,
                                 smooth=True, capstyle="round")


@visualizer("Spectrum Ribbon")
def _ribbon(f):
    """A filled spectrum silhouette with a bright crest."""
    top, crest = [], []
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        y = f.height - energy * f.height * 0.92
        top.extend([x, y])
        crest.extend([x, y])
    poly = [0, f.height] + top + [f.width, f.height]
    f.canvas.create_polygon(poly, fill=f.colour(0.35), outline="", smooth=True)
    f.canvas.create_line(*crest, fill=f.colour(0.95), width=3, smooth=True,
                         capstyle="round")


@visualizer("Aurora")
def _aurora(f):
    """Stacked drifting curtains -- slow, wide, layered."""
    for layer in range(4):
        pts = []
        drift = f.now * (0.35 + layer * 0.18)
        for i, energy in enumerate(f.bands):
            x = i * f.bar_width + f.bar_width / 2
            y = (f.cy + math.sin(i * 0.42 + drift) * f.height * 0.14
                 - energy * f.height * 0.26 + (layer - 1.5) * f.height * 0.07)
            pts.extend([x, y])
        if len(pts) >= 6:
            band = pts + [f.width, f.height, 0, f.height]
            f.canvas.create_polygon(band, fill=f.colour(0.2 + layer * 0.25),
                                    outline="", smooth=True)


@visualizer("Circular")
def _circular(f):
    """Bars radiating from an inner ring, mirrored so the figure is even."""
    ring = f.ring
    n = len(ring)
    inner = f.radius * 0.32
    span = f.radius - inner
    width = f.px(2 * math.pi * inner / n * 0.85)
    for i, energy in enumerate(ring):
        angle = i * (2 * math.pi / n) - math.pi / 2
        ca, sa = math.cos(angle), math.sin(angle)
        r = inner + energy * span
        f.canvas.create_line(f.cx + ca * inner, f.cy + sa * inner,
                             f.cx + ca * r, f.cy + sa * r,
                             fill=f.ring_colour(i), width=width,
                             capstyle="round")


@visualizer("Tunnel")
def _tunnel(f):
    """Rectangles receding toward the centre."""
    span = min(f.width, f.height) / 2
    for i, energy in enumerate(f.bands):
        depth = ((f.now * 0.45 + i / f.n) % 1.0)
        r = span * depth * (1 + energy * 0.4)
        if r < 4:
            continue
        f.canvas.create_rectangle(f.cx - r, f.cy - r * 0.72,
                                  f.cx + r, f.cy + r * 0.72,
                                  outline=f.colour(1 - depth),
                                  width=max(1, int(1 + (1 - depth) * 5)))


@visualizer("Ripple")
def _ripple(f):
    """Rings expanding out of the centre with the overall level."""
    span = min(f.width, f.height) / 2
    for k in range(5):
        phase = ((f.now * 0.55 + k / 5) % 1.0)
        r = span * phase * (0.7 + f.avg * 0.9)
        if r < 2:
            continue
        f.canvas.create_oval(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                             outline=f.colour(1 - phase),
                             width=max(1, int((1 - phase) * 6 + f.avg * 4)))
