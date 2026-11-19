"""Home Assistant MQTT discovery payloads for twin devices.

Topic: ``homeassistant/<component>/<object_id>/config`` (retained). ``object_id`` may only use
[a-zA-Z0-9_-], so the twin's dotted ids (``shade.living``) become ``<home>_shade_living``.

Field names come from our knowledge of the Home Assistant MQTT integration (light default schema,
cover, climate, binary_sensor, availability, device, origin). No web access and no running Home
Assistant were available, so they are NOT validated against HA; the templates are rendered with
Jinja2 in tests/test_b02_connect.py against the real state payloads. Marked [knowledge] in notes.md.

Command topics accept Home Assistant style payloads; :func:`ha_command` translates them to the
contract's ``{"action", "args"}``.
"""
from __future__ import annotations

import json
import re
from typing import Any

PREFIX = "homeassistant"
COMPONENT = {"light": "light", "shade": "cover", "thermostat": "climate", "contact": "binary_sensor"}


def base(home: str, device_id: str) -> str:
    return f"islab/{home}/{device_id}"


def object_id(home: str, device_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{home}_{device_id}")


def config_topic(home: str, device_id: str, kind: str) -> str | None:
    comp = COMPONENT.get(kind)
    return f"{PREFIX}/{comp}/{object_id(home, device_id)}/config" if comp else None


def config(home: str, device_id: str, kind: str, name: str, qos: int = 1) -> dict[str, Any] | None:
    comp = COMPONENT.get(kind)
    if comp is None:
        return None
    b = base(home, device_id)
    cfg: dict[str, Any] = {
        "name": None,                                   # entity takes the device name
        "unique_id": object_id(home, device_id),
        "availability_topic": f"{b}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "qos": qos,
        "device": {"identifiers": [object_id(home, device_id)], "name": name,
                   "manufacturer": "Intelligent Spaces Lab (simulated)", "model": f"twin {kind}"},
        "origin": {"name": "islab.connect", "url": "https://www.intersog.com"},
    }
    st = f"{b}/state"
    if kind == "light":
        cfg.update({"command_topic": f"{b}/light/set", "state_topic": st,
                    "state_value_template": "{{ value_json.state }}",
                    "payload_on": "on", "payload_off": "off",
                    "brightness_command_topic": f"{b}/brightness/set",
                    "brightness_state_topic": st,
                    "brightness_value_template": "{{ value_json.brightness }}",
                    "brightness_scale": 100})
    elif kind == "shade":
        cfg.update({"device_class": "shade", "command_topic": f"{b}/cover/set",
                    "payload_open": "OPEN", "payload_close": "CLOSE", "payload_stop": None,
                    "state_topic": st, "value_template": "{{ value_json.state }}",
                    "state_open": "open", "state_closed": "closed",
                    "state_opening": "opening", "state_closing": "closing",
                    "position_topic": st, "position_template": "{{ value_json.position }}",
                    "set_position_topic": f"{b}/position/set",
                    "position_open": 100, "position_closed": 0})
    elif kind == "thermostat":
        cfg.update({"modes": ["off", "heat", "cool"],
                    "mode_command_topic": f"{b}/mode/set",
                    "mode_state_topic": st, "mode_state_template": "{{ value_json.mode }}",
                    "temperature_command_topic": f"{b}/temperature/set",
                    "temperature_state_topic": st,
                    "temperature_state_template": "{{ value_json.setpoint }}",
                    "current_temperature_topic": st,
                    "current_temperature_template": "{{ value_json.current_temp }}",
                    "min_temp": 5, "max_temp": 30, "temp_step": 0.5,
                    "temperature_unit": "C", "precision": 0.1})
    elif kind == "contact":
        cfg.update({"device_class": "window", "state_topic": st,
                    "value_template": "{{ value_json.state }}",
                    "payload_on": "open", "payload_off": "closed"})
    return cfg


# HA command subtopic -> translator(payload str) -> (action, args)
_HA_COMMANDS = {
    "light/set": lambda p: (p.strip().lower(), {}),
    "brightness/set": lambda p: ("set_brightness", {"brightness": float(p)}),
    "cover/set": lambda p: ({"OPEN": "open", "CLOSE": "close"}[p.strip().upper()], {}),
    "position/set": lambda p: ("set_position", {"position": float(p)}),
    "mode/set": lambda p: ("set_mode", {"mode": p.strip().lower()}),
    "temperature/set": lambda p: ("set_setpoint", {"setpoint": float(p)}),
}
HA_SUBTOPICS = tuple(_HA_COMMANDS)


def ha_command(subtopic: str, payload: bytes | str) -> tuple[str, dict] | None:
    """Translate a Home Assistant command payload; None if it cannot be parsed."""
    fn = _HA_COMMANDS.get(subtopic)
    if fn is None:
        return None
    try:
        return fn(payload.decode() if isinstance(payload, bytes) else payload)
    except (KeyError, ValueError):
        return None


def dumps(cfg: dict[str, Any]) -> bytes:
    return json.dumps(cfg, separators=(",", ":")).encode()
