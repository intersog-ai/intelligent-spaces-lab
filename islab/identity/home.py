"""The twin home with the B06 trust policy in front of the B01 policy engine.

    h = IdentityHome(seed=1)
    out = h.request(ev, "unlock_door", {"door": "front"}, text="unlock the front door")
    h.phone_reply("anna", out.request_id, "yes", after_s=20)   # the only way a level 3 runs
    h.sim.run(until=h.sim.now + 200)
    h.level3_by_voice_alone()                                   # must be 0

Wiring: islab.twin.build_home + the AlarmPanel from islab.suites.house (so "disarm" is a real device
command), islab.policy StateStore / ActionTracker / AuditLog / PolicyEngine with
``TrustPolicy.engine_config()``, and a registry that adds two level-3 tools the base registry lacks:
``open_garage`` and ``disarm_alarm`` (added here by subclassing, not by editing islab.policy).
A probe wraps ``PolicyEngine._execute`` and records every execution with its level and approval, so the
count of level-3 actions run without a phone approval is observed independently of the engine's own
bookkeeping.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.policy import ActionTracker, AuditLog, Command, PolicyEngine, StateStore, Tool, ToolRegistry
from islab.policy.engine import _intent
from islab.suites.house import AlarmPanel
from islab.twin import build_home

from .trust import LEVEL3, Evidence, TrustPolicy

ALARM = "alarm.panel"
# islab.policy.engine._intent has no words for the two tools added here (it would say "open_garage done").
_WORDS = {"open_garage": "open the garage", "disarm_alarm": "disarm the alarm"}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


class IdentityToolRegistry(ToolRegistry):
    """ToolRegistry + open_garage + disarm_alarm (both level 3 in household.yaml)."""

    def __init__(self, people=("anna", "mark", "sam")):
        super().__init__(people=list(people), alarm_device=ALARM)
        self.register(Tool("open_garage", "Open the garage door.", _obj({}), "device",
                           build=lambda a: [Command("garage.door", "open", {}, {"state": "open", "position": 100},
                                                    "so it may still be closed")]))
        self.register(Tool("disarm_alarm", "Disarm the intrusion alarm.", _obj({}), "device",
                           build=lambda a: [Command(ALARM, "disarm", {}, {"mode": "disarmed"},
                                                    "so the alarm may still be armed")]))


@dataclass
class Outcome:
    request_id: str | None
    tool: str
    status: str
    reason: str
    actor: str | None
    tier: str
    say: str = ""
    notes: list = field(default_factory=list)


class IdentityHome:
    def __init__(self, seed: int = 0, policy: TrustPolicy | None = None, engine_config: dict | None = None):
        self.policy = policy or TrustPolicy()
        self.sim = Sim(seed=seed)
        self.bus = Bus(self.sim)
        self.home = build_home(self.sim, self.bus, seed=seed)
        self.panel = AlarmPanel(self.sim, self.bus, ALARM)
        self.home.devices[ALARM] = self.panel
        self.cfg = engine_config if engine_config is not None else self.policy.engine_config()
        self.audit = AuditLog(self.sim)
        self.store = StateStore(self.sim, self.bus)
        self.tracker = ActionTracker(self.sim, self.bus, timeout_s=self.cfg.get("action_timeout_s", 30),
                                     audit=self.audit)
        self.registry = IdentityToolRegistry(people=list(self.cfg.get("people", {})))
        self.engine = PolicyEngine(self.sim, self.bus, self.store, registry=self.registry, config=self.cfg,
                                   tracker=self.tracker, audit=self.audit)
        self.notes = []                                   # (t, person, payload)
        self.bus.subscribe("notify/+", lambda e: self.notes.append((self.sim.now, e.topic.split("/")[1], e.payload)),
                           name="b06.phones")
        self.executions = []                              # probe: everything that reached devices
        self._orig_execute = self.engine._execute
        self.engine._execute = self._probe_execute
        self.sim.run(until=self.sim.now + 5)              # first state reports
        self.panel.update(mode="armed_away")              # the house starts armed (keypad, local)
        self.sim.run(until=self.sim.now + 2)

    def _probe_execute(self, d, p):
        self.executions.append({"request_id": d.request_id, "tool": d.tool, "level": self.engine.level(d.tool),
                                "actor": d.actor, "channel": p.channel, "approval": d.approval,
                                "approved_via": (d.approval or {}).get("via"), "t": self.sim.now})
        return self._orig_execute(d, p)

    # ---- requests ----------------------------------------------------------------------------
    def request(self, ev: Evidence, tool: str, args: dict | None = None, text: str = "") -> Outcome:
        tier = self.policy.tier(ev)
        n0 = len(self.notes)
        d = self.engine.propose(self.policy.proposal(ev, tool, args or {}, text=text))
        o = Outcome(d.request_id, tool, d.status, d.reason, d.actor, tier,
                    notes=[(p, pl.get("text")) for _, p, pl in self.notes[n0:]])
        o.say = self.say_for(o, args or {})
        return o

    def say_for(self, o: Outcome, args: dict) -> str:
        """What the home says out loud at the satellite (plain words, no score, no name guess)."""
        what = _WORDS.get(o.tool) or _intent(o.tool, args)
        names = " or ".join(n.capitalize() for n in self.engine.approvers(o.tool)) or "an adult"
        known = o.actor in self.engine.people
        if o.status == "awaiting_approval":
            if known:
                return f"Okay {o.actor.capitalize()}. To {what} I need a yes on the phone. I've sent the request."
            return f"Only {names} can {what}. I've sent them a request."
        if o.status == "denied" and o.reason == "not_permitted":
            if known:
                return f"Sorry {o.actor.capitalize()}, you can't {what} in this house."
            return (f"I can't {what} for a voice I don't recognise. "
                    f"{names} can do it in the app or at the keypad.")
        if o.status == "asked":
            return f"I'll check with {o.actor.capitalize() if known else names} on the phone first."
        if o.status == "executing":
            return f"Okay, I'll {what}."
        return f"I didn't {what}."

    def phone_reply(self, person: str, request_id: str, answer: str = "yes", after_s: float = 15.0):
        """A person answers on their phone (reply/<person>), after_s seconds from now."""
        def send():
            self.bus.publish(Envelope(f"reply/{person}", {"request_id": request_id, "answer": answer},
                                      f"phone.{person}", self.sim.now))
        self.sim.after(after_s, send)

    def voice_approval(self, ev: Evidence, request_id: str) -> bool:
        """Someone says "yes" out loud to a pending approval. The engine refuses it for level 3."""
        who = self.policy.who(ev, "named")
        return self.engine.voice_answer(request_id, who, ev.score if who else None, "yes")

    # ---- counts ------------------------------------------------------------------------------
    def level3_executions(self):
        return [e for e in self.executions if e["level"] >= 3 or e["tool"] in LEVEL3]

    def level3_by_voice_alone(self) -> int:
        """Level-3 executions without a recorded phone approval (must be 0)."""
        return sum(1 for e in self.level3_executions() if e["approved_via"] != "phone")
