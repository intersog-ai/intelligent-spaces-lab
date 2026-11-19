"""B03: reading-quality layer (islab.readings) and the b03_readings experiment."""
import pathlib
import sys

import pytest

from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim
from islab.readings import (DEAD, DISPUTED, HELD, OK, REJECT_DUPLICATE, REJECT_OUT_OF_ORDER,
                            REJECT_RANGE, REJECT_UNSOURCED, STALE, STUCK, UNKNOWN, NaiveView,
                            QualityView, ReadingQuality, TimestampView, max_age_from_devices)
from islab.twin import build_home

ROOT = pathlib.Path(__file__).resolve().parents[1]
REG = {"temp.a": {"kind": "temperature"}, "temp.b": {"kind": "temperature"},
       "leak.x": {"kind": "leak"}, "win.x": {"kind": "contact"}}


def mk(**kw):
    sim = Sim(seed=1)
    kw.setdefault("sweep_s", None)
    q = ReadingQuality(sim, None, registry=REG, max_age={"temp.a": 150, "temp.b": 150, "leak.x": 750},
                       **kw)
    return sim, q


def env(dev, payload, ts, seq, eid=None, topic="state", source=None):
    e = Envelope(topic=f"{topic}/{dev}", payload=payload, source=source or dev, ts=ts, seq=seq)
    if eid:
        e.event_id = eid
    return e


def feed(sim, q, t, e):
    sim.now = t
    return q.ingest(e)


# ---- provenance, duplicates, order --------------------------------------------------------------
def test_unsourced_reading_is_rejected():
    sim, q = mk()
    assert feed(sim, q, 1, env("temp.a", {"temperature": 21.0}, 1, 1, source="someone.else")) == REJECT_UNSOURCED
    assert feed(sim, q, 1, env("temp.a", {"temperature": 21.0}, None, 2)) == REJECT_UNSOURCED
    assert q.assess("temp.a", "temperature").status == UNKNOWN


def test_duplicates_by_event_id_and_by_seq():
    sim, q = mk()
    e = env("temp.a", {"temperature": 21.0}, 10, 1, eid="e1")
    assert feed(sim, q, 10, e) == "accepted"
    assert feed(sim, q, 10.2, e) == REJECT_DUPLICATE                       # same event id
    assert feed(sim, q, 10.3, env("temp.a", {"temperature": 21.0}, 10, 1, eid="e2")) == REJECT_DUPLICATE  # same seq
    assert q.stats["duplicate"] == 2


def test_late_state_rejected_late_event_kept():
    sim, q = mk()
    feed(sim, q, 10, env("win.x", {"state": "closed"}, 10, 5))
    assert feed(sim, q, 11, env("win.x", {"state": "open"}, 4, 4)) == REJECT_OUT_OF_ORDER
    assert q.assess("win.x", "state").value == "closed"
    assert feed(sim, q, 12, env("leak.x", {"type": "leak"}, 3, 3, topic="event")) == "accepted"


# ---- clock skew and staleness ------------------------------------------------------------------
def test_skew_is_estimated_and_corrected_so_a_lagging_clock_is_not_stale():
    sim, q = mk()
    issues = []
    q.on_issue(issues.append)
    for i in range(20):
        t = 1000 + 60 * i
        feed(sim, q, t, env("temp.a", {"temperature": 21.0 + 0.01 * (i % 3)}, t - 300, i + 1))
    assert q.skew("temp.a") == pytest.approx(300, abs=0.01)
    a = q.assess("temp.a", "temperature")
    assert a.status == OK                       # device ts is 300 s old, corrected it is fresh
    assert any(i["kind"] == "clock_skew" for i in issues)


def test_silence_makes_a_reading_stale_and_the_sweep_tells():
    sim, q = mk()
    issues = []
    q.on_issue(issues.append)
    feed(sim, q, 100, env("temp.a", {"temperature": 21.0}, 100, 1))
    sim.now = 200
    assert q.assess("temp.a", "temperature").ok
    sim.now = 260
    assert q.assess("temp.a", "temperature").status == STALE
    q.sweep()
    assert [i["kind"] for i in issues] == ["stale"]
    q.sweep()
    assert len(issues) == 1                     # one notice per episode


def test_frozen_timestamp_is_stale():
    sim, q = mk()
    feed(sim, q, 100, env("temp.a", {"temperature": 21.0}, 100, 1))
    feed(sim, q, 160, env("temp.a", {"temperature": 21.0}, 100, 2))   # replayed snapshot
    assert q.assess("temp.a", "temperature").status == STALE


def test_last_gasp_battery_zero_is_dead_at_once():
    sim, q = mk()
    issues = []
    q.on_issue(issues.append)
    feed(sim, q, 100, env("leak.x", {"leak": False, "battery": 0}, 100, 1))
    assert q.assess("leak.x", "leak").status == DEAD
    assert issues[0]["kind"] == "battery_empty"


# ---- plausibility ---------------------------------------------------------------------------------
def test_spike_is_held_then_rejected():
    sim, q = mk()
    feed(sim, q, 0, env("temp.a", {"temperature": 21.0}, 0, 1))
    assert feed(sim, q, 60, env("temp.a", {"temperature": 36.0}, 60, 2)) == HELD
    assert q.assess("temp.a", "temperature").value == 21.0
    assert feed(sim, q, 120, env("temp.a", {"temperature": 21.1}, 120, 3)) == "accepted"
    assert q.assess("temp.a", "temperature").value == 21.1
    assert q.stats["held_rejected"] == 1


def test_real_fast_step_is_held_one_report_then_accepted():
    sim, q = mk()
    feed(sim, q, 0, env("temp.a", {"temperature": 21.0}, 0, 1))
    assert feed(sim, q, 60, env("temp.a", {"temperature": 26.0}, 60, 2)) == HELD
    feed(sim, q, 120, env("temp.a", {"temperature": 26.1}, 120, 3))
    assert q.assess("temp.a", "temperature").value == 26.1
    assert q.stats["held_confirmed"] == 1


def test_out_of_range_rejected():
    sim, q = mk()
    assert feed(sim, q, 0, env("temp.a", {"temperature": 85.0}, 0, 1)) == REJECT_RANGE


def test_stuck_sensor_detected_and_cleared():
    sim, q = mk()
    for i in range(25):
        feed(sim, q, 60 * i, env("temp.a", {"temperature": 20.43}, 60 * i, i + 1))
    assert q.assess("temp.a", "temperature").status == STUCK
    feed(sim, q, 60 * 25, env("temp.a", {"temperature": 20.6}, 60 * 25, 26))
    assert q.assess("temp.a", "temperature").ok


def test_noisy_sensor_is_not_stuck():
    sim, q = mk()
    for i in range(40):
        feed(sim, q, 60 * i, env("temp.a", {"temperature": 21.0 + 0.02 * (i % 4)}, 60 * i, i + 1))
    assert q.assess("temp.a", "temperature").ok


# ---- latched alarm events -------------------------------------------------------------------------
def test_leak_event_with_only_dry_heartbeats_is_disputed_until_a_person_confirms():
    sim, q = mk()
    issues = []
    q.on_issue(issues.append)
    feed(sim, q, 0, env("leak.x", {"leak": False}, 0, 1))
    feed(sim, q, 10, env("leak.x", {"type": "leak"}, 10, 2, topic="event"))
    a = q.assess("leak.x", "leak")
    assert a.ok and a.value is True
    feed(sim, q, 300, env("leak.x", {"leak": False}, 300, 3))
    assert q.assess("leak.x", "leak").status == DISPUTED
    assert issues[-1]["kind"] == "disputed"
    q.confirm_clear("leak.x", "leak")
    a = q.assess("leak.x", "leak")
    assert a.ok and a.value is False


def test_normal_leak_clears_when_the_device_goes_wet_then_dry():
    sim, q = mk()
    feed(sim, q, 0, env("leak.x", {"leak": True}, 0, 1))
    feed(sim, q, 0.1, env("leak.x", {"type": "leak"}, 0.1, 2, topic="event"))
    feed(sim, q, 600, env("leak.x", {"leak": False}, 600, 3))
    a = q.assess("leak.x", "leak")
    assert a.ok and a.value is False


# ---- disagreement ----------------------------------------------------------------------------------
def test_two_sources_disagree_after_hold_time():
    sim, q = mk()
    issues = []
    q.on_issue(issues.append)
    q.add_pair("room", ("temp.a", "temperature"), ("temp.b", "temperature"), tol=1.5, hold_s=600)
    for i in range(15):
        t = 60 * i
        feed(sim, q, t, env("temp.a", {"temperature": 20.0 + 0.01 * (i % 2)}, t, 2 * i + 1))
        feed(sim, q, t + 1, env("temp.b", {"temperature": 22.0 + 0.01 * (i % 2)}, t + 1, 2 * i + 2))
    assert q.pair("room")["state"] == "disagree"
    assert any(i["kind"] == "disagreement" for i in issues)


# ---- views on the twin ------------------------------------------------------------------------------
def test_views_on_a_reordered_window():
    sim = Sim(seed=3)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=3)
    max_age = max_age_from_devices(home.devices)
    naive, ts = NaiveView(sim, bus), TimestampView(sim, bus, max_age)
    qv = QualityView(sim, bus, home.registry, max_age)
    sim.run(until=10)
    # 'open' arrives 20 s late, after 'closed': the window is really closed
    hook = home.faults.link(["state/window.kitchen"], delay=20.0)
    home["window.kitchen"].set_open(True)
    home.faults.remove_link(hook)
    sim.run(until=11)
    home["window.kitchen"].set_open(False)
    sim.run(until=40)
    assert naive.get("window.kitchen", "state").value == "open"       # latest arrival wins: wrong
    assert ts.get("window.kitchen", "state").value == "closed"
    assert qv.get("window.kitchen", "state").value == "closed"
    assert qv.q.stats["out_of_order"] == 1


def test_max_age_from_heartbeats():
    sim = Sim(seed=0)
    home = build_home(sim, Bus(sim), seed=0)
    m = max_age_from_devices(home.devices)
    assert m["temp.kitchen"] == 150 and m["leak.sink"] == 750 and m["thermo.kitchen"] == 75


# ---- the experiment (short) ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def b03():
    sys.path.insert(0, str(ROOT / "experiments" / "b03_readings"))
    import run as b03run
    yield b03run
    sys.path.remove(str(ROOT / "experiments" / "b03_readings"))
    sys.modules.pop("run", None)


def test_experiment_three_days(b03):
    r = b03.run_seed(0, days=3)          # spike, stuck, real leak, one-shot leak, start of drift
    n, t, q = r["naive"], r["timestamp"], r["quality"]
    lies = ("stuck", "spike", "leak_oneshot", "duplicate", "reorder")
    assert sum(n["bad_by_fault"].get(k, 0) for k in lies) > 100
    assert sum(q["bad_by_fault"].get(k, 0) for k in lies) == 0
    assert q["false_alarms"] == 0 and n["false_alarms"] > 0
    assert n["leak_water_min_valve_open"] > 60 and q["leak_water_min_valve_open"] == 0
    assert q["refusal_episodes_told"] == q["refusal_episodes"] > 0
    assert q["detect_stuck_s"] is not None and q["detect_stuck_s"] < 1500
    assert t["bad_by_fault"].get("stuck", 0) > 0


def test_experiment_is_deterministic(b03):
    assert b03.run_seed(5, days=1) == b03.run_seed(5, days=1)
