"""One seeded leak in the twin under one B04 condition, and what to measure from it.

>>> r = run_leak(seed=1, design="state_machine", condition="normal")
>>> r["metrics"]["confirmed_s"] is not None
True
"""
from __future__ import annotations

from islab.alarm.path import MonitoringCentre, WanLink
from islab.core.bus import Bus
from islab.core.sim import Sim
from islab.twin import build_home
from .devices import FlowSensor, GroundTruth
from .explain import check, explain
from .hub import ACCOUNT, KEY, SafetyHub
from .notify import Phone, PushRelay

HORIZON_S = 360.0
OUTAGE = (-30.0, 270.0)              # internet_down: relative to the leak
STATE_LOSS_S = 15.0                  # state_report_lost: valve state reports dropped this long
CMD_DROP = 0.5                       # command_lost: each cmd/valve.main copy dropped with this p
REBOOT_AT = (0.0, 10.0)              # hub_reboot: crash this long after the leak (uniform)
REBOOT_DOWN = (20.0, 40.0)           # ... and stay down this long (uniform)

CONDITIONS = {
    "normal": "no fault",
    "valve_stuck": "valve acks every command and never moves",
    "valve_partial": "valve stops at 40% open",
    "command_lost": f"each copy of cmd/valve.main dropped with p={CMD_DROP}",
    "state_report_lost": f"every state/valve.main report dropped for {STATE_LOSS_S:g} s after the leak",
    "hub_reboot": (f"hub crashes {REBOOT_AT[0]:g}-{REBOOT_AT[1]:g} s after the leak, down "
                   f"{REBOOT_DOWN[0]:g}-{REBOOT_DOWN[1]:g} s; unsynced journal records lost"),
    "internet_down": f"internet down from {OUTAGE[0]:g} s to +{OUTAGE[1]:g} s around the leak",
    "leak_event_lost": "event/leak.sink dropped; the state copy (leak: true) still arrives",
}
CLAIM_KINDS = ("claimed_closed", "closed_confirmed")


def run_leak(seed, design, condition, persist_journal=True, controller_kw=None, keep_records=True):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed)
    valve, leak = home["valve.main"], home["leak.sink"]
    flow = FlowSensor(sim, bus, valve, leak)
    truth = GroundTruth(sim, valve)
    t_leak = 60.0 + sim.rng.random() * 60.0
    phones = [Phone(sim, bus, "anna", home=True), Phone(sim, bus, "mark", home=False)]
    relay = PushRelay(sim, phones)
    outages = [(t_leak + OUTAGE[0], t_leak + OUTAGE[1])] if condition == "internet_down" else []
    wan = WanLink(sim, outages=outages)
    centre = MonitoringCentre(sim, wan, {ACCOUNT: KEY})
    hub = SafetyHub(sim, bus, wan, centre, relay, home.internet, design=design,
                    persist_journal=persist_journal, controller_kw=controller_kw)

    def internet(up):
        home.internet.set(up)
        hub.set_internet(up)
    for a, b in outages:
        sim.at(a, internet, False)
        sim.at(b, internet, True)

    f = home.faults
    if condition == "valve_stuck":
        f.stuck("valve.main")
    elif condition == "valve_partial":
        f.partial("valve.main", 40)
    elif condition == "command_lost":
        f.link("cmd/valve.main", drop=CMD_DROP)
    elif condition == "state_report_lost":
        hook = {}
        sim.at(t_leak, lambda: hook.setdefault("h", f.link("state/valve.main", drop=1.0)))
        sim.at(t_leak + STATE_LOSS_S, lambda: f.remove_link(hook["h"]))
    elif condition == "hub_reboot":
        t_r = t_leak + sim.rng.uniform(*REBOOT_AT)
        down = sim.rng.uniform(*REBOOT_DOWN)
        sim.at(t_r, hub.reboot, down)
    elif condition == "leak_event_lost":
        f.link("event/leak.sink", drop=1.0)
    elif condition not in ("normal", "internet_down"):
        raise ValueError(condition)

    sim.at(t_leak, leak.set_leak, True)
    sim.run(until=t_leak + HORIZON_S)
    records = hub.journal.records
    m = measure(t_leak, records, truth, phones, centre, hub)
    out = {"seed": seed, "design": design, "condition": condition, "t_leak": t_leak, "metrics": m}
    if keep_records:
        out.update(records=records, hub=hub, truth=truth, phones=phones, centre=centre, flow=flow)
    return out


def _rel(t, t0):
    return None if t is None else round(t - t0, 3)


def measure(t_leak, records, truth, phones, centre, hub):
    first = lambda kinds, tag=None: next((r for r in records if r["kind"] in kinds  # noqa: E731
                                          and (tag is None or r.get("tag") == tag)), None)
    claim = first(CLAIM_KINDS)
    confirmed = first(("closed_confirmed",))
    esc = first(("told",), "escalated")
    t_true = truth.first_closed(t_leak)
    m = {
        "true_closed_s": _rel(t_true, t_leak),
        "claim_s": _rel(claim["t"], t_leak) if claim else None,
        "claim_kind": claim["kind"] if claim else None,
        "confirmed_s": _rel(confirmed["t"], t_leak) if confirmed else None,
        "escalated_s": _rel(esc["t"], t_leak) if esc else None,
        "open_at_60s": truth.position_at(t_leak + 60) > 0,
    }
    if claim:
        m["false_claim_strict"] = truth.position_at(claim["t"]) > 0
        m["false_claim_30s"] = truth.position_at(claim["t"] + 30) > 0
    else:
        m["false_claim_strict"] = m["false_claim_30s"] = False
    # people
    told = {}
    misled = False
    for ph in phones:
        rx = [x for x in ph.received]
        told[ph.person] = _rel(rx[0]["t"], t_leak) if rx else None
        upto = [x for x in rx if x["t"] <= t_leak + 60]
        if upto and upto[-1]["tag"] in ("claimed_closed", "closed") and m["open_at_60s"]:
            misled = True
    m["told_s"] = told
    m["anyone_told_60"] = any(v is not None and v <= 60 for v in told.values())
    m["everyone_told_60"] = all(v is not None and v <= 60 for v in told.values())
    m["misled_at_60"] = misled
    # alarm path
    def receipt(zone):
        for r in centre.receipts:
            if r["token"] != "NULL" and r["event"].endswith(f"WA{zone:03d}"):
                return _rel(r["t"], t_leak)
        return None
    m["leak_alarm_s"] = receipt(2)
    m["leak_alarm_receipts"] = sum(1 for r in centre.receipts
                                   if r["token"] != "NULL" and r["event"].endswith("WA002"))
    m["valve_alarm_s"] = receipt(12)
    m["dispatch_decisions"] = len(centre.dispatches)
    # explanation: faithful to the records, and true against the twin
    stmts = explain(records)
    chk = check(stmts, records)
    by_id = {r["rid"]: r for r in records}
    phys, phys_true = 0, 0
    for s in stmts:
        recs = [by_id[x] for x in s["refs"] if x in by_id]
        for r in recs:
            if r["kind"] in CLAIM_KINDS:
                phys += 1
                phys_true += truth.position_at(r["t"]) == 0
    m["explain_statements"] = chk["statements"]
    m["explain_supported"] = chk["supported"]
    m["explain_physical"] = phys
    m["explain_physical_true"] = phys_true
    # what left the home
    left = {}
    for g in hub.gates:
        for r in g.sent:
            left[r["payload_class"]] = left.get(r["payload_class"], 0) + 1
    m["egress"] = left
    m["egress_blocked"] = sum(len(g.blocked) for g in hub.gates)
    m["egress_violations"] = sum(len(g.violations) for g in hub.gates)
    m["reboots"] = len(hub.reboots)
    m["journal_lost_on_crash"] = sum(j.lost_on_crash for j in hub.journals)
    return m
