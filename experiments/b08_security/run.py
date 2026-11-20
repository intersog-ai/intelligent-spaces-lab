"""B08 "Cameras and the alarm path".

Part A  alarms (smoke, leak, intrusion) through one path (B01 hub, home internet only) or two paths
        (home internet + modelled cellular), under seven conditions. Seed = trial index; the kind
        rotates smoke, leak, intrusion; both designs see the same event times.
Part B  one day per seed with two paths: what went over cellular (messages, bytes, assumed cost),
        failovers, what the household was told.
Part C  one week per seed of front-camera observations (people, couriers, vehicles, pets, shadows,
        one unexpected person while armed) under three designs: threshold alerts, a verdict sent to
        the centre automatically, and evidence + asking a person.

    python experiments/b08_security/run.py            # full, about 2-4 minutes on 2 cores
    python experiments/b08_security/run.py --quick    # small counts, for a smoke test

Writes results.json next to this file. Virtual time only; no network, no footage.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import sys
import time
from multiprocessing import Pool

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from islab.security import scenario as sc                    # noqa: E402
from islab.security.camera import (CLIP_BYTES, DETECTOR_ASSUMPTIONS,   # noqa: E402
                                   VERDICT_WORDS)
from islab.security.cellular import CELL_ASSUMPTIONS, DEGRADED   # noqa: E402
from islab.security.dualpath import CENTRE_ACTIONS           # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
QUICK = "--quick" in sys.argv
N_ALARM = 30 if QUICK else 300
N_ALARM_ALL_DAY = 12 if QUICK else 60
N_DAYS = 2 if QUICK else 10
N_WEEKS = 4 if QUICK else 26
KIND_ORDER = list(sc.KINDS)
CAMERA_DESIGNS = [("threshold", 0.5), ("threshold", 0.6), ("threshold", 0.7), ("threshold", 0.8),
                  ("threshold", 0.9), ("verdict", 0.7), ("ask", 0.5), ("ask", 0.7)]


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 2)


def share(k, n):
    return round(100.0 * k / n, 1) if n else None


def job_alarm(job):
    seed, cond, design = job
    kind = KIND_ORDER[seed % 3]
    return cond, design, kind, sc.run_alarm(seed, cond, design, kind)


def job_day(job):
    seed, cond = job
    return cond, sc.run_day(seed, cond)


def job_week(job):
    seed, design, th = job
    return design, th, sc.run_camera_week(seed, design, threshold=th, floor=th if design == "ask" else 0.5)


def summarize_alarms(rows):
    n = len(rows)
    lat = [r["latency_s"] for r in rows if r["latency_s"] is not None]
    by_kind = {}
    for k in KIND_ORDER:
        rk = [r for r in rows if r["kind"] == k]
        by_kind[k] = {"n": len(rk), "within_60s_pct": share(sum(r["within_60s"] for r in rk), len(rk)),
                      "centre_action": sorted({r["centre_action"] for r in rk if r["centre_action"]})}
    told = {}
    for r in rows:
        for t in r["told"]:
            told[t] = told.get(t, 0) + 1
    rec = [r["told_received_s"] for r in rows if r["told_received_s"] is not None]
    return {
        "alarms": n,
        "within_60s_pct": share(sum(r["within_60s"] for r in rows), n),
        "reached_centre_pct": share(len(lat), n),
        "latency_s": {"p50": pct(lat, .5), "p95": pct(lat, .95), "max": round(max(lat), 2) if lat else None},
        "first_copy_path": {p: sum(1 for r in rows if r["first_path"] == p) for p in ("primary", "cellular")},
        "duplicate_copies_at_centre": sum(r["duplicate_copies"] for r in rows),
        "runs_with_duplicate_copies_pct": share(sum(1 for r in rows if r["duplicate_copies"]), n),
        "extra_incidents_with_incident_dedupe": sum(max(0, r["incidents"] - 1) for r in rows),
        "extra_incidents_message_dedupe_only": sum(max(0, r["message_level_incidents"] - 1) for r in rows),
        "mean_transmissions_per_alarm": round(sum(r["transmissions"] for r in rows) / n, 2),
        "cellular_alarm_messages_mean": round(sum(r["cell_alarm_messages"] for r in rows) / n, 2),
        "person_told_received_s": {"p50": pct(rec, .5), "p95": pct(rec, .95)},
        "all_paths_lost_raised_at_centre_pct": share(sum(1 for r in rows if r["all_paths_lost_at_centre"]), n),
        "egress_destinations": sorted({d for r in rows for d in r["egress_destinations"]}),
        "messages_to_household": dict(sorted(told.items(), key=lambda kv: -kv[1])),
        "by_kind": by_kind,
    }


def summarize_days(rows):
    def stat(key):
        xs = [r[key] for r in rows]
        return {"mean": round(sum(xs) / len(xs), 2), "min": min(xs), "max": max(xs)}
    classes = {}
    for r in rows:
        for c, k in r["cell_uplink_by_class"].items():
            classes[c] = classes.get(c, 0) + k
    told = {}
    for r in rows:
        for t in r["told"]:
            if "received by the monitoring centre" not in t:
                told[t] = told.get(t, 0) + 1
    return {"days": len(rows),
            "cell_messages_billed_per_day": stat("cell_messages_billed"),
            "cell_bytes_billed_per_day": stat("cell_bytes_billed"),
            "cell_usd_per_day": stat("cell_usd"),
            "cell_uplink_by_class_mean_per_day": {c: round(k / len(rows), 1) for c, k in classes.items()},
            "primary_failovers_per_day": stat("primary_failovers"),
            "cell_failures_per_day": stat("cell_failures"),
            "path_messages_to_household_per_day": stat("path_notifications"),
            "alarms_acked": f"{sum(r['alarms_acked'] for r in rows)}/{sum(r['alarms'] for r in rows)}",
            "centre_incidents": sum(r["incidents"] for r in rows),
            "all_paths_lost_at_centre": sum(r["all_paths_lost_at_centre"] for r in rows),
            "camera_items_out": sum(r["camera_items_out"] for r in rows),
            "path_messages_seen": told}


def summarize_weeks(rows):
    w = len(rows)

    def per_week(key):
        return round(sum(r[key] for r in rows) / w, 2)
    intr = [t for r in rows for t in r["intruder_tests"]]
    outcomes = {}
    for r in rows:
        for k, v in r["ask_outcomes"].items():
            outcomes[k] = outcomes.get(k, 0) + v
    keys = ("scenes", "person_labels", "false_person_labels", "person_alerts", "false_person_alerts",
            "person_alerts_while_armed", "false_person_alerts_while_armed", "real_persons_not_alerted",
            "asks", "first_asks", "false_person_asks", "signals_to_centre",
            "false_signals_to_centre", "centre_incidents", "clips_left_house")
    return {"weeks": w,
            "per_week": {k: per_week(k) for k in keys},
            "clips_left_without_consent_total": sum(r["clips_left_without_consent"] for r in rows),
            "messages_with_verdict_words_total": sum(r["messages_with_verdict_words"] for r in rows),
            "ask_outcomes_total": outcomes,
            "unexpected_person_tests": {
                "n": len(intr),
                "reported_as_person_pct": share(sum(1 for t in intr if t["detected_as"] == "person"), len(intr)),
                "owner_messaged_within_60s_pct": share(sum(1 for t in intr if t["owner_messaged_within_60s"]), len(intr)),
                "asked_within_60s_pct": share(sum(1 for t in intr if t["asked_within_60s"]), len(intr)),
                "reached_centre_pct": share(sum(1 for t in intr if t["signal_to_centre_s"] is not None), len(intr)),
                "reached_centre_s": {"p50": pct([t["signal_to_centre_s"] for t in intr if t["signal_to_centre_s"] is not None], .5),
                                     "p95": pct([t["signal_to_centre_s"] for t in intr if t["signal_to_centre_s"] is not None], .95)},
                "outcomes": _count([t["outcome"] for t in intr])},
            "egress_classes_total": _sum_dicts([r["egress_classes"] for r in rows]),
            "example_ask": next((r["example_ask"] for r in rows if r["example_ask"]), None),
            "example_tell": next((r["example_tell"] for r in rows if r["example_tell"]), None)}


def _count(xs):
    out = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out


def _sum_dicts(ds):
    out = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def main():
    t0 = time.time()
    jobs_a = [(20_000 + i, c, d) for c in sc.CONDITIONS for d in sc.DESIGNS
              for i in range(N_ALARM_ALL_DAY if c == "internet_down_all_day" else N_ALARM)]
    jobs_b = [(40_000 + i, c) for c in sc.DAY_CONDITIONS for i in range(N_DAYS)]
    jobs_c = [(30_000 + w, d, th) for d, th in CAMERA_DESIGNS for w in range(N_WEEKS)]
    # long jobs first so the two workers finish together
    jobs_a.sort(key=lambda j: j[1] != "internet_down_all_day")
    with Pool(2) as pool:
        ra = pool.map(job_alarm, jobs_a, chunksize=8)
        rb = pool.map(job_day, jobs_b, chunksize=1)
        rc = pool.map(job_week, jobs_c, chunksize=4)
    alarms = {}
    for cond, design, kind, r in ra:
        r["kind"] = kind
        alarms.setdefault(cond, {}).setdefault(design, []).append(r)
    part_a = {c: {d: summarize_alarms(alarms[c][d]) for d in sc.DESIGNS} for c in sc.CONDITIONS}
    days = {}
    for cond, r in rb:
        days.setdefault(cond, []).append(r)
    part_b = {c: summarize_days(days[c]) for c in sc.DAY_CONDITIONS}
    weeks = {}
    for design, th, r in rc:
        weeks.setdefault(f"{design}_{th}", []).append(r)
    part_c = {k: summarize_weeks(v) for k, v in weeks.items()}
    hub_911 = sorted({d for c in part_a.values() for s in c.values() for d in s["egress_destinations"]
                      if "911" in d})
    out = {
        "experiment": "b08_security",
        "date": dt.date.today().isoformat(),
        "method": ("Seeded discrete-event runs in the islab twin. Part A: one alarm per run sent by the "
                   "B01 AlarmHub over the home internet (one_path) or by DualPathAlarmHub over the home "
                   "internet plus a modelled cellular link (two_paths) to a DualPathCentre that dedupes "
                   "copies by message and repeats by open incident. Internet down = WanLink outage plus "
                   "egress gate offline (alarms queue, heartbeats drop). Part B: 24 h with two paths and "
                   "one alarm of each kind, counting cellular traffic billed. Part C: one week of front "
                   "camera scenes per seed through an assumed detector model and three designs; household "
                   "members answer asks through an assumed person model. Ground truth for camera scenes "
                   "comes from the scene generator, never from messages."),
        "params": {
            "trials_per_cell": N_ALARM, "trials_internet_down_all_day": N_ALARM_ALL_DAY,
            "days_per_condition": N_DAYS, "weeks_per_camera_design": N_WEEKS,
            "conditions": sc.DESCRIBE, "day_conditions": list(sc.DAY_CONDITIONS),
            "hub": {k: v for k, v in sc.HUB.items()},
            "dual_path": {"primary_heartbeat_s": 60, "primary_fails_after_no_ack_s": 130,
                          "cellular_heartbeat_s": 3600, "cellular_heartbeat_while_primary_failed_s": 180,
                          "cellular_probe_s": 20, "cellular_fails_after_unacked_probes": 3,
                          "first_failover": "attempt 2 goes on the other path at once, after a 5 s ack timeout",
                          "household_told_after_primary_failed_s": 300},
            "centre": {"path_supervision_s": {"primary": 180, "cellular": 9000,
                                              "cellular_when_primary_down": 450},
                       "incident_window_s": 1800, "actions": CENTRE_ACTIONS},
            "cellular_assumptions": CELL_ASSUMPTIONS, "cellular_degraded": DEGRADED,
            "detector_assumptions": {k: [[lab, p, conf] for lab, p, conf in v]
                                     for k, v in DETECTOR_ASSUMPTIONS.items()},
            "scenes_per_day": [[k, lam, list(h)] for k, lam, h in sc.SCENES],
            "unexpected_person_per_week": sc.INTRUDERS_PER_WEEK,
            "arming": "night 23:00-06:30 every day; away 08:30-17:30 Monday-Friday",
            "person_model": {"p_answer_day": 0.85, "p_answer_night": 0.5, "delay_median_s": 45,
                             "delay_spread": "x 2^N(0,1)", "p_correct_judgement": 0.97,
                             "ask_timeout_s": 180, "no_answer_rule": "ask the next person, then keep it at home"},
            "clip_bytes": CLIP_BYTES, "verdict_words": list(VERDICT_WORDS),
            "camera_designs": [f"{d}_{th}" for d, th in CAMERA_DESIGNS],
        },
        "results": {"alarms": part_a, "cellular_days": part_b, "camera_weeks": part_c,
                    "egress_destinations_containing_911": hub_911,
                    "wall_time_s": round(time.time() - t0, 1)},
        "limitations": [
            "Simulation only. The cellular link is a model with assumed latency, radio setup, loss, "
            "overhead and tariff; no modem, operator or real network was used.",
            "The camera detector is an assumed confusion table; no detector was run and no footage exists. "
            "False 'person' counts follow from these assumptions and the assumed scene rates.",
            "The person model (answer rates, delay, judgement) is assumed; real people vary widely.",
            "Alarms are injected as detector events; the full 15-device home is not built in part A.",
            "The centre's actions are a modelled workflow, not any real centre's procedure, and the "
            "operator's own time to act is not modelled.",
            "Push messages to people away from home still need the internet; a cellular SMS or call "
            "path to people is not modelled (the centre calls the keyholder in its workflow).",
            "Incident-level dedupe can merge a genuinely new alarm in the same zone into an open "
            "incident within 30 minutes if no restore was received.",
            "Not a certified alarm system and not conformance testing against EN 50136, UL or NFPA.",
        ],
    }
    (HERE / ("results_quick.json" if QUICK else "results.json")).write_text(json.dumps(out, indent=1))
    print(f"done in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
