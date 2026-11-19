"""B04 "Sent is not closed": leak -> main valve, three controller designs, eight conditions.

For every (condition, design) we run N seeded leaks (seed = trial index, so the three designs see
the same leak time and the same fault draws up to the point their behaviour differs). A run builds
a fresh twin home plus a flow meter, starts a leak 60-120 s in, and runs 360 virtual seconds after it.

Designs
  fire_and_forget  reports "closed" when the close command is sent
  ack_based        reports "closed" on the valve's accepted ack; resends on no ack; tells people
                   to shut off by hand if no ack ever arrives
  state_machine    islab/safety LeakValveController: journal, idempotent resends, confirmation
                   by fresh valve position 0 AND flow ~0, escalation after 30 s
Ablation (hub_reboot only): state_machine whose journal is lost on reboot (memory only).

    python experiments/b04_valve/run.py              # 500 per cell, 2 processes, about 1-2 min
    python experiments/b04_valve/run.py --trials 50  # quick

Writes results.json next to this file. Virtual time only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import sys
import time
from multiprocessing import Pool

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from islab.safety import scenario as sc  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DESIGNS = ["fire_and_forget", "ack_based", "state_machine"]
ABLATION = ("hub_reboot", "state_machine_no_journal")


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 2)


def share(k, n):
    return round(100.0 * k / n, 1) if n else None


def one(job):
    seed, design, cond = job
    if design == "state_machine_no_journal":
        r = sc.run_leak(seed, "state_machine", cond, persist_journal=False, keep_records=False)
    else:
        r = sc.run_leak(seed, design, cond, keep_records=False)
    m = r["metrics"]
    # unnecessary escalation: people told to shut off by hand while the valve was in fact closed
    m["escalated_while_closed"] = (m["escalated_s"] is not None and m["true_closed_s"] is not None
                                   and m["true_closed_s"] <= m["escalated_s"])
    return design, cond, m


def summarize(ms):
    n = len(ms)
    conf = [m["confirmed_s"] for m in ms if m["confirmed_s"] is not None]
    true_c = [m["true_closed_s"] for m in ms if m["true_closed_s"] is not None]
    claims = [m for m in ms if m["claim_s"] is not None]
    failing = [m for m in ms if m["open_at_60s"]]
    esc_fail = [m["escalated_s"] for m in failing if m["escalated_s"] is not None and m["escalated_s"] <= 60]
    esc_all = [m["escalated_s"] for m in ms if m["escalated_s"] is not None]
    leak_al = [m["leak_alarm_s"] for m in ms if m["leak_alarm_s"] is not None]
    v_al = [(m["valve_alarm_s"] - m["escalated_s"]) for m in ms
            if m["escalated_s"] is not None and m["valve_alarm_s"] is not None]
    st = sum(m["explain_statements"] for m in ms)
    sup = sum(m["explain_supported"] for m in ms)
    ph = sum(m["explain_physical"] for m in ms)
    pht = sum(m["explain_physical_true"] for m in ms)
    egress = {}
    for m in ms:
        for k, v in m["egress"].items():
            egress[k] = egress.get(k, 0) + v
    return {
        "runs": n,
        "valve_really_closed_pct": share(len(true_c), n),
        "valve_really_closed_s_p50": pct(true_c, 0.5),
        "confirmed_close_pct": share(len(conf), n),
        "time_to_confirmed_close_s": {"p50": pct(conf, 0.5), "p95": pct(conf, 0.95), "max": pct(conf, 1.0)},
        "closed_claims_pct": share(len(claims), n),
        "false_closed_claims_pct": share(sum(m["false_claim_30s"] for m in ms), n),
        "closed_claims_made_while_water_flowed_pct": share(sum(m["false_claim_strict"] for m in ms), n),
        "runs_valve_open_at_60s": len(failing),
        "of_those_escalated_within_60s_pct": share(len(esc_fail), len(failing)),
        "time_to_escalation_when_failing_s": {"p50": pct(esc_fail, 0.5), "p95": pct(esc_fail, 0.95)},
        "escalations_pct": share(len(esc_all), n),
        "escalations_while_valve_was_closed_pct": share(sum(m["escalated_while_closed"] for m in ms), n),
        "anyone_told_within_60s_pct": share(sum(m["anyone_told_60"] for m in ms), n),
        "everyone_told_within_60s_pct": share(sum(m["everyone_told_60"] for m in ms), n),
        "misled_at_60s_pct": share(sum(m["misled_at_60"] for m in ms), n),
        "leak_alarm_within_60s_pct": share(sum(1 for x in leak_al if x <= 60), n),
        "leak_alarm_delivered_by_end_pct": share(len(leak_al), n),
        "leak_alarm_s": {"p50": pct(leak_al, 0.5), "p95": pct(leak_al, 0.95)},
        "duplicate_leak_alarms_at_centre_pct": share(sum(m["leak_alarm_receipts"] > 1 for m in ms), n),
        "valve_alarm_within_60s_of_escalation_pct": share(sum(1 for x in v_al if x <= 60), len(esc_all)),
        "explanation_statements": st,
        "explanation_statements_matching_a_record_pct": share(sup, st),
        "explanation_physical_statements": ph,
        "explanation_physical_statements_true_pct": share(pht, ph),
        "left_the_home": egress,
        "egress_blocked": sum(m["egress_blocked"] for m in ms),
        "egress_violations": sum(m["egress_violations"] for m in ms),
        "journal_records_lost_on_crash": sum(m["journal_lost_on_crash"] for m in ms),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=500)
    ap.add_argument("--procs", type=int, default=2)
    ap.add_argument("--out", default=str(HERE / "results.json"))
    a = ap.parse_args()
    jobs = [(s, d, c) for c in sc.CONDITIONS for d in DESIGNS for s in range(a.trials)]
    jobs += [(s, ABLATION[1], ABLATION[0]) for s in range(a.trials)]
    t0 = time.time()
    with Pool(a.procs) as p:
        out = p.map(one, jobs, chunksize=50)
    wall = time.time() - t0
    cells = {}
    for d, c, m in out:
        cells.setdefault(c, {}).setdefault(d, []).append(m)
    results = {c: {d: summarize(ms) for d, ms in by.items()} for c, by in cells.items()}
    doc = {
        "experiment": "b04_valve",
        "date": dt.date.today().isoformat(),
        "method": (
            "Per condition and design, N seeded runs (seed = trial index) of a fresh twin home (15 devices) "
            "plus a simulated flow meter on the main pipe. A leak starts at 60-120 s; the run continues 360 "
            "virtual s after it. Ground truth = the valve's physical position from the device object. "
            "A 'closed' claim is false if the valve is still open 30 s after the claim; "
            "'claims made while water flowed' counts claims with the valve open at the moment of the claim. "
            "Escalation = a plain-words message telling people to shut the water off by hand. "
            "People: Anna at home (LAN), Mark away (push through the egress gate, needs the internet). "
            "Alarms: B01 AlarmHub, SIA-DCS over a simulated WAN to a simulated monitoring centre. "
            "Explanations are generated from each run's journal only and checked automatically: every time, "
            "percentage, flow figure, quoted message and count must match a cited record of the right kind; "
            "statements about the valve being closed (or not) are also checked against the twin's ground truth."),
        "params": {
            "trials_per_cell": a.trials,
            "designs": DESIGNS,
            "ablation": {"condition": ABLATION[0], "design": ABLATION[1]},
            "conditions": sc.CONDITIONS,
            "horizon_after_leak_s": sc.HORIZON_S,
            "valve_travel_s": [4.0, 8.0],
            "flow_meter": {"leak_lpm_valve_open": 6.0, "report_on_change_lpm": 0.3, "heartbeat_s": 10.0},
            "state_machine": {"attempt_timeout_s": 10.0, "escalate_after_s": 30.0, "flow_zero_lpm": 0.2,
                              "flow_fresh_s": 15.0, "flow_wait_s": 15.0, "ack_retry_s": 2.0, "max_resends": 3,
                              "retry_after_escalation_s": 30.0, "max_attempts": 8, "fsync_s": 0.002},
            "ack_based": {"ack_retry_s": 3.0, "max_resends": 2},
            "alarm": {"format": "SIA-DCS", "leak_zone": 2, "valve_zone": 12, "wan_latency_s": [0.05, 0.25]},
            "wall_time_s": round(wall, 1),
        },
        "results": results,
        "limitations": [
            "Simulation only: valve, leak sensor, flow meter, phones, WAN and monitoring centre are models.",
            "The flow meter is an assumed extra device; many homes have none. Without it, confirmation rests on "
            "the valve's own position report.",
            "The valve's travel is modelled as a jump from open to closed (or to 40%) after 4-8 s; no intermediate "
            "positions, no water hammer, no slow seepage.",
            "A new close attempt is used to make the valve report its state again; a real hub would read the "
            "attribute (e.g. Matter ValveConfigurationAndControl CurrentState) instead.",
            "Reboot model: all hub components die together, the journal survives except unsynced records; disk "
            "corruption beyond a torn last line is not modelled.",
            "One leak per run, one valve, one household layout; people's replies and actions are not modelled.",
            "Escalation timing (30 s) and attempt timeout (10 s) are design choices, not tuned to a product.",
            "Not a certified leak-protection system. Real water shut-off must be installed and tested by "
            "qualified people.",
        ],
    }
    pathlib.Path(a.out).write_text(json.dumps(doc, indent=2) + "\n")
    print(f"{len(jobs)} runs in {wall:.1f} s -> {a.out}")


if __name__ == "__main__":
    main()
