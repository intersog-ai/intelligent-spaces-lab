"""B04: local leak -> valve safety controller (islab/safety)."""
import json
import pathlib

import pytest

from islab.core.bus import Bus
from islab.core.sim import Sim
from islab.safety import controller as ctl
from islab.safety.explain import check, explain, hms
from islab.safety.journal import Journal
from islab.safety.notify import PUSH_CLASS, PUSH_PURPOSE, PUSH_RELAY, SafetyEgressGate
from islab.safety.scenario import run_leak


def kinds(records):
    return [r["kind"] for r in records]


# ---- journal ---------------------------------------------------------------------------------
def test_journal_crash_loses_only_unsynced(tmp_path):
    sim = Sim(1)
    j = Journal(sim, path=str(tmp_path / "j.jsonl"))
    j.append("a")
    j.sync()
    j.append("b")
    j.crash()
    assert kinds(j.records) == ["a"] and j.lost_on_crash == 1
    j2 = Journal(sim, path=str(tmp_path / "j.jsonl"))
    assert kinds(j2.durable) == ["a"]


def test_journal_ignores_torn_last_line(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(json.dumps({"rid": "r00001", "t": 0, "kind": "a"}) + "\n" + '{"rid": "r00002", "t": 1, "ki')
    assert kinds(Journal.load(str(p))) == ["a"]


# ---- the state machine -------------------------------------------------------------------------
def test_normal_leak_confirmed_by_valve_and_flow():
    r = run_leak(1, "state_machine", "normal")
    recs = r["records"]
    conf = [x for x in recs if x["kind"] == "closed_confirmed"]
    assert len(conf) == 1
    c = conf[0]
    assert c["position"] == 0 and c["valve_state"] == "closed" and c["flow_lpm"] <= 0.2
    # confirmed only after the valve physically closed
    assert r["truth"].position_at(c["t"]) == 0
    path = [x["to"] for x in recs if x["kind"] == "transition"]
    assert path == ["leak_detected", "closing", "closed_confirmed"]


def test_ack_is_not_enough_stuck_valve_escalates():
    r = run_leak(2, "state_machine", "valve_stuck")
    recs = r["records"]
    assert "closed_confirmed" not in kinds(recs)
    assert "cmd_ack" in kinds(recs)                         # the valve did acknowledge
    path = [x["to"] for x in recs if x["kind"] == "transition"]
    assert path == ["leak_detected", "closing", "close_failed", "escalated"]
    m = r["metrics"]
    assert 29.9 <= m["escalated_s"] <= 31.0
    esc = [x for x in recs if x["kind"] == "told" and x["tag"] == "escalated"][0]
    assert "by hand" in esc["text"] and "100% open" in esc["text"]
    assert m["valve_alarm_s"] is not None                   # second alarm reached the centre
    assert all(p.received for p in r["phones"])


def test_partial_valve_is_not_closed():
    r = run_leak(3, "state_machine", "valve_partial")
    assert "closed_confirmed" not in kinds(r["records"])
    cf = [x for x in r["records"] if x["kind"] == "close_failed"][0]
    assert cf["last_position"] == 40 and cf["last_flow_lpm"] > 1.0


def test_resends_reuse_cmd_id_and_valve_actuates_once():
    r = run_leak(4, "state_machine", "command_lost")
    sim_log = r["hub"].sim.log
    resent = [x for x in r["records"] if x["kind"] == "cmd_resent"]
    sent = {x["cmd_id"] for x in r["records"] if x["kind"] == "cmd_sent"}
    assert all(x["cmd_id"] in sent for x in resent)
    acts = [d["cmd_id"] for t, k, d in sim_log if k == "dev_actuated" and d["device"] == "valve.main"]
    assert len(acts) == len(set(acts))                      # never twice for one cmd_id


def test_duplicate_cmd_id_is_idempotent_at_the_valve():
    from islab.twin import build_home
    sim = Sim(5)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=5)
    sim.run(until=2)
    home.command("valve.main", "close", cmd_id="x1")
    home.command("valve.main", "close", cmd_id="x1")
    sim.run(until=20)
    acts = [d for t, k, d in sim.log if k == "dev_actuated" and d["device"] == "valve.main"]
    dups = [d for t, k, d in sim.log if k == "dev_duplicate_cmd" and d["device"] == "valve.main"]
    assert len(acts) == 1 and len(dups) == 1


def test_leak_event_lost_detected_from_state():
    r = run_leak(6, "state_machine", "leak_event_lost")
    det = [x for x in r["records"] if x["kind"] == "leak_detected"][0]
    assert det["via"] == "state"
    assert r["metrics"]["confirmed_s"] is not None
    assert run_leak(6, "ack_based", "leak_event_lost")["metrics"]["claim_s"] is None


def test_internet_down_valve_still_confirmed():
    r = run_leak(7, "state_machine", "internet_down")
    m = r["metrics"]
    assert m["confirmed_s"] is not None and m["confirmed_s"] < 15
    assert m["leak_alarm_s"] > 60                           # queued until the internet returns
    assert m["told_s"]["anna"] < 1 and m["told_s"]["mark"] > 60


def test_reboot_mid_action_resumes_from_journal():
    seen = 0
    for seed in range(40):
        r = run_leak(seed, "state_machine", "hub_reboot")
        recs = r["records"]
        if "resumed" not in kinds(recs):
            continue
        res = [x for x in recs if x["kind"] == "resumed"][0]
        if res["from_state"] != "closing":
            continue
        seen += 1
        assert "closed_confirmed" in kinds(recs)
        assert r["truth"].position_at([x for x in recs if x["kind"] == "closed_confirmed"][0]["t"]) == 0
        assert len({x["boot"] for x in recs}) == 2
    assert seen >= 5


def test_reboot_without_journal_forgets_the_action():
    r = run_leak(9, "state_machine", "hub_reboot", persist_journal=False)
    j2 = r["hub"].journals[-1]
    assert "resumed" not in kinds(j2.records)
    assert "closed_confirmed" not in kinds(j2.records) or \
        [x for x in j2.records if x["kind"] == "leak_detected"][0]["t"] - r["t_leak"] > 60


def test_crash_between_intent_and_send_loses_intent_and_resumes():
    sim = Sim(11)
    j = Journal(sim)
    j.new_boot()
    j.append("leak_detected", device="leak.sink", via="event", event_id="e", device_ts=0, episode="leak-1")
    j.append("transition", frm="idle", to="leak_detected")
    j.sync()
    j.append("cmd_intent", attempt=1, cmd_id="leak-1.a1.c0", device="valve.main", action="close")
    j.crash()                                    # fsync never finished
    assert kinds(j.durable) == ["leak_detected", "transition"]


def test_illegal_transition_rejected():
    r = run_leak(1, "state_machine", "normal")
    c = r["hub"].controller
    with pytest.raises(RuntimeError):
        c._transition(ctl.CLOSING)


def test_no_llm_no_internet_in_safety_package():
    src = pathlib.Path(ctl.__file__).parent
    text = "\n".join(p.read_text() for p in src.glob("*.py"))
    for banned in ("islab.policy.agent", "OpenAICompatibleLLM", "urllib", "requests", "socket"):
        assert banned not in text


# ---- baselines ----------------------------------------------------------------------------------
def test_fire_and_forget_claims_closed_while_stuck():
    m = run_leak(2, "fire_and_forget", "valve_stuck")["metrics"]
    assert m["claim_kind"] == "claimed_closed" and m["false_claim_30s"] and m["misled_at_60"]


def test_ack_based_claims_on_ack():
    m = run_leak(2, "ack_based", "valve_partial")["metrics"]
    assert m["claim_s"] < 1 and m["false_claim_30s"]


# ---- explanation --------------------------------------------------------------------------------
def test_explanation_matches_records_in_every_condition():
    from islab.safety.scenario import CONDITIONS
    for cond in CONDITIONS:
        for design in ("fire_and_forget", "ack_based", "state_machine"):
            recs = run_leak(13, design, cond)["records"]
            st = explain(recs)
            res = check(st, recs)
            assert res["unsupported"] == [], (cond, design, res["unsupported"])


def test_explanation_example_wording():
    r = run_leak(1, "state_machine", "normal")
    st = explain(r["records"])
    texts = [s["text"] for s in st]
    det = [x for x in r["records"] if x["kind"] == "leak_detected"][0]
    assert texts[0].startswith(f"At {hms(det['t'])[:5]} the sink sensor reported water")
    assert any(t.startswith("It was confirmed closed at ") for t in texts)


def test_checker_catches_wrong_time_invented_fact_and_bad_quote():
    recs = run_leak(2, "state_machine", "valve_stuck")["records"]
    st = explain(recs)
    good = check(st, recs)
    assert good["unsupported"] == []
    tampered = [dict(s) for s in st]
    i = next(i for i, s in enumerate(tampered) if "had not confirmed" in s["text"])
    tampered[i]["text"] = tampered[i]["text"].replace("100%", "0%")
    tampered.append({"text": "It was confirmed closed at 09:13:00.", "refs": [tampered[0]["refs"][0]]})
    tampered.append({"text": "The plumber was called at 09:13:00.", "refs": []})
    q = next(i for i, s in enumerate(tampered) if s["text"].startswith("People were told"))
    tampered[q]["text"] = tampered[q]["text"].replace("Closing", "Closed")
    bad = check(tampered, recs)
    assert len(bad["unsupported"]) == 4


# ---- egress ----------------------------------------------------------------------------------------
def test_only_alarm_and_push_text_leave_the_home():
    r = run_leak(2, "state_machine", "valve_stuck")
    m = r["metrics"]
    assert set(m["egress"]) <= {"alarm_event", "alarm_heartbeat", "notify_text"}
    assert m["egress_violations"] == 0


def test_push_with_extra_fields_is_blocked():
    sim = Sim(1)
    bus = Bus(sim)
    g = SafetyEgressGate(sim, bus)
    assert g.send("x", PUSH_RELAY, PUSH_PURPOSE, PUSH_CLASS, b"hi", meta={"person": "mark"})
    assert not g.send("x", PUSH_RELAY, PUSH_PURPOSE, PUSH_CLASS, b"hi",
                      meta={"person": "mark", "journal": "..."})
    assert not g.send("x", "evil.example", PUSH_PURPOSE, PUSH_CLASS, b"hi")
    assert not g.send("x", PUSH_RELAY, PUSH_PURPOSE, "video", b"hi")
