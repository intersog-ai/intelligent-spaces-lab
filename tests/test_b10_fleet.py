"""B10: the full test (every layer on), fleet rollout model, cloud plane rules."""
import itertools

import numpy as np
import pytest

from islab.fleet import ALL_FIXES, FIXES, FullHouse, full_suites, run_full_day
from islab.fleet import cloud, rollout
from islab.fleet.full import FastBus, FullGate
from islab.policy.engine import PolicyEngine
from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim


def _reset():
    PolicyEngine._ids = itertools.count(1)


@pytest.fixture(scope="module")
def day0():
    _reset()
    h = run_full_day(0, "none")
    return h, full_suites(h)


def test_full_day_suites_and_layers(day0):
    h, r = day0
    s = r["suites"]
    for k in ("P1", "P2", "P3", "P4", "ALARM", "BUS"):
        assert s[k]["pass"], (k, s[k]["failures"][:2])
    assert r["p5_route_aware"]["pass"], r["p5_route_aware"]["failures"]
    assert all(v["pass"] for v in r["layers"].values()), {k: v for k, v in r["layers"].items() if not v["pass"]}
    assert s["P3"]["numbers"]["unapproved_high_risk_executions"] == 0


def test_unchanged_p5_flags_the_new_routes(day0):
    """B01's P5, unchanged, counts B08's cellular path, a consented clip and B04 pushes as violations."""
    h, r = day0
    why = {f["why"] for f in r["suites"]["P5"]["failures"]}
    assert "egress while the internet was down" in why
    assert "forbidden payload classes left the house" in why


def test_b04_owns_the_valve_and_confirms(day0):
    h, r = day0
    b4 = r["layers"]["B04"]
    assert b4["state"] == "closed_confirmed" and b4["valve_cmd_sources"] == ["safety.leak_valve"]
    assert not b4["false_closed_claim"]


def test_b05_every_done_was_true(day0):
    b5 = day0[1]["layers"]["B05"]
    assert b5["done_said"] > 0 and b5["done_said"] == b5["done_true_when_heard"] == b5["done_confirmed_before"]


def test_b08_cellular_carries_alarm_traffic_in_outage(day0):
    h, r = day0
    assert r["layers"]["B08"]["cellular_messages_in_outage"] > 0
    assert r["layers"]["B08"]["clips_shared"] == 1


def test_b09_short_answer_covers_every_category(day0):
    assert day0[1]["layers"]["B09"]["short_answer_ok"]


def test_scenes_with_all_fixes(day0):
    sc = day0[1]["scenes"]
    for k in ("arm_away", "intrusion", "smoke", "leak", "courier", "garage", "window", "good_night",
              "sam_unlock", "camera_ask", "clip_share", "egress_summary"):
        assert sc[k], k


@pytest.mark.parametrize("fix,check", [
    ("F1_signed_replies", lambda h, r: r["layers"]["B07"]["reply_refusals"].get("bad_signature", 0) > 0),
    ("F4_house_phrases", lambda h, r: "Good night." in r["layers"]["B05"]["not_understood"]),
    ("F6_one_leak_owner", lambda h, r: len(r["layers"]["B04"]["valve_cmd_sources"]) == 2),
    ("F8_deny_union", lambda h, r: any(d.actor == "sam" and d.tool == "unlock_door" and d.status != "denied"
                                       for d in h.engine.decisions.values())),
    ("F9_destination_first", lambda h, r: not any(n["source"] == "camera.front" for n in r["layers"]["B09"]["notices"])),
])
def test_each_fix_closes_its_bug(fix, check):
    _reset()
    h = run_full_day(0, "none", fixes=ALL_FIXES - {fix}, until=86400)
    assert check(h, full_suites(h)), fix


def test_fast_bus_trace_identical():
    traces = []
    for fast in (True, False):
        _reset()
        traces.append(run_full_day(2, "light", until=4000, fast_bus=fast).sim.log)
    assert traces[0] == traces[1]


def test_fast_bus_same_order_late_subscribe():
    sim = Sim(0)
    got = {}
    for cls in (Bus, FastBus):
        sim = Sim(0)
        b = cls(sim)
        seen = []
        b.subscribe("a/#", lambda e: seen.append(("1", e.topic)))
        b.publish(Envelope("a/x", {}, "t", 0))
        sim.run()
        b.subscribe("a/+", lambda e: seen.append(("2", e.topic)))
        b.publish(Envelope("a/x", {}, "t", 0))
        sim.run()
        got[cls.__name__] = seen
    assert got["Bus"] == got["FastBus"]


def test_full_gate_mro_and_destination_first():
    sim = Sim(0)
    bus = Bus(sim)
    g = FullGate(sim, bus)
    assert not g.send("camera.front", "telemetry.camvendor.example", "telemetry", "video", size=10)
    assert g.blocked[-1]["reason"] == "destination_not_allowlisted"
    assert any(n["source"] == "camera.front" for n in g.notices)
    # cellular route carries alarm classes only
    assert g.send("alarm_hub", "receiver.monitoring.example", "alarm", "alarm_event", data=b"x",
                  meta={"route": "cellular"})
    assert not g.send("alarm_hub", "receiver.monitoring.example", "alarm", "camera_clip", size=10,
                      meta={"route": "cellular"})


def test_plane_gate_counts_only(day0):
    h, r = day0
    body, n = cloud.health_report(h, r)
    assert n <= cloud.HEALTH_MAX_BYTES and all(isinstance(v, int) for v in body["c"].values())
    out = cloud.send_to_plane(h, body, n, cloud.raw_bundle(h))
    assert out["health_report"]["sent"]
    assert not any(v["sent"] for v in out["raw"].values())
    assert not out["oversized_health_report_sent"] and not out["health_report_with_extra_field_sent"]


def test_bad_update_blocks_alarms_and_is_seen():
    _reset()
    h = run_full_day(1, "none", update={"manifests": {"alarm_hub": {"alarm": (2_000, 200)}}}, until=5 * 3600)
    assert any(r["source"] == "alarm_hub" for r in h.gate.blocked)


def test_rollout_auto_halt_limits_bad_installs():
    def after(rng, hod):
        return np.full(hod.size, 3.0)
    upd = rollout.Update("bad", 1.0, after)
    allc = rollout.simulate(rollout.FleetConfig(n_homes=300, design="all_at_once"), upd, seed=1)
    ring = rollout.simulate(rollout.FleetConfig(n_homes=300, design="rings_auto_halt"), upd, seed=1)
    assert ring["halted"] and ring["installed_new"] <= 3 + 1
    assert allc["installed_new"] > 250
    good = rollout.simulate(rollout.FleetConfig(n_homes=300), rollout.Update("good", 0.0), seed=1)
    assert not good["halted"] and good["on_old_at_end"] == good["never_updated"]


def test_rollout_maintenance_window_installs_after_halt():
    def after(rng, hod):
        return rng.uniform(0, 24, size=hod.size)
    upd = rollout.Update("quiet", 0.05, after)
    tot = 0
    for s in range(5):
        tot += rollout.simulate(rollout.FleetConfig(n_homes=1000, install="maintenance"), upd, seed=s)[
            "installed_after_halt"]
    assert tot > 0


def test_mapping_uses_ledger_dates_only():
    text = " ".join(m["status_from_ledger"] for m in cloud.MAPPING)
    for d in ("Oct 7, 2026", "Sep 20, 2029", "Aug 16, 2023"):
        assert d in text
    assert set(FIXES) >= {"F1_signed_replies", "F5_route_aware_p5"}
