"""The play queue.

Before this, "next track" simply meant the next row in whatever list happened
to be on screen, so there was no way to line something up without losing your
place. The queue sits in front of that: anything queued plays first, and when
it runs dry playback falls back to the browsing list exactly as it used to.

Kept deliberately small -- a list of paths, an index, and callbacks -- because
the interesting behaviour belongs to the player, not here.
"""

import random
import threading


class PlayQueue:
    def __init__(self, on_change=None):
        self.on_change = on_change
        self._lock = threading.Lock()
        self._explicit = []      # queued by hand; always plays first
        self._context = []       # the list you were browsing
        self._context_index = -1
        self.history = []
        # What peek_next() last committed to, as (source, path, args). The
        # decision has to be made once and remembered: with shuffle on, asking
        # twice gives two different answers, so the track that gets preloaded
        # would not be the track that plays.
        self._peeked = None

    # ------------------------------------------------------------- mutation

    def _forget_peek(self):
        self._peeked = None

    def set_context(self, paths, start=None):
        """Replace the browsing list, optionally starting at one track."""
        with self._lock:
            self._forget_peek()
            self._context = list(paths)
            self._context_index = (
                self._context.index(start)
                if start in self._context else (0 if self._context else -1))
        self._changed()

    def add(self, paths, next_up=False):
        """Queue tracks. `next_up` puts them immediately after the current one."""
        if isinstance(paths, str):
            paths = [paths]
        with self._lock:
            self._forget_peek()
            if next_up:
                self._explicit[0:0] = list(paths)
            else:
                self._explicit.extend(paths)
        self._changed()

    def remove(self, path):
        with self._lock:
            self._forget_peek()
            if path in self._explicit:
                self._explicit.remove(path)
        self._changed()

    def move(self, path, delta):
        """Nudge a queued track up or down."""
        with self._lock:
            self._forget_peek()
            if path not in self._explicit:
                return
            i = self._explicit.index(path)
            j = max(0, min(len(self._explicit) - 1, i + delta))
            if i != j:
                self._explicit.insert(j, self._explicit.pop(i))
        self._changed()

    def clear(self):
        with self._lock:
            self._forget_peek()
            self._explicit.clear()
        self._changed()

    # ------------------------------------------------------------ traversal

    def _decide(self, shuffle, repeat, current):
        """What plays next, as (source, path), touching nothing.

        Split out from next_path so the same answer can be handed to a
        preloader without consuming anything.
        """
        if self._explicit:
            return ("queue", self._explicit[0])
        if not self._context:
            return ("none", None)
        if repeat and current:
            return ("repeat", current)
        if shuffle:
            if len(self._context) == 1:
                return ("shuffle", self._context[0])
            choice = current
            while choice == current:
                choice = random.choice(self._context)
            return ("shuffle", choice)
        index = self._context_index + 1
        if index >= len(self._context):
            index = 0
        return ("context", self._context[index])

    def peek_next(self, shuffle=False, repeat=False, current=None):
        """What next_path() will return, without consuming it.

        The answer is committed, so the player can decode that track ahead of
        time and be certain it is the one that will play.
        """
        args = (shuffle, repeat, current)
        with self._lock:
            if self._peeked is None or self._peeked[2] != args:
                self._peeked = self._decide(*args) + (args,)
            return self._peeked[1]

    def next_path(self, shuffle=False, repeat=False, current=None):
        """What to play next, consuming the explicit queue first."""
        args = (shuffle, repeat, current)
        with self._lock:
            if current:
                self.history.append(current)
                del self.history[:-100]

            if self._peeked is not None and self._peeked[2] == args:
                source, path = self._peeked[0], self._peeked[1]
            else:
                source, path = self._decide(*args)
            self._peeked = None

            if source == "queue" and path in self._explicit:
                self._explicit.remove(path)
            elif source in ("shuffle", "context") and path in self._context:
                self._context_index = self._context.index(path)
            return path

    def previous_path(self):
        """Step back through what actually played, not the list order."""
        with self._lock:
            while self.history:
                path = self.history.pop()
                if path:
                    return path
        return None

    def note_playing(self, path):
        """Keep the context cursor on the track that is actually playing."""
        with self._lock:
            if path in self._context:
                self._context_index = self._context.index(path)

    # -------------------------------------------------------------- reading

    @property
    def upcoming(self):
        with self._lock:
            return list(self._explicit)

    @property
    def pending(self):
        return len(self._explicit)

    def context_after(self, limit=20):
        """What the browsing list would play next, once the queue empties."""
        with self._lock:
            if not self._context or self._context_index < 0:
                return []
            start = self._context_index + 1
            return self._context[start:start + limit]

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass
