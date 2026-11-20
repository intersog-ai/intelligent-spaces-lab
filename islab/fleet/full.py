"""The full test: one Season 1 household day with every Build Track layer switched on together.

    h = FullHouse(seed=0, faults="heavy")          # every layer, every integration fix
    schedule_full_day(h); h.run()
    run_suites(h)                                  # the five principle suites (islab.suites), unchanged
    full_suites(h)                                 # + the route-aware P5 and the B04/B05/B07/B08/B09 checks

Layers, each through its own public API (nothing in another package is edited):

  B01  twin, House wiring, suites                   islab.suites.house.House (subclassed)
  B02  connect: Matter data model, HA discovery,    ConnectLayer below (islab.connect.matter_model,
       Last Will availability                       islab.connect.ha_discovery); the real MQTT broker is
                                                    NOT in the loop (it runs on wall time; see notes.md)
  B03  readings quality                             islab.readings.ReadingQuality on the bus; FullEngine
                                                    refuses decisions on stuck, disputed or dead readings
  B04  leak -> valve state machine                  islab.safety LeakValveController + FlowSensor + Journal
  B05  voice pipeline                               islab.pipeline.VoicePipeline (mini PC tier, measured stages)
  B06  trust policy                                 islab.identity.TrustPolicy (tier per utterance, engine config)
  B07  approval gate                                islab.approval.GatedEngine; phones sign their replies
  B08  dual-path alarms, camera asks                islab.security DualPathAlarmHub / DualPathCentre /
                                                    CellularLink / CameraObserver("ask")
  B09  egress ledger                                islab.privacy2.LedgerGate (composed with B08's gate)

Integration fixes. Each one closes an interaction bug found when the layers were first switched on
together. ``FIXES`` names them; ``FullHouse(fixes=...)`` turns any subset off so the experiment can
show what each one is for. None of them weakens a check; two of them (F5, F8) are adapters that let a
B01 suite read a B08/B09 fact it was written before.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
import re

import numpy as np

from islab.alarm import LocalSiren, WanLink
from islab.approval import GatedEngine, b07_policy, make_keys, sign
from islab.approval.tools import add_b07_tools
from islab.connect import ha_discovery, matter_model
from islab.core.bus import Bus, Envelope, match
from islab.core.sim import Sim
from islab.identity import TrustPolicy
from islab.pipeline import Grammar, StageModel, Utterance, VoicePipeline, build_tiers
from islab.pipeline.intent import Interpretation
from islab.policy import ActionTracker, AuditLog, ScriptedLLM, StateStore, ToolRegistry
from islab.policy.engine import _intent
from islab.privacy2 import DEFAULT_MANIFESTS, LedgerGate
from islab.privacy2 import answer as egress_answer
from islab.readings import (DEAD, DEFAULT_PROFILES, DISPUTED, STUCK, ReadingQuality, SensorProfile,
                            max_age_from_devices)
from islab.safety.controller import CLOSED, ESCALATED, LeakValveController
from islab.safety.devices import FlowSensor, GroundTruth
from islab.safety.journal import Journal
from islab.safety.notify import Notifier, Phone as PushPhone, PushRelay
from islab.security import CellularLink, CameraObserver, DualPathAlarmHub, DualPathCentre, DualPathEgressGate
from islab.suites.day import schedule_day
from islab.suites.house import (ACCOUNT, DAY, FAULT_MIXES, KEY, SAT_ROOM, SATELLITES, AlarmPanel, House,
                                HouseAgent, HousePlanner, Phones, household_embeddings, house_intents,
                                house_policy, naive_llm, register_heating_tools)
from islab.twin import build_home, parse_time as T
from islab.voice.events import VoiceSatellite, speaker_id_from_household

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MEASURED = os.path.join(ROOT, "experiments", "b05_pipeline", "measured.json")

FIXES = {
    "F1_signed_replies": "B07 accepts only signed phone replies; the B01 day's phones sent unsigned ones",
    "F2_day_manifests": "B09 manifests did not declare the day's senders (backup, updater, camera observer)",
    "F3_one_voice_path": "B05 republishes the transcript on voice/<sat>; the B01 hub handled it a second time",
    "F4_house_phrases": "B05 grammar did not know the house phrases (good night, keep the heating on)",
    "F5_route_aware_p5": "B01 P5 counts the B08 cellular alarm path and a consented clip as forbidden egress",
    "F6_one_leak_owner": "the B01 house leak rule and the B04 controller both commanded the valve",
    "F7_prompt_words": "the scripted people answer B01 prompt wording; B07 prompts word the same action differently",
    "F8_deny_union": "B06 household.yaml replaced Sam's B01/B07 deny list (unlock, garage) instead of adding to it",
    "F9_destination_first": "B08's clip-consent check ran before the destination check, so B09 never saw a "
                            "phoning-home camera as undeclared and nobody was told",
    "F10_thermostat_not_stuck": "B03 called a thermostat holding its setpoint 'stuck' and told the household",
}
ALL_FIXES = frozenset(FIXES)

# senders the scripted day uses that B09's DEFAULT_MANIFESTS do not declare
DAY_MANIFESTS = {
    "backup": {"backup_encrypted": (200_000_000, 2)},
    "updater": {"fleet_update": (2_000, 6)},
    "camera_observer": {"alarm": (3_000_000, 10)},
}
PEOPLE_HOME = {"anna": True, "mark": False}          # who gets LAN tells vs a push (B04 default)
MATTER_KIND = {"light": "light", "thermostat": "thermostat", "shade": "shade", "contact": "contact"}
LWT_S = 15.0          # B02: Last Will after a power loss = 1.5 x 10 s keepalive (experiments/b02_connect)


# ------------------------------------------------------------------------------------------------
class FastBus(Bus):
    """islab.core.bus.Bus with the topic -> subscribers match cached (a speed wrapper only). Same
    subscribers, same order, same snapshot-at-delivery semantics; the cache is rebuilt whenever a
    subscription is added. tests/test_b10_fleet.py checks the trace is identical to Bus."""

    def __init__(self, sim, base_latency=0.01):
        super().__init__(sim, base_latency)
        self._cache, self._n_subs = {}, 0

    def _deliver(self, env):
        if len(self.subs) != self._n_subs:
            self._cache.clear()
            self._n_subs = len(self.subs)
        hit = self._cache.get(env.topic)
        if hit is None:
            hit = [s for s in self.subs if match(s[0], env.topic)]
            self._cache[env.topic] = hit
        for pattern, cb, name in hit:
            self.delivered += 1
            try:
                cb(env)
            except Exception as exc:   # one broken subscriber must not stop the home
                self.errors.append((self.sim.now, name, env.topic, repr(exc)))
                self.sim.trace('subscriber_error', name=name, topic=env.topic, error=repr(exc))


class FullGate(LedgerGate, DualPathEgressGate):
    """B09 ledger + manifests (LedgerGate) over B08's cellular route and per-clip consent
    (DualPathEgressGate) over the B01 rules. Python's MRO chains the three ``_check``/``send``.
    With ``destination_first`` (F9) an undeclared destination is refused as such before B08's
    clip-consent rule, so B09's detection sees it."""

    destination_first = True

    def _check(self, dest, purpose, cls, meta):
        if self.destination_first and dest not in self.allowlist:
            return "destination_not_allowlisted"
        return super()._check(dest, purpose, cls, meta)


class FullEngine(GatedEngine):
    """B07 gate; freshness also asks the B03 reading-quality layer. B03 can only add refusals
    (stuck, disputed, dead readings); it never relaxes the B01 freshness rule."""

    quality = None

    def check_freshness(self, deps):
        bad = super().check_freshness(deps)
        if self.quality is None:
            return bad
        seen = {(b["device_id"], b["attr"]) for b in bad}
        for d in deps:
            dev, attr = (d, None) if isinstance(d, str) else (d[0], d[1] if len(d) > 1 else None)
            if attr is None or (dev, attr) in seen:
                continue
            a = self.quality.assess(dev, attr)
            if a.status in (STUCK, DISPUTED, DEAD):
                bad.append({"device_id": dev, "attr": attr, "freshness": f"quality:{a.status}",
                            "age_s": self.store.age(dev, attr)})
        return bad


class SignedPhones(Phones):
    """B01 scripted phones, but every reply is signed with that phone's key (B07 Phone.build_reply).
    With ``action_words`` (F7) a scripted person recognises a B07 prompt by its action, not by B01's
    wording: the rule text is matched against the prompt and against "approve: <B01 words for the
    action>". What is logged is the prompt as sent."""

    def __init__(self, sim, bus, keys, sign_replies=True, action_words=True):
        self.keys, self.sign_replies, self.action_words = keys, sign_replies, action_words
        self.prompts = {}                    # request_id -> notify payload (digest, nonce)
        super().__init__(sim, bus)

    def _on(self, env):
        p = env.payload or {}
        if p.get("request_id"):
            self.prompts[p["request_id"]] = dict(p)
        rec = {"t": round(self.sim.now, 3), "person": env.topic.split("/", 1)[1], "text": p.get("text", ""),
               "kind": p.get("kind"), "request_id": p.get("request_id"), "source": env.source}
        self.log.append(rec)
        self.sim.trace("notify", **{("msg_kind" if k == "kind" else k): v for k, v in rec.items()})
        match = rec["text"].lower()
        a = p.get("action")
        if self.action_words and isinstance(a, dict) and a.get("tool"):
            match += " | approve: " + _intent(a["tool"], a.get("args") or {}).lower()
        for r in self.rules:
            if r["used"] or r["person"] != rec["person"] or self.sim.now < r["after"]:
                continue
            if r["kind"] and rec["kind"] != r["kind"]:
                continue
            if r["contains"] not in match:
                continue
            r["used"] = r["once"]
            self.sim.after(r["delay"], r["fn"], rec)

    def reply(self, person, request_id, answer="yes"):
        if not self.sign_replies:
            return super().reply(person, request_id, answer)
        pr = self.prompts.get(request_id, {})
        digest = pr.get("digest") or "-"
        nonce = pr.get("nonce", "-")
        pl = {"request_id": request_id, "digest": digest, "nonce": nonce, "answer": answer,
              "sig": sign(self.keys[person], request_id, digest, nonce, answer)}
        self.sim.trace("phone_reply", person=person, request_id=request_id, answer=answer)
        self.bus.publish(Envelope(f"reply/{person}", pl, f"phone.{person}", self.sim.now))


class HouseGrammar(Grammar):
    """B05 grammar first; if it understands nothing, the B01 house phrases (good night, keep the
    heating on, a bit warmer) from islab.suites.house.house_intents."""

    def parse(self, text, satellite="sat.kitchen", ctx=None, now=0.0):
        it = super().parse(text, satellite=satellite, ctx=ctx, now=now)
        if it.ok or it.status in ("ambiguous",):
            return it
        calls = house_intents(text, self.store.snapshot() if self.store is not None else {},
                              SAT_ROOM.get(satellite))
        if calls:
            relative = any("from_reading" in c for c in calls)
            return Interpretation(text, "ok", [{"name": c["name"], "arguments": c["arguments"]} for c in calls],
                                  resolved_by="relative" if relative else "words", norm=it.norm)
        return it


class HousePipeline(VoicePipeline):
    """B05 pipeline that also keeps the B01 House's request book (for the suites) and its override hook."""

    def __init__(self, house, *a, **k):
        self.house = house
        super().__init__(*a, **k)

    def _policy(self, utt, rec, it, ms):
        n0 = len(rec["requests"])
        super()._policy(utt, rec, it, ms)
        req = self.house._uid_req.get(utt.uid)
        for r in rec["requests"][n0:]:
            d = self.engine.decisions.get(r["request_id"])
            if req is not None:
                req["request_ids"].append(r["request_id"])
                req["tools"].append(r["tool"])
            if d is not None and d.tool == "resume_heating" and d.actor in self.engine.people \
                    and d.status == "executing":
                self.house._person_heating_override(d, utt.text)


class ConnectLayer:
    """B02 in the day loop, modelled: every state report of a Matter-mapped device is translated to its
    Matter attributes and back (round trip must reproduce the twin state); a Home Assistant discovery
    config exists for each; a device that loses power is announced offline by its Last Will after
    LWT_S (the 15.0 s measured in B02), and the household is told."""

    def __init__(self, house):
        self.h = house
        self.configs = {}
        self.roundtrips = 0
        self.mismatches = []
        self.lwt = []
        for dev, info in house.home.registry.items():
            mk = MATTER_KIND.get(info.get("kind"))
            if mk:
                cfg = ha_discovery.config("home0", dev, mk, info.get("friendly_name", dev))
                if cfg:
                    self.configs[ha_discovery.config_topic("home0", dev, mk)] = cfg
        house.bus.subscribe("state/#", self._on_state, name="connect.matter")
        faults = house.home.faults
        orig = faults.offline

        def offline(device_id, on=True):
            orig(device_id, on)
            if on:
                house.sim.after(LWT_S, self._last_will, device_id)
        faults.offline = offline

    def _on_state(self, env):
        dev = env.topic.split("/", 1)[1]
        mk = MATTER_KIND.get((self.h.home.registry.get(dev) or {}).get("kind"))
        if not mk:
            return
        st = dict(env.payload or {})
        back = matter_model.from_matter(mk, matter_model.to_matter(mk, st))
        self.roundtrips += 1
        for k, v in back.items():
            if k in st and isinstance(v, (int, float)) and isinstance(st[k], (int, float)):
                ok = abs(float(v) - float(st[k])) <= 0.5      # LevelControl is 0..254: 0.4% steps
            else:
                ok = k not in st or v == st[k]
            if not ok:
                self.mismatches.append({"t": round(self.h.sim.now, 3), "device": dev, "attr": k,
                                        "twin": st.get(k), "matter_roundtrip": v})

    def _last_will(self, device_id):
        if not self.h.home.devices[device_id].offline:
            return
        self.lwt.append((self.h.sim.now, device_id))
        self.h.bus.publish(Envelope(f"availability/{device_id}", {"state": "offline"}, "connect.lwt",
                                    self.h.sim.now))
        name = self.h.home.registry.get(device_id, {}).get("friendly_name", device_id)
        self.h.engine._notify(list(self.h.cfg.get("default_notify") or ["anna"]),
                              f"The {name} went offline: its connection dropped {int(LWT_S)} seconds ago. "
                              f"It may not respond until it is back.", "tell")


# ------------------------------------------------------------------------------------------------
def full_policy(trust, fixes):
    """policy.yaml -> B07 additions -> B06 household -> B01 house heating tools; with F8 the deny
    lists of every layer are united (deny wins) instead of the last layer replacing the earlier."""
    base = b07_policy()
    cfg = trust.engine_config(base)
    if "F8_deny_union" in fixes:
        for who, rule in (base.get("permissions") or {}).items():
            if "deny" in rule:
                d = cfg["permissions"].setdefault(who, {}).setdefault("deny", [])
                d.extend(x for x in rule["deny"] if x not in d)
    return house_policy(cfg)


def stage_model(tier="minipc_cpu"):
    with open(MEASURED) as f:
        m = json.load(f)
    return StageModel(tier, build_tiers(m["stages"])[tier])


class FullHouse(House):
    """The B01 House with every Build Track layer switched on. Same day script, same suites."""

    def __init__(self, seed=0, faults="none", fixes=ALL_FIXES, tier="minipc_cpu", update=None,
                 b05_voice=True, fast_bus=True):
        self.fixes = frozenset(fixes)
        self.update = dict(update or {})
        self.seed, self.fault_mix = seed, faults
        self.sim = Sim(seed=seed)
        self.bus = (FastBus if fast_bus else Bus)(self.sim)
        self.home = build_home(self.sim, self.bus, seed=seed)
        self.panel = AlarmPanel(self.sim, self.bus, "alarm.panel")
        self.home.devices["alarm.panel"] = self.panel
        self.home.registry["alarm.panel"] = {"friendly_name": "Alarm panel", "room": "hall", "kind": "alarm_panel",
                                             "capabilities": list(AlarmPanel.capabilities),
                                             "cluster": AlarmPanel.cluster, "power": "mains"}
        # B04 flow meter on the main pipe (a device the base twin does not have)
        self.flow = FlowSensor(self.sim, self.bus, self.home["valve.main"], self.home["leak.sink"])
        self.home.devices["flow.main"] = self.flow
        self.home.registry["flow.main"] = {"friendly_name": "Main pipe flow meter", "room": "utility",
                                           "kind": "flow", "capabilities": [], "cluster": "FlowMeasurement",
                                           "power": "mains"}
        self.valve_truth = GroundTruth(self.sim, self.home["valve.main"])
        # B06 trust + B07 gate config
        self.trust = TrustPolicy()
        self.cfg = full_policy(self.trust, self.fixes)
        self.audit = AuditLog(self.sim, None)
        self.store = StateStore(self.sim, self.bus)
        # B03 reading quality on the same bus
        profiles = dict(DEFAULT_PROFILES)
        if "F10_thermostat_not_stuck" in self.fixes:
            # a thermostat that has reached its setpoint reports the same value for hours: not stuck
            profiles[("thermostat", "current_temp")] = SensorProfile(-30, 60, 0.05)
        self.quality = ReadingQuality(self.sim, self.bus, registry=self.home.registry, profiles=profiles,
                                      max_age=max_age_from_devices(self.home.devices))
        self.quality.on_issue(self._on_quality_issue)
        self.tracker = ActionTracker(self.sim, self.bus, timeout_s=self.cfg.get("action_timeout_s", 30),
                                     audit=self.audit)
        self.registry = ToolRegistry(people=list(self.cfg.get("people", {})), alarm_device="alarm.panel")
        register_heating_tools(self.registry)
        add_b07_tools(self.registry)
        # B08 + B09: one gate, two routes out, a ledger row for every attempt
        manifests = {k: dict(v) for k, v in DEFAULT_MANIFESTS.items()}
        if "F2_day_manifests" in self.fixes:
            manifests.update(copy.deepcopy(DAY_MANIFESTS))
        for src, lim in (self.update.get("manifests") or {}).items():
            manifests.setdefault(src, {}).update(lim)
        self.gate = FullGate(self.sim, self.bus, manifests=manifests)
        self.gate.destination_first = "F9_destination_first" in self.fixes
        self.link = WanLink(self.sim)
        self.cell = CellularLink(self.sim)
        self.centre = DualPathCentre(self.sim, self.link, {ACCOUNT: KEY})
        self.alarm = DualPathAlarmHub(self.sim, self.bus, self.link, self.centre, cell_link=self.cell,
                                      account=ACCOUNT, key=KEY, egress=self.gate, person="anna")
        self.siren = LocalSiren(self.sim, self.bus)
        for dev in ("smoke.hall", "leak.sink"):
            self.siren.interconnect(self.home[dev])
        # B07 engine
        self.keys = make_keys(self.sim.rng, list(self.cfg.get("people", {})))
        self.engine = FullEngine(self.sim, self.bus, self.store, self.keys, config=self.cfg, registry=self.registry,
                                 tracker=self.tracker, audit=self.audit, egress_handler=self._egress_handler)
        self.engine.quality = self.quality
        self.engine.listeners.append(self._on_final)
        self.planner = HousePlanner()
        self.agent = HouseAgent(self.engine, self.planner)
        self.llm_agent = HouseAgent(self.engine, ScriptedLLM(naive_llm))
        self.llm_agent.planner.name = "naive_llm_double"
        self.planner_path = "rules"
        # voice: satellites keep the household embeddings; B06 decides who speaks; B05 runs the pipeline
        hh = household_embeddings()
        self.sid = speaker_id_from_household(hh, threshold="room_strict")
        self.satellites = {s: VoiceSatellite(self.sim, self.bus, s, tier="pi5_hailo10h", household=hh)
                           for s in SATELLITES}
        self.b05_voice = b05_voice
        self.pipeline = None
        if b05_voice:
            grammar = HouseGrammar(self.store) if "F4_house_phrases" in self.fixes else Grammar(self.store)
            self.pipeline = HousePipeline(self, self.sim, self.bus, self.engine, stage_model(tier), home=self.home,
                                          grammar=grammar)
        if not b05_voice or "F3_one_voice_path" not in self.fixes:
            self.bus.subscribe("voice/+", self._on_voice, name="hub.voice")
        self._uid_req, self._uids = {}, itertools.count(1)
        self.trust_log = []
        # people: phones sign replies (B07)
        self.phones = SignedPhones(self.sim, self.bus, self.keys, sign_replies="F1_signed_replies" in self.fixes,
                                   action_words="F7_prompt_words" in self.fixes)
        # B04 leak controller, its journal, and its notifier (LAN for people home, push for people away)
        self.journal = Journal(self.sim)
        self.push_phones = [PushPhone(self.sim, self.bus, p, home) for p, home in PEOPLE_HOME.items()]
        self.notifier = Notifier(self.sim, self.bus, self.gate, PushRelay(self.sim, self.push_phones), PEOPLE_HOME)
        self.leak_ctl = LeakValveController(self.sim, self.bus, self.journal, self.alarm, self.notifier)
        # B08 camera asks
        self.camera_obs = CameraObserver(self.sim, self.bus, design="ask", alarm_hub=self.alarm, gate=self.gate,
                                         centre=self.centre, armed=lambda t: "set to away" if self.alarm.armed
                                         else "off")
        # B02 data model and availability
        self.connect = ConnectLayer(self)
        # probes and bookkeeping (as House)
        self.cmd_log, self.reply_log, self.exec_checks = [], [], []
        self.bus.subscribe("cmd/#", self._spy_cmd, name="probe.cmd")
        self.bus.subscribe("reply/#", self._spy_reply, name="probe.reply")
        self._orig_execute = self.engine._execute
        self.engine._execute = self._probe_execute
        self.requests, self.spoken, self.outages = [], [], []
        self._callbacks, self._relock = {}, {}
        self._ids = itertools.count(1)
        self._leak = {"active": False, "attempts": 0}
        self._garage_watch = None
        self._heating = {"paused_by_window": False, "override": False}
        self.store.listen(self._local_rules)
        self.bus.subscribe("state/alarm.panel", self._on_panel, name="hub.alarm_mode")
        self._apply_faults(FAULT_MIXES[faults] if isinstance(faults, str) else dict(faults or {}))

    # ---- B03: reading problems are told (principle 4) ------------------------------------------
    def _on_quality_issue(self, rec):
        name = self.home.registry.get(rec["device"], {}).get("friendly_name", rec["device"])
        to = list(self.cfg.get("default_notify") or ["anna"])
        if rec["kind"] in ("stuck", "disputed", "dead"):
            self.engine._notify(to, f"I can't trust the {name} right now ({rec['kind']}). "
                                    f"I won't act on its readings until it looks right again.", "tell")
        elif rec["kind"] == "clock_skew":
            self.engine._notify(to, f"I can't trust the {name}'s clock: it is off by about "
                                    f"{abs(int(rec.get('skew_s', 0)))} seconds. Its readings may be refused as old.",
                                "tell")

    # ---- local rules: B04 owns the leak --------------------------------------------------------
    def _local_rules(self, kind, env):
        if "F6_one_leak_owner" in self.fixes and env.topic.split("/", 1)[1] == "leak.sink":
            return
        return super()._local_rules(kind, env)

    # ---- voice: B06 tier, then the B05 pipeline -------------------------------------------------
    def say(self, person, text, satellite="sat.living"):
        if re.search(r"\bwhat did you send (out|outside)\b|\bwhat left the house\b", (text or "").lower()):
            return self.handle(text, person=person, channel="voice", satellite=satellite)
        name = person.capitalize() if person else None
        emb = self.satellites[satellite]._pick_embedding(name) if name else None
        scores = {} if emb is None else {k.lower(): float(v) for k, v in
                                         self.sid.scores(emb=np.asarray(emb, dtype=np.float32)).items()}
        ev = self.trust.evidence(scores, live=None)
        tier = self.trust.tier(ev)
        named = self.trust.who(ev, "named")
        self.trust_log.append({"t": round(self.sim.now, 3), "person": person, "tier": tier, "named": named,
                               "score": round(ev.score, 4), "text": text})
        if not self.b05_voice or self.planner_path == "llm":
            agent = self.llm_agent if self.planner_path == "llm" else self.agent
            delay = 1.2
            self.sim.after(delay, self.handle, text, named, ev.score if named else None, "voice", satellite, agent)
            return
        uid = f"u{next(self._uids):04d}"
        req = {"t": round(self.sim.now, 3), "text": text, "person": named, "confidence": ev.score if named else None,
               "channel": "voice", "satellite": satellite, "request_ids": [], "tools": [], "uid": uid}
        self.requests.append(req)
        self._uid_req[uid] = req
        speech_s = 0.35 * len(text.split()) + 0.3
        self.pipeline.speak(Utterance(uid, satellite, named, ev.score if named else None, text, hyp=text,
                                      speech_s=speech_s), at=self.sim.now)

    def handle(self, text, person=None, confidence=None, channel="voice", satellite=None, agent=None):
        if re.search(r"\bwhat did you send (out|outside)\b|\bwhat left the house\b", (text or "").lower()):
            req = {"t": round(self.sim.now, 3), "text": text, "person": person, "confidence": confidence,
                   "channel": channel, "satellite": satellite, "request_ids": [], "tools": []}
            self.requests.append(req)
            rep = self.gate.day_report(int(self.sim.now // DAY))
            req["answer"] = egress_answer.short(rep)
            req["answer_coverage"] = egress_answer.coverage(req["answer"], rep)
            if satellite:
                self.speak(satellite, req["answer"])
            else:
                self.engine._notify([person], req["answer"])
            return req
        return super().handle(text, person=person, confidence=confidence, channel=channel, satellite=satellite,
                              agent=agent)


def schedule_full_day(house):
    """The B01 day (islab/suites/day.py) plus two B08 steps: at 02:14:10 the front camera sees what looks
    like a person while the house is armed; Anna, who is away, answers the camera ask after 45 s."""
    schedule_day(house)
    house.sim.at(T("02:14:10"), lambda: (house.sim.trace("scene_step", actor="intruder",
                                                         what="The front camera sees what looks like a person"),
                                         house.home["camera.front"].detect("person", 0.81)))
    house.probes = {}

    def lock_probe():
        house.probes["front_lock_at_19_05"] = house.home["lock.front"].state.get("state")
    house.sim.at(T("19:05"), lock_probe)
    house.phones.when("anna", "share this clip with the monitoring centre", lambda rec: house.phones.reply(
        "anna", rec["request_id"], "yes"), delay=45, kind="ask", after=T("02:00"))
    return house


def run_full_day(seed=0, faults="none", fixes=ALL_FIXES, update=None, until=DAY, fast_bus=True):
    h = FullHouse(seed=seed, faults=faults, fixes=fixes, update=update, fast_bus=fast_bus)
    schedule_full_day(h)
    h.run(until=until)
    return h
