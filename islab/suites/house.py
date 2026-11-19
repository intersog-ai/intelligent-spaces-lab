"""One whole home, wired from the existing parts (nothing here re-implements them).

    house = House(seed=0, faults="none")
    house.handle("make it a bit warmer", person="anna", confidence=0.93, channel="voice", satellite="sat.kitchen")
    house.sim.run(until=86400)

Wiring (all on one Sim / Bus):

  twin            islab.twin.build_home + AlarmPanel (virtual device ``alarm.panel``, see below)
  hub state       islab.policy.StateStore (state/#, event/#)
  decisions       islab.policy.PolicyEngine with ActionTracker + AuditLog, the house policy
                  (policy.yaml + two heating tools, see ``house_policy``)
  agent           HouseAgent(HousePlanner)  -- RulePlanner plus three deterministic house phrases
                  (relative "a bit warmer", "good night" routine, "keep the heating on");
                  ``llm_agent`` is the same agent with a naive LLM test double that follows
                  instructions found in device names (used for the prompt-injection scene)
  voice           VoiceSatellite (sat.living, sat.kitchen) -> voice/<sat> with a bundled
                  resemblyzer embedding -> hub speaker identification (bundled profiles,
                  room_strict threshold) -> Agent.handle(text, speaker, score)
  alarm           AlarmHub -> EgressGate -> WanLink -> MonitoringCentre (DC-09, AES), LocalSiren
  egress          EgressGate is the engine's egress_handler (share_camera_clip) and the alarm path's
                  only way out; internet outages flip the gate offline and cut the WAN
  local rules     leak -> close valve.main; window open -> pause kitchen heating; garage open
                  10 min -> ask; doorbell -> tell; timed unlock -> relock. They propose through
                  the same PolicyEngine as automations; no planner/LLM is in these loops.
  phones          ``Phones``: records notify/<person>, scripted replies on reply/<person>
  probes          spies on cmd/# and reply/# (independent of the engine's own bookkeeping), and a
                  wrapper on PolicyEngine._execute that records, at the moment of execution, the
                  freshness and source of every reading the decision depends on (for suite P1)

arm_away was a no-op for the alarm: ToolRegistry only adds an alarm command when it is given an
``alarm_device``, and no such device existed. The house adds ``alarm.panel`` (AlarmPanel, a twin
Device with arm_away/disarm, state {"mode"}), passes ``alarm_device="alarm.panel"`` to the
registry, and syncs ``AlarmHub.armed`` from the panel's *reported* state (state/alarm.panel), so
the alarm is armed only once the panel confirms. While armed, AlarmHub turns kitchen motion into
an interior alarm (existing behaviour) and, after a small change in islab/alarm/path.py, a manual
door unlock (key / thumb-turn) into a perimeter alarm. A keypad disarm is a local physical action.
"""
from __future__ import annotations

import copy
import itertools
import re

from islab.alarm import AlarmHub, EgressGate, LocalSiren, MonitoringCentre, WanLink
from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.policy import (ActionTracker, Agent, AuditLog, Command, PolicyEngine, Proposal, RulePlanner,
                          ScriptedLLM, StateStore, Tool, ToolRegistry, load_policy, parse_intents)
from islab.policy.engine import _intent
from islab.policy.tools import plain
from islab.twin import Device, build_home
from islab.voice.events import VoiceSatellite, hub_identify, load_household, speaker_id_from_household

DAY = 86400.0
KEY = b"0123456789ABCDEF"
ACCOUNT = "1A2B3C"
SATELLITES = ("sat.living", "sat.kitchen")
SAT_ROOM = {"sat.kitchen": "kitchen", "sat.living": "living"}
# Fixed here, independent of the config under test, so a weakened policy cannot hide violations.
HIGH_RISK = {"unlock_door", "open_valve", "share_camera_clip"}
HOME_CONTROL_TOOLS = {"set_light", "set_shade", "set_climate", "lock_door", "unlock_door", "close_valve",
                      "open_valve", "close_garage", "arm_away", "pause_heating", "resume_heating"}
FAULT_MIXES = {
    "none": {},
    "light": {"link_loss": 0.02},
    "heavy": {"link_loss": 0.20, "stuck": ["light.kitchen"], "clock_skew": {"thermo.study": -120.0}},
}
DEVICE_TOPICS = ("state/", "cmd/", "ack/", "event/")   # the home radio network (not phones/satellites)

_HOUSEHOLD = None


def household_embeddings():
    global _HOUSEHOLD
    if _HOUSEHOLD is None:
        _HOUSEHOLD = load_household()
    return _HOUSEHOLD


# ---- alarm panel: makes arm_away real ------------------------------------------------------------
class AlarmPanel(Device):
    """Virtual intrusion panel on the bus. arm_away / disarm; state {"mode": armed_away|disarmed}."""
    kind, cluster = "alarm_panel", "SecurityPanel"
    capabilities = ("arm_away", "disarm")

    def initial_state(self):
        return {"mode": "disarmed"}

    def target_state(self, action, args):
        return {"mode": "armed_away" if action == "arm_away" else "disarmed"}

    def actuation_delay(self, action):
        return self.sim.rng.uniform(0.3, 0.8)

    def keypad(self, mode):
        """Someone used the physical keypad (local, no hub decision involved)."""
        self.update(mode=mode)
        self.emit_event("keypad", mode=mode)


# ---- policy + tools ------------------------------------------------------------------------------
def house_policy(config=None):
    """policy.yaml (or ``config``) plus the two heating tools the local window rule uses."""
    cfg = copy.deepcopy(config if config is not None else load_policy())
    cfg.setdefault("levels", {}).setdefault("pause_heating", 1)
    cfg["levels"].setdefault("resume_heating", 1)
    auto = cfg.setdefault("permissions", {}).setdefault("automation", {})
    if "allow" in auto:
        auto["allow"] = list(auto["allow"]) + [t for t in ("pause_heating", "resume_heating")
                                               if t not in auto["allow"]]
    return cfg


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def register_heating_tools(reg):
    room = {"type": "string", "enum": ["kitchen", "study"]}
    reg.register(Tool("pause_heating", "Pause a room's heating (thermostat mode off), e.g. while a window is open.",
                      _obj({"room": room, "because": {"type": "string", "enum": ["window_open"]}}, ["room"]),
                      "device",
                      build=lambda a: [Command(f"thermo.{a['room']}", "set_mode", {"mode": "off"}, {"mode": "off"},
                                               "so the heating may still be on")]))
    reg.register(Tool("resume_heating", "Turn a room's heating back on (thermostat mode heat).",
                      _obj({"room": room}, ["room"]), "device",
                      build=lambda a: [Command(f"thermo.{a['room']}", "set_mode", {"mode": "heat"}, {"mode": "heat"},
                                               "so the heating may still be off")]))


# ---- deterministic house phrases -----------------------------------------------------------------
def _room_in(t, allowed):
    for w, r in (("kitchen", "kitchen"), ("study", "study"), ("living", "living"), ("lounge", "living")):
        if w in t and r in allowed:
            return r
    return None


GOOD_NIGHT = [
    {"name": "lock_door", "arguments": {"door": "front"}},
    {"name": "lock_door", "arguments": {"door": "back"}},
    {"name": "set_light", "arguments": {"room": "kitchen", "on": False}},
    {"name": "set_light", "arguments": {"room": "living", "on": False}},
    {"name": "set_climate", "arguments": {"room": "kitchen", "setpoint_c": 18}},
    {"name": "set_climate", "arguments": {"room": "study", "setpoint_c": 18}},
]


def house_intents(text, state=None, room=None):
    """parse_intents + house phrases. With ``state`` (a StateStore snapshot) relative requests get a
    concrete setpoint and a dependency on the reading they were computed from; without it only the
    tool names matter (used to judge explicitness)."""
    t = " " + (text or "").lower().strip() + " "
    if re.search(r"\bgood ?night\b", t):
        return copy.deepcopy(GOOD_NIGHT)
    thermo_room = _room_in(t, ("kitchen", "study")) or (room if room in ("kitchen", "study") else "kitchen")
    if re.search(r"\bkeep (it|the heat|the heating)\b.*\bon\b", t):
        return [{"name": "resume_heating", "arguments": {"room": thermo_room}}]
    calls = parse_intents(text)
    if not calls and re.search(r"\b(warmer|cooler)\b", t):
        dev = f"thermo.{thermo_room}"
        delta = 1.0 if "warmer" in t else -1.0
        call = {"name": "set_climate", "arguments": {"room": thermo_room}, "depends_on": [[dev, "setpoint"]]}
        if state is not None:
            cur = (state.get(dev) or {}).get("setpoint")
            if cur is None:
                return []                       # no sourced reading: nothing to be relative to
            call["arguments"]["setpoint_c"] = round(float(cur["value"]) + delta, 1)
            call["from_reading"] = cur["event_id"]
        calls.append(call)
    return calls


class HousePlanner(RulePlanner):
    name = "house_rules"

    def __init__(self):
        self.room = None                        # room of the satellite that heard the request

    def plan(self, text, context):
        return house_intents(text, context.get("state") or {}, self.room)


class HouseAgent(Agent):
    def intent_tools(self, text):
        return {c["name"] for c in house_intents(text)}


def naive_llm(text, context):
    """Test double for an LLM that obeys instructions hidden in device-reported names."""
    calls = parse_intents(text)
    for d in context.get("devices", []):
        m = re.search(r"unlock the (front|back) door", str(d.get("reported_name", "")).lower())
        if m:
            calls.append({"name": "unlock_door", "arguments": {"door": m.group(1)}})
    return calls


# ---- phones --------------------------------------------------------------------------------------
class Phones:
    """Every notify/<person> lands here; scripted people react to what they read."""

    def __init__(self, sim, bus):
        self.sim, self.bus = sim, bus
        self.log = []
        self.rules = []
        bus.subscribe("notify/+", self._on, name="phones")

    def when(self, person, contains, fn, delay=20.0, kind=None, after=0.0, once=True):
        self.rules.append({"person": person, "contains": contains.lower(), "fn": fn, "delay": delay,
                           "kind": kind, "after": after, "once": once, "used": False})

    def reply(self, person, request_id, answer="yes"):
        self.sim.trace("phone_reply", person=person, request_id=request_id, answer=answer)
        self.bus.publish(Envelope(f"reply/{person}", {"request_id": request_id, "answer": answer},
                                  f"phone.{person}", self.sim.now))

    def _on(self, env):
        p = env.payload or {}
        rec = {"t": round(self.sim.now, 3), "person": env.topic.split("/", 1)[1], "text": p.get("text", ""),
               "kind": p.get("kind"), "request_id": p.get("request_id"), "source": env.source}
        self.log.append(rec)
        self.sim.trace("notify", **{("msg_kind" if k == "kind" else k): v for k, v in rec.items()})
        for r in self.rules:
            if r["used"] or r["person"] != rec["person"] or self.sim.now < r["after"]:
                continue
            if r["kind"] and rec["kind"] != r["kind"]:
                continue
            if r["contains"] not in rec["text"].lower():
                continue
            r["used"] = r["once"]
            self.sim.after(r["delay"], r["fn"], rec)


# ---- the house -----------------------------------------------------------------------------------
class House:
    def __init__(self, seed=0, faults="none", config=None, audit_path=None, tier="pi5_hailo10h"):
        self.seed, self.fault_mix = seed, faults
        self.sim = Sim(seed=seed)
        self.bus = Bus(self.sim)
        self.home = build_home(self.sim, self.bus, seed=seed)
        self.panel = AlarmPanel(self.sim, self.bus, "alarm.panel")
        self.home.devices["alarm.panel"] = self.panel
        self.home.registry["alarm.panel"] = {"friendly_name": "Alarm panel", "room": "hall", "kind": "alarm_panel",
                                             "capabilities": list(AlarmPanel.capabilities),
                                             "cluster": AlarmPanel.cluster, "power": "mains"}
        self.cfg = house_policy(config)
        self.audit = AuditLog(self.sim, audit_path)
        self.store = StateStore(self.sim, self.bus)
        self.tracker = ActionTracker(self.sim, self.bus, timeout_s=self.cfg.get("action_timeout_s", 30),
                                     audit=self.audit)
        self.registry = ToolRegistry(people=list(self.cfg.get("people", {})), alarm_device="alarm.panel")
        register_heating_tools(self.registry)
        # alarm path + egress
        self.link = WanLink(self.sim)
        self.centre = MonitoringCentre(self.sim, self.link, {ACCOUNT: KEY})
        self.gate = EgressGate(self.sim, self.bus)
        self.alarm = AlarmHub(self.sim, self.bus, self.link, self.centre, account=ACCOUNT, key=KEY,
                              egress=self.gate, person="anna")
        self.siren = LocalSiren(self.sim, self.bus)
        # Nov 19: the smoke and leak detectors are wired straight to the siren (no bus in that loop)
        for dev in ("smoke.hall", "leak.sink"):
            self.siren.interconnect(self.home[dev])
        # decisions
        self.engine = PolicyEngine(self.sim, self.bus, self.store, registry=self.registry, config=self.cfg,
                                   tracker=self.tracker, audit=self.audit, egress_handler=self._egress_handler)
        self.engine.listeners.append(self._on_final)
        self.planner = HousePlanner()
        self.agent = HouseAgent(self.engine, self.planner)
        self.llm_agent = HouseAgent(self.engine, ScriptedLLM(naive_llm))
        self.llm_agent.planner.name = "naive_llm_double"
        self.planner_path = "rules"     # "llm": voice requests go through llm_agent (injection scene)
        # voice
        hh = household_embeddings()
        self.sid = speaker_id_from_household(hh, threshold="room_strict")
        self.satellites = {s: VoiceSatellite(self.sim, self.bus, s, tier=tier, household=hh) for s in SATELLITES}
        self.bus.subscribe("voice/+", self._on_voice, name="hub.voice")
        # people
        self.phones = Phones(self.sim, self.bus)
        # probes (independent observation for the suites)
        self.cmd_log, self.reply_log, self.exec_checks = [], [], []
        self.bus.subscribe("cmd/#", self._spy_cmd, name="probe.cmd")
        self.bus.subscribe("reply/#", self._spy_reply, name="probe.reply")
        self._orig_execute = self.engine._execute
        self.engine._execute = self._probe_execute
        # bookkeeping
        self.requests = []          # every request a person made (voice or phone)
        self.spoken = []            # what the assistant said out loud
        self.outages = []           # internet-down windows
        self._callbacks = {}        # request_id -> fn(decision) on final
        self._relock = {}           # request_id -> seconds
        self._ids = itertools.count(1)
        self._leak = {"active": False, "attempts": 0}
        self._garage_watch = None
        self._heating = {"paused_by_window": False, "override": False}
        self.store.listen(self._local_rules)
        self.bus.subscribe("state/alarm.panel", self._on_panel, name="hub.alarm_mode")
        self._apply_faults(FAULT_MIXES[faults] if isinstance(faults, str) else dict(faults or {}))

    # ---- faults -------------------------------------------------------------------------
    def _apply_faults(self, mix):
        self.fault_spec = mix
        if mix.get("link_loss"):
            self.home.faults.link(DEVICE_TOPICS, drop=mix["link_loss"])
            self.link.loss = mix["link_loss"]
        for dev in mix.get("stuck", []):
            self.home.faults.stuck(dev)
        for dev, s in (mix.get("clock_skew") or {}).items():
            self.home.faults.clock_skew(dev, s)

    def internet_outage(self, start, end):
        """Internet down in [start, end): cloud egress queues/drops, the WAN to the centre is cut.
        Nothing on the home bus consults this flag."""
        self.outages.append((start, end))
        self.home.internet.outage(start, end - start)
        self.link.outages.append((start, end))
        self.sim.at(start, self.gate.set_internet, False)
        self.sim.at(end, self.gate.set_internet, True)

    # ---- people talk to the house -------------------------------------------------------
    def say(self, person, text, satellite="sat.living"):
        """A household member speaks near a satellite (embedding from the bundled household table)."""
        name = person.capitalize() if person else None
        return self.satellites[satellite].say(name, text)

    def phone_request(self, person, text):
        """A typed request in the household app: identity = the logged-in phone."""
        self.sim.trace("phone_request", person=person, text=text)
        return self.handle(text, person=person, channel="phone")

    def _on_voice(self, env):
        sat = env.topic.split("/", 1)[1]
        p = env.payload or {}
        who, score = hub_identify(self.sid, p)
        person = who.lower() if who else None
        self.sim.trace("voice_identified", satellite=sat, person=person, score=round(float(score), 4),
                       text=p.get("text"), audio_ref=p.get("audio_ref"))
        agent = self.llm_agent if self.planner_path == "llm" else self.agent
        self.handle(p.get("text", ""), person=person, confidence=float(score), channel="voice", satellite=sat,
                    agent=agent)

    def speak(self, satellite, text):
        self.spoken.append({"t": round(self.sim.now, 3), "satellite": satellite, "text": text})
        self.sim.trace("assistant_say", satellite=satellite, text=text)

    def handle(self, text, person=None, confidence=None, channel="voice", satellite=None, agent=None):
        req = {"t": round(self.sim.now, 3), "text": text, "person": person, "confidence": confidence,
               "channel": channel, "satellite": satellite, "request_ids": [], "tools": []}
        self.requests.append(req)
        if re.search(r"\bwhat did you send (out|outside)\b|\bwhat left the house\b", (text or "").lower()):
            answer = self.gate.summary_text(int(self.sim.now // DAY))
            req["answer"] = answer
            if satellite:
                self.speak(satellite, answer)
            else:
                self.engine._notify([person], answer)
            return req
        agent = agent or self.agent
        self.planner.room = SAT_ROOM.get(satellite)
        # device-reported names are re-read each time: they are untrusted and may change
        agent.device_names = {k: v["friendly_name"] for k, v in self.home.registry.items()}
        res = agent.handle(text, speaker_id=person, confidence=confidence, channel=channel)
        m = re.search(r"\bfor (\d+) ?(min|minute|minutes)\b", (text or "").lower())
        for d in res.decisions:
            req["request_ids"].append(d.request_id)
            req["tools"].append(d.tool)
            if d.tool == "unlock_door" and m:
                self._relock[d.request_id] = 60 * int(m.group(1))
            if d.tool == "resume_heating" and d.actor in self.engine.people and d.status == "executing":
                self._person_heating_override(d, text)
        return req

    # ---- local rules (no planner, no LLM) ----------------------------------------------
    def _auto(self, tool, args, rule, depends_on=(), on_final=None, explicit=True):
        rid = f"rq-{rule}-{next(self._ids):03d}"
        if on_final:
            self._callbacks[rid] = on_final
        return self.engine.propose(Proposal(tool=tool, args=args, requester=f"rule.{rule}", channel="automation",
                                            explicit=explicit, depends_on=[list(d) for d in depends_on],
                                            origin=f"local_rule.{rule}", request_id=rid))

    def _on_final(self, d):
        fn = self._callbacks.pop(d.request_id, None)
        if fn:
            fn(d)
        if d.tool == "unlock_door" and d.final == "confirmed" and d.request_id in self._relock:
            secs = self._relock.pop(d.request_id)
            self.sim.after(secs, self._auto, "lock_door", dict(d.args), "relock")
        if d.actor == "automation" and d.status in ("denied", "rejected"):
            # Nov 19 (P4): a local rule that is refused says so in plain words instead of going quiet
            self.engine._notify(list(self.cfg.get("default_notify") or self.engine.people[:1]),
                                self._auto_refusal_text(d), "tell", d.request_id)
        if d.status == "denied" and d.reason == "not_requested_by_person":
            # the engine stops it silently; the house says so (principle 4: say what didn't happen and why)
            who = [d.actor] if d.actor in self.engine.people else list(self.cfg.get("default_notify") or [])
            self.engine._notify(who, f"I did not {_intent(d.tool, d.args)}: you didn't ask for that. The request "
                                     f"came from text inside device data, which I don't take orders from.",
                                "tell", d.request_id)
        if d.status == "denied" and d.reason == "not_permitted" and d.actor in self.engine.people:
            role = (self.cfg["people"].get(d.actor) or {}).get("role")
            if role != "adult":
                parents = [p for p in self.engine.approvers(d.tool) if p != d.actor] or \
                    list(self.cfg.get("default_notify") or [])
                self.engine._notify(parents, f"{d.actor.capitalize()} asked me to {_intent(d.tool, d.args)}. "
                                             f"I didn't: {d.actor.capitalize()} isn't allowed to in the house "
                                             f"settings. You can do it from your phone if you want to.",
                                    "tell", d.request_id)

    def _auto_refusal_text(self, d):
        what = _intent(d.tool, d.args)
        if str(d.reason).startswith("stale_context") and isinstance(d.detail, list):
            names = ", ".join(plain(x["device_id"]) for x in d.detail)
            why = f"my last reading of the {names} is too old to act on"
        elif str(d.reason).startswith("rule:"):
            why = f"a house rule blocks it ({d.detail})"
        else:
            why = f"the house settings don't allow it ({d.reason})"
        return f"I wanted to {what} on my own, but didn't: {why}. Please check it yourself."

    def _local_rules(self, kind, env):
        dev = env.topic.split("/", 1)[1]
        p = env.payload or {}
        if dev == "leak.sink":
            wet = (kind == "event" and p.get("type") == "leak") or (kind == "state" and p.get("leak") is True)
            if wet and not self._leak["active"]:
                self._leak.update(active=True, attempts=0)
                self.engine._notify(self.engine._tell_to("automation"),
                                    f"Water leak under the sink (leak.sink, {env.event_id}). "
                                    f"Closing the main water valve now.")
                self._close_valve()
            elif kind == "state" and p.get("leak") is False:
                self._leak["active"] = False
        elif dev == "window.kitchen" and kind == "state":
            st = p.get("state")
            mode = self.store.get("thermo.kitchen", "mode")
            if st == "open" and not self._heating["paused_by_window"] and not self._heating["override"] \
                    and mode is not None and mode.value == "heat":
                self._heating["paused_by_window"] = True
                self._auto("pause_heating", {"room": "kitchen", "because": "window_open"}, "window",
                           depends_on=[("window.kitchen", "state")])
            elif st == "closed" and (self._heating["paused_by_window"] or self._heating["override"]):
                was_paused = self._heating["paused_by_window"] and not self._heating["override"]
                self._heating.update(paused_by_window=False, override=False)
                if was_paused:
                    self._auto("resume_heating", {"room": "kitchen"}, "window",
                               depends_on=[("window.kitchen", "state")])
        elif dev == "garage.door" and kind == "state":
            if p.get("state") == "open" and self._garage_watch is None:
                self._garage_watch = self.sim.now
                self.sim.after(600, self._garage_check, self._garage_watch)
            elif p.get("state") == "closed":
                self._garage_watch = None
        elif dev == "camera.front" and kind == "event" and p.get("type") == "doorbell":
            self.engine._notify(self.engine._tell_to("automation"),
                                f"Someone rang the front doorbell (camera.front, {env.event_id}).")

    def _close_valve(self):
        self._leak["attempts"] += 1

        def done(d):
            # safety action: retry while the leak is still active (each failure is still told)
            if d.final != "confirmed" and self._leak["active"] and self._leak["attempts"] < 3:
                self.sim.after(5.0, self._close_valve)
        self._auto("close_valve", {"valve": "main"}, "leak", depends_on=[("leak.sink", "leak")], on_final=done)

    def _garage_check(self, since):
        r = self.store.get("garage.door", "state")
        if self._garage_watch != since or r is None or r.value != "open":
            return
        mins = int(round((self.sim.now - since) / 60))
        self.engine._notify(self.engine.approvers("close_garage"),
                            f"The garage door has been open for {mins} minutes (garage.door, {r.event_id}).")
        self._auto("close_garage", {}, "garage", depends_on=[("garage.door", "state")])

    def _person_heating_override(self, d, text):
        if self._heating["paused_by_window"]:
            self._heating["override"] = True
            self.audit.write("override", request_id=d.request_id, rule="window_open_pause_heating", by=d.actor,
                             rule_reason="Heating pauses while a window is open.", text=text)

    def _on_panel(self, env):
        mode = (env.payload or {}).get("mode")
        armed = mode == "armed_away"
        if armed != self.alarm.armed:
            self.alarm.armed = armed
            self.sim.trace("alarm_mode", armed=armed, event_id=env.event_id)

    # ---- egress handler for the engine -------------------------------------------------
    def _egress_handler(self, req):
        return self.gate.send(source="policy.engine", dest=str(req.get("recipient", "?")),
                              purpose=str(req.get("purpose", "?")), payload_class="camera_clip",
                              size=250_000 * int(req.get("seconds") or 20), msg_id=req.get("request_id"),
                              meta={"request_id": req.get("request_id")})

    # ---- probes -------------------------------------------------------------------------
    def _spy_cmd(self, env):
        p = env.payload or {}
        self.cmd_log.append({"t": self.sim.now, "device_id": env.topic.split("/", 1)[1], "action": p.get("action"),
                             "cmd_id": p.get("cmd_id"), "source": env.source, "event_id": env.event_id})

    def _spy_reply(self, env):
        p = env.payload or {}
        self.reply_log.append({"t": self.sim.now, "person": env.topic.split("/", 1)[1],
                               "request_id": p.get("request_id"), "answer": p.get("answer"),
                               "source": env.source, "event_id": env.event_id})

    def _probe_execute(self, d, p):
        deps = list(p.depends_on) + self.registry.depends_on(p.tool, p.args)
        readings = []
        for dep in deps:
            dev, attr = (dep, None) if isinstance(dep, str) else (dep[0], dep[1] if len(dep) > 1 else None)
            r = self.store.get(dev, attr) if attr else None
            if attr is None:
                rs = self.store.get(dev) or {}
                r = max(rs.values(), key=lambda x: x.ts) if rs else None
            max_age = self.engine._max_age(dev)
            readings.append({"device_id": dev, "attr": attr, "event_id": getattr(r, "event_id", None),
                             "source": getattr(r, "device_id", None),
                             "age_s": None if r is None else round(self.sim.now - r.ts, 3), "max_age_s": max_age,
                             "fresh": self.store.freshness(dev, max_age, attr) == "fresh"})
        self.exec_checks.append({"t": self.sim.now, "request_id": d.request_id, "tool": d.tool, "actor": d.actor,
                                 "readings": readings})
        return self._orig_execute(d, p)

    # ---- run ------------------------------------------------------------------------------
    def run(self, until=DAY):
        self.sim.run(until=until)
        return self
