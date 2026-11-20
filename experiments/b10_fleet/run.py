"""B10: fleet, cloud and the full test.

Part A  the full test: the Season 1 household day with every Build Track layer on (islab.fleet.FullHouse),
        scored by the five principle suites (islab.suites, unchanged) plus a route-aware P5 adapter and one
        check per layer, under no, light and heavy faults; the B01-only House on the same seeds for reference;
        one run per integration fix with that fix switched off, to show the interaction bug it closes.
Part B  the fleet: calibrate what a bad update looks like in a home's counts-only health report on the twin,
        then a staged rollout over N simulated homes (islab.fleet.rollout) under four designs.
Part C  the cloud plane: what a counts-only health report is, what a raw bundle would be, and what the
        B09 gate lets out of each; the cloud mapping table.

    python experiments/b10_fleet/run.py              # about 2-3 minutes on 2 cores
    python experiments/b10_fleet/run.py --seeds 2    # quick
Writes results.json next to this file.
"""
import argparse
import datetime
import itertools
import json
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np                                                         # noqa: E402

from islab.fleet import ALL_FIXES, FIXES, SCENES, SUITE_ORDER, full_suites, run_full_day   # noqa: E402
from islab.fleet import cloud, rollout                                     # noqa: E402
from islab.policy.engine import PolicyEngine                              # noqa: E402
from islab.suites import run_day                                           # noqa: E402
from islab.suites.principles import SCENE_KEYS, run_suites, scene_checks   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MIXES = ["none", "light", "heavy"]
BAD_UPDATE = {"manifests": {"alarm_hub": {"alarm": (2_000, 200)}}}   # a typo: 2,000 -> 200 alarm messages a day
LAYERS = ["B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09"]


def _compact(h, res, t0):
    s, lay = res["suites"], res["layers"]
    body, nbytes = cloud.health_report(h, res)
    blocked_alarm = [r["t"] for r in h.gate.blocked if r["source"] == "alarm_hub"]
    voice_dup = [r for r in h.requests if r["channel"] == "voice" and "uid" not in r and "answer" not in r]
    sam = [d for d in h.engine.decisions.values() if d.actor == "sam" and d.tool == "unlock_door"]
    return {
        "suites": {k: s[k]["pass"] for k in SUITE_ORDER}, "P5r": res["p5_route_aware"]["pass"],
        "failures": {k: s[k]["failures"][:3] for k in SUITE_ORDER if not s[k]["pass"]},
        "p5r_failures": res["p5_route_aware"]["failures"][:3],
        "numbers": {"actions": s["P2"]["numbers"]["actions"],
                    "false_success_messages": s["P2"]["numbers"]["false_success_messages"],
                    "unapproved_high_risk": s["P3"]["numbers"]["unapproved_high_risk_executions"],
                    "silent_failures": s["P4"]["numbers"]["silent_failures"],
                    "alarm_delivery_rate": s["ALARM"]["numbers"]["delivery_rate"],
                    "alarm_delays_s": s["ALARM"]["numbers"]["delays_s"],
                    "siren_within_1s": s["ALARM"]["numbers"]["siren_within_1s"],
                    "home_control_in_outage": s["P5"]["numbers"]["home_control_pass_rate_in_window"],
                    "p5_egress_during_outage": s["P5"]["numbers"]["egress_during_outage"],
                    "p5_forbidden_classes": s["P5"]["numbers"]["forbidden_classes_out"],
                    "p5_untagged": s["P5"]["numbers"]["egress_untagged"]},
        "layers": {k: lay[k] for k in LAYERS if k in lay},
        "scenes": {k: res["scenes"].get(k) for k in SCENES},
        "health": body["c"], "health_bytes": nbytes, "health_failing": cloud.failing(body["c"]),
        "first_alarm_blocked_h": round(min(blocked_alarm) / 3600, 3) if blocked_alarm else None,
        "probe": {"hub_voice_second_handling": len(voice_dup),
                  "hub_voice_second_decisions": sum(len(r["request_ids"]) for r in voice_dup),
                  "sam_unlock_status": [d.status for d in sam],
                  "valve_close_cmd_sources": lay["B04"]["valve_cmd_sources"],
                  "front_lock_at_19_05": getattr(h, "probes", {}).get("front_lock_at_19_05"),
                  "courier_unlock_final": next((h.engine.decisions[x].final for r in h.requests
                                                if r["text"] == "Unlock the front door for 2 minutes."
                                                for x in r["request_ids"]), None),
                  "leak_tells": sum(1 for n in h.phones.log if "leak" in n["text"].lower() or
                                    "water detected" in n["text"].lower())},
        "wall_s": round(time.time() - t0, 2),
    }


def job(spec):
    kind, seed, mix, off = spec["kind"], spec["seed"], spec["mix"], spec.get("off")
    t0 = time.time()
    PolicyEngine._ids = itertools.count(1)     # request ids are process-global in B01: reset per run
    if kind == "b01":
        h = run_day(seed=seed, faults=mix)
        s = run_suites(h)
        sc = scene_checks(h)
        return {**spec, "suites": {k: s[k]["pass"] for k in SUITE_ORDER},
                "numbers": {"alarm_delivery_rate": s["ALARM"]["numbers"]["delivery_rate"],
                            "home_control_in_outage": s["P5"]["numbers"]["home_control_pass_rate_in_window"]},
                "scenes": {k: sc.get(k) for k in SCENE_KEYS}, "wall_s": round(time.time() - t0, 2)}
    fixes = ALL_FIXES - {off} if off else ALL_FIXES
    h = run_full_day(seed=seed, faults=mix, fixes=fixes, update=BAD_UPDATE if kind == "bad" else None)
    res = full_suites(h)
    out = {**spec, **_compact(h, res, t0)}
    if kind == "full" and seed == 0 and mix == "none":
        body, nbytes = cloud.health_report(h, res)
        bundle = cloud.raw_bundle(h)
        out["cloud"] = {"health_report": body, "health_bytes": nbytes, "raw_bundle": bundle,
                        "plane_gate": cloud.send_to_plane(h, body, nbytes, bundle)}
    return out


# ---------------------------------------------------------------------------------------------
def rate(rows, f):
    return round(sum(1 for r in rows if f(r)) / len(rows), 4) if rows else None


def agg_full(rows):
    out = {}
    for mix in MIXES:
        rs = [r for r in rows if r["mix"] == mix]
        if not rs:
            continue
        m = {"runs": len(rs), "suites": {k: rate(rs, lambda r, k=k: r["suites"][k]) for k in SUITE_ORDER}}
        if "P5r" in rs[0]:
            m["suites"]["P5r"] = rate(rs, lambda r: r["P5r"])
            m["layers"] = {k: rate(rs, lambda r, k=k: r["layers"][k]["pass"]) for k in LAYERS}
            m["scenes"] = {k: rate(rs, lambda r, k=k: bool(r["scenes"].get(k))) for k in SCENES}
            delays = sorted(x for r in rs for x in r["numbers"]["alarm_delays_s"])
            m["alarm_delays_p50_p95_s"] = [delays[len(delays) // 2], delays[int(0.95 * (len(delays) - 1))]] \
                if delays else None
            m["unapproved_high_risk_total"] = sum(r["numbers"]["unapproved_high_risk"] for r in rs)
            m["false_success_messages_total"] = sum(r["numbers"]["false_success_messages"] for r in rs)
            m["silent_failures_total"] = sum(r["numbers"]["silent_failures"] for r in rs)
            m["alarm_delivery_rate_mean"] = round(float(np.mean([r["numbers"]["alarm_delivery_rate"] or 0
                                                                 for r in rs])), 4)
            m["home_control_in_outage_mean"] = round(float(np.mean([r["numbers"]["home_control_in_outage"] or 0
                                                                   for r in rs])), 4)
            m["b05_done_said"] = sum(r["layers"]["B05"]["done_said"] for r in rs)
            m["b05_done_true"] = sum(r["layers"]["B05"]["done_true_when_heard"] for r in rs)
            m["b04_confirmed_close_s"] = [r["layers"]["B04"]["confirmed_close_s_after_detect"] for r in rs]
            m["b08_cellular_in_outage_mean"] = round(float(np.mean([r["layers"]["B08"]["cellular_messages_in_outage"]
                                                                   for r in rs])), 2)
            m["b09_short_answer_ok"] = rate(rs, lambda r: r["layers"]["B09"]["short_answer_ok"])
            m["b09_background_share_mean"] = round(float(np.mean([r["layers"]["B09"]["background_share_messages"]
                                                                 for r in rs])), 4)
            m["b03_issues_mean"] = round(float(np.mean([r["layers"]["B03"]["issues"] for r in rs])), 2)
            m["b02_matter_mismatches"] = sum(r["layers"]["B02"]["matter_mismatches"] for r in rs)
            m["health_failing"] = rate(rs, lambda r: r["health_failing"])
            m["health_bytes_max"] = max(r["health_bytes"] for r in rs)
            m["p5_failure_reasons"] = sorted({f["why"] for r in rs for f in r["failures"].get("P5", [])})
            m["p5r_failure_reasons"] = sorted({f["why"] for r in rs for f in r["p5r_failures"]})
            m["other_failures"] = {k: sorted({f.get("why", "?") for r in rs for f in r["failures"].get(k, [])})
                                   for k in SUITE_ORDER if k != "P5" and any(k in r["failures"] for r in rs)}
            m["scene_misses"] = {k: [r["seed"] for r in rs if not r["scenes"].get(k)] for k in SCENES
                                 if any(not r["scenes"].get(k) for r in rs)}
            m["courier_unlock_finals"] = [r["probe"]["courier_unlock_final"] for r in rs]
            m["front_lock_at_19_05"] = [r["probe"]["front_lock_at_19_05"] for r in rs]
            m["wall_s_mean"] = round(float(np.mean([r["wall_s"] for r in rs])), 2)
        else:
            m["scenes"] = {k: rate(rs, lambda r, k=k: bool(r["scenes"].get(k))) for k in SCENE_KEYS}
            m["alarm_delivery_rate_mean"] = round(float(np.mean([r["numbers"]["alarm_delivery_rate"] or 0
                                                                 for r in rs])), 4)
            m["home_control_in_outage_mean"] = round(float(np.mean([r["numbers"]["home_control_in_outage"] or 0
                                                                   for r in rs])), 4)
        out[mix] = m
    return out


def ablation_row(r, ref):
    diff = {"suites_now_failing": [k for k in SUITE_ORDER if ref["suites"][k] and not r["suites"][k]],
            "P5r_now_failing": ref["P5r"] and not r["P5r"],
            "layers_now_failing": [k for k in LAYERS if ref["layers"][k]["pass"] and not r["layers"][k]["pass"]],
            "scenes_now_failing": [k for k in SCENES if ref["scenes"].get(k) and not r["scenes"].get(k)]}
    lay = r["layers"]
    ev = {
        "F1_signed_replies": {"b07_reply_refusals": lay["B07"]["reply_refusals"]},
        "F2_day_manifests": {"b09_blocked_by_source": lay["B09"]["blocked_by_source"],
                             "b09_quarantined": lay["B09"]["quarantined"], "clips_blocked": lay["B08"]["clips_blocked"]},
        "F3_one_voice_path": {"hub_voice_second_handling": r["probe"]["hub_voice_second_handling"],
                              "hub_voice_second_decisions": r["probe"]["hub_voice_second_decisions"],
                              "actions": r["numbers"]["actions"], "actions_with_fix": ref["numbers"]["actions"]},
        "F4_house_phrases": {"b05_not_understood": lay["B05"]["not_understood"]},
        "F6_one_leak_owner": {"valve_close_cmds": lay["B04"]["valve_close_cmds"],
                              "valve_cmd_sources": r["probe"]["valve_close_cmd_sources"],
                              "leak_tells": r["probe"]["leak_tells"], "leak_tells_with_fix": ref["probe"]["leak_tells"]},
        "F7_prompt_words": {"b07_tickets": lay["B07"]["tickets"]},
        "F8_deny_union": {"sam_unlock_status": r["probe"]["sam_unlock_status"],
                          "with_fix": ref["probe"]["sam_unlock_status"]},
        "F9_destination_first": {"b09_notices": lay["B09"]["notices"], "with_fix": ref["layers"]["B09"]["notices"],
                                 "blocked_by_reason": lay["B09"]["blocked_by_reason"]},
        "F10_thermostat_not_stuck": {"b03_issue_kinds": lay["B03"]["issue_kinds"], "b03_told": lay["B03"]["issues_told"],
                                     "with_fix_told": ref["layers"]["B03"]["issues_told"]},
    }.get(r["off"], {})
    return {"fix": r["off"], "what": FIXES[r["off"]], "seed": r["seed"], "mix": r["mix"], **diff, "evidence": ev}


# ---------------------------------------------------------------------------------------------
def fleet_part(bad_rows, full_rows, n_homes, seeds):
    t_block = [r["first_alarm_blocked_h"] for r in bad_rows if r["first_alarm_blocked_h"] is not None]
    p_affect = rate(bad_rows, lambda r: r["health_failing"])
    good_false = rate(full_rows, lambda r: r["health_failing"])
    tb = np.array(t_block) if t_block else np.array([3.4])

    def loud_after(rng, hod):
        b = rng.choice(tb, size=hod.size)
        return np.where(hod < b, b - hod, 0.1)

    def quiet_after(rng, hod):
        return rng.uniform(0, 24, size=hod.size)

    updates = {
        "good": rollout.Update("good", 0.0, None, p_false_per_day=good_false or 0.0),
        "bad_loud": rollout.Update("bad_loud", p_affect or 0.0, loud_after, p_false_per_day=good_false or 0.0),
        "bad_quiet": rollout.Update("bad_quiet", 0.05, quiet_after, p_false_per_day=good_false or 0.0),
    }
    designs = {
        "all_at_once": rollout.FleetConfig(n_homes=n_homes, design="all_at_once"),
        "rings_operator": rollout.FleetConfig(n_homes=n_homes, design="rings_operator"),
        "rings_auto_halt": rollout.FleetConfig(n_homes=n_homes, design="rings_auto_halt"),
        "rings_auto_halt_maintenance": rollout.FleetConfig(n_homes=n_homes, design="rings_auto_halt",
                                                           install="maintenance"),
    }
    keys = ["installed_new", "installed_new_before_halt", "installed_after_halt", "homes_failing", "halt_h",
            "halt_after_first_failure_report_h", "first_failing_report_h", "on_new_at_end", "on_old_at_end",
            "never_updated", "never_updated_away", "on_new_at_end_offline_at_end", "rings_opened",
            "hours_to_95pct_on_new"]
    out = {}
    for un, u in updates.items():
        for dn, cfg in designs.items():
            rows = [rollout.simulate(cfg, u, seed=s) for s in range(seeds)]
            out[f"{un}/{dn}"] = rollout.summarize(rows, keys)
    calib = {"bad_update": "alarm_hub manifest daily count 2,000 -> 200 (config typo)",
             "twin_days": len(bad_rows), "health_failing_share": p_affect,
             "first_alarm_blocked_h_of_day": sorted(t_block),
             "alarm_suite_pass_share_bad": rate(bad_rows, lambda r: r["suites"]["ALARM"]),
             "alarm_delivery_rate_bad_mean": round(float(np.mean([r["numbers"]["alarm_delivery_rate"] or 0
                                                                  for r in bad_rows])), 4),
             "good_version_false_failing_share": good_false,
             "quiet_bad_update": "ASSUMED: 5% of homes affected, failure 0-24 h after install (uniform)"}
    return {"calibration": calib, "results": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--fleet-seeds", type=int, default=20)
    ap.add_argument("--homes", type=int, default=1000)
    a = ap.parse_args()
    t0 = time.time()
    specs = [{"kind": "full", "seed": s, "mix": m} for m in MIXES for s in range(a.seeds)]
    specs += [{"kind": "b01", "seed": s, "mix": m} for m in MIXES for s in range(a.seeds)]
    specs += [{"kind": "ablate", "seed": 0, "mix": "none", "off": f} for f in sorted(FIXES)
              if f != "F5_route_aware_p5"]
    specs += [{"kind": "bad", "seed": s, "mix": m} for m in MIXES for s in range(max(1, a.seeds // 2))]
    with Pool(2) as pool:
        rows = pool.map(job, specs, chunksize=1)
    full = [r for r in rows if r["kind"] == "full"]
    b01 = [r for r in rows if r["kind"] == "b01"]
    abl = [r for r in rows if r["kind"] == "ablate"]
    bad = [r for r in rows if r["kind"] == "bad"]
    ref0 = next(r for r in full if r["seed"] == 0 and r["mix"] == "none")
    t_twin = time.time() - t0
    fleet = fleet_part(bad, full, a.homes, a.fleet_seeds)
    t_all = time.time() - t0
    results = {
        "full_test": {"layers_on": "B02 connect (modelled in the loop), B03, B04, B05, B06, B07, B08, B09 + B01 twin",
                      "integrated": agg_full(full), "b01_only_reference": agg_full(b01),
                      "fixes": FIXES,
                      "ablation": [ablation_row(r, ref0) for r in abl],
                      "p5_adapter_note": "F5 is an adapter, not a runtime switch: P5 (unchanged) and P5r are both "
                                         "computed on every run",
                      "seed0_none_layers": ref0["layers"], "seed0_none_health": ref0["health"]},
        "fleet": fleet,
        "cloud": {"seed0_none": ref0.get("cloud"), "mapping": cloud.MAPPING,
                  "plane_rules": {"purpose": cloud.HEALTH_PURPOSE, "class": cloud.HEALTH_CLASS,
                                  "max_bytes": cloud.HEALTH_MAX_BYTES, "per_day": cloud.HEALTH_PER_DAY,
                                  "host": cloud.PLANE_HOST}},
        "runtime_s": {"twin_runs": round(t_twin, 1), "total": round(t_all, 1), "runs": len(rows)},
    }
    out = {
        "experiment": "b10_fleet",
        "date": datetime.date(2026, 11, 19).isoformat(),
        "method": ("A: FullHouse day (islab/fleet/full.py) with every layer on, scored by islab.suites (unchanged), "
                   "a route-aware P5 adapter and one check per layer; same seeds with the B01-only House; one run "
                   "per integration fix with that fix off. B: bad update calibrated on the twin, then a staged "
                   "rollout over simulated homes (islab/fleet/rollout.py). C: counts-only health report vs raw "
                   "bundle through the B09 gate (islab/fleet/cloud.py); mapping table from docs/facts-ledger.md."),
        "params": {"seeds_per_mix": a.seeds, "mixes": MIXES, "bad_update_seeds_per_mix": max(1, a.seeds // 2),
                   "fleet_homes": a.homes, "fleet_seeds": a.fleet_seeds, "fleet_horizon_h": 14 * 24,
                   "fleet_plan": rollout.PLAN, "soak_h": 24, "halt_min_failing": 2, "operator_delay_h": 24,
                   "check_every_h": 6, "availability_assumptions": rollout.AVAILABILITY_ASSUMPTIONS,
                   "b05_tier": "minipc_cpu (measured stages from experiments/b05_pipeline/measured.json)",
                   "b02_last_will_s": 15.0, "bad_update": BAD_UPDATE},
        "results": results,
        "limitations": [
            "Everything ran in simulation. The B02 MQTT broker is not in the day loop (it runs on wall time); B02 "
            "enters as its Matter data-model round trip, HA discovery configs and the measured 15 s Last Will, modelled.",
            "Seeds per fault mix were reduced to keep the run under about 3 minutes on 2 shared cores.",
            "The fleet is a lightweight model: availability, ring sizes, soak, halt threshold, operator delay and the "
            "quiet bad update are assumptions. Only the loud bad update's effect on a home's health report and the "
            "good version's false-failing share come from twin days.",
            "Voices in the day are the bundled household embeddings; B05 stage times are the mini PC tier "
            "(measured on this CPU, scaled where marked in B05). No LLM was run.",
            "No real cloud was used. Product features beyond docs/facts-ledger.md rows 8a-8c were not verified "
            "(documentation pages could not be opened from this machine).",
        ],
    }
    with open(os.path.join(HERE, "results.json"), "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps(results["runtime_s"]), "->", os.path.join(HERE, "results.json"))


if __name__ == "__main__":
    main()
