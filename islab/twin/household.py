"""Scripted household days.

A script is a list of ``(time, actor, action, args)`` tuples::

    [("18:04", "Anna", "open_window", {}),
     ("09:12", "house", "leak", {}),
     ("18:51", "courier", "doorbell", {}),
     ("22:40", "Mark", "say", {"text": "Good night"})]

``time`` is ``"HH:MM"``, ``"HH:MM:SS"`` or virtual seconds since ``day_start``
(midnight). ``actor`` is ground truth: it is written to ``sim.log`` as
``("household", ...)`` but never put on the bus -- the hub has to work out who
did what, just as in a real home. Unknown actions raise immediately.

Actions (``args`` in brackets, all optional):

=================  =====================================================
open_window        [device="window.kitchen"]
close_window       [device="window.kitchen"]
leak / leak_stop   [device="leak.sink"]
smoke / smoke_clear [device="smoke.hall"]
motion             [device="motion.kitchen"]
doorbell           person detection + doorbell ring on camera.front
parcel / vehicle / person   camera detection [confidence]
say                voice/<satellite> {audio_ref, text} [text, satellite="sat.living"]
manual_lock / manual_unlock   [device="lock.front"]
temperature        [device="temp.kitchen", value]
=================  =====================================================
"""
from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, Callable

from islab.core.bus import Envelope

if TYPE_CHECKING:
    from islab.twin.home import Home

ScriptStep = tuple[float | str, str, str, dict[str, Any]]


def parse_time(t: float | str) -> float:
    """'18:04' -> 65040.0 ; numbers pass through."""
    if isinstance(t, (int, float)):
        return float(t)
    parts = [int(x) for x in t.split(":")]
    h, m, s = (parts + [0, 0])[:3]
    return float(h * 3600 + m * 60 + s)


DEFAULT_DAY: list[ScriptStep] = [
    ("07:05", "Mark", "say", {"text": "Good morning"}),
    ("07:06", "Mark", "motion", {}),
    ("08:20", "Anna", "manual_unlock", {}),
    ("08:21", "Anna", "manual_lock", {}),
    ("09:12", "house", "leak", {}),
    ("12:30", "courier", "parcel", {}),
    ("18:04", "Anna", "open_window", {}),
    ("18:30", "Anna", "close_window", {}),
    ("18:51", "courier", "doorbell", {}),
    ("22:40", "Mark", "say", {"text": "Good night"}),
]


class Household:
    """Turns a script into scheduled sim events acting on a Home."""

    def __init__(self, home: "Home", day_start: float = 0.0):
        self.home, self.day_start = home, day_start
        self._audio = itertools.count(1)
        self.actions: dict[str, Callable[[str, dict], None]] = {
            "open_window": lambda a, k: self._dev(k, "window.kitchen").set_open(True),
            "close_window": lambda a, k: self._dev(k, "window.kitchen").set_open(False),
            "leak": lambda a, k: self._dev(k, "leak.sink").set_leak(True),
            "leak_stop": lambda a, k: self._dev(k, "leak.sink").set_leak(False),
            "smoke": lambda a, k: self._dev(k, "smoke.hall").trigger(),
            "smoke_clear": lambda a, k: self._dev(k, "smoke.hall").clear(),
            "motion": lambda a, k: self._dev(k, "motion.kitchen").trigger(),
            "doorbell": self._doorbell,
            "person": lambda a, k: self._cam(k).detect("person", k.get("confidence")),
            "parcel": lambda a, k: self._cam(k).detect("parcel", k.get("confidence")),
            "vehicle": lambda a, k: self._cam(k).detect("vehicle", k.get("confidence")),
            "say": self._say,
            "manual_lock": lambda a, k: self._dev(k, "lock.front").manual(True),
            "manual_unlock": lambda a, k: self._dev(k, "lock.front").manual(False),
            "temperature": lambda a, k: self._dev(k, "temp.kitchen").set_temperature(k["value"]),
        }

    def _dev(self, args: dict, default: str):
        return self.home.devices[args.get("device", default)]

    def _cam(self, args: dict):
        return self._dev(args, "camera.front")

    def _doorbell(self, actor: str, args: dict) -> None:
        cam = self._cam(args)
        cam.detect("person", args.get("confidence"))
        cam.ring()

    def _say(self, actor: str, args: dict) -> None:
        sat = args.get("satellite", "sat.living")
        ref = f"audio/{sat}/{next(self._audio):04d}"
        payload = {"audio_ref": ref}
        if "text" in args:
            payload["text"] = args["text"]
        self.home.sim.trace("voice_truth", audio_ref=ref, speaker=actor)
        self.home.bus.publish(Envelope(topic=f"voice/{sat}", payload=payload,
                                       source=sat, ts=self.home.sim.now))

    def schedule(self, script: list[ScriptStep]) -> None:
        for t, actor, action, args in script:
            if action not in self.actions:
                raise ValueError(f"unknown household action {action!r}")
            self.home.sim.at(self.day_start + parse_time(t), self._do, actor, action, dict(args))

    def _do(self, actor: str, action: str, args: dict) -> None:
        self.home.sim.trace("household", actor=actor, action=action, args=args)
        self.actions[action](actor, args)
