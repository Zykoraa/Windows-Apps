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

import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw

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
