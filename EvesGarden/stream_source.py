"""Play audio straight from a remote URL, without saving it first.

ffmpeg pulls the stream and writes raw PCM to a pipe; a reader thread keeps a
bounded buffer full and the audio thread pops from it. Measured on a typical
track: first PCM about half a second after ffmpeg starts, and buffering runs
roughly six times faster than playback, so the buffer stays ahead.

Seeking restarts ffmpeg with -ss rather than trying to rewind a pipe, which
is the only thing a pipe cannot do.
"""

import subprocess
import threading
import time
from collections import deque

import numpy as np

BYTES_PER_FRAME = 4          # s16le stereo
TARGET_SECONDS = 12          # how far ahead to buffer
READ_CHUNK = 1 << 16


class StreamSource:
    """A seekable PCM feed backed by ffmpeg reading a URL."""

    def __init__(self, url, ffmpeg, sample_rate=44100, channels=2,
                 duration=None, on_error=None):
        self.url = url
        self.ffmpeg = ffmpeg
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration or 0.0
        self.on_error = on_error

        self._proc = None
        self._reader = None
        self._buffer = deque()
        self._buffered = 0
        self._lock = threading.Lock()
        self._space = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._eof = False
        self._started_at = 0.0     # stream position ffmpeg was told to start at
        self._frames_read = 0
        self.error = None

    # ------------------------------------------------------------- lifecycle

    @property
    def max_bytes(self):
        return TARGET_SECONDS * self.sample_rate * BYTES_PER_FRAME

    def start(self, offset=0.0):
        self.stop()
        self._stop.clear()
        self._eof = False
        self._buffer.clear()
        self._buffered = 0
        self._started_at = max(0.0, offset)
        self._frames_read = 0

        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            # A dropped connection mid-song should recover rather than end
            # playback.
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
        if offset > 0:
            command += ["-ss", f"{offset:.3f}"]
        command += [
            "-i", self.url,
            "-vn",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate), "-ac", str(self.channels),
            "-",
        ]

        creation = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=creation,
            )
        except Exception as e:
            self.error = f"Could not start ffmpeg: {e}"
            if self.on_error:
                self.on_error(self.error)
            return False

        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return True

    def stop(self):
        self._stop.set()
        with self._space:
            self._space.notify_all()
        proc, self._proc = self._proc, None
        if proc is not None:
            for finish in (proc.terminate, proc.kill):
                try:
                    finish()
                    proc.wait(timeout=1.0)
                    break
                except Exception:
                    continue
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
        reader, self._reader = self._reader, None
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.5)
        with self._lock:
            self._buffer.clear()
            self._buffered = 0

    # ---------------------------------------------------------------- reading

    def _pump(self):
        """Fill the buffer, blocking while it is already full."""
        proc = self._proc
        try:
            while not self._stop.is_set() and proc and proc.stdout:
                with self._space:
                    while (self._buffered >= self.max_bytes
                           and not self._stop.is_set()):
                        self._space.wait(0.2)
                if self._stop.is_set():
                    break
                data = proc.stdout.read(READ_CHUNK)
                if not data:
                    self._eof = True
                    break
                with self._lock:
                    self._buffer.append(data)
                    self._buffered += len(data)
        except Exception as e:
            self.error = str(e)
        finally:
            self._eof = True
            if proc is not None and proc.poll() not in (0, None) and not self._stop.is_set():
                try:
                    detail = (proc.stderr.read() or b"").decode("utf-8", "replace")
                except Exception:
                    detail = ""
                if detail.strip():
                    self.error = detail.strip().splitlines()[-1][:200]
                    if self.on_error:
                        self.on_error(self.error)

    def read(self, frames):
        """Up to `frames` of int16 audio, shaped (n, channels).

        Returns an empty array while waiting on the network, and None once
        the stream is finished.
        """
        wanted = frames * BYTES_PER_FRAME
        deadline = time.time() + 5.0
        while True:
            with self._lock:
                have = self._buffered
            if have >= wanted or self._eof or self._stop.is_set():
                break
            if time.time() > deadline:
                break          # underrun; hand back silence and keep going
            time.sleep(0.02)

        with self._lock:
            if not self._buffer and self._eof:
                return None
            out = bytearray()
            while self._buffer and len(out) < wanted:
                block = self._buffer[0]
                need = wanted - len(out)
                if len(block) <= need:
                    out += block
                    self._buffer.popleft()
                    self._buffered -= len(block)
                else:
                    out += block[:need]
                    self._buffer[0] = block[need:]
                    self._buffered -= need
        with self._space:
            self._space.notify_all()

        if not out:
            return np.zeros((0, self.channels), dtype=np.int16)

        usable = (len(out) // BYTES_PER_FRAME) * BYTES_PER_FRAME
        samples = np.frombuffer(bytes(out[:usable]), dtype=np.int16)
        self._frames_read += usable // BYTES_PER_FRAME
        return samples.reshape((-1, self.channels))

    # --------------------------------------------------------------- position

    def position(self):
        return self._started_at + self._frames_read / float(self.sample_rate)

    def seek(self, seconds):
        self.start(offset=max(0.0, seconds))

    @property
    def buffered_seconds(self):
        with self._lock:
            return self._buffered / (self.sample_rate * BYTES_PER_FRAME)
