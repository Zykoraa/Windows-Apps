"""Discord Rich Presence: show what's playing, with cover art.

Everything here is best-effort. Discord not running, no application ID
configured, the socket dropping mid-song -- none of it may interfere with
playback, so every call is guarded and failures only ever disable the
feature until the next reconnect attempt.

Setup, once:
  1. https://discord.com/developers/applications -> New Application.
     Name it whatever you want shown as the app name in Discord.
  2. Copy the Application ID.
  3. Put DISCORD_CLIENT_ID=<that id> in your .env.
"""

import asyncio
import os
import threading
import time

RECONNECT_SECONDS = 30
# Discord rejects details/state shorter than 2 characters and truncates at 128.
_MIN, _MAX = 2, 128


def _fit(value, fallback="—"):
    text = (value or "").strip()
    if len(text) < _MIN:
        text = (text + " " + fallback).strip()
    return text[:_MAX]


class DiscordPresence:
    """Pushes now-playing state to Discord, on its own thread."""

    def __init__(self, client_id=None, enabled=True):
        self.client_id = (client_id or os.getenv("DISCORD_CLIENT_ID") or "").strip()
        self.enabled = bool(enabled)
        self.last_error = None

        self._rpc = None
        self._connected = False
        self._next_attempt = 0.0
        self._lock = threading.Lock()
        self._pending = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._last_payload = None

    # ------------------------------------------------------------- lifecycle

    @property
    def available(self):
        return bool(self.client_id) and self.enabled

    @property
    def connected(self):
        return self._connected

    def start(self):
        if not self.available or self._thread is not None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self._disconnect()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if not self.enabled:
            self.clear()
            self.stop()
        else:
            self.start()

    # ---------------------------------------------------------------- update

    def update(self, title, artist, album=None, cover_url=None,
               position=0.0, duration=0.0, playing=True):
        """Queue a presence update. Cheap and safe to call often."""
        if not self.available:
            return

        payload = {
            "details": _fit(title, "Unknown title"),
            "state": _fit(f"by {artist}" if artist else "", "Unknown artist"),
            "large_image": cover_url or "album",
            "large_text": _fit(album or title, "Eve's Garden"),
            "small_image": "play" if playing else "pause",
            "small_text": "Playing" if playing else "Paused",
        }
        if playing and duration and duration > 0:
            now = time.time()
            start = now - max(0.0, position)
            payload["start"] = int(start)
            payload["end"] = int(start + duration)

        # Skip no-op churn: the progress loop calls this every tick.
        comparable = {k: v for k, v in payload.items() if k not in ("start", "end")}
        if comparable == self._last_payload and playing:
            return
        self._last_payload = comparable

        with self._lock:
            self._pending = payload
        self._wake.set()

    def clear(self):
        with self._lock:
            self._pending = "clear"
        self._last_payload = None
        self._wake.set()

    # ------------------------------------------------------------- internals

    def _connect(self):
        if self._connected or time.time() < self._next_attempt:
            return self._connected
        try:
            from pypresence import Presence
        except ImportError:
            self.last_error = "pypresence is not installed"
            self._next_attempt = time.time() + 3600
            return False

        try:
            self._rpc = Presence(self.client_id)
            self._rpc.connect()
            self._connected = True
            self.last_error = None
        except Exception as e:
            # Discord closed, not installed, or the id is wrong. Back off.
            self._rpc = None
            self._connected = False
            self.last_error = f"{type(e).__name__}: {e}"
            self._next_attempt = time.time() + RECONNECT_SECONDS
        return self._connected

    def _disconnect(self):
        rpc, self._rpc = self._rpc, None
        self._connected = False
        if rpc is not None:
            try:
                rpc.clear()
            except Exception:
                pass
            try:
                rpc.close()
            except Exception:
                pass
            # pypresence leaves its asyncio loop open; without this, closing
            # the socket can emit "I/O operation on closed pipe" from a
            # __del__ at interpreter shutdown, into the app's stderr log.
            loop = getattr(rpc, "loop", None)
            if loop is not None:
                try:
                    if not loop.is_closed():
                        # Cancel pypresence's pending reader first, or closing
                        # the loop trades one warning for another.
                        for task in asyncio.all_tasks(loop):
                            task.cancel()
                        loop.close()
                except Exception:
                    pass

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            if self._stop.is_set():
                break

            with self._lock:
                payload, self._pending = self._pending, None
            if payload is None:
                continue
            if not self._connect():
                continue

            try:
                if payload == "clear":
                    self._rpc.clear()
                else:
                    self._rpc.update(**payload)
            except Exception as e:
                # A dropped pipe surfaces here; drop the connection and let
                # the next update retry after the backoff.
                self.last_error = f"{type(e).__name__}: {e}"
                self._disconnect()
                self._next_attempt = time.time() + RECONNECT_SECONDS
        self._disconnect()
