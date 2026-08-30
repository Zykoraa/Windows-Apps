"""Short animations, so panels stop snapping in and out of existence.

Every transient surface in this app appeared instantly: overlays were
place()d and place_forget()n, album art was swapped in a single configure()
call, hover states flipped between two colours with nothing in between. No
individual frame was wrong, but the result read as abrupt -- the app looked
like it was cutting between slides rather than moving.

Tk has no animation support, so this is the usual after()-driven tween: a
normalised 0..1 clock, an easing curve, and a per-widget token so that
re-triggering an animation cancels the one already running instead of leaving
two of them fighting over the same property.

The clock is wall-time, not a frame count. A dropped frame should shorten the
animation, not stretch it -- an overlay that takes 400 ms to open because the
library was mid-render is worse than one that jumps the last third.
"""

import time

# 120 ms is the low end of "perceptible but not waiting": short enough that
# the UI still feels immediate, long enough that the eye tracks the movement
# instead of just noticing that something changed.
FAST = 120
BASE = 170
SLOW = 240

# Set False to make every animation resolve on its first frame. Useful when
# profiling, and the hook a "reduce motion" setting would hang off.
enabled = True

_running = {}


def ease_out_cubic(t):
    """Fast to start, settling at the end. The default for things entering."""
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t):
    """Symmetric. For things that move between two on-screen positions."""
    if t < 0.5:
        return 4 * t * t * t
    return 1 - ((-2 * t + 2) ** 3) / 2


def linear(t):
    return t


def blend(a, b, t):
    """Mix two "#rrggbb" colours. Used for hover tints and cross-fades."""
    try:
        ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
        br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    except (ValueError, IndexError, TypeError):
        return b
    t = min(1.0, max(0.0, t))
    return "#{:02x}{:02x}{:02x}".format(
        int(ar + (br - ar) * t),
        int(ag + (bg - ag) * t),
        int(ab + (bb - ab) * t),
    )


def cancel(widget, name="default"):
    """Drop any animation registered under this widget and name."""
    _running.pop((id(widget), name), None)


def animate(widget, duration, step, done=None, easing=ease_out_cubic,
            fps=60, name="default"):
    """Call step(eased_t) roughly every frame for `duration` milliseconds.

    `name` namespaces animations on one widget, so an overlay can fade and
    slide at once without each cancelling the other. Starting an animation
    with a name already in flight supersedes the old one -- the stale tick
    sees a token mismatch on its next frame and returns without touching
    anything.

    step() is always called once with 1.0 before done(), including when
    animation is disabled, so callers can treat this as "reach the end state,
    possibly gradually" rather than having to set the final values twice.
    """
    key = (id(widget), name)

    if not enabled or duration <= 0:
        _running.pop(key, None)
        step(1.0)
        if done:
            done()
        return

    token = _running.get(key, 0) + 1
    _running[key] = token
    start = time.perf_counter()
    interval = max(1, int(1000 / fps))

    def tick():
        if _running.get(key) != token:
            return                      # superseded by a later animate() call
        try:
            if not widget.winfo_exists():
                _running.pop(key, None)
                return
        except Exception:
            # The interpreter is going away; there is nothing left to animate.
            _running.pop(key, None)
            return

        elapsed = (time.perf_counter() - start) * 1000
        t = min(1.0, elapsed / duration)
        try:
            step(easing(t))
        except Exception:
            _running.pop(key, None)
            return

        if t >= 1.0:
            _running.pop(key, None)
            if done:
                try:
                    done()
                except Exception:
                    pass
            return
        try:
            widget.after(interval, tick)
        except Exception:
            _running.pop(key, None)

    tick()
