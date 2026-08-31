"""Custom widgets for the surfaces CustomTkinter does not do well.

Two things here. The first is SeekBar, which replaces the CTkSlider that used
to be the progress bar. A stock slider is a thin grey rail with a dot on it:
it tells you the playhead position and nothing else. It has no hover state, so
there is no feedback that it is even interactive; no readout, so scrubbing to
"about two-thirds through" means letting go and checking; and no notion of a
buffered region, which the streaming preview path genuinely has.

The second is the artwork placeholder. Cover art is decoded on a worker
thread, so every art label started empty and then popped. In a list that is a
hundred rows of flicker as you scroll. A placeholder tile of the right size,
painted immediately, turns that into a fill.
"""

import math
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter

import motion
import theme_ui

_placeholders = {}
_gradients = {}


# ------------------------------------------------------------------ colours

def dominant_colour(image, fallback=None):
    """The strongest colour in an image, as "#rrggbb".

    colorgram is slow enough to be worth keeping off the UI thread -- callers
    are expected to already be on a worker.
    """
    try:
        import colorgram
        colours = colorgram.extract(image.resize((80, 80)), 3)
        if colours:
            c = colours[0].rgb
            return "#%02x%02x%02x" % (c.r, c.g, c.b)
    except Exception:
        pass
    return fallback


def luminance(hex_colour):
    try:
        r = int(hex_colour[1:3], 16) / 255
        g = int(hex_colour[3:5], 16) / 255
        b = int(hex_colour[5:7], 16) / 255
    except (ValueError, IndexError, TypeError):
        return 0.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def clamp_luminance(hex_colour, ceiling=0.32, fallback="#202020"):
    """Darken a colour until light text will read against it.

    Album covers are frequently pale, and a dominant colour used raw gave
    near-white text on a near-white panel.
    """
    if not hex_colour:
        return fallback
    try:
        lum = luminance(hex_colour)
        if lum <= ceiling:
            return hex_colour
        factor = ceiling / lum
        return "#%02x%02x%02x" % (
            int(int(hex_colour[1:3], 16) * factor),
            int(int(hex_colour[3:5], 16) * factor),
            int(int(hex_colour[5:7], 16) * factor),
        )
    except (ValueError, IndexError):
        return fallback


def lift_luminance(hex_colour, floor=0.62, fallback="#e8e8e8"):
    """The counterpart to clamp_luminance, for themes with dark text."""
    if not hex_colour:
        return fallback
    try:
        lum = luminance(hex_colour)
        if lum >= floor:
            return hex_colour
        return blend(hex_colour, "#ffffff",
                     min(0.85, (floor - lum) / max(0.05, 1.0 - lum)))
    except (ValueError, IndexError):
        return fallback


def readable_tint(hex_colour, ink, fallback):
    """Push a cover colour away from the text colour until they contrast.

    Which direction depends on the theme: nine of the palettes are dark with
    light text, but Rose Pine Dawn and Nordic Light are the other way round,
    and darkening a tint for them produced dark-on-dark.
    """
    if not hex_colour:
        return fallback
    if luminance(ink) >= 0.5:
        return clamp_luminance(hex_colour, 0.32, fallback)
    return lift_luminance(hex_colour, 0.62, fallback)


def _rgb(hex_colour):
    return (int(hex_colour[1:3], 16), int(hex_colour[3:5], 16),
            int(hex_colour[5:7], 16))


# ---------------------------------------------------------------- gradients

def gradient_image(width, height, top, bottom):
    """A vertical two-stop gradient, cached by its arguments.

    Built one pixel wide and stretched, which is both faster than drawing
    `height` separate lines and gives the same result.
    """
    width, height = max(1, int(width)), max(1, int(height))
    key = (width, height, top, bottom)
    hit = _gradients.get(key)
    if hit is not None:
        return hit

    t, b = _rgb(top), _rgb(bottom)
    strip = Image.new("RGB", (1, height))
    pixels = strip.load()
    for y in range(height):
        f = y / max(1, height - 1)
        pixels[0, y] = tuple(int(t[i] + (b[i] - t[i]) * f) for i in range(3))
    image = strip.resize((width, height), Image.Resampling.BILINEAR)

    if len(_gradients) > 40:
        _gradients.clear()
    _gradients[key] = image
    return image


# ------------------------------------------------------------- placeholders

def _note_mark(size, colour):
    """A quaver, drawn geometrically so it stays crisp at any size."""
    mark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(mark)
    u = size / 24.0
    head_r = 4.2 * u
    cx, cy = 8.5 * u, 17.5 * u
    d.ellipse([cx - head_r, cy - head_r * 0.82,
               cx + head_r, cy + head_r * 0.82], fill=colour)
    stem_x = cx + head_r - 0.9 * u
    d.rectangle([stem_x, 4.5 * u, stem_x + 1.5 * u, cy], fill=colour)
    d.polygon([(stem_x + 1.5 * u, 4.5 * u), (16.5 * u, 7.5 * u),
               (16.5 * u, 11.5 * u), (stem_x + 1.5 * u, 9.0 * u)], fill=colour)
    return mark


def placeholder_art(size, surface, ink):
    """A soft tile to stand in for cover art that is missing or still loading.

    Deliberately low contrast and deliberately neutral: this should read as
    "nothing here yet", not as a piece of artwork in its own right. Tinting it
    with the theme accent made a wall of them look like an intentional design
    element rather than absence, which is the opposite of the point -- so the
    tile is derived from the text colour instead.
    """
    size = int(size)
    key = (size, surface, ink)
    hit = _placeholders.get(key)
    if hit is not None:
        return hit

    top = motion.blend(surface, ink, 0.07)
    tile = gradient_image(size, size, top, surface).copy()

    glyph = max(12, int(size * 0.52))
    mark = _note_mark(glyph, _rgb(motion.blend(surface, ink, 0.80)))
    mark.putalpha(mark.getchannel("A").point(lambda a: int(a * 0.26)))
    tile.paste(mark, ((size - glyph) // 2, (size - glyph) // 2), mark)

    if len(_placeholders) > 60:
        _placeholders.clear()
    _placeholders[key] = tile
    return tile


def placeholder_ctk(size, surface, ink):
    """placeholder_art as a CTkImage, cached alongside it."""
    key = ("ctk", size, surface, ink)
    hit = _placeholders.get(key)
    if hit is None:
        art = placeholder_art(size, surface, ink)
        hit = ctk.CTkImage(light_image=art, dark_image=art, size=(size, size))
        _placeholders[key] = hit
    return hit


def crossfade(label, old_image, new_image, size, duration=motion.BASE,
              name="art"):
    """Dissolve one cover into the next inside a CTkLabel.

    Tk cannot animate opacity, so the blend happens in PIL and each frame is
    handed over as a fresh CTkImage. At 60fps for ~170 ms that is ten images,
    which is cheap next to the JPEG decode that produced them.
    """
    if old_image is None or old_image.size != new_image.size:
        image = ctk.CTkImage(light_image=new_image, dark_image=new_image,
                             size=(size, size))
        label._fade_image = image
        label.configure(image=image)
        return

    def step(t):
        frame = Image.blend(old_image, new_image, t)
        image = ctk.CTkImage(light_image=frame, dark_image=frame,
                             size=(size, size))
        # The label keeps no strong reference of its own, so the CTkImage has
        # to outlive the frame that built it or Tk paints an empty box.
        label._fade_image = image
        label.configure(image=image)

    motion.animate(label, duration, step, easing=motion.linear, name=name)


# ------------------------------------------------------------------ seek bar

class SeekBar(tk.Canvas):
    """A progress bar you can actually scrub.

    Filled track, a buffered region behind it, a knob that appears on hover,
    and -- the part the stock slider could never do -- a readout bubble above
    the cursor showing the position you are about to seek to.

    The value is only committed on release. Committing on every drag pixel
    (which is what a CTkSlider command does) reset the EQ filter state dozens
    of times a second, which is why the old code needed a separate _seeking
    flag in the App to suppress it. That logic now lives in here.
    """

    IDLE_H = 4.0
    HOVER_H = 6.0
    KNOB_R = 7

    def __init__(self, parent, theme, command=None, formatter=None,
                 on_scrub=None, on_drag=None, wheel_step=None, height=None,
                 **kwargs):
        self.theme = theme
        self.formatter = formatter          # value -> str, drives the bubble
        self.command = command              # committed value, on release
        self.on_drag = on_drag              # live value, on every drag pixel
        self.on_scrub = on_scrub            # called with True/False on drag
        self.wheel_step = wheel_step

        self._value = 0.0
        self._buffered = 1.0
        self._hover = False
        self._dragging = False
        self._hover_x = None
        self._thickness = self.IDLE_H
        # Vertical room reserved above the track for the readout bubble.
        self._bubble_pad = 20 if formatter else 0

        height = height or (self._bubble_pad + 14)
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         takefocus=0, **kwargs)

        # Items are created once and moved with coords(); deleting and
        # recreating them ten times a second flickered.
        self._bg_id = self.create_line(0, 0, 0, 0, capstyle=tk.ROUND)
        self._buf_id = self.create_line(0, 0, 0, 0, capstyle=tk.ROUND)
        self._fill_id = self.create_line(0, 0, 0, 0, capstyle=tk.ROUND)
        self._tick_id = self.create_line(0, 0, 0, 0, state="hidden")
        self._knob_id = self.create_oval(0, 0, 0, 0, width=0, state="hidden")
        self._bubble_id = self.create_line(0, 0, 0, 0, capstyle=tk.ROUND,
                                           state="hidden")
        self._bubble_txt = self.create_text(0, 0, text="", state="hidden",
                                            font=theme_ui.font("time"))

        self.set_palette(theme)

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        if wheel_step:
            self.bind("<MouseWheel>", self._on_wheel)
        self.configure(cursor="hand2")

    # ------------------------------------------------------------- palette

    def set_palette(self, theme, background=None):
        self.theme = theme
        base = background or theme["surface"]
        self._c_track = motion.blend(base, theme["text"], 0.16)
        self._c_buffer = motion.blend(base, theme["text"], 0.30)
        self._c_tick = motion.blend(base, theme["text"], 0.55)
        self.configure(bg=base)
        self.itemconfigure(self._bg_id, fill=self._c_track)
        self.itemconfigure(self._buf_id, fill=self._c_buffer)
        self.itemconfigure(self._fill_id, fill=theme["accent"])
        self.itemconfigure(self._tick_id, fill=self._c_tick)
        self.itemconfigure(self._knob_id, fill=theme["text"])
        self.itemconfigure(self._bubble_id, fill=theme["surface_hover"])
        self.itemconfigure(self._bubble_txt, fill=theme["text"])
        self._redraw()

    # --------------------------------------------------------------- value

    def set(self, value):
        """Move the playhead. Ignored while the user is scrubbing."""
        if self._dragging:
            return
        value = min(1.0, max(0.0, float(value or 0.0)))
        if abs(value - self._value) < 1e-4:
            return
        self._value = value
        self._redraw()

    def get(self):
        return self._value

    def set_buffered(self, fraction):
        fraction = min(1.0, max(0.0, float(fraction or 0.0)))
        if abs(fraction - self._buffered) < 1e-3:
            return
        self._buffered = fraction
        self._redraw()

    @property
    def scrubbing(self):
        return self._dragging

    # -------------------------------------------------------------- layout

    def _span(self):
        width = self.winfo_width()
        return self.KNOB_R, max(self.KNOB_R + 1, width - self.KNOB_R)

    def _centre_y(self):
        return self._bubble_pad + (self.winfo_height() - self._bubble_pad) / 2

    def _x_to_value(self, x):
        x0, x1 = self._span()
        return min(1.0, max(0.0, (x - x0) / float(x1 - x0)))

    def _value_to_x(self, value):
        x0, x1 = self._span()
        return x0 + (x1 - x0) * min(1.0, max(0.0, value))

    # --------------------------------------------------------------- paint

    def _redraw(self):
        if not self.winfo_exists() or self.winfo_width() <= 1:
            return
        x0, x1 = self._span()
        cy = self._centre_y()
        w = self._thickness

        self.coords(self._bg_id, x0, cy, x1, cy)
        self.itemconfigure(self._bg_id, width=w)

        buf_x = self._value_to_x(self._buffered)
        self.coords(self._buf_id, x0, cy, buf_x, cy)
        self.itemconfigure(
            self._buf_id, width=w,
            state="normal" if self._buffered > 0.001 else "hidden")

        fill_x = self._value_to_x(self._value)
        self.coords(self._fill_id, x0, cy, fill_x, cy)
        self.itemconfigure(
            self._fill_id, width=w,
            state="normal" if self._value > 0.001 else "hidden")

        show_knob = self._hover or self._dragging
        r = self.KNOB_R if show_knob else 0
        self.coords(self._knob_id, fill_x - r, cy - r, fill_x + r, cy + r)
        self.itemconfigure(self._knob_id,
                           state="normal" if show_knob else "hidden")

        self._paint_bubble(cy, w)

    def _paint_bubble(self, cy, thickness):
        showing = (self._hover and self._hover_x is not None
                   and self.formatter is not None)
        if not showing:
            for item in (self._bubble_id, self._bubble_txt, self._tick_id):
                self.itemconfigure(item, state="hidden")
            return

        x0, x1 = self._span()
        hx = min(x1, max(x0, self._hover_x))

        # A faint tick under the bubble, so it is obvious which point on the
        # track the readout refers to.
        self.coords(self._tick_id, hx, cy - thickness / 2 - 3,
                    hx, cy + thickness / 2 + 3)
        self.itemconfigure(self._tick_id, width=2, state="normal")

        try:
            text = self.formatter(self._x_to_value(hx))
        except Exception:
            text = ""
        if not text:
            self.itemconfigure(self._bubble_id, state="hidden")
            self.itemconfigure(self._bubble_txt, state="hidden")
            return

        by = self._bubble_pad / 2
        self.itemconfigure(self._bubble_txt, text=text, state="normal")
        self.coords(self._bubble_txt, hx, by)
        bounds = self.bbox(self._bubble_txt)
        if not bounds:
            return
        half = (bounds[2] - bounds[0]) / 2 + 6

        # Keep the whole bubble on-canvas at either end of the track.
        left, right = 2 + half, self.winfo_width() - 2 - half
        bx = min(right, max(left, hx)) if right > left else hx
        self.coords(self._bubble_txt, bx, by)

        pill_h = (bounds[3] - bounds[1]) + 8
        inset = min(half, pill_h / 2)
        self.coords(self._bubble_id, bx - half + inset, by,
                    bx + half - inset, by)
        self.itemconfigure(self._bubble_id, width=pill_h, state="normal")
        self.tag_raise(self._bubble_id)
        self.tag_raise(self._bubble_txt)

    def _animate_thickness(self, target):
        start = self._thickness

        def step(t):
            self._thickness = start + (target - start) * t
            self._redraw()

        motion.animate(self, motion.FAST, step, name="thickness")

    # -------------------------------------------------------------- events

    def _on_enter(self, event):
        self._hover = True
        self._hover_x = event.x
        self._animate_thickness(self.HOVER_H)

    def _on_leave(self, _event=None):
        if self._dragging:
            return
        self._hover = False
        self._hover_x = None
        self._animate_thickness(self.IDLE_H)

    def _on_motion(self, event):
        if not self._hover:
            return
        self._hover_x = event.x
        self._redraw()

    def _on_press(self, event):
        self._dragging = True
        self._hover = True
        self._hover_x = event.x
        self._value = self._x_to_value(event.x)
        if self.on_scrub:
            self.on_scrub(True)
        if self.on_drag:
            self.on_drag(self._value)
        self._redraw()

    def _on_drag(self, event):
        if not self._dragging:
            return
        self._hover_x = event.x
        self._value = self._x_to_value(event.x)
        # Volume wants to follow the cursor; seeking deliberately does not,
        # because committing mid-drag resets the EQ filter state.
        if self.on_drag:
            self.on_drag(self._value)
        self._redraw()

    def _on_release(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self._value = self._x_to_value(event.x)
        if self.command:
            self.command(self._value)
        if self.on_scrub:
            self.on_scrub(False)
        # Releasing outside the widget still has to end the hover state.
        inside = (0 <= event.x <= self.winfo_width()
                  and 0 <= event.y <= self.winfo_height())
        if inside:
            self._redraw()
        else:
            self._on_leave()

    def _on_wheel(self, event):
        direction = 1 if event.delta > 0 else -1
        self._value = min(1.0, max(0.0,
                                   self._value + direction * self.wheel_step))
        self._redraw()
        if self.command:
            self.command(self._value)
        return "break"


# -------------------------------------------------------------- glyph shapes

# Everything below is drawn in a 24x24 box and scaled to the widget, so one
# set of coordinates serves a 40px transport button and a 50px play button.
GLYPH_BOX = 24.0


def _pts(points, cx, cy, s):
    """Map 24x24 viewbox points onto canvas coordinates."""
    out = []
    for x, y in points:
        out.append(cx + (x - 12) * s)
        out.append(cy + (y - 12) * s)
    return out


def _g_play(c, cx, cy, s, col, w):
    return [c.create_polygon(_pts([(8.5, 5), (19.5, 12), (8.5, 19)], cx, cy, s),
                             fill=col, outline=col, width=w * 0.7,
                             joinstyle=tk.ROUND)]


def _g_pause(c, cx, cy, s, col, w):
    return [c.create_line(_pts([(9, 5.5), (9, 18.5)], cx, cy, s), fill=col,
                          width=w * 1.7, capstyle=tk.ROUND),
            c.create_line(_pts([(15, 5.5), (15, 18.5)], cx, cy, s), fill=col,
                          width=w * 1.7, capstyle=tk.ROUND)]


def _g_prev(c, cx, cy, s, col, w):
    return [c.create_line(_pts([(6, 5.5), (6, 18.5)], cx, cy, s), fill=col,
                          width=w * 1.4, capstyle=tk.ROUND),
            c.create_polygon(_pts([(19, 5.5), (19, 18.5), (8.5, 12)], cx, cy, s),
                             fill=col, outline=col, width=w * 0.7,
                             joinstyle=tk.ROUND)]


def _g_next(c, cx, cy, s, col, w):
    return [c.create_polygon(_pts([(5, 5.5), (5, 18.5), (15.5, 12)], cx, cy, s),
                             fill=col, outline=col, width=w * 0.7,
                             joinstyle=tk.ROUND),
            c.create_line(_pts([(18, 5.5), (18, 18.5)], cx, cy, s), fill=col,
                          width=w * 1.4, capstyle=tk.ROUND)]


def _g_shuffle(c, cx, cy, s, col, w):
    head = (w * 2.4, w * 2.8, w * 1.2)
    return [c.create_line(_pts([(3.5, 7.5), (7.5, 7.5), (16.5, 16.5),
                                (20.5, 16.5)], cx, cy, s),
                          fill=col, width=w, capstyle=tk.ROUND,
                          joinstyle=tk.ROUND, arrow=tk.LAST, arrowshape=head),
            c.create_line(_pts([(3.5, 16.5), (7.5, 16.5), (16.5, 7.5),
                                (20.5, 7.5)], cx, cy, s),
                          fill=col, width=w, capstyle=tk.ROUND,
                          joinstyle=tk.ROUND, arrow=tk.LAST, arrowshape=head)]


def _g_repeat(c, cx, cy, s, col, w):
    """A ring with a gap, drawn as a polyline so Tk orients the arrowhead.

    Two earlier attempts placed a triangle by hand: against a smoothed rounded
    rectangle the corners collapsed into a lump, and against an arc the head
    either buried itself in the stroke or spilled across the gap. Handing the
    curve to create_line and asking for arrow=LAST puts the head on the
    tangent, which is the one thing that makes it read as a loop.
    """
    r = 6.9
    steps = 28
    # Ends at 90 degrees -- the top -- travelling clockwise, so the head
    # points right, into a gap that sits between one and two o'clock.
    a0, sweep = 416.0, -326.0
    pts = []
    for i in range(steps + 1):
        a = math.radians(a0 + sweep * i / steps)
        pts.append((12 + r * math.cos(a), 12 - r * math.sin(a)))
    head = (w * 2.3, w * 2.7, w * 1.25)
    return [c.create_line(_pts(pts, cx, cy, s), fill=col, width=w,
                          capstyle=tk.ROUND, joinstyle=tk.ROUND,
                          smooth=True, arrow=tk.LAST, arrowshape=head)]


def _g_repeat_one(c, cx, cy, s, col, w):
    """As repeat, with a 1 inside.

    The digit is stroked rather than set in a font: at this size the ring's
    interior is about thirteen pixels across, and a real glyph small enough to
    fit inside it was not legible.
    """
    items = _g_repeat(c, cx, cy, s, col, w)
    items.append(c.create_line(
        _pts([(10.8, 10.5), (12.1, 9.3), (12.1, 15.0)], cx, cy, s),
        fill=col, width=w * 0.9, capstyle=tk.ROUND, joinstyle=tk.ROUND))
    return items


def _volume_cone(c, cx, cy, s, col, w):
    return c.create_polygon(
        _pts([(3.5, 9.5), (7, 9.5), (11, 5.5), (11, 18.5), (7, 14.5),
              (3.5, 14.5)], cx, cy, s),
        fill=col, outline=col, width=w * 0.6, joinstyle=tk.ROUND)


def _g_mute(c, cx, cy, s, col, w):
    return [_volume_cone(c, cx, cy, s, col, w),
            c.create_line(_pts([(14, 9.5), (19.5, 15)], cx, cy, s), fill=col,
                          width=w, capstyle=tk.ROUND),
            c.create_line(_pts([(19.5, 9.5), (14, 15)], cx, cy, s), fill=col,
                          width=w, capstyle=tk.ROUND)]


def _volume_waves(c, cx, cy, s, col, w, count):
    items = []
    for i in range(count):
        r = (4.2 + i * 3.4) * s
        ox = cx - 1.5 * s
        items.append(c.create_arc(ox - r, cy - r, ox + r, cy + r,
                                  start=-52, extent=104, style=tk.ARC,
                                  outline=col, width=w))
    return items


def _g_volume_low(c, cx, cy, s, col, w):
    return [_volume_cone(c, cx, cy, s, col, w)] + \
        _volume_waves(c, cx, cy, s, col, w, 1)


def _g_volume_high(c, cx, cy, s, col, w):
    return [_volume_cone(c, cx, cy, s, col, w)] + \
        _volume_waves(c, cx, cy, s, col, w, 2)


def _g_close(c, cx, cy, s, col, w):
    return [c.create_line(_pts([(7, 7), (17, 17)], cx, cy, s), fill=col,
                          width=w, capstyle=tk.ROUND),
            c.create_line(_pts([(17, 7), (7, 17)], cx, cy, s), fill=col,
                          width=w, capstyle=tk.ROUND)]


GLYPHS = {
    "close": _g_close,
    "play": _g_play,
    "pause": _g_pause,
    "prev": _g_prev,
    "next": _g_next,
    "shuffle": _g_shuffle,
    "repeat": _g_repeat,
    "repeat_one": _g_repeat_one,
    "mute": _g_mute,
    "volume_low": _g_volume_low,
    "volume_high": _g_volume_high,
}


class GlyphButton(tk.Canvas):
    """A transport control drawn as vectors rather than as a font glyph.

    The bar used text: U+1F500 SHUFFLE, U+23EE PREVIOUS TRACK, U+1F501 REPEAT.
    That is a bet that the user's font stack covers those code points, and on
    Windows it does not -- the fallback draws each one as a boxed outline, so
    the transport read as four grey rectangles either side of the play button.

    Drawing them takes the font out of the equation. It also turns hover and
    active into properties of the widget rather than a text-colour swap, so
    the controls can carry a hierarchy: the play button is filled and primary,
    everything else sits back in the secondary text colour until you point at
    it, and shuffle/repeat light up in the accent when they are on.
    """

    def __init__(self, parent, theme, glyph, size=40, command=None,
                 primary=False, background=None, glyph_scale=None,
                 stroke=None, **kwargs):
        self.theme = theme
        self._glyph = glyph
        self.command = command
        self.primary = primary
        self._size = size
        self._scale = glyph_scale or (0.46 if primary else 0.58)
        self._stroke = stroke or (2.4 if primary else 2.0)

        self._active = False
        self._hover = 0.0
        self._pressed = False
        self._items = []
        self._disc = None

        super().__init__(parent, width=size, height=size, highlightthickness=0,
                         bd=0, takefocus=0, **kwargs)
        self.set_palette(theme, background)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.configure(cursor="hand2")

    # ------------------------------------------------------------- palette

    def set_palette(self, theme, background=None):
        self.theme = theme
        self._bg = background or theme["surface"]
        self.configure(bg=self._bg)
        self._redraw()

    def set_glyph(self, glyph):
        if glyph == self._glyph:
            return
        self._glyph = glyph
        self._redraw()

    def set_active(self, active):
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        self._redraw()

    # --------------------------------------------------------------- paint

    def _colours(self):
        """Ink and disc for the current hover / active state.

        Hover lightens toward the theme's *text* colour, not toward
        accent_hover. In these palettes accent_hover is a second accent rather
        than a lighter first one -- Tokyo Night pairs blue with green,
        Synthwave pink with cyan -- so using it here made the control change
        hue under the cursor, which read as a glitch. Blending toward the text
        colour raises contrast in the dark themes and the two light ones
        alike.
        """
        t = self.theme
        lift = 0.18 * self._hover
        if self.primary:
            return t["bg"], motion.blend(t["accent"], t["text"], lift)
        if self._active:
            ink = motion.blend(t["accent"], t["text"], lift)
        else:
            ink = motion.blend(t["text_secondary"], t["text"], self._hover)
        return ink, motion.blend(self._bg, t["surface_hover"], self._hover)

    def _redraw(self):
        for item in self._items:
            self.delete(item)
        self._items = []
        if self._disc is not None:
            self.delete(self._disc)
            self._disc = None

        ink, disc = self._colours()
        c = self._size / 2.0
        # The whole control dips slightly while held, which is the only
        # feedback a canvas can give without a border.
        press = 0.94 if self._pressed else 1.0

        if self.primary:
            r = (self._size / 2.0 - 1) * (1 + 0.04 * self._hover) * press
            self._disc = self.create_oval(c - r, c - r, c + r, c + r,
                                          fill=disc, outline=disc)
        elif self._hover > 0.01:
            r = (self._size / 2.0 - 2) * press
            self._disc = self.create_oval(c - r, c - r, c + r, c + r,
                                          fill=disc, outline=disc)

        draw = GLYPHS.get(self._glyph)
        if draw is None:
            return
        s = self._size * self._scale / GLYPH_BOX * press
        self._items = draw(self, c, c, s, ink, self._stroke * press)

    def _animate_hover(self, target):
        start = self._hover

        def step(t):
            self._hover = start + (target - start) * t
            self._redraw()

        motion.animate(self, motion.FAST, step, name="hover")

    # -------------------------------------------------------------- events

    def _on_enter(self, _event=None):
        self._animate_hover(1.0)

    def _on_leave(self, _event=None):
        self._pressed = False
        self._animate_hover(0.0)

    def _on_press(self, _event=None):
        self._pressed = True
        self._redraw()

    def _on_release(self, event):
        was = self._pressed
        self._pressed = False
        self._redraw()
        inside = (0 <= event.x <= self._size and 0 <= event.y <= self._size)
        if was and inside and self.command:
            self.command()


# ------------------------------------------------------- now-playing artwork

def _cover_fill(image, width, height):
    """Scale to cover a box, then centre-crop -- never letterbox, never squash."""
    width, height = max(1, int(width)), max(1, int(height))
    sw, sh = image.size
    scale = max(width / sw, height / sh)
    box = (max(1, int(sw * scale)), max(1, int(sh * scale)))
    scaled = image.resize(box, Image.Resampling.BICUBIC)
    left = (box[0] - width) // 2
    top = (box[1] - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def blurred_backdrop(art, width, height, darken=0.42, floor="#000000"):
    """A cover blown up to fill the screen, blurred down to pure colour.

    Blurred at thumbnail scale and then upscaled. Running GaussianBlur over a
    1300px image costs about a fifth of a second and this reruns on every
    window resize; over a 260px one it costs nothing, and at this radius the
    difference does not survive the upscale anyway.

    The result is darkened hard. It is a ground for text to sit on, not a
    picture -- anything bright enough to read as artwork competes with the
    cover about to be laid on top of it.
    """
    width, height = max(1, int(width)), max(1, int(height))
    seed_w = 260
    seed_h = max(1, int(height * seed_w / float(width)))
    seed = _cover_fill(art.convert("RGB"), seed_w, seed_h)
    seed = seed.filter(ImageFilter.GaussianBlur(seed_w / 18.0))
    base = seed.resize((width, height), Image.Resampling.BICUBIC)

    # blend(a, b, t) is a*(1-t) + b*t, so this pulls every pixel toward the
    # floor colour while keeping its hue. A plain brightness scale washes the
    # whole thing toward grey instead. It is also one C call rather than a
    # Python loop over a million pixels, which is what this used to be.
    solid = Image.new("RGB", (width, height), _rgb(floor))
    return Image.blend(solid, base, darken)


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (size[0] - 1, size[1] - 1)],
                                           radius=radius, fill=255)
    return mask


def rounded_cover(art, size, radius=14, pad=52, blur=22, drop=16, opacity=0.62):
    """Cover art with rounded corners and a soft drop shadow, as RGBA.

    Returned canvas is `pad` larger on every side so the shadow has somewhere
    to fall; paste it with its own alpha and the shadow lands on whatever is
    behind.
    """
    size = int(size)
    cover = art.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
    mask = _rounded_mask((size, size), radius)

    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shade = Image.new("L", canvas.size, 0)
    shade.paste(mask.point(lambda a: int(a * opacity)), (pad, pad + drop))
    shadow.putalpha(shade.filter(ImageFilter.GaussianBlur(blur)))
    canvas = Image.alpha_composite(canvas, shadow)

    art_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    art_layer.paste(cover, (pad, pad))
    art_layer.putalpha(Image.new("L", canvas.size, 0))
    alpha = Image.new("L", canvas.size, 0)
    alpha.paste(mask, (pad, pad))
    art_layer.putalpha(alpha)
    return Image.alpha_composite(canvas, art_layer)


def scrim(width, height, colour="#000000", top=0.0, bottom=0.55):
    """A vertical alpha ramp, for holding text off a busy background."""
    width, height = max(1, int(width)), max(1, int(height))
    layer = Image.new("RGBA", (1, height), _rgb(colour) + (0,))
    px = layer.load()
    r, g, b = _rgb(colour)
    for y in range(height):
        f = y / max(1, height - 1)
        px[0, y] = (r, g, b, int(255 * (top + (bottom - top) * f)))
    return layer.resize((width, height), Image.Resampling.BILINEAR)


def compose_stage(art, width, height, cover_size, cover_xy, tint="#000000",
                  darken=0.42):
    """The whole left-hand picture as one image: backdrop plus floating cover.

    Doing this in PIL rather than stacking Tk widgets is what makes the drop
    shadow possible at all -- Tk has no alpha compositing between widgets, so
    a shadow can only exist if whatever is behind it is part of the same
    image.
    """
    stage = blurred_backdrop(art, width, height, darken=darken,
                             floor=tint).convert("RGBA")
    stage = Image.alpha_composite(stage, scrim(width, height, "#000000",
                                               0.0, 0.42))
    plate = rounded_cover(art, cover_size)
    x = int(cover_xy[0] - (plate.size[0] - cover_size) / 2)
    y = int(cover_xy[1] - (plate.size[1] - cover_size) / 2)
    stage.alpha_composite(plate, (x, y))
    return stage.convert("RGB")
