import random

import pytest

from islab.core.sim import Sim
from islab.core.bus import Bus, Envelope
from islab.alarm import dc09
from islab.alarm.path import AlarmHub, LocalSiren, MonitoringCentre, WanLink
from islab.alarm.egress import EgressGate
from islab.alarm.privacy import RecordingStore

KEY = b"0123456789ABCDEF"


# ---------------------------------------------------------------- DC-09 encoder
def test_crc16_arc_check_value():
    assert dc09.crc16(b"123456789") == 0xBB3D        # CRC-16/ARC catalogue check value


def test_crc_matches_pysiaalarm():
    ev = pytest.importorskip("pysiaalarm.event")
    rng = random.Random(1)
    for _ in range(200):
        s = "".join(chr(rng.randint(32, 126)) for _ in range(rng.randint(1, 80)))
        assert f"{dc09.crc16(s.encode()):04X}" == ev.BaseEvent._crc_calc(s)


def _msg(**kw):
    base = dict(mtype=dc09.ADM_CID, account="1A2B3C", sequence=42, receiver="12", line="3",
                event="1111 01 001", timestamp=dc09.sim_timestamp(3600))
    base.update(kw)
    return dc09.DC09Message(**base)


def test_frame_layout_plain_adm_cid():
    f = dc09.encode(_msg())
    assert f.startswith(b"\n") and f.endswith(b"\r")
    s = f.decode()[1:-1]
    body = s[8:]
    assert s[4:8] == f"0{len(body):03X}"
    assert int(s[:4], 16) == dc09.crc16(body.encode())
    assert body == '"ADM-CID"0042R12L3#1A2B3C[#1A2B3C|1111 01 001]_01:00:00,11-01-2026'


@pytest.mark.parametrize("mtype,event", [(dc09.ADM_CID, "3154 01 002"),
                                          (dc09.SIA_DCS, "Nri1/WA002"), (dc09.NULL, "")])
@pytest.mark.parametrize("enc", [False, True])
def test_roundtrip(mtype, event, enc):
    m = _msg(mtype=mtype, event=event, encrypted=enc, xdata=["Vhall"] if mtype == dc09.SIA_DCS else [])
    d = dc09.decode(dc09.encode(m, key=KEY if enc else None), key=KEY if enc else None)
    assert d.crc_ok and d.length_ok
    assert (d.token, d.sequence, d.receiver, d.line, d.event, d.encrypted) == \
        (mtype, 42, "12", "3", event, enc)
    assert d.timestamp == m.timestamp
    if mtype != dc09.NULL:
        assert d.account == "1A2B3C"


def test_encrypted_payload_is_hex_blocks_and_needs_right_key():
    f = dc09.encode(_msg(encrypted=True), key=KEY)
    s = f.decode()
    assert '"*ADM-CID"' in s
    hexpart = s.split("[", 1)[1].strip("\r")
    assert len(hexpart) % 32 == 0 and all(c in "0123456789ABCDEF" for c in hexpart)
    assert "1111" not in s                                     # event not in clear
    with pytest.raises(dc09.DC09Error):
        dc09.decode(f, key=b"X" * 16)


def test_validation():
    with pytest.raises(dc09.DC09Error):
        dc09.encode(_msg(account="XYZ!"))
    with pytest.raises(dc09.DC09Error):
        dc09.encode(_msg(event="111 1 1"))
    with pytest.raises(dc09.DC09Error):
        dc09.encode(_msg(encrypted=True, timestamp=None), key=KEY)


def test_ack_roundtrip_plain_and_encrypted():
    for enc in (False, True):
        d = dc09.decode(dc09.encode(_msg(encrypted=enc), key=KEY if enc else None), KEY)
        ack = dc09.build_ack(d, KEY, dc09.sim_timestamp(3601))
        r = dc09.parse_response(ack, KEY)
        assert r["kind"] == "ACK" and r["sequence"] == 42 and r["crc_ok"] and r["account"] == "1A2B3C"
    assert dc09.parse_response(dc09.build_nak(dc09.sim_timestamp(0)))["kind"] == "NAK"


def test_interop_with_pysiaalarm():
    """Real sockets against the receiver Home Assistant uses. Pins the exact outcomes."""
    pytest.importorskip("pysiaalarm")
    from islab.alarm.interop import run_matrix
    m = run_matrix()
    known_mismatch = {"adm_cid_leak_1154", "no_receiver_field", "two_frames_one_tcp_write"}
    for transport, rows in m["transports"].items():
        res = {r["case"]: r for r in rows}
        for name, r in res.items():
            if name in known_mismatch:
                continue
            assert r["pass"], (transport, name, r)
        assert res["adm_cid_leak_1154"]["response"] == "ACK"
        assert res["adm_cid_leak_1154"]["callback_code"] == "YN"     # CID 154 unmapped there
        assert res["no_receiver_field"]["response_header_nonstandard"]  # ACK says 'None'
        assert res["enc128_adm_cid_smoke"]["response_decrypted"].startswith("]_")
        if transport == "aio_TCP":
            assert res["two_frames_one_tcp_write"]["callback_fired"] == 0


# ---------------------------------------------------------------- alarm path
def _house(loss=0.0, outages=(), key=None, egress=False, heartbeat_s=60.0, **hubkw):
    sim = Sim(seed=3)
    bus = Bus(sim)
    link = WanLink(sim, loss=loss, outages=outages)
    centre = MonitoringCentre(sim, link, {"1A2B3C": key}, supervision_s=300)
    gate = EgressGate(sim, bus) if egress else None
    hub = AlarmHub(sim, bus, link, centre, account="1A2B3C", key=key, egress=gate,
                   heartbeat_s=heartbeat_s, **hubkw)
    siren = LocalSiren(sim, bus)
    notes = []
    bus.subscribe("notify/+", lambda e: notes.append(e.payload["text"]), name="phone")
    return sim, bus, link, centre, hub, siren, gate, notes


def _event(bus, sim, dev, typ, **extra):
    bus.publish(Envelope(topic=f"event/{dev}", payload={"type": typ, **extra}, source=dev, ts=sim.now))


def test_smoke_reaches_centre_and_centre_dispatches():
    sim, bus, link, centre, hub, siren, gate, notes = _house(egress=True)
    sim.at(10, _event, bus, sim, "smoke.hall", "smoke")
    sim.run(until=40)
    assert len(centre.receipts) >= 1
    alarm = [r for r in centre.receipts if r["token"] == dc09.ADM_CID]
    assert alarm[0]["event"] == "1111 01 001"
    assert len(centre.dispatches) == 1
    assert siren.activations and siren.activations[0][0] < 10.1
    assert hub.alarms[0]["t_ack"] is not None and hub.alarms[0]["t_ack"] - 10 < 1.0
    assert any("monitoring centre" in n for n in notes)
    assert gate.sent and all(r["purpose"] == "alarm" for r in gate.sent)


def test_hub_cannot_dispatch():
    names = [n.lower() for n in dir(AlarmHub)]
    assert not any("dispatch" in n or "911" in n or "emergency" in n for n in names)
    sim, bus, link, centre, hub, siren, gate, notes = _house()
    topics = []
    bus.subscribe("#", lambda e: topics.append((e.source, e.topic)), name="spy")
    sim.at(5, _event, bus, sim, "smoke.hall", "smoke")
    sim.run(until=30)
    assert {t for s, t in topics if s == AlarmHub.NAME} <= {"notify/owner"}


def test_retries_through_outage_and_deduplicates():
    sim, bus, link, centre, hub, siren, gate, notes = _house(outages=[(5, 50)])
    sim.at(10, _event, bus, sim, "leak.sink", "leak")
    sim.run(until=200)
    rec = hub.alarms[0]
    assert rec["attempts"] > 1 and rec["t_ack"] is not None and rec["t_ack"] >= 50
    assert len([d for d in centre.dispatches if "154" in d["event"]]) == 1
    assert siren.activations[0][1] == "leak.sink"            # local chime regardless


def test_lost_acks_cause_duplicates_that_centre_suppresses():
    sim, bus, link, centre, hub, siren, gate, notes = _house(loss=0.4)
    for i in range(20):
        sim.at(10 + 30 * i, hub.raise_alarm, "intrusion", 3)
    sim.run(until=2000)
    assert all(a["t_ack"] is not None for a in hub.alarms)
    assert centre.duplicates > 0
    assert len(centre.dispatches) == 20


def test_siren_independent_of_hub_and_wan():
    sim, bus, link, centre, hub, siren, gate, notes = _house(outages=[(0, 10_000)])
    hub.crash()
    sim.at(5, _event, bus, sim, "smoke.hall", "smoke")
    sim.run(until=100)
    assert siren.activations and siren.activations[0][0] == pytest.approx(5.01)
    assert centre.receipts == []
    s2 = LocalSiren(Sim())                                   # no bus at all: hard-wired
    s2.hardwire("smoke.hall")
    assert s2.activations


def test_hub_exception_does_not_reach_bus():
    sim, bus, link, centre, hub, siren, gate, notes = _house()
    hub._classify = lambda *a: 1 / 0
    sim.at(5, _event, bus, sim, "smoke.hall", "smoke")
    sim.run(until=10)
    assert hub.errors and siren.activations


def test_bus_duplicate_event_sends_once():
    sim, bus, link, centre, hub, siren, gate, notes = _house()
    env = Envelope(topic="event/smoke.hall", payload={"type": "smoke"}, source="smoke.hall", ts=0)
    sim.at(5, bus.publish, env)
    sim.at(5.5, bus.publish, env)
    sim.run(until=30)
    assert len(hub.alarms) == 1


def test_supervision_heartbeat_detects_line_fault():
    sim, bus, link, centre, hub, siren, gate, notes = _house(outages=[(400, 1400)])
    sim.run(until=2000)
    assert centre.line_faults and 400 < centre.line_faults[0][0] < 800
    assert any("link is down" in n for n in notes)
    assert any("restored" in n for n in notes)
    assert centre.dispatches == []                           # heartbeats never dispatch


def test_encrypted_sia_dcs_path_and_intrusion_when_armed():
    sim, bus, link, centre, hub, siren, gate, notes = _house(key=KEY, fmt=dc09.SIA_DCS, armed=True)
    sim.at(5, _event, bus, sim, "motion.kitchen", "motion")
    sim.at(6, _event, bus, sim, "lock.front", "forced_entry")
    sim.run(until=30)
    evs = sorted(r["event"] for r in centre.receipts if r["token"] == dc09.SIA_DCS)
    assert evs == ["Nri1/BA003", "Nri1/BA007"]
    assert not siren.activations                             # siren is for smoke/leak only


def test_disarmed_motion_is_not_an_alarm():
    sim, bus, link, centre, hub, siren, gate, notes = _house()
    sim.at(5, _event, bus, sim, "motion.kitchen", "motion")
    sim.run(until=20)
    assert hub.alarms == []


# ---------------------------------------------------------------- egress gate
def _gate(**settings):
    sim = Sim(seed=1)
    bus = Bus(sim)
    out = []
    bus.subscribe("egress/#", lambda e: out.append(e), name="isp")
    return sim, bus, EgressGate(sim, bus, settings=settings), out


def test_allowlisted_send_publishes_from_gate():
    sim, bus, g, out = _gate()
    got = []
    assert g.send("alarm_hub", "receiver.monitoring.example", "alarm", "alarm_event",
                  b"x" * 60, transport=got.append)
    sim.run()
    assert got == [b"x" * 60] and out[0].topic == "egress/alarm" and out[0].source == g.NAME
    assert g.report(0)["by_purpose"]["alarm"]["bytes"] == 60


def test_phone_home_device_blocked_and_logged():
    sim, bus, g, out = _gate()
    assert not g.send("plug.cheap", "telemetry.unknown-vendor.example", "fleet_update",
                      "update_check", b"hi")
    assert not g.send("plug.cheap", "updates.vendor.example", "telemetry", "telemetry", b"hi")
    r = g.report(0)
    assert r["blocked"] == 2 and r["blocked_by_source"] == {"plug.cheap": 2}
    assert "destination_not_allowlisted" in r["blocked_by_reason"]
    sim.run()
    assert out == []


@pytest.mark.parametrize("purpose,cls,dest,settings,meta,ok", [
    ("alarm", "video", "receiver.monitoring.example", {"alarm_clips_allowed": True}, {}, False),
    ("alarm", "audio", "receiver.monitoring.example", {}, {}, False),
    ("alarm", "camera_clip", "receiver.monitoring.example", {}, {}, False),
    ("alarm", "camera_clip", "receiver.monitoring.example", {"alarm_clips_allowed": True}, {}, True),
    ("backup_encrypted", "camera_clip", "backup.storage.example", {"alarm_clips_allowed": True}, {"encrypted": True}, False),
    ("backup_encrypted", "encrypted_blob", "backup.storage.example", {}, {}, False),
    ("backup_encrypted", "encrypted_blob", "backup.storage.example", {}, {"encrypted": True}, True),
    ("speech_recognition_optional", "speech_audio", "asr.provider.example", {}, {"retention": "not_stored"}, False),
    ("speech_recognition_optional", "speech_audio", "asr.provider.example", {"cloud_speech_opt_in": True}, {}, False),
    ("speech_recognition_optional", "speech_audio", "asr.provider.example", {"cloud_speech_opt_in": True}, {"retention": "not_stored"}, True),
    ("llm_text_optional", "text", "llm.provider.example", {}, {}, False),
    ("llm_text_optional", "text", "llm.provider.example", {"cloud_llm_opt_in": True}, {}, True),
    ("llm_text_optional", "speech_audio", "llm.provider.example", {"cloud_llm_opt_in": True}, {}, False),
    ("alarm", "alarm_event", "backup.storage.example", {}, {}, False),
    ("fleet_update", "update_check", "updates.vendor.example", {}, {}, True),
])
def test_payload_rules(purpose, cls, dest, settings, meta, ok):
    sim, bus, g, out = _gate(**settings)
    assert g.send("x", dest, purpose, cls, b"1234", meta=meta) is ok


def test_watchdog_flags_egress_not_from_gate():
    sim, bus, g, out = _gate()
    bus.publish(Envelope(topic="egress/alarm", payload={}, source="camera.front", ts=0))
    sim.run()
    assert g.violations and g.violations[0]["source"] == "camera.front"


def test_internet_down_queue_or_drop():
    sim, bus, g, out = _gate(cloud_speech_opt_in=True)
    got = []
    g.set_internet(False)
    assert g.send("hub", "receiver.monitoring.example", "alarm", "alarm_event", b"a", transport=got.append, msg_id="m1")
    assert g.send("hub", "receiver.monitoring.example", "alarm", "alarm_event", b"b", transport=got.append, msg_id="m1")
    assert not g.send("hub", "receiver.monitoring.example", "alarm", "alarm_heartbeat", b"h", transport=got.append)
    assert not g.send("voice", "asr.provider.example", "speech_recognition_optional", "speech_audio",
                      b"pcm", meta={"retention": "not_stored"})
    assert len(g.queue) == 1 and len(g.dropped_offline) == 2 and got == []
    g.set_internet(True)
    assert got == [b"b"] and g.queue == []          # one copy, the newest retry


def test_e09_summary_sentence():
    sim, bus, g, out = _gate(cloud_speech_opt_in=True)
    for i in range(12):
        g.send("voice", "asr.provider.example", "speech_recognition_optional", "speech_audio",
               size=30_000, meta={"retention": "not_stored"})
    g.send("backup", "backup.storage.example", "backup_encrypted", "encrypted_blob",
           size=50_000_000, meta={"encrypted": True})
    assert g.summary_text(0, include_routine=False) == (
        "Twelve voice requests for speech recognition, not stored, and last night's encrypted "
        "backup. No camera footage left the house.")


# ---------------------------------------------------------------- privacy
def _store():
    sim = Sim()
    st = RecordingStore(sim, retention_days=7)
    return sim, st


def test_delete_removes_every_copy_with_receipt():
    sim, st = _store()
    st.add("rec1", b"clip-one" * 100)
    st.add("rec10", b"clip-ten" * 100)
    r = st.delete("rec1")
    locs = {x["location"] for x in r["removed"]}
    assert locs == {"main", "index", "thumbs", "backup_queue"}
    assert r["verified_absent_locally"] and r["complete"] and r["receipt_id"]
    assert "rec10" in st.main and "rec10" in st.index and len(st.backup_queue) == 1


def test_delete_finds_planted_copies():
    sim, st = _store()
    data = b"front-door-clip" * 50
    st.add("rec7", data)
    export = st.register_location("export_cache", {})
    export["share-9f2c.mp4"] = bytes(data)                   # copy under an unrelated name
    st.main["tmp_transcode_0001"] = bytes(data)              # stray copy inside main store
    st.thumbs["grid_cache_x"] = st.thumbs["rec7"]            # stray thumbnail copy
    r = st.delete("rec7")
    found = {(x["location"], x["key"]) for x in r["removed"]}
    assert ("export_cache", "share-9f2c.mp4") in found
    assert ("main", "tmp_transcode_0001") in found
    assert ("thumbs", "grid_cache_x") in found
    assert export == {} and r["verified_absent_locally"]


def test_retention_purge():
    sim, st = _store()
    st.add("old", b"a" * 10)
    sim.run(until=3 * 86400)
    st.add("new", b"b" * 10)
    sim.run(until=8 * 86400)
    receipts = st.purge_expired()
    assert [r["rec_id"] for r in receipts] == ["old"] and "new" in st.main


def test_delete_after_backup_is_honest_about_offsite_copy():
    sim, st = _store()
    st.add("rec3", b"z" * 64)
    st.run_backup()
    r = st.delete("rec3")
    assert r["verified_absent_locally"] and not r["complete"]
    assert r["pending_remote"][0]["location"] == "offsite_backup"
