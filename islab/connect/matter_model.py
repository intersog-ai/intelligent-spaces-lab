"""Matter data-model view of twin devices. MODELLED: no Matter stack, no Thread/Wi-Fi radio,
no commissioning, no fabric, no Interaction Model transport.

What is real here: cluster ids and attribute ids are read from ``chip.clusters.Objects`` (package
``home-assistant-chip-clusters``, generated from the connectedhomeip SDK) when it is installed, and
checked against the constants below. Without it the constants are used (``SOURCE`` says which).

What comes from our own knowledge of the Matter specification (no web access while writing; marked
[knowledge] and listed in experiments/b02_connect/notes.md):
* device type ids (0x0101 Dimmable Light, 0x0301 Thermostat, 0x0202 Window Covering, 0x0015 Contact Sensor);
* WindowCovering lift percent is CLOSED-ness: 0 = fully open, 10000 (100.00%) = fully closed.
  The twin and Home Assistant use percent OPEN, so the adapter inverts it;
* BooleanState.StateValue on a Contact Sensor: True = contact (closed), False = open. Inverted too;
* Thermostat temperatures are int16 in 0.01 degC (SystemMode off=0, cool=3, heat=4 IS checked against the SDK);
* LevelControl.CurrentLevel is 0..254 (1..254 in use when on).
"""
from __future__ import annotations

from typing import Any

# (cluster name, cluster id, {attribute name: attribute id}) -- checked against the SDK package below
CLUSTERS: dict[str, tuple[int, dict[str, int]]] = {
    "OnOff":         (0x0006, {"OnOff": 0x0000}),
    "LevelControl":  (0x0008, {"CurrentLevel": 0x0000}),
    "Thermostat":    (0x0201, {"LocalTemperature": 0x0000, "OccupiedHeatingSetpoint": 0x0012,
                               "UnoccupiedHeatingSetpoint": 0x0014, "SystemMode": 0x001C}),
    "WindowCovering": (0x0102, {"OperationalStatus": 0x000A,
                                "TargetPositionLiftPercent100ths": 0x000B,
                                "CurrentPositionLiftPercent100ths": 0x000E}),
    "BooleanState":  (0x0045, {"StateValue": 0x0000}),
}

DEVICE_TYPES = {   # [knowledge] Matter device library ids
    "light": (0x0101, "Dimmable Light", ["OnOff", "LevelControl"]),
    "thermostat": (0x0301, "Thermostat", ["Thermostat"]),
    "shade": (0x0202, "Window Covering", ["WindowCovering"]),
    "contact": (0x0015, "Contact Sensor", ["BooleanState"]),
}

SYSTEM_MODE = {"off": 0, "cool": 3, "heat": 4}   # verified against SystemModeEnum when the SDK package is present


def _sdk_check() -> str:
    """Compare our constants with the SDK-generated package. Returns a provenance string."""
    try:
        from chip.clusters import Objects as C   # home-assistant-chip-clusters
    except Exception:
        return "constants in islab/connect/matter_model.py (chip.clusters not installed)"
    for cname, (cid, attrs) in CLUSTERS.items():
        cl = getattr(C, cname)
        if cl.id != cid:
            raise AssertionError(f"{cname}: cluster id {cl.id:#06x} != {cid:#06x}")
        for aname, aid in attrs.items():
            got = getattr(cl.Attributes, aname).attribute_id
            if got != aid:
                raise AssertionError(f"{cname}.{aname}: {got:#06x} != {aid:#06x}")
    E = C.Thermostat.Enums.SystemModeEnum
    if (E.kOff.value, E.kCool.value, E.kHeat.value) != (SYSTEM_MODE["off"], SYSTEM_MODE["cool"],
                                                        SYSTEM_MODE["heat"]):
        raise AssertionError("Thermostat.SystemModeEnum differs")
    try:
        from importlib.metadata import version
        v = version("home-assistant-chip-clusters")
    except Exception:
        v = "?"
    return f"chip.clusters.Objects (home-assistant-chip-clusters {v}), ids verified"


SOURCE = _sdk_check()


def descriptor(kind: str) -> dict[str, Any] | None:
    """Static endpoint description, published retained on islab/<home>/<dev>/matter."""
    if kind not in DEVICE_TYPES:
        return None
    dt, name, clusters = DEVICE_TYPES[kind]
    return {"endpoint": 1, "device_type": f"0x{dt:04X}", "device_type_name": name,
            "clusters": {c: {"id": f"0x{CLUSTERS[c][0]:04X}",
                             "attributes": {a: f"0x{i:04X}" for a, i in CLUSTERS[c][1].items()}}
                         for c in clusters},
            "source": SOURCE, "modelled": True}


def to_matter(kind: str, state: dict[str, Any]) -> dict[str, Any]:
    """Twin state -> {"Cluster.Attribute": value} in Matter units."""
    if kind == "light":
        on = state.get("state") == "on"
        b = state.get("brightness", 0)
        return {"OnOff.OnOff": on, "LevelControl.CurrentLevel": round(b * 254 / 100) if on else 0}
    if kind == "thermostat":
        return {"Thermostat.LocalTemperature": round(state.get("current_temp", 0) * 100),
                "Thermostat.OccupiedHeatingSetpoint": round(state.get("setpoint", 0) * 100),
                "Thermostat.SystemMode": SYSTEM_MODE.get(state.get("mode", "off"), 0)}
    if kind == "shade":
        closed_pct100 = round((100 - state.get("position", 100)) * 100)   # inverted: closed-ness
        moving = state.get("state") in ("opening", "closing")
        return {"WindowCovering.CurrentPositionLiftPercent100ths": closed_pct100,
                "WindowCovering.OperationalStatus": (0b01 if state.get("state") == "opening" else 0b10)
                if moving else 0}
    if kind == "contact":
        return {"BooleanState.StateValue": state.get("state") == "closed"}  # True = contact = closed
    return {}


def from_matter(kind: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`to_matter` for the attributes that carry state (used by tests)."""
    if kind == "light":
        on = bool(attrs["OnOff.OnOff"])
        return {"state": "on" if on else "off",
                "brightness": round(attrs["LevelControl.CurrentLevel"] * 100 / 254) if on else 0}
    if kind == "thermostat":
        inv = {v: k for k, v in SYSTEM_MODE.items()}
        return {"current_temp": attrs["Thermostat.LocalTemperature"] / 100,
                "setpoint": attrs["Thermostat.OccupiedHeatingSetpoint"] / 100,
                "mode": inv[attrs["Thermostat.SystemMode"]]}
    if kind == "shade":
        return {"position": 100 - attrs["WindowCovering.CurrentPositionLiftPercent100ths"] / 100}
    if kind == "contact":
        return {"state": "closed" if attrs["BooleanState.StateValue"] else "open"}
    return {}
