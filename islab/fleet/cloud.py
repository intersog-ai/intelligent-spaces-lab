"""What a cloud management plane may see, under the B09 egress rules, and where things run.

``health_report(house, res)``   counts only, built from a finished FullHouse day and its checks:
                                no names, no words people said, no device readings, no times of day.
``raw_bundle(house)``           what a "send us everything" plane would ask for: trace, audit log,
                                ledger, journal. Sizes and what personal content they carry.
``PlaneGate``                   the FullGate plus one declared purpose, ``fleet_health``: class
                                ``health_counts``, to one allowlisted plane host, at most
                                ``HEALTH_MAX_BYTES`` and ``HEALTH_PER_DAY`` a day. Anything else to
                                the plane is refused by the same rules as any other sender.
``MAPPING``                     the cloud mapping table (no real cloud was used). Dates only from
                                docs/facts-ledger.md rows 8a-8c.
"""
from __future__ import annotations

import json

from .full import FullGate

PLANE_HOST = "fleet.plane.example"
HEALTH_PURPOSE, HEALTH_CLASS = "fleet_health", "health_counts"
HEALTH_MAX_BYTES, HEALTH_PER_DAY = 1024, 24
RAW_CLASSES = ("raw_trace", "audit_log", "egress_ledger", "safety_journal")


class PlaneGate(FullGate):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.allowlist.setdefault(PLANE_HOST, set()).add(HEALTH_PURPOSE)
        self.manifests["fleet.health"] = {HEALTH_PURPOSE: (HEALTH_MAX_BYTES, HEALTH_PER_DAY)}

    def _check(self, dest, purpose, cls, meta):
        if purpose == HEALTH_PURPOSE:
            if dest != PLANE_HOST:
                return "destination_not_allowlisted"
            if cls != HEALTH_CLASS:
                return f"payload_class_not_allowed_for_purpose:{cls}"
            if set(meta) - {"version", "counts_only"}:
                return "health_report_carries_extra_fields"
            return None
        if dest == PLANE_HOST:
            return "purpose_not_allowed_for_destination"
        return super()._check(dest, purpose, cls, meta)


def health_report(house, res, version="v1"):
    """Counts only. Every value is an integer or a pass bit; keys are fixed names."""
    s, lay = res["suites"], res["layers"]
    g = house.gate
    counts = {
        "suites_failed": sum(1 for k, v in s.items() if not v["pass"] and k != "P5")
        + (0 if res["p5_route_aware"]["pass"] else 1),
        "silent_failures": s["P4"]["numbers"]["silent_failures"],
        "unapproved_high_risk": s["P3"]["numbers"]["unapproved_high_risk_executions"],
        "alarms_not_acked": s["P4"]["numbers"]["alarms_not_acked_by_centre"],
        "alarm_msgs_blocked": sum(1 for r in g.blocked if r["source"] == "alarm_hub"),
        "alarm_delivery_pct": int(round(100 * (s["ALARM"]["numbers"]["delivery_rate"] or 0))),
        "valve_escalations": int(bool(lay["B04"]["escalated"])),
        "reading_issues": lay["B03"]["issues"],
        "egress_blocked": len(g.blocked),
        "egress_notices": len(g.notices),
        "done_not_confirmed": lay["B05"]["done_said"] - lay["B05"]["done_confirmed_before"] if "B05" in lay else 0,
        "bus_errors": s["BUS"]["numbers"]["subscriber_errors"],
    }
    body = {"v": version, "c": counts}
    return body, len(json.dumps(body, separators=(",", ":")).encode())


def failing(counts):
    """The plane's health rule: a home is failing if anything that must be zero is not."""
    return any(counts[k] for k in ("unapproved_high_risk", "alarms_not_acked", "alarm_msgs_blocked",
                                   "silent_failures", "done_not_confirmed", "bus_errors"))


def raw_bundle(house):
    trace = sum(len(json.dumps({"t": t, "kind": k, **d}, default=str)) + 1 for t, k, d in house.sim.log)
    audit = sum(len(json.dumps(r, default=str)) + 1 for r in house.audit.records)
    ledger = sum(len(json.dumps(r, default=str)) + 1 for r in house.gate.ledger)
    journal = sum(len(json.dumps(r, default=str)) + 1 for r in house.journal.records)
    people = {"anna", "mark", "sam"}
    named = sum(1 for t, k, d in house.sim.log if str(d.get("person", "")).lower() in people)
    return {"bytes": {"raw_trace": trace, "audit_log": audit, "egress_ledger": ledger, "safety_journal": journal},
            "total_bytes": trace + audit + ledger + journal,
            "personal_content": {"requests_in_peoples_words": len(house.requests),
                                 "messages_to_people_verbatim": len(house.phones.log),
                                 "trace_records_naming_a_person": named,
                                 "trace_records": len(house.sim.log)}}


def send_to_plane(house, report_body, nbytes, bundle):
    """Offer each item to the plane through its own fresh PlaneGate with the house gate's manifests
    (so one refusal's quarantine does not decide the next item). Returns outcomes."""
    def fresh():
        return PlaneGate(house.sim, house.bus, manifests=house.gate.manifests)
    notices = []
    gate = fresh()
    ok_h = gate.send("fleet.health", PLANE_HOST, HEALTH_PURPOSE, HEALTH_CLASS, size=nbytes,
                     meta={"version": report_body["v"], "counts_only": True})
    gate = fresh()
    big = gate.send("fleet.health", PLANE_HOST, HEALTH_PURPOSE, HEALTH_CLASS, size=HEALTH_MAX_BYTES + 1,
                    meta={"version": "v1", "counts_only": True})
    big_reason = gate.blocked[-1]["reason"] if not big else None
    notices += [n["reason"] for n in gate.notices]
    raw = {}
    for cls, b in bundle["bytes"].items():
        gate = fresh()
        ok = gate.send("fleet.health", PLANE_HOST, HEALTH_PURPOSE, cls, size=b)
        raw[cls] = {"bytes": b, "sent": ok, "reason": gate.blocked[-1]["reason"] if not ok else None}
        notices += [n["reason"] for n in gate.notices]
    gate = fresh()
    ok_m = gate.send("fleet.health", PLANE_HOST, HEALTH_PURPOSE, HEALTH_CLASS, size=nbytes,
                     meta={"version": report_body["v"], "counts_only": True, "home_address": "x"})
    mixed_reason = gate.blocked[-1]["reason"] if not ok_m else None
    return {"health_report": {"bytes": nbytes, "sent": ok_h},
            "raw": raw,
            "oversized_health_report_sent": big,
            "oversized_reason": big_reason,
            "health_report_with_extra_field_sent": ok_m, "extra_field_reason": mixed_reason,
            "household_notices": notices}


# ---- the cloud mapping table (no real cloud) ---------------------------------------------------
# "Where it runs" is this study's design, the same in every column: the home core never moves.
# Product facts are limited to docs/facts-ledger.md rows 8a-8c. Product features beyond those rows
# were NOT verified in this study (the documentation pages could not be opened from this machine)
# and are not claimed.
HOME_CORE = ("policy engine and approval gate (B01, B07), action tracker (B01-B05), leak state machine (B04), "
             "reading quality (B03), voice pipeline (B05), trust policy (B06), alarm hub with both paths (B08), "
             "egress gate and ledger (B09)")
MAPPING = [
    {"stack": "AWS IoT Greengrass V2",
     "status_from_ledger": "Greengrass V1 support ended Oct 7, 2026; V2 is the migration target (ledger 8a)",
     "home_hub": HOME_CORE + ", packaged as software for the hub",
     "cloud_plane": "update packages, rings and halt rule; counts-only health reports (fleet_health)",
     "never_in_cloud": "decisions, approvals, raw traces, audio, clips (except a consented clip to the "
                       "monitoring centre, which is not the plane)",
     "not_verified_here": "Greengrass V2 deployment, rollout and abort features"},
    {"stack": "Azure IoT Hub + DPS + Azure Device Registry",
     "status_from_ledger": "Azure IoT Central retires Sep 20, 2029; Microsoft recommends IoT Hub, DPS and "
                           "Azure Device Registry (ledger 8b)",
     "home_hub": HOME_CORE,
     "cloud_plane": "device identity and provisioning, update rings and halt rule, counts-only health reports",
     "never_in_cloud": "same as above",
     "not_verified_here": "IoT Hub update and configuration features, DPS details"},
    {"stack": "Self-hosted MQTT broker + Home Assistant fleet",
     "status_from_ledger": "no vendor dates; B02 measured a local broker (amqtt 0.12.2) and HA discovery "
                           "payloads in this repo",
     "home_hub": HOME_CORE + "; local MQTT broker per home (B02)",
     "cloud_plane": "an operator-run update server and health collector speaking the same fleet_health "
                    "purpose; no third-party cloud",
     "never_in_cloud": "same as above",
     "not_verified_here": "any HA multi-home management tooling (none assumed)"},
    {"stack": "Google Cloud IoT Core",
     "status_from_ledger": "retired Aug 16, 2023 (ledger 8c)",
     "home_hub": "-", "cloud_plane": "not an option", "never_in_cloud": "-", "not_verified_here": "-"},
]
