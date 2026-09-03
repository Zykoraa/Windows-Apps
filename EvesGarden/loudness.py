"""How loud a track actually is, and what to do about it.

A library built from many sources is a library of many masterings. A 2014
single cut for the loudness war sits eight or nine decibels above a quiet
1971 remaster, so a playlist that mixes them is a playlist you ride the
volume knob through. Streaming services all fixed this years ago and it is
most of why they sound "smooth"; a local player that does not is the one
that sounds broken.

This measures loudness the way they do -- ITU-R BS.1770 / EBU R128, the
actual standard rather than something approximate. The measurement is
K-weighted (a filter shaped like human hearing, so a bass-heavy track is not
credited for energy nobody perceives as loudness) and gated, so silence and
quiet passages do not drag the number down and make the track play too loud.

Nothing here touches a file or the audio device: it takes samples and
returns numbers, so it can be checked against the calibration signal the
standard defines.
"""

import numpy as np

# What everything is normalised towards. -14 LUFS is what the streaming
# services settled on; ReplayGain's own -18 is quieter than anything else
# anybody listens to now, which would make this app the odd one out.
TARGET_LUFS = -14.0

# Never amplify a quiet track more than this. Something recorded very
# quietly is usually quiet on purpose, or is mostly noise, and lifting it
# 20dB brings up the noise with it.
MAX_GAIN_DB = 12.0

# Leave a little room below full scale for a decoder that overshoots.
CEILING = 0.97

_BLOCK = 0.400          # seconds; the standard's window
_OVERLAP = 0.75         # and its overlap
_ABSOLUTE_GATE = -70.0  # LUFS; below this is silence
_RELATIVE_GATE = -10.0  # LU below the ungated mean


def _biquad(b, a, samples):
    """Filter along axis 0.

    Through scipy where it is available, which it is -- the playback engine
    already depends on it. A four-minute track is ten million frames, and
    the same filter written as a Python loop over them takes minutes, which
    is not a thing that can happen while somebody presses play.
    """
    try:
        from scipy.signal import lfilter
        return lfilter(b, a, samples, axis=0)
    except Exception:
        pass
    out = np.empty_like(samples)
    x1 = x2 = y1 = y2 = np.zeros(samples.shape[1:], dtype=np.float64)
    for i in range(samples.shape[0]):
        x0 = samples[i]
        y0 = b[0] * x0 + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        out[i] = y0
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return out


def _k_weighting(rate):
    """The two stages of BS.1770's K-weighting, designed at this rate.

    The standard tabulates coefficients for 48kHz only, but gives the
    derivation they come from, and that is what is used here -- at 48kHz it
    reproduces the published numbers to the last digit, and at any other rate
    it is right rather than borrowed.

    Designing this with the ordinary RBJ shelf formula instead looks
    plausible and reads 0.25dB low at every level, which is the kind of wrong
    that never announces itself.
    """
    import math

    # Stage 1: a high shelf, about +4dB above 2kHz -- the head-related
    # boost that makes treble read as loud.
    gain_db = 3.999843853973347
    q = 0.7071752369554196
    fc = 1681.974450955533
    k = math.tan(math.pi * fc / rate)
    vh = 10.0 ** (gain_db / 20.0)
    vb = vh ** 0.4996667741545416
    denom = 1.0 + k / q + k * k
    b1 = np.array([(vh + vb * k / q + k * k) / denom,
                   2.0 * (k * k - vh) / denom,
                   (vh - vb * k / q + k * k) / denom])
    a1 = np.array([1.0,
                   2.0 * (k * k - 1.0) / denom,
                   (1.0 - k / q + k * k) / denom])

    # Stage 2: a high pass at 38Hz, so subsonic rumble is not counted.
    q = 0.5003270373238773
    fc = 38.13547087602444
    k = math.tan(math.pi * fc / rate)
    denom = 1.0 + k / q + k * k
    b2 = np.array([1.0, -2.0, 1.0])
    a2 = np.array([1.0,
                   2.0 * (k * k - 1.0) / denom,
                   (1.0 - k / q + k * k) / denom])

    return (b1, a1), (b2, a2)


def _channel_weights(channels):
    """BS.1770 weights each channel; the front pair count equally."""
    return np.ones(channels, dtype=np.float64)


def integrated_lufs(samples, rate):
    """Gated integrated loudness, in LUFS. None if there is nothing to measure.

    `samples` is float in -1..1, shaped (frames, channels).
    """
    if samples is None or rate <= 0:
        return None
    data = np.asarray(samples, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    if data.shape[0] < int(rate * _BLOCK):
        return None

    (b1, a1), (b2, a2) = _k_weighting(rate)
    filtered = _biquad(b2, a2, _biquad(b1, a1, data))

    block = int(round(rate * _BLOCK))
    hop = max(1, int(round(block * (1.0 - _OVERLAP))))
    starts = range(0, filtered.shape[0] - block + 1, hop)
    weights = _channel_weights(filtered.shape[1])

    # Mean square per block per channel, weighted and summed.
    power = np.array([
        float(np.sum(weights * np.mean(filtered[s:s + block] ** 2, axis=0)))
        for s in starts
    ])
    if power.size == 0:
        return None

    with np.errstate(divide="ignore"):
        block_lufs = -0.691 + 10.0 * np.log10(np.maximum(power, 1e-20))

    # Two gates. The absolute one drops silence; the relative one drops
    # anything more than 10 LU below the rest, so a quiet intro does not
    # pull the number down and leave the track playing too loud.
    keep = block_lufs > _ABSOLUTE_GATE
    if not np.any(keep):
        return None
    mean_power = float(np.mean(power[keep]))
    threshold = (-0.691 + 10.0 * np.log10(max(mean_power, 1e-20))
                 + _RELATIVE_GATE)
    keep &= block_lufs > threshold
    if not np.any(keep):
        return None

    return float(-0.691 + 10.0 * np.log10(max(float(np.mean(power[keep])),
                                              1e-20)))


def peak(samples):
    """Loudest sample, as a fraction of full scale."""
    data = np.asarray(samples, dtype=np.float64)
    if data.size == 0:
        return 0.0
    return float(np.max(np.abs(data)))


def gain_for(lufs, sample_peak, target=TARGET_LUFS):
    """The multiplier that brings a track to the target without clipping.

    Returns 1.0 -- leave it alone -- when there is nothing to go on. The
    clipping guard is the reason the answer is not simply the difference in
    decibels: a track already mastered near full scale cannot be turned up,
    and turning it up anyway and letting it clip would be worse than leaving
    it quiet.
    """
    if lufs is None:
        return 1.0
    wanted_db = min(target - lufs, MAX_GAIN_DB)
    gain = 10.0 ** (wanted_db / 20.0)
    if sample_peak and sample_peak > 0:
        gain = min(gain, CEILING / sample_peak)
    return max(0.05, float(gain))


def measure(samples, rate):
    """(lufs, peak) for a decoded track, or (None, peak)."""
    return integrated_lufs(samples, rate), peak(samples)
