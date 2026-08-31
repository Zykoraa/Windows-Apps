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


class Frame:
    """Everything a mode needs to draw one frame."""

    __slots__ = ("canvas", "bands", "accent", "width", "height",
                 "cx", "cy", "n", "avg", "now", "bar_width", "_palette",
                 "_ring")

    def __init__(self, canvas, bands, accent, width, height, now, palette):
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


def draw(canvas, mode, bands, accent, width, height, now, palette="Accent"):
    """Render one frame. `mode` is an index; out-of-range falls back to 0."""
    if not bands or width <= 1 or height <= 1:
        return 0
    if not (0 <= mode < len(_REGISTRY)):
        mode = 0
    canvas.delete("all")
    _REGISTRY[mode][1](Frame(canvas, bands, accent, width, height, now, palette))
    return len(_REGISTRY)


# ------------------------------------------------------------------- modes


@visualizer("Standard Bars")
def _standard(f):
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        f.canvas.create_rectangle(x0, f.height, x0 + f.bar_width - 2,
                                  f.height - energy * f.height,
                                  fill=f.band_colour(i), outline="")


@visualizer("Mirrored Bars")
def _mirrored(f):
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        f.canvas.create_rectangle(x0, f.cy - energy * f.cy,
                                  x0 + f.bar_width - 2, f.cy + energy * f.cy,
                                  fill=f.band_colour(i), outline="")


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


@visualizer("Waveform")
def _waveform(f):
    for sign in (-1, 1):
        points = []
        for i, energy in enumerate(f.bands):
            points.extend([i * f.bar_width, f.cy + sign * energy * f.cy])
        if points:
            f.canvas.create_line(*points, fill=f.colour(0.5), width=3, smooth=True)


@visualizer("Particles")
def _particles(f):
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        x1 = x0 + f.bar_width - 4
        colour = f.band_colour(i)
        f.canvas.create_rectangle(x0, f.height, x1,
                                  f.height - energy * f.height * 0.8,
                                  fill=colour, outline="")
        dot_y = f.height - energy * f.height - 10
        f.canvas.create_oval(x0, dot_y - 4, x1, dot_y + 4, fill=colour, outline="")


@visualizer("Pulse")
def _pulse(f):
    """Concentric rings breathing with the overall level."""
    for weight, tone in ((1.0, 0.9), (0.68, 0.6), (0.4, 0.32)):
        r = f.radius * weight * (0.30 + 0.70 * f.avg)
        if r < 2:
            continue
        f.canvas.create_oval(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                             outline=f.colour(tone),
                             width=f.px(2 + f.avg * f.radius * 0.05))


@visualizer("Radar")
def _radar(f):
    """A rotating polar plot of the spectrum, with a sweep line."""
    ring = f.ring
    n = len(ring)
    spin = f.now * 0.9
    f.canvas.create_oval(f.cx - f.radius, f.cy - f.radius,
                         f.cx + f.radius, f.cy + f.radius,
                         outline=f.colour(0.15), width=1)
    points = []
    for i, energy in enumerate(ring):
        angle = i * (2 * math.pi / n) + spin
        r = f.radius * (0.12 + 0.88 * energy)
        points.extend([f.cx + math.cos(angle) * r,
                       f.cy + math.sin(angle) * r])
    if len(points) >= 6:
        f.canvas.create_polygon(points, fill="", outline=f.colour(0.75),
                                width=f.px(2), smooth=True)
    f.canvas.create_line(f.cx, f.cy,
                         f.cx + math.cos(spin) * f.radius,
                         f.cy + math.sin(spin) * f.radius,
                         fill=f.colour(1.0), width=f.px(2))


@visualizer("Starburst")
def _starburst(f):
    """Spikes out from a ring, with a shorter set pointing back in."""
    ring = f.ring
    n = len(ring)
    inner = f.radius * 0.30
    for i, energy in enumerate(ring):
        angle = i * (2 * math.pi / n) - math.pi / 2
        ca, sa = math.cos(angle), math.sin(angle)
        out = inner + energy * (f.radius - inner)
        back = inner - energy * inner * 0.55
        f.canvas.create_line(f.cx + ca * back, f.cy + sa * back,
                             f.cx + ca * out, f.cy + sa * out,
                             fill=f.ring_colour(i),
                             width=f.px(1 + energy * 3), capstyle="round")


@visualizer("Galaxy")
def _galaxy(f):
    """Two spiral arms on a tilted disc, sized by the spectrum."""
    ring = f.ring
    n = len(ring)
    for arm in (0.0, math.pi):
        for i, energy in enumerate(ring):
            t = i / n
            angle = arm + t * 2.4 * 2 * math.pi + f.now * 0.35
            r = f.radius * (0.08 + 0.92 * t)
            x = f.cx + math.cos(angle) * r
            y = f.cy + math.sin(angle) * r * 0.62
            size = f.px(1 + energy * 6)
            f.canvas.create_oval(x - size, y - size, x + size, y + size,
                                 fill=f.ring_colour(i), outline="")


@visualizer("Fire")
def _fire(f):
    """Flames across the whole width, height following each band."""
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        flame = energy * f.height * 0.95
        for _ in range(max(1, int(energy * 9))):
            spread = f.bar_width * 1.4
            climb = random.random() ** 0.6
            fx = x + random.uniform(-spread, spread)
            fy = f.height - climb * flame
            size = max(1.5, (1 - climb) * f.bar_width * 0.9 + 1)
            f.canvas.create_oval(fx - size, fy - size, fx + size, fy + size,
                                 fill=f.colour(climb), outline="")


@visualizer("Matrix")
def _matrix(f):
    """Rain: each column falls at its own band's pace, in fading dashes.

    This drew a bar per band before, which made it a slightly taller copy of
    Standard Bars rather than a mode of its own.
    """
    dash = max(4.0, f.height / 26)
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        speed = 40 + energy * 320
        head = ((f.now * speed) + i * 61) % (f.height + dash * 8)
        trail = int(3 + energy * 9)
        for k in range(trail):
            y = head - k * dash * 1.6
            if y < -dash or y > f.height:
                continue
            f.canvas.create_line(x, y, x, y - dash * 0.7,
                                 fill=f.colour(1 - k / trail),
                                 width=f.px(f.bar_width * 0.4))


@visualizer("Hexagon")
def _hexagon(f):
    """A hex frame with bars standing off each edge, along its normal."""
    r = f.radius * 0.62
    per = max(1, f.n // 6)
    for h in range(6):
        a1 = h * math.pi / 3 - math.pi / 2
        a2 = (h + 1) * math.pi / 3 - math.pi / 2
        x1, y1 = f.cx + math.cos(a1) * r, f.cy + math.sin(a1) * r
        x2, y2 = f.cx + math.cos(a2) * r, f.cy + math.sin(a2) * r
        f.canvas.create_line(x1, y1, x2, y2, fill=f.colour(0.25),
                             width=f.px(2))
        mx, my = (x1 + x2) / 2 - f.cx, (y1 + y2) / 2 - f.cy
        length = math.hypot(mx, my) or 1.0
        nx, ny = mx / length, my / length
        for k in range(per):
            index = (h * per + k) % f.n
            energy = f.bands[index]
            t = (k + 0.5) / per
            px_, py_ = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            reach = energy * (f.radius - r) * 0.95
            f.canvas.create_line(px_, py_, px_ + nx * reach, py_ + ny * reach,
                                 fill=f.band_colour(index), width=f.px(3),
                                 capstyle="round")


@visualizer("Hyperspace")
def _hyperspace(f):
    """Streaks pulled out from the centre, faster where the band is loud."""
    ring = f.ring
    n = len(ring)
    for i, energy in enumerate(ring):
        angle = i * (2 * math.pi / n)
        phase = ((f.now * (0.25 + energy * 1.4)) + i * 0.137) % 1.0
        r1 = f.radius * phase
        r0 = max(0.0, r1 - f.radius * (0.06 + energy * 0.22))
        ca, sa = math.cos(angle), math.sin(angle)
        f.canvas.create_line(f.cx + ca * r0, f.cy + sa * r0,
                             f.cx + ca * r1, f.cy + sa * r1,
                             fill=f.colour(phase),
                             width=f.px(1 + phase * energy * 4),
                             capstyle="round")


@visualizer("Infinity")
def _infinity(f):
    points = []
    for i, energy in enumerate(f.bands):
        v = i / f.n * 2 * math.pi + f.now
        scale = min(f.width, f.height) / 3 + energy * 100
        denom = 1 + math.sin(v) ** 2
        points.extend([f.cx + scale * math.cos(v) / denom,
                       f.cy + scale * math.sin(v) * math.cos(v) / denom])
    if points:
        f.canvas.create_polygon(points, outline=f.colour(0.6), fill="",
                                width=3, smooth=True)


@visualizer("EKG")
def _ekg(f):
    points = []
    for i, energy in enumerate(f.bands):
        points.extend([i * f.bar_width,
                       f.cy + math.sin(i * 0.5) * energy * (f.height / 2)])
    if points:
        f.canvas.create_line(*points, fill=f.colour(0.5), width=3)


@visualizer("Vortex")
def _vortex(f):
    """A spiral drawn as one continuous ribbon rather than loose dots."""
    ring = f.ring
    n = len(ring)
    points = []
    for i, energy in enumerate(ring):
        t = i / n
        angle = t * 5.2 * math.pi + f.now * 1.1
        r = f.radius * (0.06 + 0.94 * t) * (0.72 + 0.28 * energy)
        points.extend([f.cx + math.cos(angle) * r,
                       f.cy + math.sin(angle) * r])
    if len(points) >= 6:
        f.canvas.create_line(*points, fill=f.colour(0.7), width=f.px(2),
                             smooth=True, capstyle="round")


# --------------------------------------------------------------- new modes


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
    f.canvas.create_line(*crest, fill=f.colour(0.95), width=3, smooth=True)


@visualizer("Bar Reflection")
def _reflection(f):
    """Bars standing on a mirrored, faded copy of themselves."""
    floor = f.height * 0.62
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 3
        x1 = x0 + f.bar_width - 6
        h = energy * floor * 0.95
        colour = f.band_colour(i)
        f.canvas.create_rectangle(x0, floor, x1, floor - h, fill=colour, outline="")
        f.canvas.create_rectangle(x0, floor + 3, x1, floor + 3 + h * 0.42,
                                  fill=f.colour(0.15), outline="")


@visualizer("Orbit Rings")
def _orbit(f):
    """Concentric rings, each breathing with its own band."""
    span = min(f.width, f.height) / 2 - 12
    for i, energy in enumerate(f.bands):
        r = span * (i + 1) / f.n * (0.72 + energy * 0.45)
        f.canvas.create_oval(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                             outline=f.band_colour(i), width=max(1, int(1 + energy * 5)))
        a = f.now * (0.4 + i * 0.13)
        px, py = f.cx + math.cos(a) * r, f.cy + math.sin(a) * r
        sz = 2 + energy * 7
        f.canvas.create_oval(px - sz, py - sz, px + sz, py + sz,
                             fill=f.band_colour(i), outline="")


@visualizer("DNA Helix")
def _helix(f):
    """Two counter-phase strands with rungs between them."""
    prev = None
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        phase = i * 0.55 + f.now * 2.2
        amp = f.height * 0.22 * (0.45 + energy)
        y1 = f.cy + math.sin(phase) * amp
        y2 = f.cy - math.sin(phase) * amp
        colour = f.band_colour(i)
        if i % 2 == 0:
            f.canvas.create_line(x, y1, x, y2, fill=f.colour(0.25), width=2)
        for y in (y1, y2):
            sz = 3 + energy * 8
            f.canvas.create_oval(x - sz, y - sz, x + sz, y + sz,
                                 fill=colour, outline="")
        if prev:
            f.canvas.create_line(prev[0], prev[1], x, y1, fill=colour, width=2)
            f.canvas.create_line(prev[0], prev[2], x, y2, fill=colour, width=2)
        prev = (x, y1, y2)


@visualizer("Kaleidoscope")
def _kaleidoscope(f):
    """One wedge of bars, mirrored around the centre."""
    wedges = 8
    inner = f.radius * 0.16
    for w in range(wedges):
        base = w * 2 * math.pi / wedges + f.now * 0.25
        flip = -1 if w % 2 else 1
        for i, energy in enumerate(f.bands):
            angle = base + flip * (i / f.n) * (2 * math.pi / wedges)
            r1 = inner + energy * (f.radius - inner)
            f.canvas.create_line(f.cx + math.cos(angle) * inner,
                                 f.cy + math.sin(angle) * inner,
                                 f.cx + math.cos(angle) * r1,
                                 f.cy + math.sin(angle) * r1,
                                 fill=f.band_colour(i), width=f.px(3),
                                 capstyle="round")


@visualizer("Rainfall")
def _rainfall(f):
    """Drops falling at a speed set by their band."""
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        for k in range(3):
            speed = 60 + i * 12 + k * 40
            y = ((f.now * speed) + k * 137 + i * 53) % (f.height + 60) - 30
            length = 8 + energy * 44
            f.canvas.create_line(x, y, x, y + length,
                                 fill=f.band_colour(i),
                                 width=max(1, int(1 + energy * 4)))


@visualizer("Constellation")
def _constellation(f):
    """Points on a slow orbit, linked to the neighbours they drift near."""
    ring = f.ring
    n = len(ring)
    # Sparse on purpose. One point per band drew a dotted outline of a circle,
    # which reads as a smudge rather than as stars.
    stars = 26
    points = []
    for s in range(stars):
        i = int(s * n / stars)
        energy = ring[i]
        angle = s * (2 * math.pi / stars) + f.now * 0.22
        r = f.radius * (0.32 + 0.68 * energy)
        points.append((f.cx + math.cos(angle) * r,
                       f.cy + math.sin(angle) * r, energy, i))
    near = f.radius * 0.42
    # Only the next few neighbours: every pair is quadratic, and the ring is
    # twice as long as the band list it came from.
    for i, (x1, y1, _e, _b) in enumerate(points):
        for x2, y2, _e2, _b2 in points[i + 1:i + 3]:
            if math.hypot(x2 - x1, y2 - y1) < near:
                f.canvas.create_line(x1, y1, x2, y2, fill=f.colour(0.25),
                                     width=1)
    for x, y, energy, i in points:
        size = f.px(1.5 + energy * 7)
        f.canvas.create_oval(x - size, y - size, x + size, y + size,
                             fill=f.ring_colour(i), outline="")


@visualizer("Tunnel")
def _tunnel(f):
    """Rounded rectangles receding toward the centre."""
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


@visualizer("Bloom")
def _bloom(f):
    """Petals opening from the centre -- one per band, mirrored."""
    ring = f.ring
    n = len(ring)
    # Sampled, not one petal per band: ninety-six of them overlapped into a
    # single blob, which is the opposite of a flower.
    petals = 15
    for p in range(petals):
        i = int(p * n / petals)
        energy = ring[i]
        angle = p * (2 * math.pi / petals) + f.now * 0.18
        reach = f.radius * (0.30 + energy * 0.70)
        # A fixed base width, not one scaled by reach: petals sized off their
        # own length buried every short one inside its neighbours, so the
        # flower came out as a spiky blob.
        w = f.radius * 0.20
        tip_x = f.cx + math.cos(angle) * reach
        tip_y = f.cy + math.sin(angle) * reach
        spread = math.pi / petals
        lx = f.cx + math.cos(angle + spread) * w
        ly = f.cy + math.sin(angle + spread) * w
        rx = f.cx + math.cos(angle - spread) * w
        ry = f.cy + math.sin(angle - spread) * w
        f.canvas.create_polygon([f.cx, f.cy, lx, ly, tip_x, tip_y, rx, ry],
                                fill=f.ring_colour(i), outline="", smooth=True)


@visualizer("Grid Pulse")
def _grid(f):
    """A cell matrix lighting up column by column with the spectrum."""
    rows = 8
    cell_h = f.height / rows
    # Fixed 3px gaps ate the whole cell once the columns got narrow, which
    # left the mode rendering as a field of hairlines.
    gap_x = min(3.0, f.bar_width * 0.22)
    gap_y = min(3.0, cell_h * 0.18)
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + gap_x
        x1 = x0 + f.bar_width - gap_x * 2
        for r in range(int(energy * rows + 0.5)):
            y1 = f.height - (r + 1) * cell_h + gap_y
            y2 = f.height - r * cell_h - gap_y
            f.canvas.create_rectangle(x0, y1, x1, y2,
                                      fill=f.colour(r / max(1, rows - 1)),
                                      outline="")


@visualizer("Ripple")
def _ripple(f):
    """Expanding rings triggered by overall level."""
    span = min(f.width, f.height) / 2
    for k in range(5):
        phase = ((f.now * 0.55 + k / 5) % 1.0)
        r = span * phase * (0.7 + f.avg * 0.9)
        if r < 2:
            continue
        f.canvas.create_oval(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                             outline=f.colour(1 - phase),
                             width=max(1, int((1 - phase) * 6 + f.avg * 4)))


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


@visualizer("Lissajous")
def _lissajous(f):
    """A parametric curve whose ratio drifts with the music."""
    a = 3 + f.avg * 2
    b = 2 + math.sin(f.now * 0.2) * 1.5
    scale = min(f.width, f.height) * 0.38
    pts = []
    steps = 160
    for k in range(steps + 1):
        t = k / steps * 2 * math.pi
        energy = f.bands[int(k / (steps + 1) * f.n) % f.n]
        r = scale * (0.75 + energy * 0.5)
        pts.extend([f.cx + math.sin(a * t + f.now * 0.6) * r,
                    f.cy + math.sin(b * t) * r])
    f.canvas.create_line(*pts, fill=f.colour(0.7), width=2, smooth=True)


@visualizer("Comet Trail")
def _comet(f):
    """A head on a wide orbit with a fading tail behind it."""
    tail = 34
    rx, ry = f.width * 0.38, f.height * 0.36
    for k in range(tail):
        # A wider step: at 0.05 radians the whole tail covered an eighth of
        # the orbit and the dots simply overlapped into a lump.
        t = f.now * 1.1 - k * 0.11
        energy = f.bands[(k * 3) % f.n]
        x = f.cx + math.cos(t) * rx * (0.85 + energy * 0.3)
        y = f.cy + math.sin(t * 1.4) * ry * (0.85 + energy * 0.3)
        size = (tail - k) / tail * (2 + energy * 7) + 1
        f.canvas.create_oval(x - size, y - size, x + size, y + size,
                             fill=f.colour(1 - k / tail), outline="")


@visualizer("Equaliser Wall")
def _wall(f):
    """Bars with a peak marker that falls back slowly."""
    for i, energy in enumerate(f.bands):
        x0 = i * f.bar_width + 2
        x1 = x0 + f.bar_width - 4
        h = energy * f.height * 0.9
        f.canvas.create_rectangle(x0, f.height, x1, f.height - h,
                                  fill=f.band_colour(i), outline="")
        peak = f.height - h - 8 - math.sin(f.now * 3 + i) * 3
        f.canvas.create_rectangle(x0, peak, x1, peak + 4,
                                  fill=f.colour(0.98), outline="")


@visualizer("Sunburst")
def _sunburst(f):
    """Wedges radiating from the centre, sized by band."""
    ring = f.ring
    n = len(ring)
    step = 360.0 / n
    for i, energy in enumerate(ring):
        r = f.radius * (0.22 + energy * 0.78)
        f.canvas.create_arc(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                            start=i * step + f.now * 8, extent=step * 0.86,
                            fill=f.ring_colour(i), outline="",
                            style="pieslice")
