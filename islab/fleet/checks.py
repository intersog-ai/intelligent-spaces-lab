"""Checks over a finished FullHouse day: the five principle suites unchanged, a route-aware P5
adapter, and one check per Build Track layer.

    res = full_suites(house)       # {"suites": {...}, "p5_route_aware": {...}, "layers": {...}, "scenes": {...}}
"""
from __future__ import annotations

import copy

from islab.safety.controller import CLOSED
from islab.safety.notify import PUSH_CLASS, PUSH_PURPOSE, PUSH_RELAY
from islab.security.dualpath import CELLULAR
from islab.suites.principles import SCENE_KEYS, p5_core_stays_home, run_suites, scene_checks
from islab.twin import parse_time as T

SUITE_ORDER = ["P1", "P2", "P3", "P4", "P5", "ALARM", "BUS"]


def _route_aware_view(house):
    """A copy of the house whose gate shows only what B01's P5 rule is about. Removed from the view,
    each checked here instead: cellular alarm traffic (B08's second path, alarm classes only), clips
    that left with a person's single-use consent (B08), and B04 plain-text pushes (purpose
    ``notify_push``, which B01's purpose list predates) that carry nothing but the person.
    Everything else is checked by P5 unchanged."""
    g = house.gate
    keep = []
    for r in g.sent:
        meta = r.get("meta") or {}
        if r["purpose"] == PUSH_PURPOSE and r["payload_class"] == PUSH_CLASS and r["dest"] == PUSH_RELAY \
                and set(meta) <= {"person"}:
            continue
        if meta.get("route") == CELLULAR and r["purpose"] == "alarm" and \
                r["payload_class"] in ("alarm_event", "alarm_heartbeat"):
            continue
        if r["payload_class"] == "camera_clip" and meta.get("consent_id") in g.consents:
            continue
        keep.append(r)
    gv = copy.copy(g)
    gv.sent = keep
    hv = copy.copy(house)
    hv.gate = gv
    return hv


def p5_route_aware(house):
    r = p5_core_stays_home(_route_aware_view(house))
    r["suite"] = "P5r"
    r["name"] = "the core stays at home (route-aware: B08 cellular alarm path and consented clips allowed)"
    return r


def layer_checks(h):
    out = {}
    # B02
    c = h.connect
    lwt_told = sum(1 for n in h.phones.log if "went offline" in n["text"])
    out["B02"] = {"ha_discovery_configs": len(c.configs), "matter_roundtrips": c.roundtrips,
                  "matter_mismatches": len(c.mismatches), "last_will_offline": len(c.lwt),
                  "last_will_told": lwt_told,
                  "pass": len(c.mismatches) == 0 and lwt_told >= len(c.lwt)}
    # B03
    q = h.quality
    refused_quality = [r for r in h.audit.of("decision") if str(r.get("reason", "")).startswith("stale_context")
                       and any(str(x.get("freshness", "")).startswith("quality:") for x in (r.get("detail") or [])
                               if isinstance(x, dict))]
    issues = [i for i in q.issues if i["kind"] != "resolved"]
    told = sum(1 for n in h.phones.log if n["text"].startswith("I can't trust the"))
    tellable = [i for i in issues if i["kind"] in ("stuck", "disputed", "dead", "clock_skew")]
    out["B03"] = {"issues": len(issues), "issue_kinds": sorted({i["kind"] for i in issues}),
                  "issues_told": told, "rejected_at_ingest": len(q.rejected),
                  "rejected_by_reason": {k: q.stats[k] for k in ("duplicate", "out_of_order", "unsourced",
                                                                   "out_of_range")},
                  "decisions_refused_on_quality": len(refused_quality),
                  "pass": told >= len(tellable)}
    # B04
    ctl, truth = h.leak_ctl, h.valve_truth
    j = h.journal.records
    leak_t = next((r["t"] for r in j if r["kind"] == "leak_detected"), None)
    closed = next((r["t"] for r in j if r["kind"] == "closed_confirmed"), None)
    phys = truth.first_closed(leak_t or 0.0) if leak_t is not None else None
    false_claim = closed is not None and truth.position_at(closed) != 0
    valve_cmds = sum(1 for x in h.cmd_log if x["device_id"] == "valve.main" and x["action"] == "close")
    owners = sorted({str(x["source"]) for x in h.cmd_log if x["device_id"] == "valve.main"})
    out["B04"] = {"state": ctl.state, "leak_detected_s_after_09_12": round(leak_t - T("09:12"), 3)
                  if leak_t is not None else None,
                  "confirmed_close_s_after_detect": round(closed - leak_t, 3) if closed and leak_t else None,
                  "physically_closed_s_after_detect": round(phys - leak_t, 3) if phys and leak_t else None,
                  "false_closed_claim": false_claim, "valve_close_cmds": valve_cmds, "valve_cmd_sources": owners,
                  "escalated": any(r["kind"] == "close_failed" for r in j),
                  "pass": ctl.state == CLOSED and not false_claim}
    # B05
    if h.pipeline is not None:
        recs = list(h.pipeline.records.values())
        checks = [c for r in recs for c in r["done_checks"]]
        outcomes = {}
        for r in recs:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        not_understood = [r["text"] for r in recs if r["outcome"] in ("not_understood", "clarify")]
        out["B05"] = {"utterances": len(recs), "outcomes": outcomes, "done_said": len(checks),
                      "done_confirmed_before": sum(c["confirmed_before"] for c in checks),
                      "done_true_when_heard": sum(bool(c["true_at_audio"]) for c in checks),
                      "not_understood": not_understood,
                      "pass": all(c["confirmed_before"] and c["true_at_audio"] for c in checks)
                      and not not_understood}
    # B06
    tl = h.trust_log
    l3_voice = [d for d in h.engine.decisions.values() if d.level == 3 and d.executed and d.approval is None]
    out["B06"] = {"utterances": len(tl), "tiers": {t: sum(1 for x in tl if x["tier"] == t)
                                                   for t in ("named", "comfort", "guest")},
                  "level3_without_phone_approval": len(l3_voice), "pass": not l3_voice}
    # B07
    led = h.engine.ledger
    out["B07"] = {"tickets": len(led.tickets), "reply_refusals": dict(led.refused),
                  "execution_refusals": dict(led.exec_refused), "prompts": len(h.engine.prompts),
                  "denials": dict(h.engine.denials),
                  "pass": not led.exec_refused and not led.refused.get("bad_signature")}
    # B08
    g = h.gate
    cell = [r for r in g.sent if (r.get("meta") or {}).get("route") == CELLULAR]
    win = h.outages[0] if h.outages else (0, 0)
    cell_in = [r for r in cell if win[0] <= r["t_out"] < win[1]]
    asks = list(h.camera_obs.asks.values())
    out["B08"] = {"cellular_messages": len(cell), "cellular_messages_in_outage": len(cell_in),
                  "cellular_alarm_events": sum(1 for r in cell if r["payload_class"] == "alarm_event"),
                  "primary_failovers": sum(1 for x in h.alarm.path_log if x[1] == "primary" and x[2] == "failed"),
                  "path_messages_told": len(h.alarm.told),
                  "centre_incidents": len(h.centre.incidents), "centre_repeats": len(h.centre.repeats),
                  "camera_asks": len(asks), "camera_outcomes": [a["outcome"] for a in asks],
                  "clips_shared": len(h.camera_obs.clips_shared),
                  "clips_blocked": sum(1 for r in g.blocked if r["payload_class"] == "camera_clip"),
                  "pass": all(a["outcome"] in ("person_shared", "person_said_no", "no_answer_kept_home")
                              for a in asks) and (not asks or len(h.camera_obs.clips_shared) ==
                                                  sum(a["outcome"] == "person_shared" for a in asks))}
    # B09
    rep = g.day_report(0)
    attempts = len(g.ledger)
    ans = next((r for r in h.requests if "answer" in r), None)
    out["B09"] = {"ledger_rows": attempts, "sent": rep["messages"], "bytes": rep["bytes"],
                  "background_share_messages": rep["background_share_messages"],
                  "blocked": rep["blocked"], "blocked_by_source": rep["blocked_by_source"],
                  "blocked_by_reason": rep["blocked_by_reason"],
                  "notices": [{"source": n["source"], "reason": n["reason"]} for n in g.notices],
                  "quarantined": sorted(g.quarantined),
                  "short_answer": ans.get("answer") if ans else None,
                  "short_answer_ok": bool(ans and ans["answer_coverage"]["ok"]),
                  "bytes_add_up": sum(r["bytes"] for r in g.sent if r["t_out"] < 86400) == rep["bytes"],
                  "pass": bool(ans and ans["answer_coverage"]["ok"])}
    return out


def full_scenes(h):
    s = scene_checks(h)
    # the leak belongs to the B04 controller here (F6); B01's check looks for the house rule's action
    if "F6_one_leak_owner" in h.fixes:
        j = h.journal.records
        lt = next((r["t"] for r in j if r["kind"] == "leak_detected"), None)
        ct = next((r["t"] for r in j if r["kind"] == "closed_confirmed"), None)
        s["leak"] = h.leak_ctl.state == CLOSED
        s["leak_valve_closed_s"] = round(ct - T("09:12"), 3) if ct else None
    # B01's clip_share check also wants zero camera items out all day; B08 lets one consented clip go
    # to the monitoring centre. Here: the share with grandma failed after approval, and no clip left
    # without a single-use consent.
    cl = next((r for r in h.requests if r["text"] == "Share the front camera clip with grandma"), None)
    dec = h.engine.decisions
    unconsented = [r for r in h.gate.sent if r["payload_class"] in ("camera_clip", "image", "video")
                   and (r.get("meta") or {}).get("consent_id") not in h.gate.consents]
    s["clip_share"] = bool(cl and cl["request_ids"]) and dec[cl["request_ids"][0]].final == "failed" and \
        dec[cl["request_ids"][0]].approval is not None and not unconsented
    cam = list(h.camera_obs.asks.values())
    s["camera_ask"] = bool(cam) and cam[0]["outcome"] == "person_shared" and bool(h.camera_obs.clips_shared)
    return s


def full_suites(h):
    s = run_suites(h)
    return {"suites": s, "p5_route_aware": p5_route_aware(h), "layers": layer_checks(h), "scenes": full_scenes(h)}


SCENES = SCENE_KEYS + ["camera_ask"]
