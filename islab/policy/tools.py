"""Typed tool registry, shaped like Model Context Protocol tools.

Each tool has ``name``, ``description`` and ``inputSchema`` (JSON Schema). ``list_tools()`` returns
exactly the MCP ``tools/list`` shape, and ``openai_tools()`` the OpenAI function-calling shape, so
the same registry can be served to any model. A tool never publishes anything itself: it only
*describes* the device commands it maps to (``Command``) and how each is confirmed (``expect``).
The policy engine decides; the action tracker sends and confirms.

Device action/attribute vocabulary (matches islab/twin/devices.py; ``expect`` = what a later
state/<id> must show for the action to count as CONFIRMED):
  lock.*        "lock" / "unlock"                 -> {"state": "locked" | "unlocked"}
  valve.main    "close" / "open"                  -> {"state": "closed", "position": 0} | open/100
  garage.door   "close"                           -> {"state": "closed", "position": 0}
  shade.*       "set_position" {"position"}       -> {"position": p}
  light.*       "on" / "off" / "set_brightness"   -> {"state": "on"|"off"} (+ {"brightness": b})
  thermo.*      "set_setpoint" {"setpoint"}       -> {"setpoint": x}
  <alarm_device> "arm_away" (optional, not in the contract's device list) -> {"mode": "armed_away"}
"""
from dataclasses import dataclass, field

DEVICES = ["valve.main", "lock.front", "lock.back", "shade.living", "thermo.kitchen", "thermo.study",
           "window.kitchen", "garage.door", "leak.sink", "smoke.hall", "motion.kitchen", "camera.front",
           "light.kitchen", "light.living", "temp.kitchen"]

# Plain-words names used in messages to people. These come from the hub's own config, never from
# device-reported metadata (which is untrusted and may carry injected text).
PLAIN_NAMES = {
    "valve.main": "main water valve", "lock.front": "front door lock", "lock.back": "back door lock",
    "shade.living": "living room shade", "thermo.kitchen": "kitchen thermostat",
    "thermo.study": "study thermostat", "garage.door": "garage door", "light.kitchen": "kitchen light",
    "light.living": "living room light", "camera.front": "front camera", "alarm.panel": "alarm",
}


def plain(device_id):
    return PLAIN_NAMES.get(device_id, device_id)


@dataclass
class Command:
    device_id: str
    action: str
    args: dict
    expect: dict                 # attr -> value a later state must show
    # What it means, in plain words, if this command is not confirmed:
    risk_if_unconfirmed: str = ""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    kind: str                    # "read" | "device" | "message" | "egress"
    build: object = None         # callable(args) -> list[Command]
    depends_on: object = None    # callable(args) -> list[device_id] whose freshness the call relies on

    def mcp(self):
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


# ---- minimal JSON-schema validator (the subset the tools use) ----------------------------
_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def validate(schema, value, path="args"):
    """Return a list of error strings ([] = valid)."""
    errs = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                errs.append(f"{path}.{r}: required")
        for k, v in value.items():
            if k not in props:
                if schema.get("additionalProperties", True) is False:
                    errs.append(f"{path}.{k}: unexpected property")
                continue
            errs += validate(props[k], v, f"{path}.{k}")
        return errs
    if t in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected {t}"]
        if t == "integer" and not float(value).is_integer():
            return [f"{path}: expected integer"]
        if value != value or value in (float("inf"), float("-inf")):
            return [f"{path}: not a finite number"]
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")
    elif t in _TYPES:
        if not isinstance(value, _TYPES[t]):
            return [f"{path}: expected {t}"]
        if t == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    return errs


class ToolRegistry:
    def __init__(self, people=("anna", "mark", "sam"), alarm_device=None):
        self.people = list(people)
        self.alarm_device = alarm_device
        self._tools = {}
        self._register_defaults()

    # ---- registry API --------------------------------------------------------------------
    def register(self, tool):
        self._tools[tool.name] = tool

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return list(self._tools)

    def list_tools(self):
        """MCP tools/list shape."""
        return [t.mcp() for t in self._tools.values()]

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}}
                for t in self._tools.values()]

    def check(self, name, args):
        """(tool, errors). tool is None when the name is not a registered tool."""
        tool = self._tools.get(name)
        if tool is None:
            return None, [f"unknown tool '{name}'"]
        return tool, validate(tool.input_schema, args if args is not None else {})

    def commands(self, name, args):
        tool = self._tools[name]
        return tool.build(args) if tool.build else []

    def depends_on(self, name, args):
        tool = self._tools[name]
        return list(tool.depends_on(args)) if tool.depends_on else []

    # ---- default tools -------------------------------------------------------------------
    def _register_defaults(self):
        person = {"type": "string", "enum": self.people}
        R = self.register

        R(Tool("get_state", "Read the latest sourced reading of a device (value, timestamp, age, event id).",
               _obj({"device_id": {"type": "string", "enum": DEVICES}}, ["device_id"]), "read"))

        R(Tool("set_climate", "Set a room thermostat setpoint in degrees Celsius.",
               _obj({"room": {"type": "string", "enum": ["kitchen", "study"]},
                     "setpoint_c": {"type": "number", "minimum": 10, "maximum": 28}}, ["room", "setpoint_c"]),
               "device",
               build=lambda a: [Command(f"thermo.{a['room']}", "set_setpoint", {"setpoint": a["setpoint_c"]},
                                        {"setpoint": float(a["setpoint_c"])},
                                        "the temperature setting may not have changed")]))

        def _light(a):
            dev, risk = f"light.{a['room']}", "so the light may not have changed"
            if not a["on"]:
                return [Command(dev, "off", {}, {"state": "off"}, risk)]
            if "brightness" in a:
                b = a["brightness"]
                return [Command(dev, "set_brightness", {"brightness": b},
                                {"state": "on" if b > 0 else "off", "brightness": b}, risk)]
            return [Command(dev, "on", {}, {"state": "on"}, risk)]
        R(Tool("set_light", "Turn a light on or off, optionally with brightness 0-100.",
               _obj({"room": {"type": "string", "enum": ["kitchen", "living"]}, "on": {"type": "boolean"},
                     "brightness": {"type": "integer", "minimum": 0, "maximum": 100}}, ["room", "on"]),
               "device", build=_light))

        valve = _obj({"valve": {"type": "string", "enum": ["main"]}})
        R(Tool("close_valve", "Close the main water valve (shuts off water to the house).", valve, "device",
               build=lambda a: [Command("valve.main", "close", {}, {"state": "closed", "position": 0},
                                        "so water may still be running")]))
        R(Tool("open_valve", "Open the main water valve.", valve, "device",
               build=lambda a: [Command("valve.main", "open", {}, {"state": "open", "position": 100},
                                        "so the water may still be off")]))

        R(Tool("close_garage", "Close the garage door.", _obj({}), "device",
               build=lambda a: [Command("garage.door", "close", {}, {"state": "closed", "position": 0},
                                        "so it may still be open")]))

        door = _obj({"door": {"type": "string", "enum": ["front", "back"]}}, ["door"])
        R(Tool("lock_door", "Lock a door.", door, "device",
               build=lambda a: [Command(f"lock.{a['door']}", "lock", {}, {"state": "locked"},
                                        "so it may still be open")]))
        R(Tool("unlock_door", "Unlock a door.", door, "device",
               build=lambda a: [Command(f"lock.{a['door']}", "unlock", {}, {"state": "unlocked"},
                                        "so it may still be locked")]))

        R(Tool("set_shade", "Set the living room shade position, 0 = closed, 100 = open.",
               _obj({"shade": {"type": "string", "enum": ["living"]},
                     "position": {"type": "integer", "minimum": 0, "maximum": 100}}, ["position"]),
               "device",
               build=lambda a: [Command(f"shade.{a.get('shade', 'living')}", "set_position", {"position": a["position"]},
                                        {"position": a["position"]}, "so it may not have moved")]))

        R(Tool("notify_person", "Send a plain-text message to a household member's phone.",
               _obj({"person": person, "text": {"type": "string", "maxLength": 500}}, ["person", "text"]),
               "message"))
        R(Tool("ask_person", "Ask a household member a yes/no question on their phone.",
               _obj({"person": person, "question": {"type": "string", "maxLength": 500}}, ["person", "question"]),
               "message"))

        def _arm(a):
            cmds = [Command("lock.front", "lock", {}, {"state": "locked"}, "so it may still be open"),
                    Command("lock.back", "lock", {}, {"state": "locked"}, "so it may still be open"),
                    Command("garage.door", "close", {}, {"state": "closed", "position": 0},
                            "so it may still be open")]
            if self.alarm_device:
                cmds.append(Command(self.alarm_device, "arm_away", {}, {"mode": "armed_away"},
                                    "so the alarm may not be armed"))
            return cmds
        R(Tool("arm_away", "Secure the house for being away: lock both doors, close the garage"
               + (", arm the alarm." if self.alarm_device else "."),
               _obj({}), "device", build=_arm))

        R(Tool("share_camera_clip", "Share a short clip from a camera with someone outside the house.",
               _obj({"camera": {"type": "string", "enum": ["front"]},
                     "recipient": {"type": "string", "maxLength": 120},
                     "seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, ["camera", "recipient"]),
               "egress"))
