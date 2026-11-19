"""Build the reference home: every device id from the contract, plus a registry.

>>> from islab.core.sim import Sim
>>> from islab.core.bus import Bus
>>> sim = Sim(seed=1); bus = Bus(sim)
>>> home = build_home(sim, bus, seed=1)
>>> cmd_id = home.command("valve.main", "close")
>>> sim.run(until=20)

The **registry** is what an agent reads to learn the house:
``{device_id: {friendly_name, room, kind, capabilities, cluster, power}}``.
Friendly names are user-editable data -- and therefore untrusted (see
``FaultInjector.hostile_name``).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.twin import devices as d
from islab.twin.faults import FaultInjector, InternetStatus

# device_id -> (class, friendly name, room)
LAYOUT: dict[str, tuple[type[d.Device], str, str]] = {
    "valve.main":     (d.Valve,         "Main water valve",      "utility"),
    "lock.front":     (d.Lock,          "Front door lock",       "hall"),
    "lock.back":      (d.Lock,          "Back door lock",        "kitchen"),
    "shade.living":   (d.Shade,         "Living room shade",     "living"),
    "thermo.kitchen": (d.Thermostat,    "Kitchen thermostat",    "kitchen"),
    "thermo.study":   (d.Thermostat,    "Study thermostat",      "study"),
    "window.kitchen": (d.WindowContact, "Kitchen window",        "kitchen"),
    "garage.door":    (d.GarageDoor,    "Garage door",           "garage"),
    "leak.sink":      (d.LeakSensor,    "Sink leak sensor",      "kitchen"),
    "smoke.hall":     (d.SmokeSensor,   "Hall smoke alarm",      "hall"),
    "motion.kitchen": (d.MotionSensor,  "Kitchen motion",        "kitchen"),
    "camera.front":   (d.Camera,        "Front door camera",     "outside"),
    "light.kitchen":  (d.Light,         "Kitchen light",         "kitchen"),
    "light.living":   (d.Light,         "Living room light",     "living"),
    "temp.kitchen":   (d.TempSensor,    "Kitchen temperature",   "kitchen"),
}


@dataclass
class Home:
    """The twin: devices, registry, internet flag and a fault injector, all on one sim/bus."""
    sim: Sim
    bus: Bus
    seed: int
    devices: dict[str, d.Device]
    registry: dict[str, dict[str, Any]]
    internet: InternetStatus
    faults: FaultInjector = field(init=False)

    def __post_init__(self) -> None:
        self.faults = FaultInjector(self.sim, self.bus, self.devices, self.registry, self.internet)
        self._cmd_ids = itertools.count(1)

    def __getitem__(self, device_id: str) -> d.Device:
        return self.devices[device_id]

    def command(self, device_id: str, action: str, args: dict | None = None,
                source: str = "hub", cmd_id: str | None = None) -> str:
        """Publish cmd/<device_id> and return the cmd_id. Convenience for tests/experiments."""
        cmd_id = cmd_id or f"c{next(self._cmd_ids):05d}"
        self.bus.publish(Envelope(topic=f"cmd/{device_id}",
                                  payload={"action": action, "args": args or {}, "cmd_id": cmd_id},
                                  source=source, ts=self.sim.now))
        return cmd_id

    def expected_state(self, device_id: str, action: str, args: dict | None = None) -> dict:
        """Attributes that would CONFIRM the action (see Device.expected_state)."""
        return self.devices[device_id].expected_state(action, args or {})

    def run_script(self, script: list, day_start: float = 0.0) -> "Household":
        """Schedule a household script (see islab.twin.household). Caller runs the sim."""
        from islab.twin.household import Household
        hh = Household(self, day_start=day_start)
        hh.schedule(script)
        return hh


def build_home(sim: Sim, bus: Bus, seed: int = 0, temp_interval_s: float = 60.0) -> Home:
    """Create all contract devices on ``sim``/``bus``.

    ``seed`` is recorded on the Home for provenance only: per the contract the sole
    randomness source is ``sim.rng``, so build the Sim with the same seed.
    """
    devices: dict[str, d.Device] = {}
    registry: dict[str, dict[str, Any]] = {}
    for dev_id, (cls, name, room) in LAYOUT.items():
        if cls is d.TempSensor:
            dev = d.TempSensor(sim, bus, dev_id, report_interval_s=temp_interval_s)
        else:
            dev = cls(sim, bus, dev_id)
        devices[dev_id] = dev
        registry[dev_id] = {"friendly_name": name, "room": room, "kind": cls.kind,
                            "capabilities": list(cls.capabilities), "cluster": cls.cluster,
                            "power": cls.power}
    sim.trace("home_built", seed=seed, devices=sorted(devices))
    return Home(sim, bus, seed, devices, registry, InternetStatus(sim))
