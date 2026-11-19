"""Policy engine: people set limits and can override (principle 3).

Every proposed tool call, whoever proposed it (rule planner, LLM, automation), passes through
``PolicyEngine.propose``. The order of checks is fixed and each step writes to the audit log:

 1. tool exists and arguments match its JSON schema           -> else ``rejected``
 2. identity: voice match >= min_confidence names the speaker; otherwise the speaker is a guest.
    Voice can raise trust to a named person's permissions; it can never authorize level 3.
 3. per-person permissions (deny wins; optional allow-list)   -> else ``denied``
 4. traceability: a level-3 call that the person's words do not ask for is denied outright
    (stops injected / hallucinated high-risk calls before they reach anyone's phone)
 5. house rules: block automations; a named person's explicit request overrides (logged)
 6. freshness: every reading the decision depends on must be fresh  -> else ``denied`` (stale)
 7. level 1 act and tell | level 2 ask first (silence is not a yes) |
    level 3 never alone: single-use, expiring approval via ``reply/<approver>`` (phone) only.
Freshness is checked again at the moment an approved action is executed.
"""
import itertools
import os
from dataclasses import dataclass, field

from islab.core.bus import Envelope
from .actions import ActionTracker
from .audit import AuditLog
from .state import FRESH
from .tools import ToolRegistry, plain

POLICY_PATH = os.path.join(os.path.dirname(__file__), "policy.yaml")


def load_policy(path=POLICY_PATH):
    """Load the person-editable policy (YAML, or JSON if the file ends in .json)."""
    with open(path) as f:
        text = f.read()
    if path.endswith(".json"):
        import json
        return json.loads(text)
    import yaml
    return yaml.safe_load(text)


@dataclass
class Proposal:
    tool: str
    args: object = field(default_factory=dict)
    requester: str = None        # speaker_id / person name / automation id
    confidence: float = None     # voice identity confidence (voice channel only)
    channel: str = "voice"       # "voice" | "phone" | "automation"
    explicit: bool = True        # traceable to the person's own words?
    depends_on: list = field(default_factory=list)   # device_id or (device_id, attr)
    text: str = ""
    origin: str = "?"            # which planner proposed it
    request_id: str = None


@dataclass
class Decision:
    request_id: str
    tool: str
    args: object
    status: str                  # rejected|denied|answered|executing|asked|awaiting_approval|sent
    reason: str
    level: int = None
    actor: str = None
    detail: object = None
    result: object = None        # get_state answer
    final: str = None            # confirmed|failed|timed_out|declined|no_answer|expired|sent_unconfirmed|...
    approval: dict = None

    @property
    def executed(self):
        return self.status in ("executing", "sent") or self.final in ("confirmed", "failed", "timed_out",
                                                                       "sent_unconfirmed")


@dataclass
class _Pending:
    kind: str                    # "ask" | "approval"
    decision: Decision
    proposal: Proposal
    asked: list
    deadline: float
    used: bool = False


_ROOM_WORDS = {"living": "living room"}


def _summary(tool, a):
    a = a or {}
    if tool == "set_light":
        # Integration fix (Nov 19): room "living" read as "The living light is on" to people.
        return f"The {_ROOM_WORDS.get(a.get('room'), a.get('room'))} light is {'on' if a.get('on') else 'off'}" + (
            f" at {a['brightness']}%" if "brightness" in a else "")
    if tool == "set_climate":
        return f"The {a.get('room')} is set to {a.get('setpoint_c')} C"
    if tool in ("lock_door", "unlock_door"):
        return f"The {a.get('door')} door is {'locked' if tool == 'lock_door' else 'unlocked'}"
    if tool == "close_valve":
        return "The main water valve is closed"
    if tool == "open_valve":
        return "The main water valve is open"
    if tool == "close_garage":
        return "The garage door is closed"
    if tool == "set_shade":
        return f"The living room shade is at {a.get('position')}%"
    if tool == "arm_away":
        return "The house is secured for away (doors locked, garage closed)"
    # Integration (islab/suites/house.py, Nov 19): the house registers two heating tools.
    if tool == "pause_heating":
        why = f" because the {a.get('room')} window is open" if a.get("because") == "window_open" else ""
        return f"The {a.get('room')} heating is paused{why}"
    if tool == "resume_heating":
        return f"The {a.get('room')} heating is back on"
    return f"{tool} done"


def _intent(tool, a):
    a = a or {}
    words = {"unlock_door": f"unlock the {a.get('door', '?')} door", "open_valve": "turn the water back on",
             "share_camera_clip": f"share a {a.get('camera', '?')} camera clip with {a.get('recipient', '?')}",
             "close_garage": "close the garage", "arm_away": "secure the house for away",
             "lock_door": f"lock the {a.get('door', '?')} door",
             # Integration fix (Nov 19): without these, denial texts for level-1 tools read
             # "I didn't the study is set to 22.0 c because ..." (summary text used as a verb phrase).
             "set_climate": f"set the {a.get('room', '?')} to {a.get('setpoint_c', '?')} C",
             "set_light": f"turn the {a.get('room', '?')} light {'on' if a.get('on') else 'off'}",
             "set_shade": f"move the living room shade to {a.get('position', '?')}%",
             "close_valve": "close the main water valve",
             "pause_heating": f"pause the {a.get('room', '?')} heating",
             "resume_heating": f"turn the {a.get('room', '?')} heating back on"}
    return words.get(tool, _summary(tool, a).lower())


class PolicyEngine:
    _ids = itertools.count(1)

    def __init__(self, sim, bus, store, registry=None, config=None, tracker=None, audit=None,
                 egress_handler=None, source="policy.engine"):
        self.sim, self.bus, self.store = sim, bus, store
        self.cfg = config if config is not None else load_policy()
        self.registry = registry or ToolRegistry(people=list(self.cfg.get("people", {})))
        self.audit = audit or AuditLog(sim)
        self.tracker = tracker or ActionTracker(sim, bus, timeout_s=self.cfg.get("action_timeout_s", 10),
                                                audit=self.audit)
        self.egress_handler = egress_handler   # callable(request dict) -> bool; the egress gate owns egress/*
        self.source = source
        self.decisions = {}
        self.pending = {}
        self.approvals = []                    # consumed approval records
        self.executions = []                   # everything the engine let through to devices/outside
        self.listeners = []                    # callables(decision) on final outcome
        bus.subscribe("reply/+", self._on_reply, name="policy.engine")

    # ---- config helpers --------------------------------------------------------------
    @property
    def people(self):
        return list(self.cfg.get("people", {}))

    def level(self, tool):
        return int(self.cfg.get("levels", {}).get(tool, 3))

    def permitted(self, actor, tool):
        p = (self.cfg.get("permissions") or {}).get(actor) or {}
        if tool in (p.get("deny") or []):
            return False
        if "allow" in p:
            return tool in (p.get("allow") or [])
        return True

    def approvers(self, tool):
        a = self.cfg.get("approvers") or {}
        names = a.get(tool, a.get("default", []))
        return [n for n in names if n in self.people and self.permitted(n, tool)]

    def resolve_identity(self, p):
        if p.channel == "automation":
            return "automation", "automation"
        if p.channel == "phone" and p.requester in self.people:
            return p.requester, "phone"
        if p.channel == "voice" and p.requester in self.people:
            th = float((self.cfg.get("voice") or {}).get("min_confidence", 0.8))
            if p.confidence is not None and p.confidence >= th:
                return p.requester, "voice"
        return "guest", "unverified"

    def _max_age(self, device_id):
        f = self.cfg.get("freshness") or {}
        return float((f.get("per_device") or {}).get(device_id, f.get("max_age_s", 120)))

    def check_freshness(self, deps):
        bad = []
        for d in deps:
            dev, attr = (d, None) if isinstance(d, str) else (d[0], d[1] if len(d) > 1 else None)
            st = self.store.freshness(dev, self._max_age(dev), attr)
            if st != FRESH:
                bad.append({"device_id": dev, "attr": attr, "freshness": st, "age_s": self.store.age(dev, attr)})
        return bad

    def _active_rules(self, tool, args):
        h = (self.sim.now % 86400) / 3600.0
        out = []
        for r in self.cfg.get("rules") or []:
            if tool not in (r.get("tools") or []):
                continue
            w = (r.get("when") or {}).get("between_h")
            if w:
                a, b = w
                if not ((a <= h < b) if a <= b else (h >= a or h < b)):
                    continue
            if any((args or {}).get(k) != v for k, v in (r.get("args") or {}).items()):
                continue
            gt = r.get("args_gt") or {}
            if gt and not all(isinstance((args or {}).get(k), (int, float)) and args[k] > v for k, v in gt.items()):
                continue
            out.append(r)
        return out

    def _notify(self, people, text, kind="tell", request_id=None):
        for person in people:
            payload = {"text": text, "kind": kind}
            if request_id:
                payload["request_id"] = request_id
            self.bus.publish(Envelope(f"notify/{person}", payload, source=self.source, ts=self.sim.now))

    def _tell_to(self, actor):
        return [actor] if actor in self.people else list(self.cfg.get("default_notify") or self.people[:1])

    # ---- main entry ------------------------------------------------------------------
    def propose(self, p: Proposal) -> Decision:
        p.request_id = p.request_id or f"rq{next(self._ids):06d}"
        rid = p.request_id
        self.audit.write("proposal", request_id=rid, tool=p.tool, args=p.args, requester=p.requester,
                         confidence=p.confidence, channel=p.channel, explicit=p.explicit, origin=p.origin,
                         depends_on=p.depends_on, text=p.text)

        tool, errs = self.registry.check(p.tool, p.args)
        if tool is None:
            return self._decide(Decision(rid, p.tool, p.args, "rejected", "unknown_tool", detail=errs))
        if errs:
            return self._decide(Decision(rid, p.tool, p.args, "rejected", "bad_args", detail=errs))

        actor, how = self.resolve_identity(p)
        lvl = self.level(p.tool)
        d = Decision(rid, p.tool, p.args, "pending", "", level=lvl, actor=actor)
        self.audit.write("identity", request_id=rid, actor=actor, via=how, claimed=p.requester,
                         confidence=p.confidence)

        if not self.permitted(actor, p.tool):
            d.status, d.reason = "denied", "not_permitted"
            d.detail = f"{actor} is not allowed to use {p.tool}"
            self._notify(self._tell_to(actor) if actor in self.people else [],
                         # Integration fix (Nov 19): person names are shown capitalized ("for Sam").
                         f"I can't {_intent(p.tool, p.args)} for {actor.capitalize()}: that isn't allowed in the "
                         f"house settings.")
            return self._decide(d)

        if lvl >= 3 and not p.explicit and p.channel != "automation":
            d.status, d.reason = "denied", "not_requested_by_person"
            d.detail = "high-risk call not traceable to the person's request (possible injection/hallucination)"
            return self._decide(d)

        for r in self._active_rules(p.tool, p.args):
            if p.explicit and actor in self.people:
                self.audit.write("override", request_id=rid, rule=r["id"], by=actor, rule_reason=r.get("reason"),
                                 text=p.text)
            else:
                d.status, d.reason, d.detail = "denied", f"rule:{r['id']}", r.get("reason")
                return self._decide(d)

        deps = list(p.depends_on) + self.registry.depends_on(p.tool, p.args)
        stale = self.check_freshness(deps)
        if stale:
            d.status, d.reason, d.detail = "denied", "stale_context", stale
            names = ", ".join(f"{plain(s['device_id'])} ({s['freshness']})" for s in stale)
            self._notify(self._tell_to(actor) if p.channel != "automation" else [],
                         f"I didn't {_intent(p.tool, p.args)} because my reading of {names} is not fresh.")
            return self._decide(d)

        if tool.kind == "read":
            return self._decide(self._answer(d, p))

        if lvl <= 1:
            d.status, d.reason = "executing", "level1_act_and_tell"
            self._decide(d)
            return self._execute(d, p)

        if lvl == 2:
            asked = [actor] if actor in self.people else self.approvers(p.tool) or self._tell_to(actor)
            d.status, d.reason = "asked", "level2_ask_first"
            t = float(self.cfg.get("ask_timeout_s", 120))
            self.pending[rid] = _Pending("ask", d, p, asked, self.sim.now + t)
            self._notify(asked, f"Should I {_intent(p.tool, p.args)}? (asked by {actor})", "ask", rid)
            self.sim.after(t, self._expire, rid)
            return self._decide(d)

        asked = self.approvers(p.tool)
        if not asked:
            d.status, d.reason = "denied", "no_approver_configured"
            return self._decide(d)
        d.status, d.reason = "awaiting_approval", "level3_needs_phone_approval"
        t = float(self.cfg.get("approval_ttl_s", 120))
        self.pending[rid] = _Pending("approval", d, p, asked, self.sim.now + t)
        flag = "" if p.explicit else " NOTE: you did not ask for this; the assistant suggested it."
        self._notify(asked, f"Approve: {_intent(p.tool, p.args)}? Requested by {actor} via {how}.{flag} "
                            f"Reply yes within {int(t)} s.", "approval", rid)
        self.sim.after(t, self._expire, rid)
        return self._decide(d)

    # ---- approvals / answers ---------------------------------------------------------
    def _on_reply(self, env):
        person = env.topic.split("/", 1)[1]
        pl = env.payload or {}
        self._handle_answer(pl.get("request_id"), person, pl.get("answer"), via="phone", event_id=env.event_id)

    def voice_answer(self, request_id, speaker_id, confidence, answer):
        """A spoken yes/no. Can settle a level-2 question for the identified person; never level 3."""
        pend = self.pending.get(request_id)
        if pend and pend.kind == "approval":
            self.audit.write("approval_rejected", request_id=request_id, by=speaker_id, confidence=confidence,
                             reason="voice_cannot_authorize_level3")
            return False
        th = float((self.cfg.get("voice") or {}).get("min_confidence", 0.8))
        if confidence is None or confidence < th:
            self.audit.write("answer_rejected", request_id=request_id, by=speaker_id, confidence=confidence,
                             reason="voice_not_identified")
            return False
        return self._handle_answer(request_id, speaker_id, answer, via="voice")

    def _handle_answer(self, rid, person, answer, via, event_id=None):
        pend = self.pending.get(rid)
        if pend is None:
            self.audit.write("approval_rejected", request_id=rid, by=person, via=via, event_id=event_id,
                             reason="unknown_used_or_expired")
            return False
        if person not in pend.asked:
            self.audit.write("approval_rejected", request_id=rid, by=person, via=via, event_id=event_id,
                             reason="not_an_approver")
            return False
        if self.sim.now > pend.deadline:
            self._expire(rid)
            return False
        d, p = pend.decision, pend.proposal
        del self.pending[rid]
        pend.used = True
        if answer != "yes":
            d.final = "declined"
            self.audit.write("answer", request_id=rid, by=person, via=via, answer=answer, event_id=event_id)
            self._finalize(d)
            return True
        rec = {"request_id": rid, "approval_kind": pend.kind, "by": person, "via": via, "event_id": event_id,
               "at": self.sim.now, "tool": d.tool}
        self.audit.write("approval" if pend.kind == "approval" else "answer", answer="yes", **rec)
        stale = self.check_freshness(list(p.depends_on) + self.registry.depends_on(p.tool, p.args))
        if stale:
            d.status, d.reason, d.detail, d.final = "denied", "stale_context_at_execution", stale, "not_run"
            self._notify(pend.asked, f"I didn't {_intent(d.tool, d.args)}: my readings went stale while waiting.")
            self.audit.write("decision", request_id=rid, status=d.status, reason=d.reason, detail=stale)
            self._finalize(d)
            return True
        if pend.kind == "approval":
            self.approvals.append(rec)
            d.approval = rec
        d.status = "executing"
        self._execute(d, p)
        return True

    def _expire(self, rid):
        pend = self.pending.pop(rid, None)
        if pend is None:
            return
        d = pend.decision
        d.final = "no_answer" if pend.kind == "ask" else "expired"
        self.audit.write("expired", request_id=rid, pending_kind=pend.kind, asked=pend.asked,
                         note="silence is not a yes; nothing was done")
        who = self._tell_to(d.actor) if d.actor in self.people else pend.asked
        self._notify(who, f"Nobody answered, so I did not {_intent(d.tool, d.args)}.")
        self._finalize(d)

    # ---- execution -------------------------------------------------------------------
    def _answer(self, d, p):
        dev = p.args["device_id"]
        readings = self.store.get(dev)
        if not readings:
            d.status, d.reason, d.final = "answered", "no_reading", "unknown"
            d.result = {"device_id": dev, "known": False}
        else:
            age_ok = self.store.freshness(dev, self._max_age(dev))
            d.status, d.reason, d.final = "answered", "read", age_ok
            d.result = {"device_id": dev, "freshness": age_ok,
                        "attrs": {k: {"value": r.value, "ts": r.ts, "age_s": round(self.sim.now - r.ts, 3),
                                      "event_id": r.event_id} for k, r in readings.items()}}
        return d

    def _execute(self, d, p):
        tool = self.registry.get(d.tool)
        rec = {"request_id": d.request_id, "tool": d.tool, "args": d.args, "level": d.level, "actor": d.actor,
               "approval": d.approval, "t": self.sim.now}
        self.executions.append(rec)
        self.audit.write("execute", **rec)
        if tool.kind == "device":
            cmds = self.registry.commands(d.tool, d.args)

            def done(act, d=d):
                d.final = act.outcome
                d.detail = {"outcome": act.outcome, "detail": act.detail, "message": act.message}
                self._finalize(d)
            self.tracker.submit(d.request_id, d.tool, cmds, self._tell_to(d.actor), _summary(d.tool, d.args),
                                on_outcome=done, tell_on_success=True)
        elif tool.kind == "message":
            a = d.args
            if d.tool == "notify_person":
                self._notify([a["person"]], a["text"], "tell", d.request_id)
            else:
                self._notify([a["person"]], a["question"], "ask", d.request_id)
            d.status, d.final = "sent", "sent_unconfirmed"   # a phone does not confirm reading it
            self._finalize(d)
        elif tool.kind == "egress":
            ok = False
            if self.egress_handler is not None:
                ok = bool(self.egress_handler({"purpose": "camera_clip", "request_id": d.request_id,
                                               "approval": d.approval, **d.args}))
            d.final = "handed_to_egress_gate" if ok else "failed"
            if not ok:
                self._notify(self._tell_to(d.actor), "I couldn't share the camera clip: the egress gate "
                                                    "did not accept it, so nothing left the house.")
            self._finalize(d)
        return d

    def _decide(self, d):
        self.decisions[d.request_id] = d
        self.audit.write("decision", request_id=d.request_id, tool=d.tool, status=d.status, reason=d.reason,
                         level=d.level, actor=d.actor, detail=d.detail)
        if d.status in ("rejected", "denied", "answered"):
            if d.final is None:
                d.final = "not_run"
            self._finalize(d)
        return d

    def _finalize(self, d):
        self.audit.write("final", request_id=d.request_id, tool=d.tool, status=d.status, final=d.final,
                         reason=d.reason)
        for fn in list(self.listeners):
            fn(d)
