"""Action tracker: a command sent is not a result (principle 2), and failures are said out loud
(principle 4).

Lifecycle of every device command:  sent -> acknowledged -> confirmed | failed | timed_out
  * ``ack accepted`` only means the device received the command. It is recorded, never reported
    as done.
  * CONFIRMED requires a later ``state/<device>`` whose source ts is after the send time and whose
    attributes show every expected value (attrs may arrive across several state messages).
  * ``ack rejected`` -> failed (rejected). Some commands of a multi-command action confirmed and
    others not -> failed (partial). Nothing confirmed by the deadline -> timed_out.
On failed / timed_out a plain-words ``notify/<person>`` goes out, e.g.
"The back door lock didn't respond, so it may still be open."
The tracker is the only component in islab/policy that publishes ``cmd/*``.
"""
from dataclasses import dataclass, field
from islab.core.bus import Envelope
from .tools import plain

CONFIRMED, FAILED, TIMED_OUT = "confirmed", "failed", "timed_out"


@dataclass
class CmdTrack:
    cmd_id: str
    device_id: str
    action: str
    args: dict
    expect: dict
    risk: str
    sent_at: float
    ack: str = None              # None | "accepted" | "rejected"
    ack_reason: str = None
    seen: dict = field(default_factory=dict)   # attr -> (value, ts, event_id) observed after send
    status: str = "sent"         # sent | acknowledged | confirmed | rejected | timed_out
    confirmed_by: str = None     # event_id of the confirming state
    ack_seq: int = None          # device sequence number of the accepted ack
    base_ts: float = None        # latest device ts seen before the command

    def matches(self):
        return all(a in self.seen and self.seen[a][0] == v for a, v in self.expect.items())


@dataclass
class Action:
    request_id: str
    tool: str
    summary: str
    notify_to: list
    commands: list
    deadline: float
    outcome: str = None          # None while pending | confirmed | failed | timed_out
    detail: str = None
    message: str = None          # plain-words text sent to people (if any)
    on_outcome: object = None

    @property
    def status(self):
        if self.outcome:
            return self.outcome
        if any(c.ack == "accepted" for c in self.commands):
            return "acknowledged"
        return "sent"


class ActionTracker:
    def __init__(self, sim, bus, timeout_s=10.0, audit=None, source="policy.actions", ack_retry_s=3.0,
                 max_resends=2):
        self.sim, self.bus = sim, bus
        self.timeout_s = timeout_s
        # Resend (Nov 19): no ack within ack_retry_s -> publish the SAME cmd_id again, up to max_resends
        # times. Devices treat a repeated cmd_id as idempotent (islab/twin/devices.py). An ack still only
        # means "received"; only a later state confirms. ack_retry_s=None disables resends.
        self.ack_retry_s, self.max_resends = ack_retry_s, max_resends
        self.audit = audit
        self.source = source
        self.actions = {}            # request_id -> Action
        self._by_cmd = {}            # cmd_id -> (Action, CmdTrack)
        self._open = {}              # device_id -> list[(Action, CmdTrack)]
        self._last_ts = {}           # device_id -> latest device ts seen
        bus.subscribe("ack/#", self._on_ack, name="policy.actions")
        bus.subscribe("state/#", self._on_state, name="policy.actions")

    # ---- submit ----------------------------------------------------------------------
    def submit(self, request_id, tool, commands, notify_to, summary="", on_outcome=None,
               tell_on_success=True, timeout_s=None):
        t = self.timeout_s if timeout_s is None else timeout_s
        act = Action(request_id, tool, summary, list(notify_to), [], self.sim.now + t, on_outcome=on_outcome)
        act.tell_on_success = tell_on_success
        self.actions[request_id] = act
        for i, c in enumerate(commands):
            cmd_id = f"{request_id}.c{i}"
            tr = CmdTrack(cmd_id, c.device_id, c.action, dict(c.args), dict(c.expect),
                          c.risk_if_unconfirmed, self.sim.now)
            tr.base_ts = self._last_ts.get(c.device_id)
            act.commands.append(tr)
            self._by_cmd[cmd_id] = (act, tr)
            self._open.setdefault(c.device_id, []).append((act, tr))
            self._send(tr)
            self._log("cmd_sent", act, cmd_id=cmd_id, device_id=c.device_id, action=c.action, args=c.args)
            if self.ack_retry_s and self.max_resends:
                self.sim.after(self.ack_retry_s, self._maybe_resend, act, tr, 1)
        self.sim.after(t, self._deadline, request_id)
        if not commands:
            self._finish(act, CONFIRMED, "nothing to do")
        return act

    def _send(self, tr):
        self.bus.publish(Envelope(f"cmd/{tr.device_id}",
                                  {"action": tr.action, "args": dict(tr.args), "cmd_id": tr.cmd_id},
                                  source=self.source, ts=self.sim.now))

    def _maybe_resend(self, act, tr, n):
        if act.outcome or tr.ack is not None or tr.status != "sent" or self.sim.now >= act.deadline:
            return
        self._send(tr)
        self._log("cmd_resent", act, cmd_id=tr.cmd_id, device_id=tr.device_id, attempt=n + 1)
        if n < self.max_resends:
            self.sim.after(self.ack_retry_s, self._maybe_resend, act, tr, n + 1)

    # ---- bus handlers ----------------------------------------------------------------
    def _on_ack(self, env):
        hit = self._by_cmd.get((env.payload or {}).get("cmd_id"))
        if not hit:
            return
        act, tr = hit
        if act.outcome or tr.status in ("confirmed", "rejected"):
            return
        status = env.payload.get("status")
        if status == "accepted":
            tr.ack = "accepted"
            if tr.ack_seq is None:          # keep the first ack (a resend's re-ack comes later)
                tr.ack_seq = env.seq or None
            if tr.status == "sent":
                tr.status = "acknowledged"   # received, NOT done
            self._log("cmd_ack", act, cmd_id=tr.cmd_id, status="accepted", note="received, not confirmed")
        elif status == "rejected":
            tr.ack, tr.ack_reason, tr.status = "rejected", env.payload.get("reason"), "rejected"
            self._log("cmd_ack", act, cmd_id=tr.cmd_id, status="rejected", reason=tr.ack_reason)
            self._maybe_finish(act)

    def _on_state(self, env):
        dev = env.topic.split("/", 1)[1]
        self._last_ts[dev] = max(env.ts, self._last_ts.get(dev, env.ts))
        for act, tr in list(self._open.get(dev, [])):
            if act.outcome or tr.status in ("confirmed", "rejected"):
                continue
            # Ordering against the command never uses the hub clock against the device clock
            # (a device whose clock lags would never confirm; found in the twin, Nov 19).
            # 1) it must arrive at the hub after the command was sent;
            # 2) if the device acked, its state must carry a later device sequence number than the ack;
            # 3) otherwise its device ts must be newer than anything that device sent before the command.
            if self.sim.now <= tr.sent_at:
                continue
            if tr.ack_seq:                     # device numbers its messages (seq > 0)
                if env.seq <= tr.ack_seq:
                    continue
            elif tr.base_ts is not None and env.ts <= tr.base_ts:
                continue
            for k, v in (env.payload or {}).items():
                if k in tr.expect:
                    prev = tr.seen.get(k)
                    if prev is None or env.ts >= prev[1]:
                        tr.seen[k] = (v, env.ts, env.event_id)
            if tr.matches():
                tr.status, tr.confirmed_by = "confirmed", env.event_id
                self._log("cmd_confirmed", act, cmd_id=tr.cmd_id, device_id=dev, event_id=env.event_id,
                          state_ts=env.ts)
                self._maybe_finish(act)

    # ---- resolution ------------------------------------------------------------------
    def _maybe_finish(self, act):
        if act.outcome:
            return
        if all(c.status in ("confirmed", "rejected") for c in act.commands):
            self._resolve(act)

    def _deadline(self, request_id):
        act = self.actions.get(request_id)
        if act and not act.outcome:
            for c in act.commands:
                if c.status in ("sent", "acknowledged"):
                    c.status = "timed_out"
            self._resolve(act)

    def _resolve(self, act):
        ok = [c for c in act.commands if c.status == "confirmed"]
        rej = [c for c in act.commands if c.status == "rejected"]
        to = [c for c in act.commands if c.status == "timed_out"]
        if len(ok) == len(act.commands):
            outcome, detail = CONFIRMED, "all commands confirmed by later state"
        elif rej and not ok and not to:
            outcome, detail = FAILED, "rejected"
        elif ok:
            outcome, detail = FAILED, "partial"
        elif rej:
            outcome, detail = FAILED, "rejected"
        else:
            acked = any(c.ack == "accepted" for c in to)
            outcome = TIMED_OUT
            detail = "acknowledged but never confirmed" if acked else "no response"
        self._finish(act, outcome, detail)

    def _finish(self, act, outcome, detail):
        act.outcome, act.detail = outcome, detail
        for c in act.commands:
            lst = self._open.get(c.device_id, [])
            self._open[c.device_id] = [p for p in lst if p[1] is not c]
        if outcome == CONFIRMED:
            if getattr(act, "tell_on_success", True) and act.commands:
                srcs = ", ".join(f"{c.device_id} ({c.confirmed_by})" for c in act.commands)
                act.message = f"{act.summary or 'Done'}. Confirmed by {srcs}."
                self._notify(act, act.message)
        else:
            act.message = self._failure_text(act)
            self._notify(act, act.message)
        self._log("outcome", act, outcome=outcome, detail=detail, message=act.message,
                  commands=[{"cmd_id": c.cmd_id, "device_id": c.device_id, "status": c.status,
                             "ack": c.ack, "confirmed_by": c.confirmed_by} for c in act.commands])
        if act.on_outcome:
            act.on_outcome(act)

    @staticmethod
    def _line(c):
        name = plain(c.device_id)
        cap = name[0].upper() + name[1:]
        risk = f", {c.risk}" if c.risk else ""
        if c.status == "rejected":
            why = f" ({c.ack_reason})" if c.ack_reason else ""
            return f"The {name} refused the command{why}{risk}."
        if c.ack == "accepted":
            return f"The {name} got the command but never showed it happened{risk}."
        if c.seen:
            return f"The {name} reported a different state than asked{risk}."
        return f"The {name} didn't respond{risk}."

    def _failure_text(self, act):
        bad = [c for c in act.commands if c.status != "confirmed"]
        good = [c for c in act.commands if c.status == "confirmed"]
        lines = " ".join(self._line(c) for c in bad)
        if good:
            done = ", ".join(plain(c.device_id) for c in good)
            return f"Only part of that worked. Confirmed: {done}. {lines}"
        return lines

    def _notify(self, act, text):
        for person in act.notify_to:
            self.bus.publish(Envelope(f"notify/{person}", {"text": text, "kind": "tell",
                                                          "request_id": act.request_id},
                                      source=self.source, ts=self.sim.now))

    def _log(self, kind, act, **f):
        self.sim.trace(kind, request_id=act.request_id, **f)
        if self.audit is not None:
            self.audit.write(kind, request_id=act.request_id, tool=act.tool, **f)
