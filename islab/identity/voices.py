"""The series' synthetic voices as a household, and helpers to enroll and score them.

Voice files are not distributed. Point ``ISLAB_VO`` at a folder laid out like the Season 1 voice-over
(``<dir>/*.mp3`` and ``<dir>/v2/*.mp3``, file names ``E06_<Voice>.mp3``). Voice tags in file names:
  David (narrator), Amanda, Ollie, AnnaAssist (assistant voices), Anna (ElevenLabs "Courtney"),
  Mark ("Chris"), Sam ("Eric").
In B06 the household is played by the three voices with enough audio (household.yaml -> people.*.voice);
the S1 character voices, which have only 1-7 s each, are never enrolled and play guests.
"""
from __future__ import annotations

import glob
import os

import numpy as np

from islab.voice import audio

# file tag -> voice name used in B06 (the S1 character tags are renamed to the stock voice behind them)
TAGS = (("AnnaAssist", "AnnaAssist"), ("Anna", "Courtney"), ("Mark", "Chris"), ("Sam", "Eric"),
        ("David", "David"), ("Amanda", "Amanda"), ("Ollie", "Ollie"))
GUEST_VOICES = ("Courtney", "Chris", "Eric", "AnnaAssist")


def vo_dir() -> str:
    return os.environ.get("ISLAB_VO", "audio")


def voice_of(fname: str) -> str | None:
    b = os.path.basename(fname)
    if b.startswith(("sfx", "music")) or "_" not in b:
        return None
    tag = b.split("_", 1)[1].rsplit(".", 1)[0]
    for t, name in TAGS:
        if tag.startswith(t):
            return name
    return None


def _order(f):
    b = os.path.basename(f)
    return (b[1:3], "/v2/" in f, b)


def load_segments(vo: str | None = None) -> dict:
    """voice -> list of speech segments (16 kHz float32), chronological, silence (>= 0.3 s at -40 dBFS)
    removed by ffmpeg silencedetect, same segmentation as experiments/speaker_id."""
    vo = vo or vo_dir()
    files = sorted(glob.glob(f"{vo}/*.mp3") + glob.glob(f"{vo}/v2/*.mp3"), key=_order)
    out = {}
    for f in files:
        v = voice_of(f)
        if v is None:
            continue
        w = audio.load(f)
        out.setdefault(v, []).extend(w[int(s * audio.SR):int(e * audio.SR)]
                                     for s, e in audio.silence_segments(f, -40, 0.3))
    return out


def split_seconds(segs, seconds: float):
    """First ``seconds`` of speech -> (enroll segments, remaining segments)."""
    need = int(seconds * audio.SR)
    enr, rest = [], []
    for s in segs:
        if need <= 0:
            rest.append(s)
        elif len(s) <= need:
            enr.append(s); need -= len(s)
        else:
            enr.append(s[:need]); rest.append(s[need:]); need = 0
    return enr, rest


def windows(segs, win: float = 2.0, hop: float = 1.0, min_s: float = 1.0):
    out = []
    for s in segs:
        out += audio.windows(s, win, hop, min_s)
    return out


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-12)


def score_matrix(E: np.ndarray, profiles: dict) -> tuple[np.ndarray, list]:
    """Cosine scores of unit embeddings E (n x 256) against profiles -> (n x k, names)."""
    names = list(profiles)
    P = unit(np.array([profiles[n] for n in names]))
    return unit(E) @ P.T, names
