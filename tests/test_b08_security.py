"""B08: cameras as evidence, a second (cellular) alarm path, receiver dedupe."""
import json
import pathlib

import pytest

from islab.alarm import dc09
from islab.alarm.path import AlarmHub, WanLink
from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.security import (VERDICT_WORDS, CameraObserver, CellularLink, DualPathAlarmHub,
                            DualPathCentre, DualPathEgressGate, ObservingCamera, parse_event)
from islab.security import scenario as sc

KEY = b"0123456789ABCDEF"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def rig(two_paths=True, outages=(), cell_outages=()):
    sim = Sim(seed=1)
    bus = Bus(sim)
    link = WanLink(sim, outages=outages)
    centre = DualPathCentre(sim, link, {"1A2B3C": KEY})
    gate = DualPathEgressGate(sim, bus)
    for a, b in outages:
        sim.at(a, gate.set_internet, False)
        sim.at(b, gate.set_internet, True)
    told = []
    bus.subscribe("notify/owner", lambda e: told.append(e.payload["text"]), "phone")
    kw = dict(account="1A2B3C", key=KEY, egress=gate, fmt=dc09.SIA_DCS)
    if two_paths:
        cell = CellularLink(sim, outages=cell_outages)
        hub = DualPathAlarmHub(sim, bus, link, centre, cell_link=cell, **kw)
    else:
        cell = None
        hub = AlarmHub(sim, bus, link, centre, **kw)
    return sim, bus, centre, gate, hub, cell, told


def smoke(sim, bus, t):
    sim.at(t, lambda: bus.publish(Envelope(topic="event/smoke.hall", payload={"type": "smoke"},
                                           source="smoke.hall", ts=sim.now)))


def test_parse_event():
    assert parse_event("Nri1/FA001") == ("FA", 1, False, "fire")
    assert parse_event("Nri1/WH002") == ("WA", 2, True, "water")
    assert parse_event("1130 01 008") == ("130", 8, False, "burglary")
    assert parse_event("3111 01 001")[2] is True
    assert parse_event("") is None


def test_cellular_outage_drops_and_bills_nothing():
    sim = Sim(seed=2)
    cell = CellularLink(sim, outages=[(0, 10)])
    got = []
    cell.send(b"x" * 50, got.append)
    sim.run(until=5)
    assert got == [] and cell.stats["bytes"] == 0 and cell.stats["outage_dropped"] == 1
    sim.at(20, cell.send, b"x" * 50, got.append)
    sim.run(until=40)
    assert got and cell.stats["bytes"] == 50 + cell.p["overhead_bytes"]


def test_internet_down_alarm_goes_over_cellular_within_60s():
    sim, bus, centre, gate, hub, cell, told = rig(outages=[(600, 1800)])
    smoke(sim, bus, 700)
    sim.run(until=1200)
    alarms = [r for r in centre.receipts if r["token"] != dc09.NULL]
    assert alarms and alarms[0]["t"] - 700 <= 60 and alarms[0]["path"] == "cellular"
    assert centre.incidents[0]["action"] == "centre_calls_911_with_address_sensor_time"
    assert any("cellular backup" in t for t in told)


def test_one_path_waits_for_the_internet():
    sim, bus, centre, gate, hub, cell, told = rig(two_paths=False, outages=[(600, 1800)])
    smoke(sim, bus, 700)
    sim.run(until=1700)
    assert not [r for r in centre.receipts if r["token"] != dc09.NULL]
    sim.run(until=2000)
    assert [r for r in centre.receipts if r["token"] != dc09.NULL]


def test_both_paths_down_tells_person_to_call_911_themselves():
    sim, bus, centre, gate, hub, cell, told = rig(outages=[(600, 1500)], cell_outages=[(600, 1500)])
    smoke(sim, bus, 650)
    sim.run(until=1400)
    assert any("Both paths" in t and "call 911 yourself" in t for t in told)
    sim.run(until=2400)
    assert any("reached again" in t for t in told)
    assert len(centre.incidents) == 1


def test_same_frame_on_both_paths_is_one_incident():
    sim, bus, centre, gate, hub, cell, told = rig()
    sim.run(until=100)
    msg = dc09.DC09Message(mtype=dc09.SIA_DCS, account="1A2B3C", sequence=77, line="1",
                           event="Nri1/FA001", timestamp=dc09.sim_timestamp(sim.now), encrypted=True)
    raw = dc09.encode(msg, key=KEY)
    centre.receive(raw, lambda a: None, path="primary")
    centre.receive(raw, lambda a: None, path="cellular")
    assert len(centre.incidents) == 1 and centre.duplicates == 1


def test_reboot_reraise_is_a_repeat_not_a_new_incident():
    r = sc.run_alarm(5, "hub_reboot", "two_paths", "smoke")
    assert r["message_level_incidents"] == 2      # the B04 duplicate: a new sequence number
    assert r["incidents"] == 1                    # attached to the open incident


def test_restore_closes_incident():
    sim, bus, centre, gate, hub, cell, told = rig()
    for t, ev in ((100, "smoke"), (200, "smoke_clear"), (300, "smoke")):
        sim.at(t, lambda ev=ev: bus.publish(Envelope(topic="event/smoke.hall", payload={"type": ev},
                                                     source="smoke.hall", ts=sim.now)))
    sim.run(until=400)
    assert len(centre.incidents) == 2


def test_cellular_route_carries_only_alarm_signals():
    sim = Sim(seed=3)
    gate = DualPathEgressGate(sim, Bus(sim))
    dest = "receiver.monitoring.example"
    assert gate.send("x", dest, "alarm", "alarm_event", b"a", meta={"route": "cellular"})
    assert not gate.send("x", "backup.storage.example", "backup_encrypted", "encrypted_blob",
                         b"a", meta={"route": "cellular", "encrypted": True})
    cid = gate.record_consent("anna", "clip/c/1")
    assert not gate.send("x", dest, "alarm", "camera_clip", size=10,
                         meta={"route": "cellular", "consent_id": cid, "clip_ref": "clip/c/1"})
    assert gate.blocked[-1]["reason"] == "clip_not_on_cellular"


def test_clip_leaves_only_with_single_use_consent():
    sim = Sim(seed=4)
    gate = DualPathEgressGate(sim, Bus(sim))
    dest = "receiver.monitoring.example"
    assert not gate.send("x", dest, "alarm", "camera_clip", size=10, meta={"clip_ref": "clip/c/1"})
    gate.settings["alarm_clips_allowed"] = True        # a global switch is not consent
    assert not gate.send("x", dest, "alarm", "camera_clip", size=10, meta={"clip_ref": "clip/c/1"})
    cid = gate.record_consent("anna", "clip/c/1")
    assert not gate.send("x", dest, "alarm", "camera_clip", size=10,
                         meta={"consent_id": cid, "clip_ref": "clip/c/2"})
    assert gate.send("x", dest, "alarm", "camera_clip", size=10,
                     meta={"consent_id": cid, "clip_ref": "clip/c/1"})
    assert not gate.send("x", dest, "alarm", "camera_clip", size=10,
                         meta={"consent_id": cid, "clip_ref": "clip/c/1"})


def _camera_rig(answer):
    sim = Sim(seed=5)
    bus = Bus(sim)
    link = WanLink(sim)
    centre = DualPathCentre(sim, link, {"1A2B3C": KEY})
    gate = DualPathEgressGate(sim, bus)
    hub = AlarmHub(sim, bus, link, centre, account="1A2B3C", key=KEY, egress=gate, heartbeat_s=0)
    cam = ObservingCamera(sim, bus, "camera.front")
    obs = CameraObserver(sim, bus, design="ask", alarm_hub=hub, gate=gate, centre=centre,
                         armed=lambda t: "set to away")
    asks = []

    def phone(e):
        if e.payload.get("kind") == "ask":
            asks.append(e.payload["text"])
            if answer:
                sim.after(10, bus.publish, Envelope(topic="reply/anna", source="phone",
                                                    ts=sim.now, payload={"request_id": e.payload["request_id"],
                                                                         "answer": answer}))
    bus.subscribe("notify/anna", phone, "phone")
    sim.at(50, cam.detect, "person", 0.62)
    sim.at(60, cam.detect, "pet", 0.9)
    sim.run(until=1000)
    return asks, gate, centre, obs


def test_ask_states_evidence_without_verdict_and_no_means_nothing_leaves():
    asks, gate, centre, obs = _camera_rig("no")
    assert len(asks) == 1
    t = asks[0].lower()
    assert "0.62" in asks[0] and "clip/camera.front/" in asks[0] and "could also be" in t
    assert not any(w in t for w in VERDICT_WORDS)
    assert not any(r["payload_class"] == "camera_clip" for r in gate.sent)
    assert not centre.incidents


def test_yes_shares_that_one_clip_and_raises_a_verified_alarm():
    asks, gate, centre, obs = _camera_rig("yes")
    clips = [r for r in gate.sent if r["payload_class"] == "camera_clip"]
    assert len(clips) == 1 and clips[0]["meta"]["clip_ref"] == obs.asks["cam00001"]["clip_ref"]
    assert centre.incidents and centre.incidents[0]["category"] == "burglary"
    assert centre.incidents[0]["clips"] == [clips[0]["meta"]["clip_ref"]]


def test_no_answer_asks_next_person_then_keeps_it_home():
    asks, gate, centre, obs = _camera_rig(None)
    outcomes = [a["outcome"] for a in obs.asks.values()]
    assert [a["person"] for a in obs.asks.values()] == ["anna", "mark"]
    assert "no_answer_kept_home" in outcomes and not centre.incidents
    assert not [r for r in gate.sent if r["payload_class"] == "camera_clip"]


def test_hub_has_no_way_to_call_911():
    r = sc.run_alarm(1, "internet_down_10min", "two_paths", "smoke")
    assert r["egress_destinations"] == ["receiver.monitoring.example"]
    assert r["centre_action"] == "centre_calls_911_with_address_sensor_time"
    for name in dir(DualPathAlarmHub):
        assert "911" not in name and "dispatch" not in name


def test_camera_week_is_deterministic_and_same_scenes_across_designs():
    a = sc.run_camera_week(7, "ask")
    b = sc.run_camera_week(7, "ask")
    c = sc.run_camera_week(7, "verdict")
    assert a == b
    assert a["scenes_by_truth"] == c["scenes_by_truth"]
    assert a["clips_left_without_consent"] == 0 and a["messages_with_verdict_words"] == 0


@pytest.mark.parametrize("cond", ["normal", "internet_down_10min"])
def test_two_paths_alarm_runs(cond):
    r = sc.run_alarm(11, cond, "two_paths", "leak")
    assert r["within_60s"] and r["incidents"] == 1


def test_results_json_shape():
    p = ROOT / "experiments" / "b08_security" / "results.json"
    r = json.loads(p.read_text())
    for k in ("experiment", "date", "method", "params", "results", "limitations"):
        assert k in r
    a = r["results"]["alarms"]
    assert set(a) == set(sc.CONDITIONS)
    assert a["internet_down_all_day"]["two_paths"]["within_60s_pct"] == 100.0
    assert r["results"]["egress_destinations_containing_911"] == []
