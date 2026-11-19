"""Alarm path experiment: 500 seeded alarm events per WAN condition, plus the pysiaalarm
interoperability matrix. Writes results.json next to this file.

    python experiments/alarm_path/run.py            # full run
    python experiments/alarm_path/run.py --no-interop
"""
import json
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from islab.core.sim import Sim                       # noqa: E402
from islab.core.bus import Bus, Envelope             # noqa: E402
from islab.alarm import dc09                         # noqa: E402
from islab.alarm.path import AlarmHub, LocalSiren, MonitoringCentre, WanLink   # noqa: E402
from islab.alarm.egress import EgressGate            # noqa: E402

N = 500
KEY = b"0123456789ABCDEF"
HUB = dict(ack_timeout=5.0, backoff_base=1.0, backoff_cap=16.0, heartbeat_s=60.0)
LATENCY = (0.05, 0.25)
CONDITIONS = {
    "normal":             dict(loss=0.0),
    "loss_2pct":          dict(loss=0.02),
    "loss_20pct":         dict(loss=0.20),
    # event lands at a uniformly random point inside a 60 s outage
    "outage_60s":         dict(loss=0.0, outage="random_inside"),
    # worst case: the event happens the instant a 60 s outage starts
    "outage_60s_at_start": dict(loss=0.0, outage="at_start"),
    # sensitivity: same random-inside outage with a tighter backoff cap
    "outage_60s_backoff_cap_4s": dict(loss=0.0, outage="random_inside", hub={"backoff_cap": 4.0}),
    "loss_20pct_backoff_cap_4s": dict(loss=0.20, hub={"backoff_cap": 4.0}),
}
DEVICES = [("smoke.hall", "smoke"), ("leak.sink", "leak"), ("lock.front", "forced_entry")]


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 3)


def trial(seed, cond):
    sim = Sim(seed=seed)
    t_ev = 120 + sim.rng.random() * 60          # after a couple of heartbeats
    outages = []
    if cond.get("outage") == "random_inside":
        start = t_ev - sim.rng.random() * 60
        outages = [(start, start + 60)]
    elif cond.get("outage") == "at_start":
        outages = [(t_ev, t_ev + 60)]
    bus = Bus(sim)
    link = WanLink(sim, latency=LATENCY, loss=cond["loss"], outages=outages)
    centre = MonitoringCentre(sim, link, {"1A2B3C": KEY})
    gate = EgressGate(sim, bus)
    hub = AlarmHub(sim, bus, link, centre, account="1A2B3C", key=KEY, egress=gate,
                   **dict(HUB, **cond.get("hub", {})))
    siren = LocalSiren(sim, bus)
    dev, typ = DEVICES[seed % len(DEVICES)]
    sim.at(t_ev, lambda: bus.publish(Envelope(topic=f"event/{dev}", payload={"type": typ},
                                              source=dev, ts=sim.now)))
    sim.run(until=t_ev + 900)
    alarm_receipts = [r for r in centre.receipts if r["token"] != dc09.NULL]
    rec = hub.alarms[0] if hub.alarms else None
    t_recv = alarm_receipts[0]["t"] if alarm_receipts else None
    dups = sum(1 for _, tok, _ in centre.duplicate_log if tok != dc09.NULL)
    return {
        "t_event": t_ev,
        "receipt_latency": (t_recv - t_ev) if t_recv is not None else None,
        "ack_latency": (rec["t_ack"] - t_ev) if rec and rec["t_ack"] else None,
        "attempts": rec["attempts"] if rec else 0,
        "duplicates_at_centre": dups,
        "dispatch_decisions": len(centre.dispatches),
        "siren_latency": (siren.activations[0][0] - t_ev) if siren.activations else None,
        "siren_expected": dev in ("smoke.hall", "leak.sink"),
    }


def run_condition(name, cond):
    rows = [trial(10_000 + i, cond) for i in range(N)]
    lat = [r["receipt_latency"] for r in rows if r["receipt_latency"] is not None]
    ack = [r["ack_latency"] for r in rows if r["ack_latency"] is not None]
    siren = [r["siren_latency"] for r in rows if r["siren_expected"]]
    return {
        "events": N,
        "delivered": len(lat),
        "delivered_pct": round(100 * len(lat) / N, 2),
        "receipt_latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95), "p99": pct(lat, .99),
                              "max": round(max(lat), 3) if lat else None},
        "ack_latency_s": {"p50": pct(ack, .5), "p95": pct(ack, .95)},
        "delivered_within_60s_pct": round(100 * sum(1 for x in lat if x <= 60) / N, 2),
        "mean_transmissions_per_alarm": round(sum(r["attempts"] for r in rows) / N, 3),
        "duplicates_received_at_centre": sum(r["duplicates_at_centre"] for r in rows),
        "duplicates_suppressed": sum(r["duplicates_at_centre"] for r in rows),
        "dispatch_decisions_per_event": sorted({r["dispatch_decisions"] for r in rows}),
        "local_siren_latency_s_max": round(max(siren), 3) if siren else None,
        "local_siren_sounded_pct": round(100 * sum(1 for x in siren if x is not None) / len(siren), 2),
    }


def main():
    results = {"conditions": {n: run_condition(n, c) for n, c in CONDITIONS.items()}}
    if "--no-interop" not in sys.argv:
        from islab.alarm.interop import run_matrix
        m = run_matrix()
        compact = {}
        for tr, rows in m.get("transports", {}).items():
            compact[tr] = {r["case"]: {k: r.get(k) for k in
                                       ("expected", "response", "callback_code", "callback_codes",
                                        "callback_fired", "response_header_nonstandard",
                                        "response_decrypted", "pass", "raw_response")
                                       if k in r} for r in rows}
        results["pysiaalarm_interop"] = {"version": m.get("pysiaalarm_version"),
                                         "error": m.get("error"), "matrix": compact,
                                         "summary": {tr: f"{sum(v['pass'] for v in rows.values())}/{len(rows)} pass"
                                                     for tr, rows in compact.items()}}
    out = {
        "experiment": "alarm_path",
        "date": date.today().isoformat(),
        "method": ("Discrete-event twin (islab.core.sim). Per condition, 500 independent seeded "
                   "trials (seeds 10000-10499): one detector event (smoke/leak/forced entry in "
                   "rotation) at t=120-180 s; AlarmHub encodes AES-128 encrypted ADM-CID DC-09, "
                   "sends through EgressGate (purpose=alarm) over a simulated WAN "
                   f"(latency U{LATENCY} s each way, loss applied independently each direction "
                   "to frames and ACKs) to a simulated MonitoringCentre that decodes, checks "
                   "CRC/length/timestamp band (-40/+20 s), de-duplicates by "
                   "(account, sequence, token, event) and ACKs. Hub retries the same sequence "
                   "number with a fresh timestamp after an ACK timeout plus exponential backoff "
                   "with jitter. Receipt latency = first centre receipt minus event time. "
                   "Interop: real localhost sockets against pysiaalarm receivers "
                   "(aio TCP, aio UDP = what Home Assistant uses, and sync TCP)."),
        "params": {"trials_per_condition": N, "hub": HUB, "wan_latency_s": LATENCY,
                   "conditions": CONDITIONS, "format": "ADM-CID, AES-128",
                   "centre_timeband_s": [40, 20], "run_horizon_after_event_s": 900},
        "results": results,
        "limitations": [
            "WAN model is i.i.d. loss + fixed outage windows; no bursty loss, no TCP connection "
            "setup/teardown, no NAT or cellular backup path.",
            "Monitoring centre is a simulation using our own decoder; the pysiaalarm matrix is "
            "the only independent check, and pysiaalarm is a home-automation receiver, not a "
            "certified alarm receiving centre (ARC) receiver.",
            "DC-09 standard text was not consulted (paywalled); framing follows pysiaalarm code and "
            "public descriptions. Certification-level conformance is NOT claimed.",
            "One alarm per trial; no queueing of many simultaneous alarms or sequence-number "
            "wrap under load.",
            "Heartbeats run every 60 s but are not retried; supervision timing was tested in "
            "unit tests, not in this experiment.",
            "Ack timeout/backoff values are our choices, not taken from the standard.",
        ],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps(results["conditions"], indent=1))
    if "pysiaalarm_interop" in results:
        print(results["pysiaalarm_interop"]["summary"])


if __name__ == "__main__":
    main()
