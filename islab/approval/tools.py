"""B07 tools, policy additions and the words-to-arguments parser.

Adds one level-3 tool to the B01 registry (``islab.policy.tools``) without editing it:

  open_garage {"purpose": "delivery"|"guest"|"maintenance"|"family", "until": "HH:MM"}
      Open the garage door until a clock time on the same day. The home closes it at ``until``.

``purpose`` is an enum on purpose: an approval prompt never carries free text from a visitor,
a delivery note or a calendar entry. ``describe`` builds every prompt from the tool and its
validated arguments only.

``word_args`` returns only the arguments a person's own words name (door, end time, recipient,
purpose). The gate compares them with the proposal: B01 checked that the person asked for the
*tool*; B07 also checks the *arguments* the person named.
"""
import copy
import re

from islab.policy.engine import load_policy
from islab.policy.tools import Command, Tool, _obj
from islab.policy.agent import parse_intents

PURPOSES = ["delivery", "guest", "maintenance", "family"]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
HIGH_RISK = {"unlock_door", "open_valve", "share_camera_clip", "open_garage"}


def hhmm_to_s(text):
    h, m = text.split(":")
    return int(h) * 3600 + int(m) * 60


def s_to_hhmm(s):
    s = int(s) % 86400
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def until_abs(until, now):
    """Absolute sim time of an 'HH:MM' end time on the day of ``now`` (no wrap past midnight)."""
    return (now // 86400) * 86400 + hhmm_to_s(until)


def default_until(now, minutes=15):
    """now + minutes, rounded up to the next 5 minutes, as HH:MM (same day, capped at 23:55)."""
    t = int(now % 86400) + minutes * 60
    t = min(((t + 299) // 300) * 300, 23 * 3600 + 55 * 60)
    return s_to_hhmm(t)


def add_b07_tools(registry):
    """Register ``open_garage`` on a B01 ``ToolRegistry`` (MCP shape: name, description, inputSchema)."""
    registry.register(Tool(
        "open_garage", "Open the garage door until a clock time today (HH:MM). The home closes it then.",
        _obj({"purpose": {"type": "string", "enum": PURPOSES},
              "until": {"type": "string", "maxLength": 5}}, ["purpose", "until"]),
        "device",
        build=lambda a: [Command("garage.door", "open", {}, {"state": "open", "position": 100},
                                 "so it may still be closed")],
        depends_on=lambda a: ["garage.door"]))
    return registry


def valid_hhmm(s):
    return isinstance(s, str) and re.match(HHMM, s) is not None


def b07_policy(base=None):
    """B01 policy (a deep copy) plus B07 additions. The file islab/policy/policy.yaml is not changed."""
    cfg = copy.deepcopy(base if base is not None else load_policy())
    cfg["levels"]["open_garage"] = 3
    cfg.setdefault("approvers", {})["open_garage"] = ["anna", "mark"]
    perms = cfg.setdefault("permissions", {})
    perms.setdefault("sam", {}).setdefault("deny", []).append("open_garage")
    # A visitor at the door (doorbell channel) may *ask* for these; each still needs an approval.
    perms["visitor"] = {"allow": ["open_garage", "unlock_door"]}
    cfg["b07"] = {"default_window_s": 1200,   # no end time in the words: at most 20 min
                  "max_window_s": 7200,       # an end time in the words: at most 2 h ahead
                  "escalate_after_s": 45}
    return cfg


def describe(tool, args, actor, via):
    """Trusted prompt text: built from the tool and validated args only, never from untrusted text."""
    a = args or {}
    what = {
        "open_garage": lambda: f"open the garage for a {a.get('purpose')} until {a.get('until')}",
        "unlock_door": lambda: f"unlock the {a.get('door')} door",
        "lock_door": lambda: f"lock the {a.get('door')} door",
        "open_valve": lambda: "turn the main water back on",
        "close_valve": lambda: "close the main water valve",
        "share_camera_clip": lambda: f"share {a.get('seconds', 20)} s of the front camera with {a.get('recipient')}",
        "close_garage": lambda: "close the garage",
        "arm_away": lambda: "secure the house for away",
        "set_light": lambda: f"turn the {a.get('room')} light {'on' if a.get('on') else 'off'}"
                             + (f" at {a['brightness']}%" if "brightness" in a else ""),
        "set_shade": lambda: f"move the living room shade to {a.get('position')}%",
        "set_climate": lambda: f"set the {a.get('room')} to {a.get('setpoint_c')} C",
    }.get(tool, lambda: tool.replace("_", " "))()
    who = {"visitor": "a visitor at the front door", "guest": "an unidentified voice",
           "automation": "an automation"}.get(actor, actor.capitalize() if actor else "?")
    return f"{what[0].upper()}{what[1:]}? Requested by {who} via {via}."


# ---- words -> calls / named arguments -------------------------------------------------------------
_GARAGE_OPEN = re.compile(r"\bopen\b[^.]*\bgarage\b|\bgarage\b[^.]*\bopen\b")
_UNTIL = re.compile(r"\buntil (\d{1,2})[:.](\d{2})\b")


def _purpose(t):
    if re.search(r"\b(parcel|package|delivery|courier)\b", t):
        return "delivery"
    if re.search(r"\b(plumber|cleaner|repair|electrician)\b", t):
        return "maintenance"
    if re.search(r"\b(guest|friend)\b", t):
        return "guest"
    return None


def _until(t):
    m = _UNTIL.search(t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def b07_intents(text, now=0.0):
    """B01 ``parse_intents`` plus ``open_garage``. Deterministic; used by the rule planner and the
    agent doubles. Unspecified end time -> now + 15 min (rounded up to 5 min)."""
    t = " " + (text or "").lower().strip() + " "
    calls = [c for c in parse_intents(text)]
    if _GARAGE_OPEN.search(t) and not re.search(r"\b(close|shut)\b", t):
        calls.append({"name": "open_garage",
                      "arguments": {"purpose": _purpose(t) or "family", "until": _until(t) or default_until(now)}})
    return calls


def b07_intent_tools(text):
    return {c["name"] for c in b07_intents(text)}


def word_args(tool, text):
    """Only the arguments the person's words actually name."""
    t = " " + (text or "").lower() + " "
    out = {}
    if tool in ("unlock_door", "lock_door"):
        if re.search(r"\bback\b", t):
            out["door"] = "back"
        elif re.search(r"\bfront\b", t):
            out["door"] = "front"
    elif tool == "open_garage":
        u, p = _until(t), _purpose(t)
        if u:
            out["until"] = u
        if p:
            out["purpose"] = p
    elif tool == "share_camera_clip":
        m = re.search(r"\bwith ([a-z0-9@._-]+)", t)
        if m:
            out["recipient"] = m.group(1)
    return out
