"""Satellite -> hub voice events on the bus (contract topic `voice/<satellite_id>`).

    sat = VoiceSatellite(sim, bus, "sat.kitchen", tier="pi5_hailo10h")
    sat.say("Anna", "set the kitchen to twenty one")            # embedding from the bundled household table
    sat.say(None, "unlock the front door", embedding=emb)        # any 256-d embedding (e.g. a replay)

The event is published after the sampled end-of-speech -> transcript latency of the pipeline model
(VAD endpoint + uplink + STT), drawn from `sim.rng`, so timing is deterministic per seed.
Payload = {"audio_ref": str, "text": str, "speaker_embedding"?: [float]} exactly as in CONTRACT.md;
the hub decides who spoke (see `hub_identify`). The satellite never asserts identity.
"""
from __future__ import annotations
import json, os
import numpy as np

from islab.core.bus import Envelope
from .pipeline import VoicePipelineModel

EMB_PATH = os.path.join(os.path.dirname(__file__), "data", "household_embeddings.json")


def load_household(path: str = EMB_PATH) -> dict:
    """Precomputed resemblyzer embeddings (no torch needed): {'profiles': {person: [256]},
    'utterances': {person: [[256], ...]}, 'replay': {person: [[256], ...]}, 'impostors': {...}, 'threshold': {...}}"""
    with open(path) as f:
        return json.load(f)


class VoiceSatellite:
    def __init__(self, sim, bus, satellite_id: str, tier: str = "pi5_hailo10h", household: dict | None = None):
        self.sim, self.bus, self.id = sim, bus, satellite_id
        self.model = VoicePipelineModel(tier)
        self._hh = household
        self._n = 0

    @property
    def household(self):
        if self._hh is None:
            self._hh = load_household()
        return self._hh

    def _pick_embedding(self, person, kind="utterances"):
        pool = self.household.get(kind, {}).get(person)
        if not pool:
            return None
        return pool[self.sim.rng.randrange(len(pool))]

    def payload(self, text: str, embedding=None, audio_ref: str | None = None) -> dict:
        self._n += 1
        p = {"audio_ref": audio_ref or f"{self.id}/utt{self._n:05d}.wav", "text": text}
        if embedding is not None:
            p["speaker_embedding"] = [round(float(x), 5) for x in np.asarray(embedding).ravel()]
        return p

    def say(self, person: str | None, text: str, embedding=None, replay: bool = False,
            audio_ref: str | None = None, delay: float | None = None) -> dict:
        """Schedule a voice event. `person` picks a stored utterance embedding (replay=True picks a
        phone-replayed recording of that person). Returns the payload; publishes after the pipeline delay."""
        if embedding is None and person is not None:
            embedding = self._pick_embedding(person, "replay" if replay else "utterances")
        pl = self.payload(text, embedding, audio_ref)
        if delay is None:
            d = self.model.sample(self.sim.rng, "rules")
            delay = (d["vad_endpoint"] + d["net_sat_to_hub"] + d["stt"]) / 1000.0
        self.sim.trace("voice_utterance", satellite=self.id, person=person, replay=replay, text=text, delay=round(delay, 4))
        self.sim.after(delay, self._publish, pl)
        return pl

    def _publish(self, pl):
        self.bus.publish(Envelope(topic=f"voice/{self.id}", payload=pl, source=self.id, ts=self.sim.now))


def hub_identify(speaker_id, payload: dict):
    """Hub side: (person | None, score) from a voice payload using a SpeakerID with enrolled profiles.
    Returns (None, 0.0) when the payload carries no embedding."""
    emb = payload.get("speaker_embedding")
    if emb is None:
        return None, 0.0
    return speaker_id.identify(emb=np.asarray(emb, dtype=np.float32))


def speaker_id_from_household(household: dict | None = None, threshold: str | float = "room_eer"):
    """A SpeakerID with the bundled household profiles and a no-op encoder (embeddings only, no torch)."""
    from .speaker import SpeakerID, THRESHOLDS
    hh = household or load_household()

    class _NoEncoder:
        name = "precomputed"

        def embed(self, wav):
            raise RuntimeError("precomputed-embedding SpeakerID cannot embed audio; pass emb=")
    thr = THRESHOLDS[threshold] if isinstance(threshold, str) else float(threshold)
    sid = SpeakerID(model=_NoEncoder(), threshold=thr)
    for p, v in hh["profiles"].items():
        sid.profiles[p] = np.asarray(v, dtype=np.float32)
    return sid
