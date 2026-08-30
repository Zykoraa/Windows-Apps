"""Generate the Eve's Garden app icon.

Four botanical marks are defined; DESIGN picks which one is built. Change
that constant and re-run to swap the icon everywhere -- window, title bar,
tray and the executable's own icon all come from what this writes.

Outputs:
  assets/icon.ico   multi-resolution, used as the executable's icon
  assets/icon.png   256px master
  app_icon.py       the same PNG base64-encoded, so the running app never
                    depends on a file next to the exe

Everything is drawn on a supersampled canvas and downsampled, so edges stay
clean at 16px where a font glyph would turn to mush. Vein and petal detail is
deliberately low-contrast: it reads at 128px and dissolves rather than smears
at 16px.
"""

import base64
import io
import math
import os

from PIL import Image, ImageDraw, ImageFilter

SS = 8                      # supersampling factor
SIZE = 256                  # master size
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# One of: "Monstera", "Leaf", "Bloom", "Sprout".
DESIGN = "Monstera"

GRAD = [(64, 230, 138), (30, 182, 96), (13, 122, 64), (4, 60, 32)]
MARK_TOP = (255, 255, 255)
MARK_BOTTOM = (219, 245, 229)
DETAIL = (22, 132, 76)


def _gradient(size, stops):
    grad = Image.new("RGB", (1, size))
    spans = len(stops) - 1
    for y in range(size):
        t = y / max(1, size - 1) * spans
        i = min(int(t), spans - 1)
        f = t - i
        a, b = stops[i], stops[i + 1]
        grad.putpixel((0, y), tuple(int(a[c] + (b[c] - a[c]) * f) for c in range(3)))
    return grad.resize((size, size), Image.Resampling.BICUBIC)


def _rounded(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def _tint(canvas, colour, mask):
    img = Image.new("RGBA", (canvas, canvas), colour + (0,))
    img.putalpha(mask)
    return img


def _bezier(p0, p1, p2, steps=90):
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return out


def _offset(spine, width_fn):
    left, right = [], []
    n = len(spine) - 1
    for i, (x, y) in enumerate(spine):
        j, k = min(i + 1, n), max(i - 1, 0)
        dx, dy = spine[j][0] - spine[k][0], spine[j][1] - spine[k][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        w = width_fn(i / n)
        left.append((x + nx * w, y + ny * w))
        right.append((x - nx * w, y - ny * w))
    return left + right[::-1]


def _badge(canvas):
    radius = int(canvas * 0.235)
    mask = _rounded(canvas, radius)
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    img.paste(_gradient(canvas, GRAD), (0, 0), mask)

    key = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(key).ellipse([-canvas * .34, -canvas * .52,
                                 canvas * .88, canvas * .56], fill=82)
    key = Image.composite(key.filter(ImageFilter.GaussianBlur(canvas * .10)),
                          Image.new("L", (canvas, canvas), 0), mask)
    img = Image.alpha_composite(img, _tint(canvas, (255, 255, 255), key))

    vig = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(vig).ellipse([canvas * .06, canvas * .50,
                                 canvas * .94, canvas * 1.38], fill=70)
    vig = Image.composite(vig.filter(ImageFilter.GaussianBlur(canvas * .12)),
                          Image.new("L", (canvas, canvas), 0), mask)
    img = Image.alpha_composite(img, _tint(canvas, (0, 34, 18), vig))
    return img, mask, radius


def _finish(img, canvas, mask, radius):
    gl = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(gl).ellipse([-canvas * .45, -canvas * 1.0,
                                canvas * 1.45, canvas * .50], fill=42)
    gl = Image.composite(gl.filter(ImageFilter.GaussianBlur(canvas * .04)),
                         Image.new("L", (canvas, canvas), 0), mask)
    img = Image.alpha_composite(img, _tint(canvas, (255, 255, 255), gl))

    edge = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(edge).rounded_rectangle(
        [canvas * .045, canvas * .045, canvas * .955, canvas * .955],
        radius=int(radius * .84), outline=54, width=int(canvas * .012))
    return Image.alpha_composite(img, _tint(canvas, (255, 255, 255), edge))


def _compose(img, canvas, add, cut=(), details=()):
    shape = Image.new("L", (canvas, canvas), 0)
    d = ImageDraw.Draw(shape)
    for poly in add:
        d.polygon(poly, fill=255)
    for poly in cut:
        d.polygon(poly, fill=0)

    # Contact shadow: tight and offset down, so the mark reads as resting on
    # the badge rather than pasted over it.
    shadow = shape.filter(ImageFilter.GaussianBlur(canvas * .020)).point(
        lambda v: int(v * .50))
    shifted = Image.new("L", (canvas, canvas), 0)
    shifted.paste(shadow, (0, int(canvas * .020)))
    img = Image.alpha_composite(img, _tint(canvas, (0, 0, 0), shifted))

    mark = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    mark.paste(_gradient(canvas, [MARK_TOP, MARK_BOTTOM]), (0, 0), shape)

    if details:
        det = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        dd = ImageDraw.Draw(det)
        for pts, width, alpha in details:
            dd.line(pts, fill=DETAIL + (alpha,), width=max(1, width), joint="curve")
        mark = Image.alpha_composite(mark, Image.composite(
            det, Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0)), shape))
    return Image.alpha_composite(img, mark)


# ------------------------------------------------------------------ designs

def leaf(canvas):
    base = (canvas * .335, canvas * .800)
    tip = (canvas * .742, canvas * .205)
    spine = _bezier(base, (canvas * .775, canvas * .560), tip, 140)
    w = lambda t: canvas * .150 * math.sin(math.pi * (t ** .84)) ** .90
    details = [(spine, int(canvas * .015), 165)]
    for t0 in (.22, .38, .54, .70):
        i = int(t0 * (len(spine) - 1))
        x, y = spine[i]
        j, k = min(i + 1, len(spine) - 1), max(i - 1, 0)
        dx, dy = spine[j][0] - spine[k][0], spine[j][1] - spine[k][1]
        L = math.hypot(dx, dy) or 1
        tx, ty = dx / L, dy / L
        nx, ny = -ty, tx
        reach = w(t0) * 1.3
        for s in (1, -1):
            end = (x + (nx * s * .88 + tx * .52) * reach,
                   y + (ny * s * .88 + ty * .52) * reach)
            mid = (x + (nx * s * .54 + tx * .15) * reach,
                   y + (ny * s * .54 + ty * .15) * reach)
            details.append((_bezier((x, y), mid, end, 16), int(canvas * .0085), 120))
    details.append((_bezier(base, (canvas * .300, canvas * .855),
                            (canvas * .258, canvas * .880), 24), int(canvas * .028), 255))
    return [_offset(spine, w)], [], details


def monstera(canvas):
    """Fenestrated split-leaf: lobes cut inward from each side of the blade."""
    cx, cy = canvas * .50, canvas * .495
    rx, ry = canvas * .295, canvas * .335
    blade = []
    for i in range(0, 361, 2):
        a = math.radians(i) - math.pi / 2
        # heart-shaped: wider at the top, notched at the very bottom
        squash = 1.0 - .16 * max(0.0, math.sin(a))
        blade.append((cx + math.cos(a) * rx * squash,
                      cy + math.sin(a) * ry))

    cuts = []
    for side in (1, -1):
        for k, t in enumerate((-0.46, -0.14, 0.18, 0.50)):
            y = cy + t * ry * 1.02
            depth = rx * (0.90 - abs(t) * 0.22)
            half = ry * (0.075 - abs(t) * 0.012)
            cuts.append([
                (cx + side * rx * 1.25, y - half),
                (cx + side * rx * 1.25, y + half),
                (cx + side * (rx * 1.25 - depth), y + half * 0.32),
                (cx + side * (rx * 1.25 - depth), y - half * 0.32),
            ])
    # notch at the base, the way a real monstera leaf meets its stem
    cuts.append([(cx - rx * .13, cy + ry * 1.06),
                 (cx + rx * .13, cy + ry * 1.06),
                 (cx, cy + ry * .30)])

    details = [([(cx, cy - ry * .86), (cx, cy + ry * .88)], int(canvas * .014), 150)]
    details.append((_bezier((cx, cy + ry * .95), (cx, canvas * .90),
                            (cx - canvas * .045, canvas * .905), 20),
                    int(canvas * .028), 255))
    return [blade], cuts, details


def bloom(canvas):
    """Eight petals with visible separation and a seeded centre."""
    cx, cy = canvas * .50, canvas * .478
    petals, details = [], []
    n = 8
    reach = canvas * .330
    for i in range(n):
        a = i * 2 * math.pi / n - math.pi / 2
        spine = _bezier((cx + math.cos(a) * reach * .16,
                         cy + math.sin(a) * reach * .16),
                        (cx + math.cos(a) * reach * .62,
                         cy + math.sin(a) * reach * .62),
                        (cx + math.cos(a) * reach, cy + math.sin(a) * reach), 44)
        petals.append(_offset(spine, lambda t: canvas * .082 * math.sin(math.pi * t) ** .72))
        details.append((spine, int(canvas * .010), 105))
    centre = [(cx + math.cos(math.radians(a)) * canvas * .072,
               cy + math.sin(math.radians(a)) * canvas * .072)
              for a in range(0, 361, 6)]
    petals.append(centre)
    for a in range(0, 360, 45):
        r = math.radians(a)
        details.append(([(cx, cy), (cx + math.cos(r) * canvas * .052,
                                    cy + math.sin(r) * canvas * .052)],
                        int(canvas * .009), 130))
    stem = _bezier((cx, cy + reach * .70), (cx - canvas * .01, canvas * .885),
                   (cx - canvas * .03, canvas * .895), 20)
    details.append((stem, int(canvas * .026), 255))
    return petals, [], details


def sprout(canvas):
    cx = canvas * .50
    stem = _bezier((cx, canvas * .860), (cx, canvas * .60), (cx, canvas * .385), 44)
    polys = [_offset(stem, lambda t: canvas * .019 * (0.55 + 0.45 * (1 - t)))]
    details = []
    for side, base_t, tip_y in ((1, .615, .335), (-1, .705, .470)):
        base = (cx, canvas * base_t)
        tip = (cx + side * canvas * .272, canvas * tip_y)
        ctrl = (cx + side * canvas * .080, canvas * (tip_y + .045))
        sp = _bezier(base, ctrl, tip, 64)
        polys.append(_offset(sp, lambda t: canvas * .094 * math.sin(math.pi * t ** .82) ** .88))
        details.append((sp, int(canvas * .012), 150))
    return polys, [], details


DESIGNS = {"Leaf": leaf, "Monstera": monstera, "Bloom": bloom, "Sprout": sprout}


def build(which, size=256):
    canvas = size * SS
    img, mask, radius = _badge(canvas)
    add, cut, details = DESIGNS[which](canvas)
    img = _compose(img, canvas, add, cut, details)
    img = _finish(img, canvas, mask, radius)
    return img.resize((size, size), Image.Resampling.LANCZOS)



def main():
    here = os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)

    master = build(DESIGN, SIZE)
    png_path = os.path.join(assets, "icon.png")
    master.save(png_path)

    # Render each size from its own supersampled draw rather than scaling the
    # master down, so the small sizes stay crisp.
    frames = [build(DESIGN, s) for s in ICO_SIZES]
    frames[-1].save(os.path.join(assets, "icon.ico"), format="ICO",
                    sizes=[(s, s) for s in ICO_SIZES],
                    append_images=frames[:-1])

    buf = io.BytesIO()
    master.save(buf, format="PNG", optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    wrapped = "\n".join('    "%s"' % encoded[i:i + 96]
                        for i in range(0, len(encoded), 96))

    with open(os.path.join(here, "app_icon.py"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(f'''"""The app icon, embedded.

Generated by make_icon.py (design: {DESIGN}) -- do not edit by hand.

The PNG lives in the source so the window, title bar and tray icon all work
from the module alone, with no data file to locate next to a frozen exe.
"""

import base64
import io

from PIL import Image

ICON_PNG_B64 = (
{wrapped}
)

_cache = {{}}


def icon_image(size=None):
    """The icon as a PIL image, optionally resized. Results are cached."""
    if size in _cache:
        return _cache[size]
    img = Image.open(io.BytesIO(base64.b64decode(ICON_PNG_B64))).convert("RGBA")
    if size:
        img = img.resize((size, size), Image.Resampling.LANCZOS)
    _cache[size] = img
    return img


def icon_photo(size=None):
    """A Tk PhotoImage for use as the window icon."""
    from PIL import ImageTk
    return ImageTk.PhotoImage(icon_image(size))
''')

    print(f"design: {DESIGN}")
    print(f"wrote {png_path}")
    print(f"wrote {os.path.join(assets, 'icon.ico')} "
          f"({', '.join(str(s) for s in ICO_SIZES)})")
    print(f"wrote {os.path.join(here, 'app_icon.py')} "
          f"({len(encoded) // 1024} KB base64)")


if __name__ == "__main__":
    main()
