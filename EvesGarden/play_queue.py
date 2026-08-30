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

    # ------------------------------------------------------------- mutation

    def set_context(self, paths, start=None):
        """Replace the browsing list, optionally starting at one track."""
        with self._lock:
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
            if next_up:
                self._explicit[0:0] = list(paths)
            else:
                self._explicit.extend(paths)
        self._changed()

    def remove(self, path):
        with self._lock:
            if path in self._explicit:
                self._explicit.remove(path)
        self._changed()

    def move(self, path, delta):
        """Nudge a queued track up or down."""
        with self._lock:
            if path not in self._explicit:
                return
            i = self._explicit.index(path)
            j = max(0, min(len(self._explicit) - 1, i + delta))
            if i != j:
                self._explicit.insert(j, self._explicit.pop(i))
        self._changed()

    def clear(self):
        with self._lock:
            self._explicit.clear()
        self._changed()

    # ------------------------------------------------------------ traversal

    def next_path(self, shuffle=False, repeat=False, current=None):
        """What to play next, consuming the explicit queue first."""
        with self._lock:
            if current:
                self.history.append(current)
                del self.history[:-100]

            if self._explicit:
                return self._explicit.pop(0)

            if not self._context:
                return None
            if repeat and current:
                return current
            if shuffle:
                if len(self._context) == 1:
                    return self._context[0]
                choice = current
                while choice == current:
                    choice = random.choice(self._context)
                self._context_index = self._context.index(choice)
                return choice

            self._context_index += 1
            if self._context_index >= len(self._context):
                self._context_index = 0
            return self._context[self._context_index]

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
