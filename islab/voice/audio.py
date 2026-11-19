"""Audio helpers: decode with ffmpeg, segment with ffmpeg silencedetect, window, degrade.

All randomness goes through an explicit numpy Generator (seeded by the caller)."""
from __future__ import annotations
import re
import subprocess
import numpy as np

SR = 16000


def load(path: str, sr: int = SR, af: str | None = None) -> np.ndarray:
    """Decode any audio file to mono float32 at `sr` using ffmpeg. `af` = optional ffmpeg filter chain."""
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if af:
        cmd += ["-af", af]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def ffmpeg_filter(wav: np.ndarray, af: str, sr: int = SR) -> np.ndarray:
    """Run a float32 mono buffer through an ffmpeg audio filter chain."""
    cmd = ["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
           "-af", af, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    out = subprocess.run(cmd, input=wav.astype(np.float32).tobytes(), check=True, capture_output=True).stdout
    y = np.frombuffer(out, dtype=np.float32).copy()
    return y[: len(wav)] if len(y) >= len(wav) else np.pad(y, (0, len(wav) - len(y)))


def silence_segments(path: str, noise_db: float = -40.0, min_sil: float = 0.3):
    """Speech segments [(start, end)] in seconds = complement of ffmpeg silencedetect intervals."""
    cmd = ["ffmpeg", "-v", "info", "-i", path, "-af", f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True).stderr
    dur = None
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.eE+-]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.eE+-]+)", err)]
    sil = list(zip(starts, ends + [dur] * (len(starts) - len(ends))))
    segs, t = [], 0.0
    for s, e in sil:
        if s - t > 0.05:
            segs.append((t, s))
        t = e
    if dur is not None and dur - t > 0.05:
        segs.append((t, dur))
    return segs


def drop_silence(wav: np.ndarray, noise_db: float = -40.0, min_sil: float = 0.3, sr: int = SR) -> np.ndarray:
    """Numpy equivalent of the silencedetect segmentation: remove runs of 20 ms frames whose RMS is below
    noise_db dBFS lasting >= min_sil seconds, keep everything else (concatenated)."""
    f = int(0.02 * sr)
    n = len(wav) // f
    if n == 0:
        return wav
    rms = np.sqrt(np.mean(wav[: n * f].reshape(n, f) ** 2, axis=1) + 1e-12)
    silent = 20 * np.log10(rms) < noise_db
    keep = np.ones(n, bool)
    i = 0
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            if (j - i) * f >= min_sil * sr:
                keep[i:j] = False
            i = j
        else:
            i += 1
    out = wav[: n * f].reshape(n, f)[keep].ravel()
    return out if len(out) else wav


def windows(wav: np.ndarray, win_s: float, hop_s: float, min_s: float, sr: int = SR):
    """Cut a buffer into windows of win_s (hop hop_s); keep a final shorter tail if >= min_s."""
    n, w, h = len(wav), int(win_s * sr), int(hop_s * sr)
    if n < int(min_s * sr):
        return []
    if n <= w:
        return [wav]
    out = [wav[i:i + w] for i in range(0, n - w + 1, h)]
    tail_start = (len(out) - 1) * h + w
    if n - tail_start >= int(min_s * sr):
        out.append(wav[n - w:])
    return out


# ---------- degradations ----------

def pink_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    """1/f noise via spectral shaping of white noise."""
    X = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(X)); f[0] = 1
    y = np.fft.irfft(X / np.sqrt(f), n)
    return (y / (np.std(y) + 1e-12)).astype(np.float32)


def room_ir(rt60: float, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    """Synthetic room impulse response: direct path + exponentially decaying noise tail."""
    n = int(rt60 * sr)
    t = np.arange(n) / sr
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60) * 0.3
    ir[0] = 1.0
    return (ir / np.sqrt(np.sum(ir ** 2))).astype(np.float32)


def reverb(wav: np.ndarray, ir: np.ndarray) -> np.ndarray:
    from scipy.signal import fftconvolve
    y = fftconvolve(wav, ir)[: len(wav)]
    return (y * (np.std(wav) / (np.std(y) + 1e-12))).astype(np.float32)


def kitchen_noise(n: int, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    """'Kitchen-like' noise: reverberant pink noise + a 120 Hz appliance hum + sparse clatter transients."""
    x = reverb(pink_noise(n, rng), room_ir(0.5, rng, sr))
    t = np.arange(n) / sr
    x += 0.3 * np.sin(2 * np.pi * 120 * t) + 0.1 * np.sin(2 * np.pi * 240 * t)
    for _ in range(max(1, n // sr)):  # ~1 clatter per second
        i = rng.integers(0, max(1, n - 800))
        x[i:i + 800] += rng.standard_normal(800) * np.exp(-np.arange(800) / 120) * 3
    return (x / np.std(x)).astype(np.float32)


def mix_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    ps = np.mean(speech ** 2) + 1e-12
    pn = np.mean(noise ** 2) + 1e-12
    return (speech + noise * np.sqrt(ps / (pn * 10 ** (snr_db / 10)))).astype(np.float32)


def phone_replay(wav: np.ndarray, rng: np.random.Generator, sr: int = SR, snr_db: float = 30.0) -> np.ndarray:
    """Replay attack proxy: recording played through a phone speaker into the satellite mic.
    Band-pass 300-3400 Hz (4th-order Butterworth), small-speaker soft clipping, short room reverb, light noise."""
    from scipy.signal import butter, sosfilt
    sos = butter(4, [300, 3400], btype="band", fs=sr, output="sos")
    y = sosfilt(sos, wav).astype(np.float32)
    y = np.tanh(2 * y / (np.max(np.abs(y)) + 1e-9)) / 2
    y = reverb(y, room_ir(0.3, rng, sr))
    return mix_snr(y, pink_noise(len(y), rng), snr_db)


def speaker_replay(wav: np.ndarray, rng: np.random.Generator, sr: int = SR, snr_db: float = 35.0) -> np.ndarray:
    """Replay through a decent Bluetooth/smart speaker: gentle 120-7500 Hz band-limit, no clipping,
    short reverb, very light noise."""
    from scipy.signal import butter, sosfilt
    sos = butter(2, [120, 7500], btype="band", fs=sr, output="sos")
    y = sosfilt(sos, wav).astype(np.float32)
    y = reverb(y, room_ir(0.2, rng, sr))
    return mix_snr(y, pink_noise(len(y), rng), snr_db)


def satellite_channel(wav: np.ndarray, rng: np.random.Generator, sr: int = SR, rt60: float = 0.4,
                      snr_db: float = 30.0) -> np.ndarray:
    """What a far-field satellite mic hears in a living room: room reverb + light background noise."""
    y = reverb(wav, room_ir(rt60, rng, sr))
    return mix_snr(y, pink_noise(len(y), rng), snr_db)
