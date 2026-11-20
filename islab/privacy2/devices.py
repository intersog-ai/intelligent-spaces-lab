"""Two things that want the internet: a device that phones home, and cloud speech fallback.

``PhoneHomeDevice`` tries to reach its maker in one of several ways (``MODES``), from the
obvious to the quiet. Every attempt goes to the gate (or, for "bypass", straight onto the
``egress/`` topic) and is counted by the device, so the experiment can compare what the device
tried with what the gate saw and stopped.

``VoiceRouter`` handles a spoken request locally when the local recogniser is confident, and
sends the audio to the cloud only when it is not, the household opted in, and the internet is
up. Otherwise it answers locally that it did not understand. Whether the local recogniser is
confident is an input (modelled), not a measurement of any recogniser.
"""
from __future__ import annotations

from islab.core.bus import Envelope

MODES = {
    # mode: (source, dest, purpose, payload class, bytes per attempt, attempts per day)
    "undeclared_destination": ("plug.cheap", "telemetry.unknown-vendor.example", "fleet_update",
                               "telemetry", 420, 144),
    "allowlisted_host_wrong_class": ("plug.cheap", "updates.vendor.example", "fleet_update",
                                     "telemetry", 420, 144),
    "mimic_update_check": ("plug.cheap", "updates.vendor.example", "fleet_update",
                           "update_check", 420, 144),
    "declared_sender_oversize": ("fleet.agent", "updates.vendor.example", "fleet_update",
                                 "update_check", 50_000, 4),
    "declared_sender_extra_checks": ("fleet.agent", "updates.vendor.example", "fleet_update",
                                     "update_check", 1_200, 20),
    "declared_sender_low_and_slow": ("fleet.agent", "updates.vendor.example", "fleet_update",
                                     "update_check", 1_200, 2),
    "bypass_gate": ("plug.cheap", "telemetry.unknown-vendor.example", None, None, 420, 144),
}


class PhoneHomeDevice:
    def __init__(self, sim, bus, gate, mode, start_s=0.0):
        if mode not in MODES:
            raise ValueError(mode)
        self.sim, self.bus, self.gate, self.mode = sim, bus, gate, mode
        self.source, self.dest, self.purpose, self.cls, self.size, self.per_day = MODES[mode]
        self.attempts, self.got_out = [], []
        period = 86400.0 / self.per_day
        for i in range(self.per_day):
            sim.at(start_s + i * period + sim.rng.random() * min(period, 60.0), self._try)

    def _try(self):
        self.attempts.append(self.sim.now)
        if self.mode == "bypass_gate":
            # publishes egress/ itself; in the twin nothing carries it out, the watchdog logs it
            self.bus.publish(Envelope(topic="egress/fleet_update", source=self.source,
                                      ts=self.sim.now, payload={"dest": self.dest, "bytes": self.size}))
            return
        ok = self.gate.send(self.source, self.dest, self.purpose, self.cls, size=self.size)
        if ok:
            self.got_out.append((self.sim.now, self.size))

    @property
    def bytes_out(self):
        return sum(b for _, b in self.got_out)


class VoiceRouter:
    """Local first; cloud speech only when needed, allowed and possible."""

    def __init__(self, sim, gate, source="voice.pipeline"):
        self.sim, self.gate, self.source = sim, gate, source
        self.log = []

    def request(self, local_confident: bool, size: int):
        if local_confident:
            outcome = "local"
        elif not self.gate.settings.get("cloud_speech_opt_in"):
            outcome = "local_said_not_understood"      # opted out: never offered to the gate
        else:
            ok = self.gate.send(self.source, "asr.provider.example", "speech_recognition_optional",
                                "speech_audio", size=size, meta={"retention": "not_stored"})
            outcome = "cloud" if ok else "local_said_not_understood"
        self.log.append({"t": self.sim.now, "outcome": outcome, "bytes": size})
        return outcome

    def counts(self):
        out = {"local": 0, "cloud": 0, "local_said_not_understood": 0}
        for r in self.log:
            out[r["outcome"]] += 1
        return out


__all__ = ["PhoneHomeDevice", "VoiceRouter", "MODES"]
