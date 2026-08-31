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
from PIL import Image, ImageDraw, ImageFilter, ImageTk

import motion
import theme_ui
import themes

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
        # motion.blend, not a bare blend: this module has no blend of its
        # own, and the only caller sat inside a try, so a light theme
        # silently got no page tint at all rather than an error.
        return motion.blend(hex_colour, "#ffffff",
                            min(0.85, (floor - lum) / max(0.05, 1.0 - lum)))
    except (ValueError, IndexError):
        return fallback


def readable_tint(hex_colour, ink, fallback, target=4.5):
    """Push a colour away from the ink until text on it is legible.

    Which way depends on the theme: nine of the palettes are dark with light
    text, but Rose Pine Dawn and Nordic Light are the other way round, and
    darkening a tint for those produced dark on dark.

    It steps until it actually reaches the contrast target rather than
    trusting a fixed luminance floor. A mid-tone accent against mid-tone ink
    -- Rose Pine Dawn's rose on its indigo -- cleared the floor while still
    only managing 2.85:1, which is not readable.
    """
    if not hex_colour:
        return fallback
    toward = "#000000" if themes.luminance(ink) >= 0.5 else "#ffffff"
    candidate = hex_colour
    for step in range(21):
        candidate = motion.blend(hex_colour, toward, step / 20.0)
        if themes.contrast(candidate, ink) >= target:
            return candidate
    return candidate


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
        # An image, not an oval: a canvas oval this small is all staircase.
        self._knob_id = self.create_image(0, 0, anchor="center",
                                          state="hidden")
        self._knob_cache = {}
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
        # Drop the item's reference before the cached sprites go. Tk goes on
        # pointing at a PhotoImage that Python has already collected, and
        # every later itemconfigure on the knob then raises "image ...
        # doesn't exist" -- which aborted apply_theme halfway through and
        # fired ten times a second while a track was playing.
        try:
            self.itemconfigure(self._knob_id, image="", state="hidden")
        except Exception:
            pass
        self._knob_cache = {}
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

        if self._hover or self._dragging:
            self.coords(self._knob_id, fill_x, cy)
            self.itemconfigure(self._knob_id, image=self._knob_sprite(),
                               state="normal")
        else:
            self.itemconfigure(self._knob_id, state="hidden")

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

    def _knob_sprite(self):
        """The knob, anti-aliased, cached per colour pair."""
        colour = self.theme["text"]
        background = self.cget("bg")
        key = (colour, background)
        sprite = self._knob_cache.get(key)
        if sprite is None:
            size = self.KNOB_R * 2 + 2
            surface = _AASurface(size, background)
            centre = size / 2.0
            surface.create_oval(centre - self.KNOB_R, centre - self.KNOB_R,
                                centre + self.KNOB_R, centre + self.KNOB_R,
                                fill=colour, outline=colour)
            sprite = ImageTk.PhotoImage(surface.finish())
            self._knob_cache[key] = sprite
        return sprite

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


def _g_search(c, cx, cy, s, col, w):
    r = 5.4
    box = _pts([(10.4 - r, 10.4 - r), (10.4 + r, 10.4 + r)], cx, cy, s)
    return [c.create_oval(box[0], box[1], box[2], box[3], outline=col,
                          width=w),
            c.create_line(_pts([(14.5, 14.5), (19.4, 19.4)], cx, cy, s),
                          fill=col, width=w, capstyle=tk.ROUND)]


def _g_leaf(c, cx, cy, s, col, w):
    """The app mark: a record with a sprout growing out of it.

    Eve's Garden in one silhouette. A filled disc survives being drawn at
    twenty pixels in a header where an outlined leaf turns to mush, and the
    two leaves read even when they are three pixels across. The spindle hole
    is punched in the canvas background rather than left transparent, because
    a Tk canvas item has no alpha to punch with.
    """
    ground = c.cget("bg")
    r = 6.9
    disc = _pts([(12 - r, 15.9 - r), (12 + r, 15.9 + r)], cx, cy, s)
    items = [c.create_oval(disc[0], disc[1], disc[2], disc[3],
                           fill=col, outline=col)]
    hole = _pts([(10.8, 14.7), (13.2, 17.1)], cx, cy, s)
    items.append(c.create_oval(hole[0], hole[1], hole[2], hole[3],
                               fill=ground, outline=ground))
    items.append(c.create_line(_pts([(12, 9.6), (12, 2.0)], cx, cy, s),
                               fill=col, width=max(1.0, w * 1.05),
                               capstyle=tk.ROUND))
    for flip in (1, -1):
        items.append(c.create_polygon(
            _pts([(12, 7.4), (12 + flip * 3.4, 6.4), (12 + flip * 5.4, 1.9),
                  (12 + flip * 1.5, 2.6)], cx, cy, s),
            fill=col, outline=col, width=max(0.5, w * 0.35),
            joinstyle=tk.ROUND, smooth=True))
    return items


GLYPHS = {
    "close": _g_close,
    "search": _g_search,
    "leaf": _g_leaf,
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



# --------------------------------------------------- anti-aliased rendering

# A Tk canvas does not anti-alias anything. A fifty-pixel filled circle comes
# out with visibly stepped edges, which is what made the play button look
# ragged, and every diagonal in the transport had the same staircase on it.
# Tk has no way to turn that on, so the drawing is done in PIL at four times
# the size and scaled back down, which costs about a millisecond a frame.
SUPERSAMPLE = 4


def _flat(points):
    out = []
    for x, y in points:
        out.extend((x, y))
    return out


def _smooth_points(points, closed=False, steps=8):
    """Subdivide through the points, standing in for Tk's smooth=True.

    Catmull-Rom rather than Tk's B-spline: it passes through the control
    points instead of being pulled inside them, which for these shapes is
    both closer to what was drawn and slightly better looking.
    """
    if len(points) < 3:
        return points
    pts = list(points)
    pts = ([pts[-1]] + pts + [pts[0], pts[1]] if closed
           else [pts[0]] + pts + [pts[-1]])
    out = []
    for i in range(len(pts) - 3):
        p0, p1, p2, p3 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        for s in range(steps):
            t = s / float(steps)
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t
                       + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                       + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                for k in (0, 1)))
    out.append(pts[-2])
    return out


class _AASurface:
    """Enough of the Tk canvas drawing API for the glyph functions.

    The glyphs are written against a canvas, and they are also drawn straight
    onto one in a few places, so rather than rewriting them for PIL this
    stands in for the canvas and records the same calls into an image.
    """

    def __init__(self, size, background, scale=SUPERSAMPLE):
        self.scale = scale
        self._bg = background
        self.size = size
        self.image = Image.new("RGB", (int(size * scale), int(size * scale)),
                               background)
        self.draw = ImageDraw.Draw(self.image)

    def cget(self, option):
        # _g_leaf asks for the background: it punches the spindle hole with it.
        return self._bg if option == "bg" else ""

    # ------------------------------------------------------------ helpers

    def _points(self, args):
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            flat = list(args[0])
        else:
            flat = list(args)
        return [(flat[i] * self.scale, flat[i + 1] * self.scale)
                for i in range(0, len(flat) - 1, 2)]

    def _dot(self, point, radius, fill):
        x, y = point
        self.draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                          fill=fill)

    def _arrowhead(self, points, shape, fill):
        _d1, d2, d3 = shape
        d2, d3 = d2 * self.scale, d3 * self.scale
        (x1, y1), (x2, y2) = points[-2], points[-1]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        bx, by = x2 - ux * d2, y2 - uy * d2
        px, py = -uy * d3, ux * d3
        self.draw.polygon([(x2, y2), (bx + px, by + py), (bx - px, by - py)],
                          fill=fill)

    @staticmethod
    def _box(points):
        (x0, y0), (x1, y1) = points[0], points[1]
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    @staticmethod
    def _colour(value):
        return value or None

    # ------------------------------------------------- the canvas subset

    def create_line(self, *args, **kw):
        if kw.get("state") == "hidden":
            return 0
        points = self._points(args)
        if kw.get("smooth"):
            points = _smooth_points(points)
        fill = self._colour(kw.get("fill"))
        width = max(1.0, kw.get("width", 1) * self.scale)
        if fill and len(points) >= 2:
            self.draw.line(_flat(points), fill=fill, width=int(round(width)),
                           joint="curve")
            if kw.get("capstyle") == "round":
                for end in (points[0], points[-1]):
                    self._dot(end, width / 2.0, fill)
            if kw.get("arrow"):
                self._arrowhead(points, kw.get("arrowshape", (8, 10, 3)), fill)
        return 0

    def create_oval(self, *args, **kw):
        if kw.get("state") == "hidden":
            return 0
        box = self._box(self._points(args))
        outline = self._colour(kw.get("outline"))
        self.draw.ellipse(box, fill=self._colour(kw.get("fill")),
                          outline=outline,
                          width=int(round(max(1.0, kw.get("width", 1)
                                              * self.scale))))
        return 0

    def create_polygon(self, *args, **kw):
        if kw.get("state") == "hidden":
            return 0
        points = self._points(args)
        if kw.get("smooth"):
            points = _smooth_points(points, closed=True)
        if len(points) >= 3:
            self.draw.polygon(_flat(points), fill=self._colour(kw.get("fill")),
                              outline=self._colour(kw.get("outline")))
        return 0

    def create_arc(self, *args, **kw):
        if kw.get("state") == "hidden":
            return 0
        box = self._box(self._points(args))
        start = float(kw.get("start", 0.0))
        extent = float(kw.get("extent", 90.0))
        # Tk measures degrees anticlockwise from three o'clock; PIL measures
        # them clockwise, because its y axis points down.
        self.draw.arc(box, -(start + extent), -start,
                      fill=self._colour(kw.get("outline")),
                      width=int(round(max(1.0, kw.get("width", 1)
                                          * self.scale))))
        return 0

    def finish(self):
        return self.image.resize((self.size, self.size),
                                 Image.Resampling.LANCZOS)


def render_glyph(glyph, size, colour, background, stroke=1.9, fill=0.66,
                 disc=None, disc_radius=None, press=1.0):
    """One glyph, optionally on a filled disc, as an anti-aliased image."""
    surface = _AASurface(size, background)
    centre = size / 2.0
    if disc and disc_radius and disc_radius > 0:
        surface.create_oval(centre - disc_radius, centre - disc_radius,
                            centre + disc_radius, centre + disc_radius,
                            fill=disc, outline=disc)
    draw = GLYPHS.get(glyph)
    if draw:
        draw(surface, centre, centre, size * fill / GLYPH_BOX * press,
             colour, stroke * press)
    return surface.finish()


def glyph_canvas(parent, glyph, size=20, colour="#ffffff",
                 background="#000000", stroke=1.9, fill=0.66):
    """A drawn glyph with no behaviour -- an icon rather than a control."""
    canvas = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                       bd=0, takefocus=0, bg=background)
    canvas._glyph_spec = (glyph, size, fill, stroke)
    canvas._glyph_item = None
    repaint_glyph(canvas, colour, background)
    return canvas


def repaint_glyph(canvas, colour, background):
    """Redraw a glyph_canvas in new colours.

    Canvas items cannot be recoloured as a group, and for the app mark the
    background is not merely behind the glyph -- it is what punches the
    spindle hole out of it -- so a theme change has to redraw rather than
    reconfigure.
    """
    glyph, size, fill, stroke = getattr(canvas, "_glyph_spec",
                                        (None, 20, 0.66, 1.9))
    canvas.configure(bg=background)
    image = render_glyph(glyph, size, colour, background, stroke, fill)
    canvas._glyph_photo = ImageTk.PhotoImage(image)
    item = getattr(canvas, "_glyph_item", None)
    if item is None:
        canvas._glyph_item = canvas.create_image(0, 0, anchor="nw",
                                                 image=canvas._glyph_photo)
    else:
        canvas.itemconfigure(item, image=canvas._glyph_photo)


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
        self._photo = None
        self._item = None

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
        ink, disc = self._colours()
        # The whole control dips slightly while held, which is the only
        # feedback a canvas can give without a border.
        press = 0.94 if self._pressed else 1.0

        radius = 0.0
        if self.primary:
            radius = (self._size / 2.0 - 1) * (1 + 0.04 * self._hover) * press
        elif self._hover > 0.01:
            radius = (self._size / 2.0 - 2) * press

        image = render_glyph(self._glyph, self._size, ink, self._bg,
                             stroke=self._stroke, fill=self._scale,
                             disc=disc if radius else None,
                             disc_radius=radius, press=press)
        self._photo = ImageTk.PhotoImage(image)
        if self._item is None:
            self._item = self.create_image(0, 0, anchor="nw",
                                           image=self._photo)
        else:
            self.itemconfigure(self._item, image=self._photo)

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


def rounded_cover(art, size, radius=14, pad=52, blur=22, drop=16,
                  opacity=0.62, rim=40):
    """Cover art with rounded corners, a drop shadow and a hairline rim.

    Returned canvas is `pad` larger on every side so the shadow has somewhere
    to fall; paste it with its own alpha and the shadow lands on whatever is
    behind.

    The rim is not decoration. Plenty of covers are near-black, and against a
    backdrop derived from that same near-black cover they had no visible edge
    at all -- the shadow disappears too, because there is nothing for it to
    fall against. A one-pixel light edge defines the sleeve whatever the
    artwork does.
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
    plate = Image.alpha_composite(canvas, art_layer)

    if rim:
        edge = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(
            [(pad, pad), (pad + size - 1, pad + size - 1)],
            radius=radius, outline=(255, 255, 255, int(rim)), width=1)
        plate = Image.alpha_composite(plate, edge)
    return plate


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
