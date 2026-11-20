"""Intent stage: a rule grammar first, an LLM stub second.

    g = Grammar()
    it = g.parse("turn it off", satellite="sat.kitchen", ctx=DialogueContext(), now=12.0)
    it.status   # "ok" | "ambiguous" | "incomplete" | "none"
    it.calls    # [{"name": tool, "arguments": {...}}] when ok
    it.question # what the pipeline says when it cannot act

The grammar wraps the B01 deterministic parser (islab.policy.agent.parse_intents) and adds what a
voice front end needs before anything reaches the policy engine:
  * number words from speech-to-text ("twenty one" -> 21, "fifty percent" -> 50 %);
  * a missing room is taken from the satellite's room, never silently defaulted;
  * pronouns ("turn it off", "close it") resolve only from this satellite's last action within
    CONTEXT_S seconds and only when that action touched exactly one device; otherwise it asks;
  * relative requests ("a bit warmer", "same in my study") need a fresh setpoint or a previous action;
  * incomplete commands ("set the kitchen to", "turn the light") ask for the missing part.

LLMStub is NOT a language model. It is a deterministic fuzzy matcher against canonical command
phrases (difflib), standing in for an LLM fallback so the pipeline's second path can be timed and
tested. Its latency comes from the tier's assumed LLM numbers. Its calls go through the same policy
engine; a level-3 call it proposes is not traceable to the person's words and is denied there.
"""
from __future__ import annotations
import difflib
import re
from dataclasses import dataclass, field

from islab.policy.agent import parse_intents, intent_tools

CONTEXT_S = 60.0
SAT_ROOM = {"sat.kitchen": "kitchen", "sat.living": "living"}
THERMO_ROOMS = ("kitchen", "study")
LIGHT_ROOMS = ("kitchen", "living")

# tool -> device ids it touches (for context and for scoring STT errors on device names)
def devices_of(call: dict) -> list:
    n, a = call["name"], call.get("arguments") or {}
    if n == "set_light":
        return [f"light.{a.get('room')}"]
    if n == "set_climate":
        return [f"thermo.{a.get('room')}"]
    if n in ("lock_door", "unlock_door"):
        return [f"lock.{a.get('door')}"]
    if n in ("close_valve", "open_valve"):
        return ["valve.main"]
    if n == "close_garage":
        return ["garage.door"]
    if n == "set_shade":
        return ["shade.living"]
    if n == "get_state":
        return [a.get("device_id")]
    if n == "arm_away":
        return ["lock.front", "lock.back", "garage.door"]
    return []


# ---------------------------------------------------------------- number words
_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}


def words_to_numbers(text: str) -> str:
    """'set the kitchen to twenty one degrees' -> 'set the kitchen to 21 degrees';
    'fifty percent' / '50 percent' / '50%' -> '50 %'. Leaves other words alone."""
    toks = re.findall(r"[a-z]+|\d+(?:\.\d+)?|%|[^\sa-z\d]", text.lower().replace("-", " "))
    out, i = [], 0
    while i < len(toks):
        w = toks[i]
        if w in _TENS:
            n = _TENS[w]
            if i + 1 < len(toks) and toks[i + 1] in _UNITS and 0 < _UNITS[toks[i + 1]] < 10:
                n += _UNITS[toks[i + 1]]
                i += 1
            out.append(str(n))
        elif w in _UNITS and w not in ("one",):     # 'one' is too often a pronoun ("the other one")
            out.append(str(_UNITS[w]))
        elif w == "one" and i > 0 and toks[i - 1] in ("to", "at"):
            out.append("1")
        elif w == "hundred" and out and out[-1].isdigit():
            out[-1] = str(int(out[-1]) * 100)
        elif w in ("percent", "per") and out and out[-1].replace(".", "").isdigit():
            out.append("%")
            if w == "per" and i + 1 < len(toks) and toks[i + 1] == "cent":
                i += 1
        else:
            out.append(w)
        i += 1
    s = " ".join(out)
    return re.sub(r"(\d+(?:\.\d+)?)\s*%", r"\1 %", s)


# ---------------------------------------------------------------- dialogue context
@dataclass
class Turn:
    t: float
    calls: list
    devices: list


class DialogueContext:
    """Per-satellite memory of the last executed request (for 'it', 'same')."""

    def __init__(self, window_s: float = CONTEXT_S):
        self.window_s = window_s
        self.last = {}           # satellite -> Turn

    def remember(self, satellite: str, calls: list, now: float):
        devs = sorted({d for c in calls for d in devices_of(c)})
        self.last[satellite] = Turn(now, [dict(c) for c in calls], devs)

    def recent(self, satellite: str, now: float):
        t = self.last.get(satellite)
        return t if t and now - t.t <= self.window_s else None


# ---------------------------------------------------------------- grammar
@dataclass
class Interpretation:
    text: str
    status: str                       # ok | ambiguous | incomplete | none
    calls: list = field(default_factory=list)
    question: str = ""
    resolved_by: str = "words"        # words | room | context | relative | llm_stub
    norm: str = ""

    @property
    def ok(self):
        return self.status == "ok"


_PRONOUN = re.compile(r"\b(it|that|them|this one|the other one)\b")
_DEVICE_WORDS = re.compile(r"\b(light|lights|lamp|shade|shades|blind|blinds|garage|door|lock|water|valve|thermostat|"
                           r"heat|heating|temperature|camera|window)\b")
_ON = re.compile(r"\b(on)\b")
_OFF = re.compile(r"\b(off|out)\b")
_ROOM_WORDS = {"kitchen": "kitchen", "living": "living", "lounge": "living", "study": "study"}


def _rooms_in(t):
    return [r for w, r in _ROOM_WORDS.items() if re.search(rf"\b{w}\b", t)]


class Grammar:
    """Deterministic rules. ``store`` (optional, islab.policy.state.StateStore) is read for relative
    setpoints; the policy engine re-checks freshness before acting."""
    name = "rules"

    def __init__(self, store=None):
        self.store = store

    def parse(self, text: str, satellite: str = "sat.kitchen", ctx: DialogueContext | None = None,
              now: float = 0.0) -> Interpretation:
        raw = (text or "").strip()
        t = " " + words_to_numbers(raw) + " "
        t = re.sub(r"(?<!\d)\.|\.(?!\d)|[,!?;:\"\u2026]", " ", t)     # STT punctuation, keep 21.5
        t = re.sub(r"\s+", " ", t)
        it = Interpretation(raw, "none", norm=t.strip())
        if not raw:
            return it
        sat_room = SAT_ROOM.get(satellite)
        rooms = _rooms_in(t)
        recent = ctx.recent(satellite, now) if ctx else None

        # 1. "same in my study" / "same for the kitchen": repeat the last request for another room
        if re.search(r"\b(same|too|as well)\b", t) and rooms and not _DEVICE_WORDS.search(t):
            if recent and len(recent.calls) == 1 and "room" in (recent.calls[0].get("arguments") or {}):
                c = {"name": recent.calls[0]["name"], "arguments": dict(recent.calls[0]["arguments"], room=rooms[0])}
                it.status, it.calls, it.resolved_by = "ok", [c], "context"
                return it
            it.status, it.question = "ambiguous", "Same as what? Please say the whole request."
            return it

        # 2. relative heat: "a bit warmer in here", "cooler"
        m = re.search(r"\b(warmer|cooler|colder|hotter)\b", t)
        if m and not re.search(r"\d", t):
            room = rooms[0] if rooms else (sat_room if sat_room in THERMO_ROOMS else None)
            if room is None:
                it.status, it.question = "ambiguous", "Which room: the kitchen or the study?"
                return it
            cur = self._setpoint(room)
            if cur is None:
                it.status, it.question = "incomplete", f"I don't have a fresh reading of the {room} thermostat. What temperature?"
                return it
            delta = 1.0 if m.group(1) in ("warmer", "hotter") else -1.0
            it.status, it.resolved_by = "ok", "relative"
            it.calls = [{"name": "set_climate", "arguments": {"room": room, "setpoint_c": cur + delta}}]
            return it

        # 3. pronouns with no device word: context or ask
        if _PRONOUN.search(t) and not _DEVICE_WORDS.search(t) and not rooms:
            verb_on = _ON.search(t) and not _OFF.search(t)
            verb_off = bool(_OFF.search(t))
            close = re.search(r"\b(close|shut)\b", t)
            open_ = re.search(r"\bopen\b", t)
            if not (verb_on or verb_off or close or open_ or re.search(r"\b(lock|unlock|keep)\b", t)):
                return it                       # no command verb: not a request ("I'm closing it now")
            if recent and len(recent.devices) == 1:
                c = self._apply_to(recent.devices[0], t)
                if c:
                    it.status, it.calls, it.resolved_by = "ok", [c], "context"
                    return it
            it.status, it.question = "ambiguous", self._which(t, sat_room)
            return it

        # 4. incomplete commands (cut-off endings)
        if re.search(r"\b(turn|switch)\b", t) and _DEVICE_WORDS.search(t) and not (_ON.search(t) or _OFF.search(t)) \
                and not re.search(r"\d", t):
            it.status, it.question = "incomplete", "On or off?"
            return it
        if re.search(r"\b(the|my|to|at|in|and|of|a)\s*$", t.strip()) or \
                re.search(r"^\s*(close|shut|lock|unlock|open|turn|switch|set)( (on|off))?\s*$", t.strip()):
            it.status, it.question = "incomplete", "I think I missed the end of that. Please say the whole request."
            return it
        if re.search(r"\bset the (kitchen|study)( thermostat)?( to)?\s*$", t.strip() + " ") or \
                re.search(r"\bset the (kitchen|study)\b", t) and not re.search(r"\d", t) and "light" not in t:
            it.status, it.question = "incomplete", "To what temperature?"
            return it

        # 5. lights with no room: the satellite's room, never a silent default
        if re.search(r"\b(light|lights|lamp)\b", t) and not any(r in LIGHT_ROOMS for r in rooms):
            if sat_room in LIGHT_ROOMS:
                t = t.replace(" light", f" {sat_room} light", 1)
                it.resolved_by = "room"
            else:
                it.status, it.question = "ambiguous", "Which light: the kitchen or the living room?"
                return it
        # thermostat with a number but no room: the satellite's room if it has one
        if re.search(r"\b(degrees|thermostat|heat)\b", t) and re.search(r"\d", t) and not rooms:
            if sat_room in THERMO_ROOMS:
                t = t + f" {sat_room} "
                it.resolved_by = "room"
            else:
                it.status, it.question = "ambiguous", "Which room: the kitchen or the study?"
                return it

        # 6. questions are never commands: B01's parser reads "is the back door lock(ed)" as lock_door
        q = re.match(r"^\s*(is|are|was)\b.*\b(front|back) door\b", t)
        if q:
            it.status, it.calls = "ok", [{"name": "get_state", "arguments": {"device_id": f"lock.{q.group(2)}"}}]
            return it
        if re.match(r"^\s*(is|are|what|how|which|when|where|do|does)\b", t):
            calls = [c for c in parse_intents(t) if c["name"] == "get_state"]
            if calls:
                it.status, it.calls = "ok", calls
            return it
        # shade with no verb or position: B01's parser silently opens it ("cuss the living room shade")
        if re.search(r"\b(shade|shades|blind|blinds)\b", t) and not re.search(
                r"\b(close|lower|down|open|raise|up)\b|\d", t):
            it.status, it.question = "incomplete", "Open or close the living room shade?"
            return it
        calls = parse_intents(t)
        if calls:
            it.status, it.calls = "ok", calls
            return it
        return it

    # ---- helpers
    def _setpoint(self, room):
        if self.store is None:
            return None
        r = self.store.get(f"thermo.{room}", "setpoint") if hasattr(self.store, "get") else None
        v = getattr(r, "value", r)
        return float(v) if isinstance(v, (int, float)) else None

    @staticmethod
    def _apply_to(device_id, t):
        on = bool(_ON.search(t)) and not _OFF.search(t)
        off = bool(_OFF.search(t))
        if device_id.startswith("light."):
            if not (on or off or "keep" in t):
                return None
            want = on or ("keep" in t and not off)
            return {"name": "set_light", "arguments": {"room": device_id.split(".")[1], "on": bool(want)}}
        if device_id == "garage.door" and re.search(r"\b(close|shut)\b", t):
            return {"name": "close_garage", "arguments": {}}
        if device_id.startswith("lock.") and re.search(r"\block\b", t):
            return {"name": "lock_door", "arguments": {"door": device_id.split(".")[1]}}
        if device_id.startswith("lock.") and re.search(r"\bunlock\b", t):
            return {"name": "unlock_door", "arguments": {"door": device_id.split(".")[1]}}
        if device_id == "shade.living" and re.search(r"\b(close|open)\b", t):
            return {"name": "set_shade", "arguments": {"shade": "living", "position": 0 if "close" in t else 100}}
        if device_id == "valve.main" and (off or re.search(r"\b(close|shut)\b", t)):
            return {"name": "close_valve", "arguments": {}}
        return None

    @staticmethod
    def _which(t, sat_room):
        if _ON.search(t) or _OFF.search(t) or "keep" in t:
            return "Which one: the kitchen light or the living room light?"
        if re.search(r"\b(close|shut|open)\b", t):
            return "Which one: the garage door, the living room shade or the water valve?"
        return "Which device do you mean?"


# ---------------------------------------------------------------- LLM stub (second path)
CANONICAL = [
    ("turn on the kitchen light", {"name": "set_light", "arguments": {"room": "kitchen", "on": True}}),
    ("turn off the kitchen light", {"name": "set_light", "arguments": {"room": "kitchen", "on": False}}),
    ("turn on the living room light", {"name": "set_light", "arguments": {"room": "living", "on": True}}),
    ("turn off the living room light", {"name": "set_light", "arguments": {"room": "living", "on": False}}),
    ("close the garage door", {"name": "close_garage", "arguments": {}}),
    ("lock the front door", {"name": "lock_door", "arguments": {"door": "front"}}),
    ("lock the back door", {"name": "lock_door", "arguments": {"door": "back"}}),
    ("unlock the front door", {"name": "unlock_door", "arguments": {"door": "front"}}),
    ("close the living room shade", {"name": "set_shade", "arguments": {"shade": "living", "position": 0}}),
    ("open the living room shade", {"name": "set_shade", "arguments": {"shade": "living", "position": 100}}),
    ("shut off the water", {"name": "close_valve", "arguments": {}}),
]


class LLMStub:
    """Deterministic stand-in for an LLM fallback (NOT a model): best difflib match against
    CANONICAL phrases, accepted at ratio >= threshold. Returns an Interpretation."""
    name = "llm_stub"

    def __init__(self, threshold: float = 0.75):
        self.threshold = threshold

    def parse(self, text: str, **_):
        norm = words_to_numbers(text or "")
        best, score = None, 0.0
        for phrase, call in CANONICAL:
            r = difflib.SequenceMatcher(None, norm, phrase).ratio()
            if r > score:
                best, score = call, r
        it = Interpretation(text or "", "none", norm=norm, resolved_by="llm_stub")
        if best is not None and score >= self.threshold:
            it.status, it.calls = "ok", [{"name": best["name"], "arguments": dict(best["arguments"])}]
        return it


def explicit_for(call: dict, interp: Interpretation) -> bool:
    """Traceability flag for the policy engine: the person's own words ask for this tool
    (B01 intent table), or a level-1 parameter was filled from the room/context/relative rules.
    LLM-stub calls count only if the words themselves carry the intent."""
    if call["name"] in intent_tools(words_to_numbers(interp.text)):
        return True
    return interp.resolved_by in ("room", "context", "relative") and call["name"] in (
        "set_light", "set_climate", "set_shade", "lock_door", "close_garage", "close_valve")
