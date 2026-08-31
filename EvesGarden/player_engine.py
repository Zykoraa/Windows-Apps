import os
import sys
import threading
import time

import numpy as np
import pyaudio
from scipy.signal import lfilter, lfilter_zi
from pydub import AudioSegment

from stream_source import StreamSource

# Centre frequencies of the ten EQ bands, matching the slider labels.
EQ_FREQS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_Q = 1.41  # roughly one octave per band

MIN_DB, MAX_DB = -24.0, 12.0
NUM_VIS_BANDS = 16


def _design_peaking(f0, q, gain_db, fs):
    """RBJ cookbook peaking-EQ biquad, normalised so a0 == 1.

    At gain_db == 0 the numerator and denominator are identical, so the band
    is a bit-exact pass-through. That is what makes it safe to run all ten
    bands all the time and to move a slider without any audible discontinuity.
    """
    a_gain = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * min(f0, fs * 0.45) / fs
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)

    b = np.array([1 + alpha * a_gain, -2 * cos_w0, 1 - alpha * a_gain])
    a = np.array([1 + alpha / a_gain, -2 * cos_w0, 1 - alpha / a_gain])
    return b / a[0], a / a[0]


def _linear_to_db(gain):
    """Map the 0..3 slider scale onto decibels, with 1.0 landing exactly on 0 dB."""
    gain = max(float(gain), 1e-3)
    return float(np.clip(20.0 * np.log10(gain), MIN_DB, MAX_DB))


def _soft_clip(x, threshold=0.95):
    """Round off peaks instead of clipping them square.

    Boosting several EQ bands easily pushes samples past 1.0; hard clipping
    there produces the crunchy distortion the old summed filterbank was
    prone to. Only samples above the threshold are touched.
    """
    peak = np.max(np.abs(x)) if x.size else 0.0
    if peak <= threshold:
        return x
    over = np.abs(x) > threshold
    headroom = 1.0 - threshold
    x = x.copy()
    x[over] = np.sign(x[over]) * (
        threshold + headroom * np.tanh((np.abs(x[over]) - threshold) / headroom)
    )
    return x


class PlayerEngine:
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.audio_data = None          # int16, shape (frames, channels)
        self.sample_rate = 44100
        self.channels = 2
        self.playing = False
        self.paused = False
        self.current_frame = 0
        self.chunk_size = 2048
        self.volume = 1.0
        self.last_error = None

        self.eq_gains = [1.0] * 10
        self.visualizer_callback = None
        self.on_track_end_callback = None
        # Fired from the audio thread when one track rolls into the next
        # without the stream stopping. The listener must marshal to its own
        # thread; nothing here is safe to touch a UI from.
        self.on_track_advanced_callback = None
        # The UI polls smoothed_bands from its own thread. The audio thread
        # must never call into Tk: doing so made it block on the Tcl lock
        # every chunk, which starved playback down to a few percent of
        # realtime. Set visualizer_enabled to skip the FFT when nothing is
        # displaying it.
        self.visualizer_enabled = False
        self.smoothed_bands = np.zeros(NUM_VIS_BANDS)

        # Set while playing a remote URL instead of a decoded local file.
        self.stream = None
        self.stream_title = None

        self.thread = None
        self._stop_flag = threading.Event()
        self._load_lock = threading.Lock()
        self._load_generation = 0

        # The next track, decoded while this one is still playing.
        self.gapless = True
        self._preload_lock = threading.Lock()
        self._preload_path = None
        self._preloaded = None

        self._eq_active = False
        self._filters = []
        self._z = []
        self._rebuild_filters()

    # ------------------------------------------------------------------ EQ

    def _rebuild_filters(self):
        """Recompute the biquad cascade for the current gains and sample rate."""
        gains_db = [_linear_to_db(g) for g in self.eq_gains]
        self._filters = [
            _design_peaking(f, EQ_Q, db, self.sample_rate)
            for f, db in zip(EQ_FREQS, gains_db)
        ]
        self._eq_active = any(abs(db) > 0.01 for db in gains_db)
        self.reset_filters()

    def reset_filters(self):
        """Clear filter memory; call after a seek so no old audio smears in."""
        self._z = []
        for b, a in self._filters:
            zi = lfilter_zi(b, a)
            self._z.append(np.zeros((zi.shape[0], self.channels)))

    def set_eq(self, gains):
        """gains: ten linear multipliers, 1.0 meaning flat."""
        self.eq_gains = list(gains)
        self._rebuild_filters()

    def set_volume(self, volume):
        self.volume = float(np.clip(volume, 0.0, 1.0))

    def apply_eq(self, chunk):
        """Run the cascade in series.

        The original code summed ten parallel band-pass filters, which combs
        the signal: overlapping bands cancel each other, so even 'flat' gains
        did not reconstruct the input. A serial cascade of peaking filters is
        unity at 0 dB and only shapes what the sliders actually ask for.
        """
        if not self._eq_active:
            return chunk

        out = chunk
        for i, (b, a) in enumerate(self._filters):
            filtered = np.empty_like(out)
            for ch in range(self.channels):
                filtered[:, ch], self._z[i][:, ch] = lfilter(
                    b, a, out[:, ch], zi=self._z[i][:, ch]
                )
            out = filtered
        return out

    # -------------------------------------------------------------- loading

    def _pick_output_format(self):
        """Choose a device format up front and verify it is actually supported.

        The old code decoded to the device's reported channel count, then, if
        the stream failed to open, silently reopened as mono 44.1 kHz while
        still feeding it stereo data -- which plays at the wrong speed.
        """
        candidates = []
        try:
            info = self.p.get_default_output_device_info()
            rate = int(info.get("defaultSampleRate", 44100))
            chans = min(2, max(1, int(info.get("maxOutputChannels", 2))))
            candidates.append((rate, chans))
        except Exception:
            pass
        candidates += [(44100, 2), (48000, 2), (44100, 1)]

        for rate, chans in candidates:
            try:
                if self.p.is_format_supported(
                    rate, output_device=None,
                    output_channels=chans, output_format=pyaudio.paFloat32
                ):
                    return rate, chans
            except Exception:
                continue
        return 44100, 2

    @staticmethod
    def _ensure_ffmpeg():
        """Point pydub at the bundled binaries."""
        base_path = (
            sys._MEIPASS if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__))
        )
        bin_path = os.path.join(base_path, "bin")
        if bin_path not in os.environ.get("PATH", ""):
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + bin_path
        AudioSegment.converter = os.path.join(bin_path, "ffmpeg.exe")
        AudioSegment.ffprobe = os.path.join(bin_path, "ffprobe.exe")

    def _decode(self, file_path, rate, chans):
        """A file as int16 samples at the given format, or None."""
        self._ensure_ffmpeg()
        try:
            audio = AudioSegment.from_file(file_path)
        except Exception as e:
            self.last_error = f"Could not decode {os.path.basename(file_path)}: {e}"
            return None
        audio = audio.set_frame_rate(rate).set_channels(chans).set_sample_width(2)
        samples = np.frombuffer(audio.raw_data, dtype=np.int16)
        usable = (samples.size // chans) * chans
        # Kept as int16: converting a whole track to float32 up front doubled
        # peak memory for no benefit, since playback converts one small chunk
        # at a time anyway.
        return samples[:usable].reshape((-1, chans))

    # --------------------------------------------------------- preloading

    def preload(self, file_path):
        """Decode the track that plays next, while this one is still going.

        The gap between two tracks is the decode plus a PortAudio stream
        being closed and reopened. Having the samples ready lets the playback
        loop switch buffers between two chunk writes, so neither cost is
        paid.
        """
        if not file_path or not self.gapless:
            return
        with self._preload_lock:
            if self._preload_path == file_path:
                return                  # already decoded, or in flight
            self._preload_path = file_path
            self._preloaded = None
        rate, chans = self.sample_rate, self.channels
        threading.Thread(target=self._preload_worker,
                         args=(file_path, rate, chans), daemon=True).start()

    def _preload_worker(self, file_path, rate, chans):
        data = self._decode(file_path, rate, chans)
        with self._preload_lock:
            # A different track was requested while this one decoded.
            if self._preload_path == file_path:
                self._preloaded = data

    def clear_preload(self):
        with self._preload_lock:
            self._preload_path = None
            self._preloaded = None

    def preloaded_path(self):
        with self._preload_lock:
            return self._preload_path

    def preload_ready(self):
        """True once the next track's samples are decoded and waiting."""
        with self._preload_lock:
            return self._preloaded is not None

    def take_preloaded(self, file_path=None):
        """Claim the preloaded samples, as (path, data)."""
        with self._preload_lock:
            if self._preloaded is None:
                return None, None
            if file_path is not None and self._preload_path != file_path:
                return None, None
            path, data = self._preload_path, self._preloaded
            self._preload_path, self._preloaded = None, None
            return path, data

    def load_track(self, file_path):
        """Decode a file into memory. Safe to call while another load is running."""
        with self._load_lock:
            self._load_generation += 1
            generation = self._load_generation

        self.stop()
        self.last_error = None
        self.smoothed_bands = np.zeros(NUM_VIS_BANDS)

        rate, chans = self._pick_output_format()

        # If this is the track that was preloaded, the decode is already done,
        # which also makes pressing next instant rather than merely gapless.
        _path, data = self.take_preloaded(file_path)
        if data is None:
            data = self._decode(file_path, rate, chans)
        if data is None:
            self.audio_data = None
            return False

        # A newer load started while this one was decoding -- discard ours.
        with self._load_lock:
            if generation != self._load_generation:
                return False
            self.audio_data = data
            self.sample_rate = rate
            self.channels = chans
            self.current_frame = 0
            self._rebuild_filters()
        return True

    def load_stream(self, url, duration=0.0, title=None, ffmpeg=None):
        """Play a remote URL without downloading it.

        The decoded-file path keeps working untouched; the play loop simply
        pulls from whichever source is set.
        """
        self.stop()
        self.last_error = None
        self.smoothed_bands = np.zeros(NUM_VIS_BANDS)

        rate, chans = self._pick_output_format()
        self.sample_rate, self.channels = rate, chans
        self.audio_data = None
        self.stream_title = title

        if ffmpeg is None:
            base = (sys._MEIPASS if getattr(sys, "frozen", False)
                    else os.path.dirname(os.path.abspath(__file__)))
            ffmpeg = os.path.join(base, "bin", "ffmpeg.exe")

        self.stream = StreamSource(
            url, ffmpeg, sample_rate=rate, channels=chans, duration=duration,
            on_error=lambda msg: setattr(self, "last_error", msg),
        )
        self._rebuild_filters()
        if not self.stream.start():
            self.last_error = self.stream.error or "Could not open the stream"
            self.stream = None
            return False
        return True

    @property
    def is_streaming(self):
        return self.stream is not None

    # ------------------------------------------------------------ visualiser

    def compute_visualizer(self, chunk):
        if not self.visualizer_enabled or chunk.size == 0:
            return

        mono = np.mean(chunk, axis=1)
        # A window keeps spectral leakage from smearing energy across bands.
        window = np.hanning(len(mono))
        fft_data = np.abs(np.fft.rfft(mono * window))[2:]

        if len(fft_data) <= NUM_VIS_BANDS:
            return

        indices = np.logspace(
            0, np.log10(len(fft_data) - 1), num=NUM_VIS_BANDS + 1, dtype=int
        )
        energies = np.empty(NUM_VIS_BANDS)
        for i in range(NUM_VIS_BANDS):
            start, end = indices[i], max(indices[i + 1], indices[i] + 1)
            energies[i] = np.mean(fft_data[start:end])

        # Normalise against chunk length so the scale no longer depends on it,
        # then use a dB curve so quiet passages still show movement.
        energies = energies / (len(mono) / 64.0)
        energies = np.clip(np.log10(energies + 1e-6) / 2.0 + 1.0, 0.0, 1.0)

        # Asymmetric smoothing: jump to peaks, fall away gently.
        rising = energies > self.smoothed_bands
        self.smoothed_bands = np.where(
            rising,
            0.4 * self.smoothed_bands + 0.6 * energies,
            0.8 * self.smoothed_bands + 0.2 * energies,
        )
        if self.visualizer_callback is not None:
            self.visualizer_callback(self.smoothed_bands.tolist())

    # ------------------------------------------------------------- playback

    def play(self):
        if self.stream is None and (self.audio_data is None
                                    or len(self.audio_data) == 0):
            return
        if self.playing:
            self.paused = False
            return

        self._stop_flag.clear()
        self.paused = False
        self.playing = True
        self.thread = threading.Thread(target=self._play_loop, daemon=True)
        self.thread.start()

    def pause(self):
        self.paused = True

    def stop(self):
        """Signal the audio thread and wait for it to release the stream.

        The old version closed the stream itself after a 1 s join timeout,
        which could free it while the audio thread was still blocked inside
        stream.write() -- a use-after-free in PortAudio. The stream is now
        owned end-to-end by the thread that writes to it.
        """
        self._stop_flag.set()
        self.paused = False
        thread = self.thread
        if thread is not None and thread.is_alive():
            if thread is not threading.current_thread():
                thread.join(timeout=3.0)
        self.thread = None
        self.playing = False
        self.current_frame = 0
        stream, self.stream = self.stream, None
        if stream is not None:
            stream.stop()

    def _play_loop(self):
        stream = None
        finished = False
        try:
            stream = self.p.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                frames_per_buffer=self.chunk_size,
            )

            total = 0 if self.stream is not None else len(self.audio_data)
            while not self._stop_flag.is_set():
                if self.paused:
                    time.sleep(0.02)
                    continue

                if self.stream is not None:
                    block = self.stream.read(self.chunk_size)
                    if block is None:
                        finished = True
                        break
                    if block.shape[0] == 0:
                        continue          # waiting on the network
                    chunk = block.astype(np.float32) / 32768.0
                else:
                    start = self.current_frame
                    if start >= total:
                        if self._advance_in_place():
                            total = len(self.audio_data)
                            continue
                        finished = True
                        break
                    end = min(start + self.chunk_size, total)
                    chunk = self.audio_data[start:end].astype(np.float32) / 32768.0
                    self.current_frame = end

                chunk = self.apply_eq(chunk)
                self.compute_visualizer(chunk)

                if self.volume != 1.0:
                    chunk = chunk * self.volume
                chunk = _soft_clip(chunk)

                stream.write(np.ascontiguousarray(chunk, dtype=np.float32).tobytes())

        except Exception as e:
            self.last_error = f"Playback error: {type(e).__name__}: {e}"
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            self.playing = False

        if finished and self.on_track_end_callback:
            try:
                self.on_track_end_callback()
            except Exception:
                pass

    def _advance_in_place(self):
        """Roll into the preloaded track without stopping the stream.

        Only when the sample format matches what the open stream was built
        for; anything else needs a new stream, which is the gap this exists
        to avoid.
        """
        if not self.gapless or self.stream is not None:
            return False
        path, data = self.take_preloaded()
        if data is None or len(data) == 0 or data.shape[1] != self.channels:
            return False

        with self._load_lock:
            self.audio_data = data
            self.current_frame = 0
        # A new track should not inherit the filter tails of the last one.
        self.reset_filters()

        if self.on_track_advanced_callback:
            try:
                self.on_track_advanced_callback(path)
            except Exception:
                pass
        return True

    # ------------------------------------------------------------- position

    def get_duration(self):
        """Track length in seconds."""
        if self.stream is not None:
            return float(self.stream.duration or 0.0)
        if self.audio_data is None or self.sample_rate <= 0:
            return 0.0
        return len(self.audio_data) / float(self.sample_rate)

    def get_position(self):
        """Current playhead in seconds."""
        if self.stream is not None:
            return self.stream.position()
        if self.audio_data is None or self.sample_rate <= 0:
            return 0.0
        return self.current_frame / float(self.sample_rate)

    def get_progress(self):
        if self.stream is not None:
            total = self.get_duration()
            return min(1.0, self.stream.position() / total) if total else 0.0
        if self.audio_data is None or len(self.audio_data) == 0:
            return 0.0
        return min(1.0, self.current_frame / len(self.audio_data))

    def set_progress(self, percent):
        if self.stream is not None:
            total = self.get_duration()
            if total:
                self.stream.seek(float(np.clip(percent, 0.0, 1.0)) * total)
                self.reset_filters()
            return
        if self.audio_data is None or len(self.audio_data) == 0:
            return
        percent = float(np.clip(percent, 0.0, 1.0))
        self.current_frame = int(len(self.audio_data) * percent)
        self.reset_filters()

    def close(self):
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass

    def __del__(self):
        # Interpreter shutdown can already have torn down the modules this
        # touches, so never let it raise.
        try:
            self.close()
        except Exception:
            pass
