"""Simple replay checks that run on a CPU, and the replay proxies used to test them.

1. Channel / spectral cues (``LivenessCheck``). A recording played through a loudspeaker loses low bass,
   is band-limited or clipped by small drivers, and passes through a second room. Six cheap features per
   2 s window capture this. The check is ONE-CLASS: it is fitted only on the household's own live speech
   heard through the satellite (no attack audio is used), and its threshold is set so that a chosen share
   of live calibration windows is rejected (default 5 %). Anything far from "how this household sounds
   through this microphone" fails.
2. Challenge phrase (``ChallengePhrase``). The home asks for a random word + number; a pre-recorded clip
   will not contain it. Needs speech recognition, adds a turn, and a voice clone defeats it.

Neither is anti-spoofing in the biometric sense. Both are tested in experiments/b06_identity against DSP
proxies of replay, not real loudspeakers.
"""
from __future__ import annotations

import numpy as np

from islab.voice import audio

SR = audio.SR
FEATURES = ("low_band_db", "high_band_db", "flatness", "rolloff95_hz", "clip_ratio", "envelope_range_db")


def features(wav: np.ndarray, sr: int = SR) -> np.ndarray:
    """Six channel cues for one window (float32 mono at 16 kHz)."""
    from scipy.signal import welch
    w = np.asarray(wav, dtype=np.float64)
    w = w - w.mean()
    f, p = welch(w, fs=sr, nperseg=512, noverlap=256)
    p = p + 1e-15

    def band(lo, hi):
        return float(p[(f >= lo) & (f < hi)].sum())
    mid = band(150, 4000)
    low_db = 10 * np.log10(band(50, 150) / mid)
    high_db = 10 * np.log10(band(4000, 7800) / mid)
    sel = (f >= 300) & (f < 3400)
    flat = float(np.exp(np.mean(np.log(p[sel]))) / np.mean(p[sel]))
    c = np.cumsum(p); roll = float(f[np.searchsorted(c, 0.95 * c[-1])])
    peak = np.max(np.abs(w)) + 1e-12
    clip = float(np.mean(np.abs(w) >= 0.95 * peak))
    fr = int(0.02 * sr); n = len(w) // fr
    env = 10 * np.log10(np.mean(w[: n * fr].reshape(n, fr) ** 2, axis=1) + 1e-12) if n else np.zeros(1)
    erange = float(np.percentile(env, 95) - np.percentile(env, 10))
    return np.array([low_db, high_db, flat, roll, clip, erange], dtype=np.float64)


class LivenessCheck:
    """One-class check: z-scores against the household's live calibration windows; score = mean z^2
    (rule="mean", the default) or the largest z^2 of any feature (rule="max").
    ``fit`` sets the threshold at the (1 - max_live_reject) quantile of the calibration scores."""

    def __init__(self, max_live_reject: float = 0.05, use: tuple = FEATURES, rule: str = "mean"):
        self.max_live_reject = max_live_reject
        self.rule = rule
        self.use = tuple(use)
        self._idx = [FEATURES.index(u) for u in self.use]
        self.mu = self.sd = None
        self.threshold = None

    def _x(self, X):
        X = np.atleast_2d(np.asarray(X, dtype=np.float64))
        return X[:, self._idx]

    def fit_features(self, X):
        X = self._x(X)
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        s = self.score_features_raw(X)
        self.threshold = float(np.quantile(s, 1.0 - self.max_live_reject))
        return self

    def fit(self, live_wavs):
        return self.fit_features(np.array([features(w) for w in live_wavs]))

    def score_features_raw(self, Xsel):
        z = (Xsel - self.mu) / self.sd
        return np.max(z ** 2, axis=1) if self.rule == "max" else np.mean(z ** 2, axis=1)

    def score_features(self, X) -> np.ndarray:
        return self.score_features_raw(self._x(X))

    def is_live_features(self, X) -> np.ndarray:
        return self.score_features(X) <= self.threshold

    def score(self, wav) -> float:
        return float(self.score_features(features(wav))[0])

    def is_live(self, wav) -> bool:
        return self.score(wav) <= self.threshold


# ---- challenge phrase ----------------------------------------------------------------------------
WORDS = ("amber", "anchor", "apple", "arrow", "basil", "beacon", "birch", "bison", "cedar", "cobalt",
         "comet", "copper", "coral", "cotton", "falcon", "fennel", "garnet", "glacier", "harbor", "hazel",
         "indigo", "island", "jasper", "juniper", "kettle", "lantern", "lemon", "lilac", "maple", "marble",
         "meadow", "mango", "nectar", "nickel", "olive", "orchid", "otter", "pebble", "pepper", "pine",
         "quartz", "raven", "river", "saffron", "silver", "spruce", "tulip", "velvet", "walnut", "willow")
NUMBERS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
UNITS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


class ChallengePhrase:
    """Random '<word> <tens> <unit>' phrase (50 x 8 x 9 = 3,600 combinations)."""

    combinations = len(WORDS) * len(NUMBERS) * len(UNITS)

    def __init__(self, rng: np.random.Generator):
        self.rng = rng

    def issue(self) -> str:
        return f"{self.rng.choice(WORDS)} {self.rng.choice(NUMBERS)} {self.rng.choice(UNITS)}"

    @staticmethod
    def _norm(t: str) -> list:
        return "".join(ch.lower() if ch.isalnum() else " " for ch in t).split()

    @classmethod
    def verify(cls, transcript: str, phrase: str) -> bool:
        """True if the phrase's words appear consecutively in the transcript."""
        t, p = cls._norm(transcript), cls._norm(phrase)
        return any(t[i:i + len(p)] == p for i in range(len(t) - len(p) + 1))


# ---- replay proxies (in addition to islab.voice.audio.phone_replay / speaker_replay) --------------
def hifi_replay(wav: np.ndarray, rng: np.random.Generator, sr: int = SR, snr_db: float = 40.0) -> np.ndarray:
    """Replay through a good full-range speaker (or a voice clone played on one): gentle 60 Hz high-pass
    (1st order), no low-pass, no clipping, short reverb (RT60 0.15 s), very light noise."""
    from scipy.signal import butter, sosfilt
    sos = butter(1, 60, btype="high", fs=sr, output="sos")
    y = sosfilt(sos, wav).astype(np.float32)
    y = audio.reverb(y, audio.room_ir(0.15, rng, sr))
    return audio.mix_snr(y, audio.pink_noise(len(y), rng), snr_db)


REPLAY_DEVICES = {
    "phone_speaker": audio.phone_replay,       # B01 proxy: 300-3400 Hz, clipped
    "bluetooth_speaker": audio.speaker_replay,  # B01 proxy: 120-7500 Hz
    "hifi_speaker": hifi_replay,               # new: nearly flat
}
