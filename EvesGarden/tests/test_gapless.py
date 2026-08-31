"""Gapless playback: does the engine really roll into the next track?

The gap between two tracks was a decode plus a PortAudio stream being closed
and reopened. Removing it means the playback loop has to switch buffers
between two chunk writes on a stream that never stops -- so the thing worth
testing is not "did the next track play", which it always did, but "did it
play on the same stream, without the end-of-track path running".

This opens a real output device, so it plays two very short tones at zero
volume, and skips itself where there is no device to open.
"""

import math
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from player_engine import PlayerEngine

RATE = 44100
TONE_SECONDS = 0.45


def write_tone(path, freq, seconds=TONE_SECONDS, rate=RATE):
    """A short stereo tone, written with the stdlib so no ffmpeg is needed."""
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            value = int(11000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<hh", value, value)
        handle.writeframes(bytes(frames))


def _has_output_device():
    try:
        engine = PlayerEngine()
        stream = engine.p.open(format=1, channels=2, rate=RATE, output=True,
                               frames_per_buffer=1024)
        stream.stop_stream()
        stream.close()
        return True
    except Exception:
        return False


HAS_AUDIO = _has_output_device()


def wait_for(predicate, timeout=8.0, step=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


@unittest.skipUnless(HAS_AUDIO, "needs an audio output device")
class Gapless(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-gapless-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.first = os.path.join(self.dir, "first.wav")
        self.second = os.path.join(self.dir, "second.wav")
        write_tone(self.first, 440)
        write_tone(self.second, 660)

        self.engine = PlayerEngine()
        self.addCleanup(self.engine.stop)
        self.engine.set_volume(0.0)

        self.opened = []
        original = self.engine.p.open

        def counting_open(*args, **kwargs):
            self.opened.append(1)
            return original(*args, **kwargs)

        self.engine.p.open = counting_open

    def test_rolls_into_the_preloaded_track_on_one_stream(self):
        engine = self.engine
        advanced, ended = [], []
        engine.on_track_advanced_callback = advanced.append
        engine.on_track_end_callback = lambda: ended.append(1)

        self.assertTrue(engine.load_track(self.first))
        engine.preload(self.second)
        self.assertTrue(wait_for(engine.preload_ready),
                        "the next track never finished decoding")

        engine.play()
        self.assertTrue(wait_for(lambda: advanced),
                        "the engine never advanced into the preloaded track")
        # Read these the moment it happens: the second tone is short, and once
        # it runs out the ordinary end-of-track path fires legitimately.
        ended_at_boundary = len(ended)
        streams_at_boundary = len(self.opened)
        engine.stop()

        self.assertEqual(advanced, [self.second])
        self.assertEqual(ended_at_boundary, 0,
                         "the end-of-track path ran, so the stream stopped")
        self.assertEqual(streams_at_boundary, 1,
                         "a second output stream was opened mid-playback")

    def test_falls_back_to_ending_when_nothing_is_preloaded(self):
        engine = self.engine
        advanced, ended = [], []
        engine.on_track_advanced_callback = advanced.append
        engine.on_track_end_callback = lambda: ended.append(1)

        self.assertTrue(engine.load_track(self.first))
        engine.play()
        self.assertTrue(wait_for(lambda: ended),
                        "playback never reported the end of the track")
        self.assertEqual(advanced, [])

    def test_a_preload_makes_the_next_load_skip_decoding(self):
        engine = self.engine
        engine.preload(self.second)
        self.assertTrue(wait_for(engine.preload_ready))

        self.assertTrue(engine.load_track(self.second))
        self.assertIsNone(engine.preloaded_path(),
                          "the decoded samples were not claimed")
        self.assertEqual(len(engine.audio_data),
                         int(RATE * TONE_SECONDS))

    def test_switching_off_gapless_stops_it_preloading(self):
        engine = self.engine
        engine.gapless = False
        engine.preload(self.second)
        time.sleep(0.2)
        self.assertIsNone(engine.preloaded_path())


if __name__ == "__main__":
    unittest.main(verbosity=2)


@unittest.skipUnless(HAS_AUDIO, "needs an audio output device")
class Crossfade(unittest.TestCase):
    """The overlap has to keep the level flat and land on the right frame."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="eg-fade-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.first = os.path.join(self.dir, "first.wav")
        self.second = os.path.join(self.dir, "second.wav")
        write_tone(self.first, 440)
        write_tone(self.second, 660)
        self.engine = PlayerEngine()
        self.addCleanup(self.engine.stop)
        self.engine.set_volume(0.0)

    def test_equal_power_keeps_the_level_flat(self):
        # Two uncorrelated signals crossfaded with linear ramps lose about
        # 3dB in the middle. Sine/cosine legs should not.
        import numpy as np
        engine = self.engine
        self.assertTrue(engine.load_track(self.first))
        engine.crossfade = 0.2
        engine.preload(self.second)
        self.assertTrue(wait_for(engine.preload_ready))

        total = len(engine.audio_data)
        engine.current_frame = total - int(0.2 * RATE)
        self.assertTrue(engine._should_start_fade(total))
        engine._start_fade(total)

        powers = []
        while True:
            chunk = engine._mix_fade_chunk(2048)
            if chunk is None:
                break
            powers.append(float(np.sqrt((chunk ** 2).mean())))

        self.assertGreater(len(powers), 3)
        # No chunk should sag far below the others.
        self.assertGreater(min(powers), max(powers) * 0.7,
                           "the crossfade dips in the middle: %s" % powers)

    def test_fade_hands_over_at_the_right_frame(self):
        engine = self.engine
        advanced = []
        engine.on_track_advanced_callback = advanced.append
        self.assertTrue(engine.load_track(self.first))
        engine.crossfade = 0.2
        engine.preload(self.second)
        self.assertTrue(wait_for(engine.preload_ready))

        total = len(engine.audio_data)
        engine.current_frame = total - int(0.2 * RATE)
        engine._start_fade(total)
        faded = engine._fade["frames"]
        while engine._mix_fade_chunk(2048) is not None:
            pass
        engine._finish_fade()

        self.assertEqual(advanced, [self.second])
        # Playback continues in the new track exactly where the fade left it.
        self.assertEqual(engine.current_frame, faded)
        self.assertEqual(len(engine.audio_data), int(RATE * TONE_SECONDS))

    def test_zero_crossfade_never_starts_one(self):
        engine = self.engine
        self.assertTrue(engine.load_track(self.first))
        engine.crossfade = 0.0
        engine.preload(self.second)
        self.assertTrue(wait_for(engine.preload_ready))
        engine.current_frame = len(engine.audio_data) - 10
        self.assertFalse(engine._should_start_fade(len(engine.audio_data)))
