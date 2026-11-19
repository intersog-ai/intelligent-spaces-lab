"""Speaker enrollment / identification.

    sid = SpeakerID(ResemblyzerEncoder())          # or SpeakerID() for the default encoder
    sid.enroll("anna", ["anna1.mp3", wav_array])   # paths or 16 kHz float32 arrays
    person, score = sid.identify(wav)              # (None, best_score) if below threshold

IMPORTANT: a voice match is a *convenience signal*, not authentication. The speaker_id
experiment shows that a phone-speaker replay of an enrolled person's own recording is
accepted (experiments/speaker_id/results.json, "replay"). Never gate unlock/disarm/valve
actions on identify() alone; use it to pick preferences and to route an approval request.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np

from . import audio

# Operating points measured in experiments/speaker_id (resemblyzer GE2E, 2 s windows, cosine score of
# L2-normalised embeddings, 10 s enrollment, surrogate household). results.json -> "operating_points".
#   studio_*: clean studio audio both sides.  room_*: enrollment + test through a simulated room/satellite mic.
#   *_eer: FAR = FRR.  *_strict: FAR <= 1 % on the experiment's trials.
# Re-run experiments/speaker_id/run.py to refresh; tests/test_voice.py checks these match results.json.
THRESHOLDS = {
    "studio_eer": 0.7869,
    "studio_strict": 0.8025,
    "room_eer": 0.7752,
    "room_strict": 0.8241,
}
DEFAULT_THRESHOLD = THRESHOLDS["room_strict"]


def _as_wav(x, drop_silence: bool = True) -> np.ndarray:
    """Path or 16 kHz array -> float32 array with long silences (>= 0.3 s below -40 dBFS) removed,
    matching the segmentation used in the experiment."""
    w = x.astype(np.float32) if isinstance(x, np.ndarray) else audio.load(str(x))
    return audio.drop_silence(w) if drop_silence else w


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / (np.linalg.norm(v) + 1e-12)).astype(np.float32)


class ResemblyzerEncoder:
    """GE2E d-vector encoder (Resemblyzer; weights ship inside the pip wheel). 256-d, L2-normalised."""
    name = "resemblyzer-ge2e"

    def __init__(self, device: str = "cpu"):
        from resemblyzer import VoiceEncoder
        self._enc = VoiceEncoder(device, verbose=False)

    def embed(self, wav: np.ndarray) -> np.ndarray:
        # Same as resemblyzer.preprocess_wav minus long-silence trimming (we segment ourselves;
        # trimming can empty a short window). Input must already be 16 kHz.
        from resemblyzer.audio import normalize_volume, audio_norm_target_dBFS
        w = normalize_volume(wav.astype(np.float32), audio_norm_target_dBFS, increase_only=True)
        return _unit(self._enc.embed_utterance(w))


class MFCCEncoder:
    """Classical fallback (no neural weights): mean+std of 20 MFCC + deltas, CMN, L2-normalised.
    Much weaker than GE2E; exists so the API works without torch."""
    name = "mfcc-stats"

    def embed(self, wav: np.ndarray) -> np.ndarray:
        import librosa
        m = librosa.feature.mfcc(y=wav, sr=audio.SR, n_mfcc=20, n_fft=512, hop_length=160)
        d = librosa.feature.delta(m)
        f = np.concatenate([m.mean(1), m.std(1), d.std(1)])
        return _unit(f - f.mean())


def default_encoder():
    try:
        return ResemblyzerEncoder()
    except Exception:  # torch / resemblyzer missing
        return MFCCEncoder()


@dataclass
class SpeakerID:
    model: object = None
    threshold: float = DEFAULT_THRESHOLD
    window_s: float = 2.0
    profiles: dict = field(default_factory=dict)   # person -> unit embedding

    def __post_init__(self):
        if self.model is None:
            self.model = default_encoder()

    def embed(self, wav) -> np.ndarray:
        """Embedding of one utterance (averaged over window_s windows if long)."""
        w = _as_wav(wav)
        ws = audio.windows(w, self.window_s, self.window_s / 2, min_s=0.5) or [w]
        return _unit(np.mean([self.model.embed(x) for x in ws], axis=0))

    def enroll(self, person: str, wav_list) -> np.ndarray:
        """Enroll (or re-enroll) a person from a list of wav paths/arrays. Returns the profile."""
        embs = []
        for x in wav_list:
            w = _as_wav(x)
            for seg in audio.windows(w, self.window_s, self.window_s / 2, min_s=0.5) or [w]:
                embs.append(self.model.embed(seg))
        if not embs:
            raise ValueError(f"no usable audio for {person}")
        self.profiles[person] = _unit(np.mean(embs, axis=0))
        return self.profiles[person]

    def scores(self, wav=None, emb: np.ndarray | None = None) -> dict:
        """Cosine score against every profile. Pass audio (`wav`) or a precomputed embedding (`emb`)."""
        e = _unit(emb) if emb is not None else self.embed(wav)
        return {p: float(np.dot(e, v)) for p, v in self.profiles.items()}

    def identify(self, wav=None, threshold: float | None = None, emb: np.ndarray | None = None):
        """Open-set identification -> (person | None, best score)."""
        if not self.profiles:
            return None, 0.0
        s = self.scores(wav, emb)
        best = max(s, key=s.get)
        thr = self.threshold if threshold is None else threshold
        return (best if s[best] >= thr else None), s[best]

    def verify(self, person: str, wav, threshold: float | None = None):
        s = self.scores(wav)[person]
        return s >= (self.threshold if threshold is None else threshold), s
