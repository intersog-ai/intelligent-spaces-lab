"""B09 What leaves the house: ledger, three-level answer, phone-home manifests, deletion."""
import json
import pathlib
import random

from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.alarm.egress import EgressGate
from islab.alarm.privacy import make_thumb
from islab.privacy2 import (ConsentResponder, LedgerGate, PhoneHomeDevice, SharedStore,
                            VoiceRouter, coverage, phone, raw, short, transcode, words)
from islab.privacy2.answer import categories

SETTINGS = {"cloud_speech_opt_in": True, "cloud_llm_opt_in": True, "alarm_clips_allowed": True}
RES = pathlib.Path(__file__).resolve().parents[1] / "experiments" / "b09_egress" / "results.json"


def _gate(**kw):
    sim = Sim(seed=1)
    bus = Bus(sim)
    return sim, bus, LedgerGate(sim, bus, settings=kw.pop("settings", SETTINGS), **kw)


# ---------------------------------------------------------------- the ledger
def test_every_attempt_has_a_ledger_row_and_bytes_add_up():
    sim, bus, g = _gate()
    g.send("voice.pipeline", "asr.provider.example", "speech_recognition_optional", "speech_audio",
           size=10_000, meta={"retention": "not_stored"})
    g.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check", size=1200)
    g.send("plug.cheap", "telemetry.unknown-vendor.example", "fleet_update", "telemetry", size=420)
    g.set_internet(False)
    g.send("backup.agent", "backup.storage.example", "backup_encrypted", "encrypted_blob",
           size=5000, meta={"encrypted": True}, msg_id="b")
    g.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check", size=1200)
    sim.run(until=10)
    outcomes = [r["outcome"] for r in g.ledger]
    assert outcomes == ["sent", "sent", "blocked", "queued", "dropped_offline"]
    g.set_internet(True)
    rep = g.day_report(0)
    assert rep["bytes"] == sum(r["bytes"] for r in g.sent) == 16_200
    assert rep["by_visibility"]["person"]["bytes"] == 10_000
    assert rep["by_purpose"]["speech_recognition_optional"]["retention"] == ["not stored"]
    assert {r["visibility"] for r in g.ledger if r["purpose"] == "fleet_update"} == {"background"}
    assert len(raw(g, 0).splitlines()) == len(g.ledger)


# ---------------------------------------------------------------- the answer
def _random_day(seed):
    rng = random.Random(seed)
    sim = Sim(seed=seed)
    bus = Bus(sim)
    g = LedgerGate(sim, bus, settings=dict(SETTINGS))
    senders = [
        lambda: g.send("voice.pipeline", "asr.provider.example", "speech_recognition_optional",
                       "speech_audio", size=9000, meta={"retention": "not_stored"}),
        lambda: g.send("voice.pipeline", "llm.provider.example", "llm_text_optional", "text", size=300),
        lambda: g.send("privacy.notify", "push.relay.example", "notify_push", "notify_text",
                       data=b"hello", meta={"person": "mark"}),
        lambda: g.send("alarm_hub", "receiver.monitoring.example", "alarm", "alarm_event", size=90,
                       msg_id=f"ev{rng.random()}"),
        lambda: g.send("alarm_hub", "receiver.monitoring.example", "alarm", "camera_clip", size=1500),
        lambda: g.send("alarm_hub", "receiver.monitoring.example", "alarm", "alarm_heartbeat", size=97),
        lambda: g.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check", size=1200),
        lambda: g.send("backup.agent", "backup.storage.example", "backup_encrypted", "encrypted_blob",
                       size=10**6, meta={"encrypted": True}, msg_id=f"bk{rng.random()}"),
        lambda: g.send(f"dev{rng.randint(0, 3)}", "x.example", "fleet_update", "telemetry", size=1),
        lambda: bus.publish(Envelope("egress/alarm", {}, source="cam", ts=sim.now)),
    ]
    for i, fn in enumerate(senders):
        for _ in range(rng.choice([0, 0, 1, 2, 7, 30])):
            sim.at(rng.random() * 86000, fn)
    if rng.random() < 0.4:
        a = rng.random() * 80000
        sim.at(a, g.set_internet, False)
        if rng.random() < 0.6:
            sim.at(a + rng.random() * 5000, g.set_internet, True)
    sim.run(until=86400 - 1e-6)
    return g


def test_short_answer_never_omits_a_category():
    """300 random days: every category that occurred is in the spoken answer; background may be
    grouped, but then its message count is said."""
    seen = set()
    for seed in range(300):
        g = _random_day(seed)
        rep = g.day_report(0)
        text = short(rep)
        cov = coverage(text, rep)
        assert cov["ok"], (seed, text, cov)
        seen |= set(categories(rep))
    assert {"speech", "llm_text", "push", "alarm_signal", "alarm_clip", "alarm_checkin",
            "update_check", "backup", "blocked", "queued", "dropped", "bypass"} <= seen


def test_background_grouped_but_mentioned_with_count():
    sim, bus, g = _gate()
    for _ in range(3):
        g.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check", size=1200)
    text = short(g.day_report(0))
    assert "background" in text.lower() and "three" in text
    assert text.startswith("Only background traffic")


def test_the_e09_line_and_the_b01_summary_omit_background():
    sim, bus, g = _gate()
    for _ in range(12):
        g.send("voice.pipeline", "asr.provider.example", "speech_recognition_optional",
               "speech_audio", size=9000, meta={"retention": "not_stored"})
    g.send("backup.agent", "backup.storage.example", "backup_encrypted", "encrypted_blob",
           size=48_000_000, meta={"encrypted": True})
    for _ in range(4):
        g.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check", size=1200)
    rep = g.day_report(0)
    e09 = coverage("Twelve speech requests, not stored. Last night's encrypted backup.", rep)
    assert not e09["ok"] and e09["missing"] == ["update_check"]
    b01 = coverage(EgressGate.summary_text(g, 0, include_routine=False), rep)
    assert not b01["ok"]
    assert coverage(short(rep), rep)["ok"]


def test_phone_level_lists_every_purpose_and_blocked_source():
    g = _random_day(7)
    rep = g.day_report(0)
    text = phone(rep)
    for p in rep["by_purpose"]:
        assert f"- {p}:" in text
    for s in rep["blocked_by_source"]:
        assert f"from {s}:" in text


def test_words():
    assert words(287) == "two hundred eighty-seven"
    assert words(1730) == "one thousand seven hundred thirty"
    assert words(12) == "twelve"


# ---------------------------------------------------------------- phone home, manifests
def test_mimic_update_check_passes_b01_rules_but_not_the_manifest():
    for enforce, expect_out in ((False, 144), (True, 0)):
        sim = Sim(seed=3)
        bus = Bus(sim)
        g = LedgerGate(sim, bus, settings=SETTINGS, enforce_manifests=enforce)
        dev = PhoneHomeDevice(sim, bus, g, "mimic_update_check")
        sim.run(until=86400)
        assert len(dev.got_out) == expect_out
        assert len(g.notices) == (1 if enforce else 0)


def test_low_and_slow_inside_the_manifest_is_not_detected():
    sim = Sim(seed=3)
    bus = Bus(sim)
    g = LedgerGate(sim, bus, settings=SETTINGS)
    dev = PhoneHomeDevice(sim, bus, g, "declared_sender_low_and_slow")
    sim.run(until=86400)
    assert len(dev.got_out) == 2 and not g.notices      # a known limit, stated in the paper


def test_bypass_is_logged_and_told():
    sim = Sim(seed=3)
    bus = Bus(sim)
    g = LedgerGate(sim, bus, settings=SETTINGS)
    PhoneHomeDevice(sim, bus, g, "bypass_gate")
    sim.run(until=3600)
    assert g.violations and g.notices[0]["reason"] == "bypass_attempt"


def test_voice_router_opt_out_and_offline():
    sim, bus, g = _gate(settings=dict(SETTINGS, cloud_speech_opt_in=False))
    v = VoiceRouter(sim, g)
    assert v.request(False, 9000) == "local_said_not_understood"
    assert v.request(True, 9000) == "local"
    assert g.ledger == []                      # opted out: the audio is never offered to the gate
    sim, bus, g = _gate()
    v = VoiceRouter(sim, g)
    g.set_internet(False)
    assert v.request(False, 9000) == "local_said_not_understood"
    assert g.ledger[-1]["outcome"] == "dropped_offline"


# ---------------------------------------------------------------- deletion
def _store(answer, delay=60.0, keys="per_record", mark_home=False, backup=False):
    sim = Sim(seed=5)
    bus = Bus(sim)
    g = LedgerGate(sim, bus, settings=SETTINGS)
    st = SharedStore(sim, bus, g, keys=keys, ask_timeout_s=3600,
                     people_home={"anna": True, "mark": mark_home})
    data = sim.rng.randbytes(5000)
    st.add("clip1", data, owners=("anna", "mark"))
    st.add("clip2", sim.rng.randbytes(5000), owners=("anna",))
    if backup:
        st.run_backup()
    ConsentResponder(sim, bus, "mark", st, delay, answer=answer)
    return sim, bus, g, st, data


def test_shared_delete_asks_the_other_owner_first():
    for answer, status, gone in (("yes", "deleted", True), ("no", "declined", False),
                                 (None, "no_answer", False)):
        sim, bus, g, st, data = _store(answer)
        rid = st.request_delete("clip1", "anna")
        sim.run(until=30)
        assert "clip1" in st.main                      # nothing before the answer
        sim.run(until=7200)
        rc = st.receipt(rid)
        assert rc["status"] == status and ("clip1" not in st.main) == gone
        assert "clip2" in st.main
        # mark is away: the ask left as a push, and only that
        pushes = [r for r in g.ledger if r["purpose"] == "notify_push" and r["outcome"] == "sent"]
        assert len(pushes) == 1


def test_not_an_owner_is_refused():
    sim, bus, g, st, data = _store("yes")
    rid = st.request_delete("clip2", "mark")
    assert st.receipt(rid)["status"] == "refused_not_an_owner" and "clip2" in st.main


def test_delete_finds_hidden_copies_and_says_what_it_did_not_check():
    sim, bus, g, st, data = _store("yes", mark_home=True)
    exports = st.register_location("exports", {})
    dump = st.unregistered_location("debug_dump", {})
    other = st.main["clip2"]
    exports["renamed.mp4"] = data
    exports["bundle.zip"] = other + data + other
    st.derive("clip1", "exports", "small.webm", transcode(data))
    exports["recoded_elsewhere.webm"] = transcode(data + b"!")
    dump["x"] = data
    dump["oldthumb"] = make_thumb(data)
    rid = st.request_delete("clip1", "anna")
    sim.run(until=3600)
    rc = st.receipt(rid)
    assert rc["status"] == "deleted"
    assert "renamed.mp4" not in exports and "small.webm" not in exports and dump == {}
    assert exports["bundle.zip"] == other + other               # cut out, the rest kept
    assert "recoded_elsewhere.webm" in exports                  # no lineage: not found
    assert any("lineage" in s for s in rc["not_checked"])
    assert "complete" not in rc
    assert st.main["clip2"] == other


def test_backup_before_delete_per_record_key_vs_nightly_key():
    sim, bus, g, st, data = _store("yes", mark_home=True, backup=True)
    assert st.restore("clip1") == data
    rid = st.request_delete("clip1", "anna")
    sim.run(until=3600)
    b = st.receipt(rid)["backup"]
    assert b["in_backup"] and b["key_destroyed"] and b["restore_after_delete"] is False
    assert st.restore("clip1") is None and st.restore("clip2") is not None
    sim, bus, g, st, data = _store("yes", mark_home=True, backup=True, keys="nightly")
    rid = st.request_delete("clip1", "anna")
    sim.run(until=3600)
    b = st.receipt(rid)["backup"]
    assert b["restore_after_delete"] is True and "stays readable" in b["statement"]


# ---------------------------------------------------------------- published results
def test_results_contract_and_headlines():
    d = json.loads(RES.read_text())
    for k in ("experiment", "date", "method", "params", "results", "limitations"):
        assert k in d
    r = d["results"]
    assert r["a_e09_day"]["short_answer_coverage"]["ok"]
    assert not r["a_e09_day"]["e09_scripted_line_coverage"]["ok"]
    assert r["b_thirty_days"]["short_answer_all_categories_days"] == r["b_thirty_days"]["days"] == 30
    assert r["c_phone_home"]["mimic_update_check"]["ledger_gate"]["got_out"] == 0
    assert r["c_phone_home"]["mimic_update_check"]["b01_gate"]["blocked_pct"] == 0.0
    e = r["e_deletion"]
    assert e["shared_per_record_keys"]["deleted_without_yes"] == 0
    assert e["shared_per_record_keys"]["backup_restorable_after_delete"] == 0
    assert e["b09"]["collateral_records_damaged"] == 0
    assert e["b09"]["receipt_claimed_complete"] == 0
    assert r["d_internet_down"]["internet_down"]["home_control_confirmed"] == 200
