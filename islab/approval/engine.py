"""GatedEngine: the B01 policy engine with an approval flow that is bound, signed and time-limited.

Subclasses ``islab.policy.engine.PolicyEngine`` (not edited). What changes:

  * Prompts. Every level-2 question and level-3 approval goes out as a ticket
    (``ledger.ApprovalLedger``): request id, the exact action, its digest, a nonce, an expiry.
    Prompt text is built by ``tools.describe`` from the tool and validated arguments only.
  * Replies. A reply counts only if the ledger verifies it (signature, person, digest, nonce,
    expiry, single use). B01 trusted anything published on ``reply/<person>``.
  * Execution. Right before running, the action must still hash to the approved digest (B01 ran
    whatever the arguments object held at that moment), and a time-bound action must still be
    inside its window. ``open_garage`` is closed by the home at its ``until`` time.
  * Arguments a person named must match the proposal (``arg_trace``). B01 traced the tool only.
  * A level-2 call the person's words do not ask for is denied before it reaches a phone
    (``deny_untraced_l2``). B01 sent it as a question that looked like the person's own request.
  * Waiting. Nothing moves while a request waits; the requester is told it is waiting; a second
    identical request while one is pending does not send a second prompt (``coalesce``).
  * Routing. ``fanout``: all approvers at once (B01). ``escalate``: the requester first if they
    are an approver, otherwise the first approver; the others after ``escalate_after_s``.
  * Standing rules (``standing.StandingRules``): a matching rule grants the request at once and
    tells its owner; it never bypasses permissions, traceability, house rules or freshness,
    because those run first in the B01 order.

Ablation switches (``verify_replies``, ``bind_check``, ``arg_trace``, ``coalesce``) exist so the
experiments can show what each one is for. All default to on.
"""
import collections

from islab.policy.engine import PolicyEngine, Decision
from islab.policy.tools import ToolRegistry
from islab.core.bus import Envelope

from .ledger import ApprovalLedger, canon
from .tools import add_b07_tools, b07_policy, describe, word_args, valid_hhmm, until_abs, s_to_hhmm

_REFUSAL_WORDS = {
    "action_changed_after_approval": "the action changed after it was approved",
    "already_executed": "that approval was already used",
    "not_approved": "there is no valid approval for it",
    "window_passed": "its time window had already ended",
    "bad_args_at_execution": "its details were no longer valid",
}


class GatedEngine(PolicyEngine):
    def __init__(self, sim, bus, store, keys, config=None, registry=None, standing=None, routing="fanout",
                 verify_replies=True, bind_check=True, arg_trace=True, coalesce=True, deny_untraced_l2=True, **kw):
        cfg = config if config is not None else b07_policy()
        reg = registry or add_b07_tools(ToolRegistry(people=list(cfg.get("people", {}))))
        self.ledger = ApprovalLedger(sim, keys)
        self.standing = standing
        self.routing = routing
        self.verify_replies, self.bind_check = verify_replies, bind_check
        self.arg_trace, self.coalesce = arg_trace, coalesce
        self.deny_untraced_l2 = deny_untraced_l2
        super().__init__(sim, bus, store, registry=reg, config=cfg, **kw)
        self.ledger.audit = self.audit
        b = self.cfg.get("b07") or {}
        self.default_window_s = float(b.get("default_window_s", 1200))
        self.max_window_s = float(b.get("max_window_s", 7200))
        self.escalate_after_s = float(b.get("escalate_after_s", 45))
        self.prompts = []              # (t, person, request_id, kind)
        self.denials = collections.Counter()
        self.coalesced = 0
        self.rule_grants = []          # (t, rule_id, request_id)
        self.window_closes = []        # (t_scheduled, request_id, outcome)
        self.waiting_told = 0
        self._prompted = {}            # request_id -> people prompted

    # ---- identity: a visitor at the doorbell is a role, not a person ------------------------
    def resolve_identity(self, p):
        if p.channel == "doorbell":
            return "visitor", "doorbell"
        return super().resolve_identity(p)

    # ---- propose: B07 checks in front of the B01 order ---------------------------------------
    def propose(self, p):
        p.request_id = p.request_id or f"rq{next(self._ids):06d}"
        tool, errs = self.registry.check(p.tool, p.args)
        if tool is None or errs:
            return super().propose(p)
        actor, how = self.resolve_identity(p)
        lvl = self.level(p.tool)
        if lvl >= 3 and p.tool == "open_garage":
            bad = self._window_problem(p)
            if bad:
                return self._deny_b07(p, actor, lvl, "bad_window", bad)
        if (self.deny_untraced_l2 and lvl == 2 and not p.explicit and p.channel != "automation"
                and self.permitted(actor, p.tool)):
            return self._deny_b07(p, actor, lvl, "not_requested_by_person", "level-2 call not in the person's words")
        if self.arg_trace and lvl >= 3 and p.channel != "automation" and p.explicit:
            named = word_args(p.tool, p.text)
            diff = {k: {"words": v, "proposed": (p.args or {}).get(k)} for k, v in named.items()
                    if (p.args or {}).get(k) != v}
            if diff:
                return self._deny_b07(p, actor, lvl, "args_not_requested", diff)
        if self.coalesce and lvl >= 2:
            key = (actor, p.tool, canon(p.args))
            for rid, pend in self.pending.items():
                dd = pend.decision
                if (dd.actor, dd.tool, canon(dd.args)) == key:
                    return self._coalesce(p, actor, lvl, rid)
        return super().propose(p)

    def _window_problem(self, p):
        u = (p.args or {}).get("until")
        if not valid_hhmm(u):
            return "until is not HH:MM"
        end = until_abs(u, self.sim.now)
        if end <= self.sim.now:
            return f"until {u} is not in the future"
        named = "until" in word_args(p.tool, p.text) if p.channel != "automation" else False
        limit = self.max_window_s if named else self.default_window_s
        if end - self.sim.now > limit:
            return f"until {u} is more than {int(limit // 60)} min ahead"
        return None

    def _deny_b07(self, p, actor, lvl, reason, detail):
        self.audit.write("proposal", request_id=p.request_id, tool=p.tool, args=p.args, requester=p.requester,
                         confidence=p.confidence, channel=p.channel, explicit=p.explicit, origin=p.origin,
                         depends_on=p.depends_on, text=p.text)
        self.denials[reason] += 1
        d = Decision(p.request_id, p.tool, p.args, "denied", reason, level=lvl, actor=actor, detail=detail)
        if actor in self.people:
            why = {"bad_window": "the time window is not allowed",
                   "args_not_requested": "the details differ from what you asked",
                   "not_requested_by_person": "you did not ask for it"}[reason]
            self._notify([actor], f"I didn't ask anyone to approve this, because {why}: "
                                  f"{describe(p.tool, p.args, actor, 'voice').split('?')[0].lower()}.")
        return self._decide(d)

    def _coalesce(self, p, actor, lvl, pending_rid):
        self.audit.write("proposal", request_id=p.request_id, tool=p.tool, args=p.args, requester=p.requester,
                         channel=p.channel, explicit=p.explicit, origin=p.origin, text=p.text)
        self.coalesced += 1
        d = Decision(p.request_id, p.tool, p.args, "coalesced", "same_request_pending", level=lvl, actor=actor,
                     detail={"pending": pending_rid}, final="coalesced")
        self.decisions[d.request_id] = d
        self.audit.write("decision", request_id=d.request_id, tool=d.tool, status=d.status, reason=d.reason,
                         level=lvl, actor=actor, detail=d.detail)
        self._tell_requester(actor, "Still waiting for approval. Nothing happens until someone approves.")
        self._finalize(d)
        return d

    def _tell_requester(self, actor, text):
        self.waiting_told += 1
        if actor in self.people:
            self.bus.publish(Envelope(f"notify/{actor}", {"text": text, "kind": "tell"}, source=self.source,
                                      ts=self.sim.now))
        else:   # a visitor hears it from the doorbell speaker (modelled as an audit record)
            self.audit.write("told_requester", actor=actor, text=text)

    # ---- prompts -----------------------------------------------------------------------------
    def _notify(self, people, text, kind="tell", request_id=None):
        pend = self.pending.get(request_id) if request_id else None
        if kind in ("ask", "approval") and pend is not None and pend.kind == kind and not getattr(pend, "b07", 0):
            pend.b07 = 1
            return self._prompt_flow(pend)
        return super()._notify(people, text, kind, request_id)

    def _prompt_flow(self, pend):
        d, p = pend.decision, pend.proposal
        rid, now = d.request_id, self.sim.now
        if self.standing is not None:
            r = self.standing.match(d.actor, d.tool, d.args, now, channel=p.channel)
            if r is not None and r.owner in self.people and self.permitted(r.owner, d.tool):
                self.standing.use(r, now, rid)
                self.rule_grants.append((now, r.rule_id, rid))
                self.ledger.grant_by_rule(rid, d.actor, d.tool, d.args, r.rule_id, r.owner)
                if r.owner not in pend.asked:
                    pend.asked.append(r.owner)
                what = describe(d.tool, d.args, d.actor, p.channel).split("?")[0]
                super()._notify([r.owner], f"Under your standing rule '{r.rule_id}': {what[0].lower()}{what[1:]}. "
                                           f"Change or stop the rule on your phone.", "tell", rid)
                self.sim.after(0, self._handle_answer, rid, r.owner, "yes", "standing_rule:" + r.rule_id)
                return
        t = self.ledger.issue(rid, pend.kind, d.actor, d.tool, d.args, pend.deadline)
        order = list(pend.asked)
        if self.routing == "escalate" and len(order) > 1:
            if d.actor in order:
                order.remove(d.actor)
                order.insert(0, d.actor)
            self._send_prompt(t, order[:1], p.channel)
            self.sim.after(self.escalate_after_s, self._escalate, rid, order[1:], p.channel)
        else:
            self._send_prompt(t, order, p.channel)
        self.audit.write("holding", request_id=rid, note="nothing moves until approved", until=t.expires_at)
        if d.actor not in self.people or d.actor not in order:
            names = " or ".join(n.capitalize() for n in order)
            self._tell_requester(d.actor, f"I've asked {names}. Nothing happens until one of them approves.")

    def _send_prompt(self, t, people, channel):
        left = max(0, int(t.expires_at - self.sim.now))
        text = describe(t.tool, t.args, t.actor, channel) + f" Reply within {left} s."
        done = self._prompted.setdefault(t.request_id, set())
        for person in people:
            if person in done:
                continue
            done.add(person)
            if person not in t.asked:
                t.asked.append(person)
            self.prompts.append((self.sim.now, person, t.request_id, t.kind))
            self.bus.publish(Envelope(f"notify/{person}", {
                "text": text, "kind": t.kind, "request_id": t.request_id,
                "action": {"actor": t.actor, "tool": t.tool, "args": t.args},
                "digest": t.digest, "nonce": t.nonce, "expires_at": t.expires_at}, source=self.source,
                ts=self.sim.now))

    def _escalate(self, rid, people, channel):
        t = self.ledger.tickets.get(rid)
        if rid not in self.pending or t is None or t.used:
            return
        self.audit.write("escalate", request_id=rid, to=people)
        self._send_prompt(t, people, channel)

    # ---- replies -----------------------------------------------------------------------------
    def _on_reply(self, env):
        if not self.verify_replies:
            return super()._on_reply(env)
        person = env.topic.split("/", 1)[1]
        pl = env.payload or {}
        ok, reason, t = self.ledger.verify(person, pl)
        if not ok:
            if reason == "expired":
                self._expire(pl.get("request_id"))
            return
        self._handle_answer(t.request_id, person, pl.get("answer"), via="phone", event_id=env.event_id)

    def voice_answer(self, request_id, speaker_id, confidence, answer):
        pend = self.pending.get(request_id)
        th = float((self.cfg.get("voice") or {}).get("min_confidence", 0.8))
        if (pend and pend.kind == "ask" and answer == "yes" and confidence is not None and confidence >= th
                and speaker_id in pend.asked):
            self.ledger.mark_voice(request_id, speaker_id)
        return super().voice_answer(request_id, speaker_id, confidence, answer)

    # ---- execution ---------------------------------------------------------------------------
    def _execute(self, d, p):
        tool = self.registry.get(d.tool)
        if self.bind_check and (d.level or 1) >= 2 and tool is not None and tool.kind != "read":
            reason = None
            if self.registry.check(d.tool, d.args)[1]:
                reason = "bad_args_at_execution"
            elif d.tool == "open_garage" and until_abs(d.args["until"], self.sim.now) <= self.sim.now:
                reason = "window_passed"
            if reason:
                self.ledger._refuse(reason, d.request_id, d.actor, "execute")
            else:
                ok, reason, _ = self.ledger.check_execution(d.request_id, d.actor, d.tool, d.args)
                reason = None if ok else reason
            if reason:
                d.status, d.reason, d.final = "denied", f"binding:{reason}", "refused"
                self.audit.write("decision", request_id=d.request_id, status=d.status, reason=d.reason)
                who = self._tell_to(d.actor) if d.actor in self.people else self.approvers(d.tool)[:1]
                what = describe(d.tool, d.args, d.actor, "-").split("?")[0]
                super()._notify(who, f"I did not {what[0].lower()}{what[1:]}: {_REFUSAL_WORDS.get(reason, reason)}.")
                self._finalize(d)
                return d
        out = super()._execute(d, p)
        if d.tool == "open_garage" and isinstance(d.args, dict) and valid_hhmm(d.args.get("until")):
            self.sim.at(until_abs(d.args["until"], self.sim.now), self._close_window, d)
        return out

    def _close_window(self, d):
        rid = d.request_id + "w"
        to = self._tell_to(d.actor) if d.actor in self.people else (self.approvers("open_garage")[:1] or [])
        rec = [self.sim.now, d.request_id, None]
        self.window_closes.append(rec)

        def done(act, rec=rec):
            rec[2] = act.outcome
        self.audit.write("window_close", request_id=d.request_id, until=d.args.get("until"))
        self.tracker.submit(rid, "close_garage", self.registry.commands("close_garage", {}), to,
                            f"The garage closed at {s_to_hhmm(self.sim.now)}, the end of the approved window",
                            on_outcome=done)
