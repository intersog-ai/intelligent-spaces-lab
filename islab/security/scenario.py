"""Seeded B08 runs: one alarm through one or two paths, one day of cellular supervision,
one week of front-camera observations. Virtual time only; no real network, no footage.
"""
from __future__ import annotations

import math
import random
import re

from islab.alarm import dc09
from islab.alarm.path import AlarmHub, WanLink
from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim

from .camera import (CameraObserver, DetectorModel, ObservingCamera, PersonModel, VERDICT_WORDS)
from .cellular import CellularLink
from .dualpath import CELLULAR, DualPathAlarmHub, DualPathCentre, DualPathEgressGate

ACCOUNT, KEY = "1A2B3C", b"0123456789ABCDEF"
T0 = 600.0                      # warm-up: primary and cellular heartbeats established
HUB = dict(ack_timeout=5.0, backoff_base=1.0, backoff_cap=16.0, heartbeat_s=60.0,
           fmt=dc09.SIA_DCS)    # SIA-DCS after the B01 finding on the Contact ID leak code
DESIGNS = ("one_path", "two_paths")
KINDS = {"smoke": ("smoke.hall", "smoke", "smoke"),
         "leak": ("leak.sink", "leak", "leak"),
         "intrusion": ("lock.front", "forced_entry", None)}
CONDITIONS = {
    "normal": {},
    "loss_20pct": {"loss": 0.20},
    "internet_down_10min": {"down": 600.0},
    "internet_down_all_day": {"down": 86400.0},
    "internet_down_10min_cell_degraded": {"down": 600.0, "cell_degraded": True},
    "internet_and_cell_down_10min": {"down": 600.0, "cell_down": True},
    "hub_reboot": {"reboot": True},
}
DESCRIBE = {
    "normal": "home internet up; alarm 0-600 s after warm-up",
    "loss_20pct": "20% of messages lost each way on the home internet path",
    "internet_down_10min": "home internet down 600 s; alarm at a uniform time inside the outage",
    "internet_down_all_day": "home internet down 86,400 s; alarm at a uniform time inside it",
    "internet_down_10min_cell_degraded": "internet down 600 s and the cell degraded "
                                         "(30% loss, latency x4, 10% late tail)",
    "internet_and_cell_down_10min": "internet and cellular both down for the same 600 s",
    "hub_reboot": "home internet up; hub crashes 0.5-4 s after the alarm, back after 20-40 s; "
                  "the detector's next state report 0-10 s later (the B04 duplicate)",
}


def _wire(sim, bus, design, cond, outages):
    link = WanLink(sim, loss=cond.get("loss", 0.0), outages=outages)
    centre = DualPathCentre(sim, link, {ACCOUNT: KEY})
    gate = DualPathEgressGate(sim, bus)
    cell = None
    if design == "two_paths":
        cell = CellularLink(sim, outages=outages if cond.get("cell_down") else (),
                            degraded=bool(cond.get("cell_degraded")))

    def make_hub():
        if cell is None:
            return AlarmHub(sim, bus, link, centre, account=ACCOUNT, key=KEY, egress=gate, **HUB)
        return DualPathAlarmHub(sim, bus, link, centre, cell_link=cell, account=ACCOUNT, key=KEY,
                                egress=gate, **HUB)
    return link, centre, gate, cell, make_hub


def run_alarm(seed, condition, design, kind):
    cond = CONDITIONS[condition]
    sim = Sim(seed=seed)
    bus = Bus(sim)
    srng = random.Random(seed * 7919 + 1)   # event times: the same for both designs
    down = cond.get("down")
    t_ev = T0 + srng.random() * (down if down else 600.0)
    outages = [(T0, T0 + down)] if down else []
    link, centre, gate, cell, make_hub = _wire(sim, bus, design, cond, outages)
    hubs = [make_hub()]
    told = []
    bus.subscribe("notify/owner", lambda e: told.append((sim.now, e.payload.get("text"))), "phone")
    for a, b in outages:
        sim.at(a, gate.set_internet, False)
        sim.at(b, gate.set_internet, True)
    dev, typ, attr = KINDS[kind]
    sim.at(t_ev, lambda: bus.publish(Envelope(topic=f"event/{dev}", payload={"type": typ},
                                              source=dev, ts=sim.now)))
    if attr:   # the detector keeps reporting its state: the alarm condition is still true
        sim.at(t_ev, lambda: bus.publish(Envelope(topic=f"state/{dev}", payload={attr: True},
                                                  source=dev, ts=sim.now)))
    if cond.get("reboot"):
        t_crash = t_ev + 0.5 + 3.5 * srng.random()
        t_back = t_crash + 20 + 20 * srng.random()
        t_state = t_back + 10 * srng.random()
        sim.at(t_crash, lambda: hubs[-1].crash())
        sim.at(t_back, lambda: hubs.append(make_hub()))
        if attr:
            sim.at(t_state, lambda: bus.publish(Envelope(topic=f"state/{dev}", payload={attr: True},
                                                         source=dev, ts=sim.now)))
    end = max(t_ev, T0 + (down or 0)) + 900.0
    sim.run(until=end)

    alarms = [r for r in centre.receipts if r["token"] != dc09.NULL]
    t_first = min((r["t"] for r in alarms), default=None)
    first = min(alarms, key=lambda r: r["t"]) if alarms else None
    dup_copies = sum(1 for a in centre.arrivals if not a[5] and a[2] != dc09.NULL)
    cell_alarm_msgs = sum(1 for r in gate.sent if r["meta"].get("route") == CELLULAR
                          and r["payload_class"] == "alarm_event")
    lat = (t_first - t_ev) if t_first is not None else None
    t_told = next((t for t, txt in told if "received by the monitoring centre" in txt), None)
    return {
        "latency_s": lat,
        "within_60s": lat is not None and lat <= 60.0,
        "first_path": first.get("path") if first else None,
        "duplicate_copies": dup_copies,
        "incidents": len(centre.incidents),
        "message_level_incidents": centre.message_level_decisions,
        "centre_action": centre.incidents[0]["action"] if centre.incidents else None,
        "cell_alarm_messages": cell_alarm_msgs,
        "cell_messages": cell.stats["sent"] if cell else 0,
        "cell_bytes": cell.stats["bytes"] if cell else 0,
        "egress_destinations": sorted({r["dest"] for r in gate.sent}),
        "told": [txt for _, txt in told],
        "told_received_s": (t_told - t_ev) if t_told is not None else None,
        "all_paths_lost_at_centre": len(centre.all_paths_lost),
        "transmissions": sum(h.alarms[0]["attempts"] for h in hubs if h.alarms),
    }


DAY_CONDITIONS = {
    "normal": {},
    "loss_20pct": {"loss": 0.20},
    "internet_down_all_day": {"down": 86400.0},
    "internet_down_all_day_cell_degraded": {"down": 86400.0, "cell_degraded": True},
}


def run_day(seed, condition, alarms=("smoke", "leak", "intrusion")):
    """One 24 h day after warm-up, two paths, with one alarm of each kind at random times.
    Returns what went over cellular that day and what the household was told."""
    cond = DAY_CONDITIONS[condition]
    sim = Sim(seed=seed)
    bus = Bus(sim)
    srng = random.Random(seed * 7919 + 1)
    down = cond.get("down")
    outages = [(T0, T0 + down)] if down else []
    link, centre, gate, cell, make_hub = _wire(sim, bus, "two_paths", cond, outages)
    hub = make_hub()
    told = []
    bus.subscribe("notify/owner", lambda e: told.append((sim.now, e.payload.get("text"))), "phone")
    for a, b in outages:
        sim.at(a, gate.set_internet, False)
        sim.at(b, gate.set_internet, True)
    for k in alarms:
        dev, typ, attr = KINDS[k]
        t = T0 + srng.random() * 86000.0
        sim.at(t, lambda dev=dev, typ=typ: bus.publish(Envelope(
            topic=f"event/{dev}", payload={"type": typ}, source=dev, ts=sim.now)))
        if attr:   # restore an hour later so the next day's alarm is a new incident
            sim.at(t + 3600, lambda dev=dev, typ=typ: bus.publish(Envelope(
                topic=f"event/{dev}", payload={"type": typ + "_clear"}, source=dev, ts=sim.now)))
    sim.run(until=T0 + 86400.0)
    lo, hi = T0, T0 + 86400.0
    n, b = cell.bytes_between(lo, hi)
    routes = gate.by_route(lo, hi)
    acked = [a for a in hub.alarms if not a["restore"]]
    return {
        "cell_messages_billed": n,
        "cell_bytes_billed": b,
        "cell_usd": round(cell.cost_usd(b), 4),
        "cell_uplink_by_class": routes.get(CELLULAR, {}).get("classes", {}),
        "primary_failovers": sum(1 for _, p, s in hub.path_log if p == "primary" and s == "failed"),
        "cell_failures": sum(1 for _, p, s in hub.path_log if p == CELLULAR and s == "failed"),
        "path_notifications": sum(1 for _, t in told if "path" in t or "monitoring centre can" in t),
        "told": [t for _, t in told],
        "alarms_acked": sum(1 for a in acked if a["t_ack"] is not None),
        "alarms": len(acked),
        "incidents": len(centre.incidents),
        "centre_path_faults": len(centre.path_faults),
        "all_paths_lost_at_centre": len(centre.all_paths_lost),
        "camera_items_out": sum(1 for r in gate.sent if r["payload_class"] == "camera_clip"),
    }


# ------------------------------------------------------------------------- camera week
# Scenes at the front door, per day: (truth, mean count, (from hour, to hour)). ASSUMED household.
SCENES = [("person", 6.0, (7, 22)),        # residents coming and going
          ("person", 3.0, (8, 21)),        # visitors, neighbours on the path
          ("courier", 1.5, (9, 18)),       # a person who leaves a parcel
          ("vehicle", 6.0, (6, 23)),
          ("pet", 6.0, (0, 24)),
          ("shadow", 15.0, (0, 24))]       # headlights, foliage, sun and cloud
INTRUDERS_PER_WEEK = 1                     # an unexpected person while armed, as a test case


def armed_state(t):
    day, h = int(t // 86400) % 7, (t % 86400) / 3600
    if h >= 23 or h < 6.5:
        return "set to night"
    if day < 5 and 8.5 <= h < 17.5:
        return "set to away"
    return "off"


def _poisson(rng, lam):
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p < L:
            return k
        k += 1


def run_camera_week(seed, design, threshold=0.7, floor=0.5):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    cam = ObservingCamera(sim, bus, "camera.front")
    # scenes, detector and people draw from their own streams, so every design sees the same week
    srng = random.Random(seed * 7919 + 1)
    det = DetectorModel(srng)
    link = WanLink(sim)
    centre = DualPathCentre(sim, link, {ACCOUNT: KEY})
    gate = DualPathEgressGate(sim, bus)
    hub = AlarmHub(sim, bus, link, centre, account=ACCOUNT, key=KEY, egress=gate,
                   **dict(HUB, heartbeat_s=0))
    obs = CameraObserver(sim, bus, design=design, threshold=threshold, floor=floor,
                         alarm_hub=hub, gate=gate, centre=centre, armed=armed_state)
    truth = {}                     # clip_ref -> truth
    clip_re = re.compile(r"\((clip/[^)]+)\)")

    def truth_of(text):
        m = clip_re.search(text or "")
        return truth.get(m[1]) if m else None
    people = [PersonModel(sim, bus, p, truth_of, rng=random.Random(seed * 104729 + i))
              for i, p in enumerate(obs.persons)]
    scenes = []                    # (t, truth)
    for d in range(7):
        for kind, lam, (a, b) in SCENES:
            for _ in range(_poisson(srng, lam)):
                scenes.append((d * 86400 + (a + (b - a) * srng.random()) * 3600, kind))
    for _ in range(INTRUDERS_PER_WEEK):
        while True:
            t = srng.random() * 7 * 86400
            if armed_state(t) != "off":
                break
        scenes.append((t, "intruder"))
    scenes.sort()
    outcomes = []                  # per scene: (t, truth, label, conf, clip)

    def show(t, kind):
        seen_as = {"courier": "person", "intruder": "person"}.get(kind, kind)
        r = det.report(seen_as)
        if kind == "courier" and r is not None:
            # the parcel itself, a separate observation
            cam.detect("parcel", round(min(0.99, max(0.3, srng.gauss(0.80, 0.10))), 2))
        if r is None:
            outcomes.append((t, kind, None, None, None))
            return
        label, conf = r
        clip = cam.detect(label, conf)
        truth[clip] = kind
        outcomes.append((t, kind, label, conf, clip))
    for t, kind in scenes:
        sim.at(t, show, t, kind)
    sim.run(until=7 * 86400 + 900)

    is_person = {"person", "courier", "intruder"}
    by_clip = {o[4]: o for o in outcomes if o[4]}
    tells = [m for m in obs.messages if m[2] == "tell"]
    asks = [m for m in obs.messages if m[2] == "ask"]
    first_asks = [a for a in obs.asks.values() if a["i"] == 0]
    person_alerts = tells if design != "ask" else []
    false_alerts = [m for m in person_alerts if truth.get(m[4]) not in is_person]
    false_alerts_armed = [m for m in false_alerts if armed_state(m[0]) != "off"]
    alerts_armed = [m for m in person_alerts if armed_state(m[0]) != "off"]
    false_asks = [a for a in first_asks if truth.get(a["clip_ref"]) not in is_person]
    signals = obs.signals_to_centre
    false_signals = [s for s in signals if truth.get(s[1]) != "intruder"]
    intr = [o for o in outcomes if o[1] == "intruder"]
    intr_res = []
    for t, _, label, conf, clip in intr:
        asked = [a for a in obs.asks.values() if a["clip_ref"] == clip and clip]
        sig = [s for s in signals if s[1] == clip and clip]
        msg = [m for m in obs.messages if m[4] == clip and clip]
        intr_res.append({"detected_as": label, "confidence": conf,
                         "owner_messaged_within_60s": any(m[0] - t <= 60 for m in msg),
                         "asked_within_60s": any(a["t"] - t <= 60 for a in asked),
                         "signal_to_centre_s": (sig[0][0] - t) if sig else None,
                         "outcome": asked[-1]["outcome"] if asked else None})
    clips_out = [r for r in gate.sent if r["payload_class"] == "camera_clip"]
    without_consent = [r for r in clips_out
                       if gate.consents.get(r["meta"].get("consent_id"), {}).get("clip_ref")
                       != r["meta"].get("clip_ref")]
    texts = [m[3] for m in obs.messages]
    verdict_word_msgs = sum(1 for t in texts if any(w in t.lower() for w in VERDICT_WORDS))
    misses = [o for o in outcomes if o[1] in is_person and not any(
        m[4] == o[4] for m in person_alerts)] if design != "ask" else []
    return {
        "scenes": len(outcomes),
        "scenes_by_truth": {k: sum(1 for o in outcomes if o[1] == k)
                            for k in ("person", "courier", "vehicle", "pet", "shadow", "intruder")},
        "person_labels": sum(1 for o in outcomes if o[2] == "person"),
        "false_person_labels": sum(1 for o in outcomes if o[2] == "person" and o[1] not in is_person),
        "person_alerts": len(person_alerts),
        "false_person_alerts": len(false_alerts),
        "person_alerts_while_armed": len(alerts_armed),
        "false_person_alerts_while_armed": len(false_alerts_armed),
        "real_persons_not_alerted": len(misses),
        "asks": len(asks),
        "first_asks": len(first_asks),
        "false_person_asks": len(false_asks),
        "ask_outcomes": _count([a["outcome"] for a in first_asks]),
        "signals_to_centre": len(signals),
        "false_signals_to_centre": len(false_signals),
        "centre_incidents": len(centre.incidents),
        "clips_left_house": len(clips_out),
        "clips_left_without_consent": len(without_consent),
        "messages_with_verdict_words": verdict_word_msgs,
        "intruder_tests": intr_res,
        "example_ask": asks[0][3] if asks else None,
        "example_tell": tells[0][3] if tells else None,
        "answers": _count([a["answer"] for a in obs.asks.values()]),
        "egress_classes": _count([r["payload_class"] for r in gate.sent]),
        "camera_events_published": sum(1 for o in outcomes if o[4]),
        "local_clips_on_camera": len(cam.local_clips),
        "household_messages": sum(len(p.received) for p in people),
        "by_clip": None if by_clip is None else len(by_clip),
    }


def _count(xs):
    out = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out
