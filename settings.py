"""Persisted UI state.

Every launch used to reset the theme, volume, EQ, shuffle, repeat, window
position and visualiser mode, and closing mid-track lost your place. A
`recent.json` was written on every play and never read back. This keeps all
of it in one JSON file and never lets a corrupt file stop the app starting.
"""

import json
import os
import threading

DEFAULTS = {
    "theme": "Spotify Classic",
    "volume": 1.0,
    "eq_gains": [1.0] * 10,
    "eq_preset": "Flat",
    "shuffle": False,
    "repeat": False,
    "visualizer_mode": 0,
    "visualizer_visible": False,
    "window": {"x": None, "y": None, "w": 1100, "h": 800},
    "library_view": "Songs",
    "library_sort": "Title",
    "last_track": None,
    "last_position": 0.0,
    "resume_on_launch": True,
    "download_jobs": 3,
    "download_quality": "192",
    "discord_presence": True,
    "discord_client_id": "",
}


class Settings:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self._data["window"] = dict(DEFAULTS["window"])
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            return  # missing or corrupt: defaults already in place
        if not isinstance(stored, dict):
            return
        for key, value in stored.items():
            if key in DEFAULTS:
                self._data[key] = value
        # A hand-edited file must not be able to crash the EQ.
        gains = self._data.get("eq_gains")
        if not isinstance(gains, list) or len(gains) != 10:
            self._data["eq_gains"] = list(DEFAULTS["eq_gains"])
        else:
            try:
                self._data["eq_gains"] = [
                    min(3.0, max(0.0, float(g))) for g in gains
                ]
            except (TypeError, ValueError):
                self._data["eq_gains"] = list(DEFAULTS["eq_gains"])
        if not isinstance(self._data.get("window"), dict):
            self._data["window"] = dict(DEFAULTS["window"])

    def save(self):
        """Write atomically: a crash mid-write must not truncate the file."""
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock:
                payload = json.dumps(self._data, indent=2)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def get(self, key, default=None):
        with self._lock:
            value = self._data.get(key, DEFAULTS.get(key, default))
        return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
