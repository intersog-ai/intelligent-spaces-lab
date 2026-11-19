r"""Local leak -> valve safety controller, and the two designs it is compared with.

No language model and no internet anywhere in this file. Everything runs on the hub's LAN.

State machine (``LeakValveController``)::

    idle -> leak_detected -> closing -> closed_confirmed
                                    \-> close_failed -> escalated (-> closed_confirmed if it closes later)

* Detection: the leak sensor's ``event/leak.sink`` OR a ``state/leak.sink`` showing ``leak: true``
  (a lost event is recovered by the state copy).
* Closing: each attempt is one ``ActionTracker`` action (islab.policy) with a deterministic
  ``cmd_id`` = ``<episode>.a<k>.c0``. Within an attempt, a command with no ack is resent with the SAME
  cmd_id (the valve treats a repeated cmd_id as idempotent). A new attempt gets a new cmd_id: closing
  an already closed valve is harmless and makes the valve report its state again.
* Confirmed closed ONLY by fresh evidence: a ``state/valve.main`` received after the command showing
  position 0 / closed (the tracker's rule) AND a ``state/flow.main`` received after the command
  showing flow <= ``flow_zero_lpm``, received after the leak was detected and at most
  ``flow_fresh_s`` old (the meter reports on change and every 10 s, so a recent zero means zero now).
  An ack is never enough.
* Not confirmed ``escalate_after_s`` after the first command: close_failed, then escalated: people are
  told in plain words to shut the water off by hand, and a second leak alarm (zone ``VALVE_ZONE``)
  goes to the monitoring centre through the B01 alarm path. Attempts continue slowly afterwards.
* Journal: every step is a JSON-lines record. The record announcing a command is synced BEFORE the
  command is sent (write-ahead). After a hub reboot a new controller rebuilds its state from the
  durable records and resumes.

Baselines: ``FireAndForget`` reports the valve closed when it sends the command. ``AckBased`` reports
it closed on the valve's "accepted" ack, resends on no ack and tells people if no ack ever comes.
"""
from __future__ import annotations

from islab.core.bus import Envelope
from islab.policy.actions import ActionTracker, CONFIRMED
from islab.policy.tools import Command

IDLE, LEAK, CLOSING = "idle", "leak_detected", "closing"
CLOSED, FAILED, ESCALATED = "closed_confirmed", "close_failed", "escalated"
STATES = (IDLE, LEAK, CLOSING, CLOSED, FAILED, ESCALATED)
TRANSITIONS = {IDLE: {LEAK}, LEAK: {CLOSING}, CLOSING: {CLOSED, FAILED},
               FAILED: {ESCALATED}, ESCALATED: {CLOSED}, CLOSED: set()}

LEAK_ID, VALVE_ID, FLOW_ID = "leak.sink", "valve.main", "flow.main"
LEAK_ZONE, VALVE_ZONE = 2, 12        # zone 12: "main valve did not close, water still flowing"

TEXT_DETECTED = "Water detected under the kitchen sink. Closing the main water valve now."
TEXT_CLOSED = "The main water valve is closed and the water flow has stopped."
TEXT_CLAIM_CLOSED = "Water detected under the kitchen sink. The main water valve is closed."
MANUAL = "Turn off the water by hand at the main shutoff valve now."


class GuardedBus:
    """Bus view for one hub process: after the process dies it neither receives nor sends."""

    def __init__(self, bus, owner):
        self.bus, self.owner, self.sim = bus, owner, bus.sim

    def subscribe(self, pattern, cb, name="?"):
        def guarded(env):
            if self.owner.alive:
                cb(env)
        self.bus.subscribe(pattern, guarded, name=name)

    def publish(self, env):
        if self.owner.alive:
            self.bus.publish(env)


class _JournalAudit:
    """ActionTracker audit sink -> journal records (cmd_sent, cmd_resent, cmd_ack, cmd_confirmed,
    attempt_outcome)."""

    def __init__(self, ctl):
        self.ctl = ctl

    def write(self, kind, **f):
        if not self.ctl.alive:
            return
        if kind == "outcome":
            kind = "attempt_outcome"
            f.pop("commands", None)
        f.pop("tool", None)
        self.ctl.journal.append(kind, **f)


class LeakValveController:
    NAME = "safety.leak_valve"
    design = "state_machine"

    def __init__(self, sim, bus, journal, alarm_hub, notifier, attempt_timeout_s=10.0,
                 escalate_after_s=30.0, flow_zero_lpm=0.2, ack_retry_s=2.0, max_resends=3,
                 retry_after_escalation_s=30.0, max_attempts=8, fsync_s=0.002, poll_s=1.0,
                 flow_fresh_s=15.0, flow_wait_s=15.0):
        self.sim, self.journal, self.alarm_hub, self.notifier = sim, journal, alarm_hub, notifier
        self.attempt_timeout_s, self.escalate_after_s = attempt_timeout_s, escalate_after_s
        self.flow_zero_lpm, self.max_attempts, self.flow_fresh_s = flow_zero_lpm, max_attempts, flow_fresh_s
        self.flow_wait_s = flow_wait_s
        self.t_detected = None
        self.retry_after_escalation_s, self.fsync_s, self.poll_s = retry_after_escalation_s, fsync_s, poll_s
        self.alive = True
        self.bus = GuardedBus(bus, self)
        self.state, self.episode, self.attempts = IDLE, None, 0
        self.t_first_cmd, self.escalate_at = None, None
        self.last_flow = None            # (flow_lpm, hub_rx_t, event_id)
        self.last_valve = None           # (payload, hub_rx_t, event_id)
        self.valve_seen = {}             # event_id -> payload
        self._awaiting = None            # (attempt, cmd_sent_at, valve_event_id, deadline)
        self._told = set()
        self._alarm_raised, self._alarm_acked = {}, set()
        self._polling = False
        # valve readings are recorded BEFORE the tracker sees them, so a confirmation can cite them
        self.bus.subscribe(f"state/{VALVE_ID}", self._on_valve, name=self.NAME)
        self.tracker = ActionTracker(sim, self.bus, timeout_s=attempt_timeout_s, audit=_JournalAudit(self),
                                     source=self.NAME, ack_retry_s=ack_retry_s, max_resends=max_resends)
        self.bus.subscribe(f"event/{LEAK_ID}", self._on_leak_event, name=self.NAME)
        self.bus.subscribe(f"state/{LEAK_ID}", self._on_leak_state, name=self.NAME)
        self.bus.subscribe(f"state/{FLOW_ID}", self._on_flow, name=self.NAME)
        boot = journal.new_boot()
        journal.append("boot", boot_id=boot, controller=self.design)
        journal.sync()
        self._recover()

    # ---- lifecycle --------------------------------------------------------------------
    def crash(self):
        self.alive = False

    def _transition(self, to, **why):
        if to not in TRANSITIONS[self.state]:
            raise RuntimeError(f"illegal transition {self.state} -> {to}")
        self.journal.append("transition", frm=self.state, to=to, **why)
        self.state = to

    # ---- inputs -----------------------------------------------------------------------
    def _on_leak_event(self, env):
        if (env.payload or {}).get("type") == "leak":
            self._detect("event", env)

    def _on_leak_state(self, env):
        if (env.payload or {}).get("leak") is True:
            self._detect("state", env)

    def _on_valve(self, env):
        p = dict(env.payload or {})
        self.last_valve = (p, self.sim.now, env.event_id)
        self.valve_seen[env.event_id] = p

    def _on_flow(self, env):
        v = (env.payload or {}).get("flow_lpm")
        if not isinstance(v, (int, float)):
            return
        self.last_flow = (float(v), self.sim.now, env.event_id)
        if self._awaiting:
            self._try_confirm()

    # ---- detection and closing ----------------------------------------------------------
    def _detect(self, via, env):
        if self.state != IDLE:
            return
        n = len(self.journal.of("leak_detected")) + 1
        self.episode = f"leak-{n}"
        self.t_detected = self.sim.now
        self.journal.append("leak_detected", device=LEAK_ID, via=via, event_id=env.event_id,
                            device_ts=round(env.ts, 3), episode=self.episode)
        self._transition(LEAK)
        self.journal.sync()
        self._start_attempt()
        self._tell(TEXT_DETECTED, "detected")
        self._start_polling()

    def _start_attempt(self):
        if not self.alive or self.state in (CLOSED,) or self.attempts >= self.max_attempts:
            return
        if self._awaiting or any(not a.outcome for a in self.tracker.actions.values()):
            return                      # an attempt is already in flight
        k = self.attempts + 1
        cmd_id = f"{self.episode}.a{k}.c0"
        self.journal.append("cmd_intent", attempt=k, cmd_id=cmd_id, device=VALVE_ID, action="close")
        self.attempts = k
        # write-ahead: the intent is durable before the command leaves (fsync takes fsync_s)
        self.sim.after(self.fsync_s, self._submit, k)

    def _submit(self, k):
        if not self.alive:
            return
        self.journal.sync()
        if self.state == LEAK:
            self._transition(CLOSING)
        if self.t_first_cmd is None:
            self.t_first_cmd = self.sim.now
        if self.escalate_at is None:
            self.escalate_at = self.t_first_cmd + self.escalate_after_s
            self.sim.at(self.escalate_at, self._escalation_due)
        cmd = Command(VALVE_ID, "close", {}, {"state": "closed", "position": 0},
                      "water may still be flowing")
        self.tracker.submit(f"{self.episode}.a{k}", "safety.close_valve", [cmd], notify_to=[],
                            tell_on_success=False, on_outcome=self._on_attempt_outcome)
        self.journal.sync()

    def _on_attempt_outcome(self, act):
        if not self.alive:
            return
        c = act.commands[0]
        if act.outcome == CONFIRMED:
            self._awaiting = (int(act.request_id.rsplit(".a", 1)[1]), c.sent_at, c.confirmed_by,
                              self.sim.now + self.flow_wait_s)
            self.sim.after(self.flow_wait_s, self._flow_deadline, self._awaiting)
            self._try_confirm()
        else:
            self.journal.sync()
            self._after_failed_attempt()

    def _try_confirm(self):
        k, sent_at, valve_eid, _ = self._awaiting
        f = self.last_flow
        if (f is None or f[0] > self.flow_zero_lpm or self.t_detected is None or f[1] <= self.t_detected
                or self.sim.now - f[1] > self.flow_fresh_s):
            return                       # wait for a fresh flow reading near zero
        p = self.valve_seen.get(valve_eid, {})
        self._awaiting = None
        self.journal.append("closed_confirmed", attempt=k, cmd_id=f"{self.episode}.a{k}.c0",
                            valve_event_id=valve_eid, position=p.get("position"),
                            valve_state=p.get("state"), flow_lpm=f[0], flow_event_id=f[2],
                            flow_age_s=round(self.sim.now - f[1], 3),
                            after=self.state)
        was_escalated = self.state == ESCALATED
        self._transition(CLOSED)
        self.journal.sync()
        self._tell(("Update: " if was_escalated else "") + TEXT_CLOSED, "closed")

    def _flow_deadline(self, awaiting):
        if not self.alive or self._awaiting is not awaiting:
            return
        self._awaiting = None
        self.journal.append("attempt_outcome", request_id=f"{self.episode}.a{awaiting[0]}",
                            outcome="failed", detail="valve reported closed, flow did not drop")
        self.journal.sync()
        self._after_failed_attempt()

    def _after_failed_attempt(self):
        if self.state == CLOSED:
            return
        if self.state in (LEAK, CLOSING) and self.escalate_at is not None and self.sim.now >= self.escalate_at:
            self._escalate()
        delay = self.retry_after_escalation_s if self.state == ESCALATED else 0.0
        self.sim.after(delay, self._start_attempt)

    def _escalation_due(self):
        if self.alive and self.state in (LEAK, CLOSING):
            self._escalate()

    # ---- escalation ----------------------------------------------------------------------
    def _escalate(self):
        pos = self.last_valve[0].get("position") if self.last_valve else None
        vst = self.last_valve[0].get("state") if self.last_valve else None
        flow = self.last_flow[0] if self.last_flow else None
        self.journal.append("close_failed", attempts=self.attempts, waited_s=self.escalate_after_s,
                            last_position=pos, last_valve_state=vst, last_flow_lpm=flow)
        self._transition(FAILED)
        self.journal.sync()
        self._escalate_actions()

    def _escalate_actions(self):
        if VALVE_ZONE not in self._alarm_raised:
            rec = self.alarm_hub.raise_alarm("leak", VALVE_ZONE, source=VALVE_ID)
            self._alarm_raised[VALVE_ZONE] = rec["seq"]
            self.journal.append("alarm_raised", zone=VALVE_ZONE, seq=rec["seq"], why="valve_not_confirmed")
        last = self.journal.of("close_failed")[-1]
        detail = []
        if last.get("last_position") not in (None, 0):
            detail.append(f"it last reported {last['last_position']:g}% open")
        if last.get("last_flow_lpm") is not None and last["last_flow_lpm"] > self.flow_zero_lpm:
            detail.append(f"water is flowing at {last['last_flow_lpm']:.1f} litres a minute")
        why = f" ({', '.join(detail)})" if detail else ""
        alarm = ("The monitoring centre has the leak alarm." if LEAK_ZONE in self._alarm_acked
                 else "The leak alarm is being sent to the monitoring centre.")
        text = (f"The main water valve did not confirm it closed{why}. Water may still be flowing. "
                f"{MANUAL} {alarm}")
        self._transition(ESCALATED)
        self._tell(text, "escalated")
        self.journal.sync()

    # ---- telling and the alarm path ---------------------------------------------------------
    def _tell(self, text, tag):
        if self.notifier.tell(text, tag):
            self._told.add(tag)
            self.journal.append("told", tag=tag, text=text, people=sorted(self.notifier.people_home))

    def _start_polling(self):
        if not self._polling:
            self._polling = True
            self.sim.after(0.0, self._poll_alarms)

    def _poll_alarms(self):
        if not self.alive:
            return
        for rec in self.alarm_hub.alarms:
            z = rec.get("zone")
            if z not in (LEAK_ZONE, VALVE_ZONE) or rec.get("restore"):
                continue
            if z not in self._alarm_raised:
                self._alarm_raised[z] = rec["seq"]
                self.journal.append("alarm_raised", zone=z, seq=rec["seq"], why="leak_detected"
                                    if z == LEAK_ZONE else "valve_not_confirmed")
            if rec.get("t_ack") is not None and z not in self._alarm_acked:
                self._alarm_acked.add(z)
                self.journal.append("alarm_acked", zone=z, seq=rec["seq"], t_ack=round(rec["t_ack"], 4))
                self.journal.sync()
        need = {LEAK_ZONE} | ({VALVE_ZONE} if self.state in (FAILED, ESCALATED) else set())
        if need <= self._alarm_acked and self.state in (CLOSED, ESCALATED):
            self._polling = False
            return
        self.sim.after(self.poll_s, self._poll_alarms)

    # ---- recovery after a reboot -------------------------------------------------------------
    def _recover(self):
        recs = self.journal.durable
        state, first_cmd = IDLE, None
        for r in recs:
            k = r["kind"]
            if k == "transition":
                state = r["to"]
            elif k == "leak_detected":
                self.episode = r["episode"]
                self.t_detected = r["t"]
            elif k == "cmd_intent":
                self.attempts = max(self.attempts, r["attempt"])
            elif k == "cmd_sent" and first_cmd is None:
                first_cmd = r["t"]
            elif k == "told":
                self._told.add(r["tag"])
            elif k == "alarm_raised":
                self._alarm_raised[r["zone"]] = r["seq"]
            elif k == "alarm_acked":
                self._alarm_acked.add(r["zone"])
        if state == IDLE:
            return
        self.state = state
        self.journal.append("resumed", from_state=state, attempts=self.attempts)
        self.journal.sync()
        # alarms raised by the dead process may have died with it: raise again if never acknowledged
        self._alarm_raised = {z: s for z, s in self._alarm_raised.items() if z in self._alarm_acked}
        # The fresh B01 alarm hub does not know this leak episode exists and would raise it again on the
        # next leak sensor report (a duplicate at the centre). Tell it the episode is open.
        self.alarm_hub._episodes[LEAK_ID] = self.sim.now
        if LEAK_ZONE not in self._alarm_acked:
            rec = self.alarm_hub.raise_alarm("leak", LEAK_ZONE, source=LEAK_ID)
            self._alarm_raised[LEAK_ZONE] = rec["seq"]
            self.journal.append("alarm_raised", zone=LEAK_ZONE, seq=rec["seq"], why="resent_after_restart")
        self._start_polling()
        if state in (LEAK, CLOSING):
            if "detected" not in self._told:
                self._tell(TEXT_DETECTED, "detected")
            self.t_first_cmd = first_cmd
            base = first_cmd if first_cmd is not None else self.sim.now
            self.escalate_at = max(base + self.escalate_after_s, self.sim.now + self.attempt_timeout_s)
            self.sim.at(self.escalate_at, self._escalation_due)
            self._start_attempt()
        elif state == FAILED:
            self._escalate_actions()
            self.sim.after(self.retry_after_escalation_s, self._start_attempt)
        elif state == ESCALATED:
            if VALVE_ZONE not in self._alarm_acked:
                rec = self.alarm_hub.raise_alarm("leak", VALVE_ZONE, source=VALVE_ID)
                self._alarm_raised[VALVE_ZONE] = rec["seq"]
                self.journal.append("alarm_raised", zone=VALVE_ZONE, seq=rec["seq"],
                                    why="resent_after_restart")
            if "escalated" not in self._told:
                self._tell(f"The main water valve did not confirm it closed. Water may still be flowing. "
                           f"{MANUAL}", "escalated")
            self.sim.after(0.0, self._start_attempt)
        elif state == CLOSED and "closed" not in self._told:
            self._tell(TEXT_CLOSED, "closed")
        self.journal.sync()


# ==============================================================================================
# Baselines
# ==============================================================================================
class _Baseline:
    NAME = "baseline"
    design = "?"

    def __init__(self, sim, bus, journal, alarm_hub, notifier, **_):
        self.sim, self.journal, self.alarm_hub, self.notifier = sim, journal, alarm_hub, notifier
        self.alive = True
        self.bus = GuardedBus(bus, self)
        self.done = False
        self.bus.subscribe(f"event/{LEAK_ID}", self._on_leak_event, name=self.NAME)
        boot = journal.new_boot()
        journal.append("boot", boot_id=boot, controller=self.design)

    def crash(self):
        self.alive = False

    def _on_leak_event(self, env):
        if self.done or (env.payload or {}).get("type") != "leak":
            return
        self.done = True
        self.journal.append("leak_detected", device=LEAK_ID, via="event", event_id=env.event_id,
                            device_ts=round(env.ts, 3), episode="leak-1")
        self.cmd_id = f"{self.design}.close.1"
        self._send()
        self.journal.append("cmd_sent", cmd_id=self.cmd_id, device_id=VALVE_ID, action="close")
        self.on_sent()

    def _send(self):
        self.bus.publish(Envelope(f"cmd/{VALVE_ID}", {"action": "close", "args": {}, "cmd_id": self.cmd_id},
                                  source=self.NAME, ts=self.sim.now))

    def _tell(self, text, tag):
        if self.notifier.tell(text, tag):
            self.journal.append("told", tag=tag, text=text, people=sorted(self.notifier.people_home))

    def on_sent(self):
        pass


class FireAndForget(_Baseline):
    """Reports the valve closed the moment the command is sent."""
    NAME, design = "baseline.fire_and_forget", "fire_and_forget"

    def on_sent(self):
        self.journal.append("claimed_closed", basis="sent", cmd_id=self.cmd_id)
        self._tell(TEXT_CLAIM_CLOSED, "claimed_closed")


class AckBased(_Baseline):
    """Reports the valve closed on its 'accepted' ack. Resends the same cmd_id on no ack; if no ack ever
    comes, or the valve rejects, tells people to shut the water off by hand."""
    NAME, design = "baseline.ack_based", "ack_based"

    def __init__(self, *a, ack_retry_s=3.0, max_resends=2, **k):
        super().__init__(*a, **k)
        self.ack_retry_s, self.max_resends = ack_retry_s, max_resends
        self.acked = False
        self.bus.subscribe(f"ack/{VALVE_ID}", self._on_ack, name=self.NAME)

    def on_sent(self):
        self.sim.after(self.ack_retry_s, self._check, 1)

    def _check(self, n):
        if not self.alive or self.acked:
            return
        if n <= self.max_resends:
            self._send()
            self.journal.append("cmd_resent", cmd_id=self.cmd_id, attempt=n + 1)
            self.sim.after(self.ack_retry_s, self._check, n + 1)
        else:
            self._fail("no_ack")

    def _on_ack(self, env):
        p = env.payload or {}
        if self.acked or p.get("cmd_id") != getattr(self, "cmd_id", None):
            return
        self.acked = True
        self.journal.append("cmd_ack", cmd_id=self.cmd_id, status=p.get("status"))
        if p.get("status") == "accepted":
            self.journal.append("claimed_closed", basis="ack", cmd_id=self.cmd_id)
            self._tell(TEXT_CLAIM_CLOSED, "claimed_closed")
        else:
            self._fail("rejected")

    def _fail(self, why):
        self.journal.append("close_failed", reason=why, attempts=self.max_resends + 1)
        self._tell(f"Water detected under the kitchen sink. The main water valve did not respond. {MANUAL}",
                   "escalated")


DESIGNS = {"fire_and_forget": FireAndForget, "ack_based": AckBased, "state_machine": LeakValveController}
