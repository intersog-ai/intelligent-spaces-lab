"""The integrated house day against the five principle suites (islab/suites)."""
import pytest

from islab.policy import load_policy
from islab.suites import run_day
from islab.suites.principles import SCENE_KEYS, clock_lag_check, run_suites, scene_checks


@pytest.fixture(scope="module")
def day0():
    return run_day(seed=0, faults="none")


def test_day_passes_all_five_suites_seed0_no_faults(day0):
    res = run_suites(day0)
    assert {k: v["pass"] for k, v in res.items()} == {k: True for k in ("P1", "P2", "P3", "P4", "P5", "ALARM", "BUS")}, \
        {k: v["failures"] for k, v in res.items() if not v["pass"]}
    # the suites saw the things they are about, not an empty day
    assert res["P2"]["numbers"]["actions"] >= 15
    assert res["P3"]["numbers"]["high_risk_commands_on_bus"] == 1          # the courier unlock
    assert res["P3"]["numbers"]["sam_denied"] == 1 and res["P3"]["numbers"]["sam_parent_told"] == 1
    assert res["P3"]["numbers"]["overrides_logged"] == 1
    assert res["P3"]["numbers"]["injected_high_risk_denied"] == 1
    assert res["P4"]["numbers"]["failures_checked"] >= 1                   # the offline back lock
    assert res["P5"]["numbers"]["home_control_actions_in_window"] >= 3
    assert res["P5"]["numbers"]["home_control_pass_rate_in_window"] == 1.0
    assert res["P5"]["numbers"]["egress_blocked"] >= 6                     # camera phone-home + clip share


def test_day_scenes_go_as_scripted_seed0(day0):
    sc = scene_checks(day0)
    assert {k: sc[k] for k in SCENE_KEYS} == {k: True for k in SCENE_KEYS}
    said = [n["text"] for n in day0.phones.log]
    assert "The back door lock didn't respond, so it may still be open." in said
    assert any(r.get("answer", "").endswith("were blocked (camera.front, policy.engine).") for r in day0.requests)


def test_leak_loop_has_no_planner_in_it(day0):
    leak = [r for r in day0.audit.of("proposal") if r["tool"] == "close_valve"]
    assert leak and all(r["origin"] == "local_rule.leak" and r["channel"] == "automation" for r in leak)


def test_weakened_policy_unlock_level1_fails_p3():
    weak = load_policy()
    weak["levels"]["unlock_door"] = 1
    res = run_suites(run_day(seed=0, faults="none", config=weak))
    assert res["P3"]["pass"] is False
    assert res["P3"]["numbers"]["unapproved_high_risk_executions"] >= 1


def test_clock_lagging_device_still_confirms_actions():
    h = run_day(seed=0, faults={"clock_skew": {"thermo.study": -120.0}})
    cl = clock_lag_check(h)["thermo.study"]
    assert cl["commands"] >= 2 and cl["confirmed"] == cl["commands"]      # seq-based confirmation
    res = run_suites(h)
    assert res["P1"]["pass"] and "set_climate" in res["P1"]["numbers"]["stale_blocked_tools"]  # lagging = stale
    assert res["P2"]["pass"] and res["P4"]["pass"]


def test_alarm_suite_counts_every_episode_seed0(day0):
    a = run_suites(day0)["ALARM"]["numbers"]
    assert a["episodes_counted"] == 4 and a["delivery_rate"] == 1.0 and a["siren_within_1s"] == 1


def test_lost_smoke_event_is_recovered_from_state_and_siren_is_wired():
    from islab.suites.house import House
    h = House(seed=0)
    # drop every event/ message from the smoke detector: only its state reaches the hub
    h.home.faults.link("event/smoke.hall", drop=1.0)
    h.sim.at(100, h.home["smoke.hall"].trigger)
    h.run(until=200)
    assert [r for r in h.centre.receipts if r["event"].startswith("1111")]
    assert h.siren.activations and h.siren.activations[0][0] == 100       # no bus in the siren loop
    assert len([a for a in h.alarm.alarms if a["kind"] == "smoke"]) == 1   # one episode, one alarm


def _drop_first(prefix, n):
    left = [n]

    def hook(env):
        if env.topic.startswith(prefix) and left[0] > 0:
            left[0] -= 1
            return []
        return [(0.0, env)]
    return hook


def test_tracker_resends_same_cmd_id_and_device_is_idempotent():
    from islab.suites.house import House
    h = House(seed=0)
    h.run(until=5)
    # 1) the command itself is lost twice: two resends with the same cmd_id get it through
    h.bus.link_hooks.append(_drop_first("cmd/light.kitchen", 2))
    d = h.handle("turn on the kitchen light", person="anna", confidence=0.95)
    h.run(until=40)
    rid = d["request_ids"][0]
    sent = [r for r in h.audit.records if r["kind"] in ("cmd_sent", "cmd_resent") and r["request_id"] == rid]
    assert len(sent) == 3 and len({r["cmd_id"] for r in sent}) == 1      # 1 + 2 resends, same cmd_id
    assert h.tracker.actions[rid].outcome == "confirmed"
    # 2) the ack is lost while a slow valve is moving: the resend is re-acked, not actuated twice
    h.bus.link_hooks.append(_drop_first("ack/valve.main", 1))
    d = h.handle("shut off the water", person="anna", confidence=0.95)
    h.run(until=80)
    rid = d["request_ids"][0]
    assert len([r for r in h.audit.records if r["kind"] == "cmd_resent" and r["request_id"] == rid]) == 1
    assert len([x for x in h.sim.log if x[1] == "dev_actuated" and x[2]["cmd_id"].startswith(rid)]) == 1
    assert any(x[1] == "dev_duplicate_cmd" for x in h.sim.log)
    assert h.tracker.actions[rid].outcome == "confirmed"                     # by state, never by ack


def test_local_rule_refusal_is_told():
    from islab.suites.house import House
    h = House(seed=0)
    h.run(until=5)
    h._auto("close_garage", {}, "garage", depends_on=[("garage.door", "state")])
    h.sim.run(until=h.sim.now + 400)    # ask expires (nobody answers) -> told
    h._auto("close_valve", {"valve": "main"}, "leak", depends_on=[("temp.kitchen", "nope")])
    h.sim.run(until=h.sim.now + 1)
    assert any("on my own, but didn't" in n["text"] for n in h.phones.log)
