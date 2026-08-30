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
                 "cx", "cy", "n", "avg", "now", "bar_width", "_palette")

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

    def colour(self, t=0.0):
        """Colour at position t (0..1) through the palette."""
        try:
            return self._palette(t, self.accent, self.now)
        except Exception:
            return self.accent

    def band_colour(self, i):
        return self.colour(i / max(1, self.n - 1))


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
    max_r = min(f.width, f.height) / 2 - 20
    for i, energy in enumerate(f.bands):
        angle = i * (2 * math.pi / f.n)
        r = 50 + energy * max_r
        f.canvas.create_line(f.cx, f.cy, f.cx + math.cos(angle) * r,
                             f.cy + math.sin(angle) * r,
                             fill=f.band_colour(i), width=4)


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
    span = min(f.width, f.height)
    r = 100 + f.avg * span / 2
    f.canvas.create_oval(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                         outline=f.colour(0.8), width=max(2, f.avg * 20))
    r2 = 50 + f.avg * span / 3
    f.canvas.create_oval(f.cx - r2, f.cy - r2, f.cx + r2, f.cy + r2,
                         outline=f.colour(0.3), width=max(1, f.avg * 10))


@visualizer("Radar")
def _radar(f):
    t = f.now * 2
    max_r = min(f.width, f.height) / 2
    for i, energy in enumerate(f.bands):
        angle = i * (2 * math.pi / f.n) + t
        r = energy * max_r
        f.canvas.create_line(f.cx, f.cy, f.cx + math.cos(angle) * r,
                             f.cy + math.sin(angle) * r,
                             fill=f.band_colour(i), width=2)


@visualizer("Starburst")
def _starburst(f):
    max_r = min(f.width, f.height) / 2
    for i, energy in enumerate(f.bands):
        angle = i * (2 * math.pi / f.n)
        r_in = 30 + energy * 50
        r_out = r_in + energy * max_r
        f.canvas.create_line(f.cx + math.cos(angle) * r_in,
                             f.cy + math.sin(angle) * r_in,
                             f.cx + math.cos(angle) * r_out,
                             f.cy + math.sin(angle) * r_out,
                             fill=f.band_colour(i), width=max(1, int(energy * 5)))


@visualizer("Galaxy")
def _galaxy(f):
    span = min(f.width, f.height)
    for i, energy in enumerate(f.bands):
        angle = i * (2 * math.pi / f.n) + f.now * (0.5 + energy)
        r = 10 + i * span / 2 / f.n + energy * 100
        x = f.cx + math.cos(angle) * r
        y = f.cy + math.sin(angle) * r
        sz = max(2, energy * 15)
        f.canvas.create_oval(x - sz, y - sz, x + sz, y + sz,
                             fill=f.band_colour(i), outline="")


@visualizer("Fire")
def _fire(f):
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        flame_h = energy * f.height
        for _ in range(int(energy * 10)):
            fx = x + random.uniform(-15, 15)
            fy = f.height - random.uniform(0, flame_h)
            sz = random.uniform(2, 8)
            f.canvas.create_oval(fx - sz, fy - sz, fx + sz, fy + sz,
                                 fill=f.colour(1 - (fy / f.height)), outline="")


@visualizer("Matrix")
def _matrix(f):
    for i, energy in enumerate(f.bands):
        x = i * f.bar_width + f.bar_width / 2
        y = (1 - energy) * f.height
        f.canvas.create_line(x, y, x, y + energy * 200,
                             fill=f.band_colour(i), width=4)


@visualizer("Hexagon")
def _hexagon(f):
    radius = min(f.width, f.height) / 3
    for h in range(6):
        a1, a2 = h * math.pi / 3, (h + 1) * math.pi / 3
        x1, y1 = f.cx + math.cos(a1) * radius, f.cy + math.sin(a1) * radius
        x2, y2 = f.cx + math.cos(a2) * radius, f.cy + math.sin(a2) * radius
        f.canvas.create_line(x1, y1, x2, y2, fill=f.colour(h / 5), width=2)
        for i, energy in enumerate(f.bands):
            if i % 6 != h:
                continue
            t = (i // 6) / max(1, f.n // 6)
            px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            dx, dy = math.cos(a1 + math.pi / 6), math.sin(a1 + math.pi / 6)
            f.canvas.create_line(px, py, px + dx * energy * 100,
                                 py + dy * energy * 100,
                                 fill=f.band_colour(i), width=3)


@visualizer("Hyperspace")
def _hyperspace(f):
    t = f.now * 5
    limit = min(f.width, f.height) / 2
    for i, energy in enumerate(f.bands):
        angle = i * (2 * math.pi / f.n)
        dist = (t * (i + 1) * 10) % limit
        x = f.cx + math.cos(angle) * dist
        y = f.cy + math.sin(angle) * dist
        sz = max(1, (dist / 100) * energy * 10)
        f.canvas.create_oval(x - sz, y - sz, x + sz, y + sz,
                             fill=f.colour(dist / limit), outline="")


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
    t = f.now * 3
    step = min(f.width, f.height) / 2 / f.n
    for i, energy in enumerate(f.bands):
        angle = i * 0.5 + t
        r = i * step + energy * 50
        x = f.cx + math.cos(angle) * r
        y = f.cy + math.sin(angle) * r
        f.canvas.create_oval(x - 2, y - 2, x + 2, y + 2,
                             fill=f.band_colour(i), outline="")


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
    span = min(f.width, f.height) / 2 - 10
    for w in range(wedges):
        base = w * 2 * math.pi / wedges + f.now * 0.25
        flip = -1 if w % 2 else 1
        for i, energy in enumerate(f.bands):
            a = base + flip * (i / f.n) * (2 * math.pi / wedges)
            r0 = span * 0.18
            r1 = r0 + energy * span * 0.78
            f.canvas.create_line(f.cx + math.cos(a) * r0, f.cy + math.sin(a) * r0,
                                 f.cx + math.cos(a) * r1, f.cy + math.sin(a) * r1,
                                 fill=f.band_colour(i), width=3)


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
    """Points on a slow orbit, linked when they drift close together."""
    pts = []
    span = min(f.width, f.height) * 0.42
    for i, energy in enumerate(f.bands):
        a = i * (2 * math.pi / f.n) + f.now * 0.22
        r = span * (0.35 + 0.65 * energy)
        pts.append((f.cx + math.cos(a) * r, f.cy + math.sin(a) * r, energy, i))
    for i, (x1, y1, e1, bi) in enumerate(pts):
        for x2, y2, _e2, _bj in pts[i + 1:]:
            if math.hypot(x2 - x1, y2 - y1) < span * 0.55:
                f.canvas.create_line(x1, y1, x2, y2, fill=f.colour(0.3), width=1)
    for x, y, e, bi in pts:
        sz = 2 + e * 9
        f.canvas.create_oval(x - sz, y - sz, x + sz, y + sz,
                             fill=f.band_colour(bi), outline="")


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
    """Petals opening from the centre -- one per band."""
    span = min(f.width, f.height) * 0.42
    for i, energy in enumerate(f.bands):
        a = i * (2 * math.pi / f.n) + f.now * 0.18
        reach = span * (0.25 + energy * 0.9)
        w = reach * 0.30
        tipx, tipy = f.cx + math.cos(a) * reach, f.cy + math.sin(a) * reach
        lx, ly = f.cx + math.cos(a + 0.5) * w, f.cy + math.sin(a + 0.5) * w
        rx, ry = f.cx + math.cos(a - 0.5) * w, f.cy + math.sin(a - 0.5) * w
        f.canvas.create_polygon([f.cx, f.cy, lx, ly, tipx, tipy, rx, ry],
                                fill=f.band_colour(i), outline="", smooth=True)


@visualizer("Grid Pulse")
def _grid(f):
    """A cell matrix lighting up column by column with the spectrum."""
    rows = 8
    cell_h = f.height / rows
    for i, energy in enumerate(f.bands):
        lit = int(energy * rows + 0.5)
        for r in range(rows):
            if r >= lit:
                continue
            y1 = f.height - (r + 1) * cell_h + 3
            y2 = f.height - r * cell_h - 3
            x0 = i * f.bar_width + 3
            f.canvas.create_rectangle(x0, y1, x0 + f.bar_width - 6, y2,
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
    """A head orbiting with a fading tail behind it."""
    span = min(f.width, f.height) * 0.36
    tail = 26
    for k in range(tail):
        t = f.now * 1.5 - k * 0.045
        energy = f.bands[(k * 3) % f.n]
        r = span * (0.85 + energy * 0.5)
        x = f.cx + math.cos(t) * r
        y = f.cy + math.sin(t * 1.3) * r * 0.75
        sz = (tail - k) / tail * (4 + energy * 12)
        f.canvas.create_oval(x - sz, y - sz, x + sz, y + sz,
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
    span = min(f.width, f.height) / 2 - 8
    step = 360 / f.n
    for i, energy in enumerate(f.bands):
        r = span * (0.22 + energy * 0.78)
        f.canvas.create_arc(f.cx - r, f.cy - r, f.cx + r, f.cy + r,
                            start=i * step + f.now * 8, extent=step * 0.82,
                            fill=f.band_colour(i), outline="", style="pieslice")
