"""Tests for islab.twin: determinism, command/ack/state cycles, every fault type."""
import pytest

from islab.core.bus import Bus
from islab.core.sim import Sim
from islab.twin import DEFAULT_DAY, HOSTILE_NAME, LAYOUT, build_home, parse_time


class Recorder:
    """Collects every envelope delivered on the bus."""

    def __init__(self, bus):
        self.msgs = []
        bus.subscribe("#", self.msgs.append, name="recorder")

    def on(self, topic):
        return [e for e in self.msgs if e.topic == topic]

    def acks(self, dev, cmd_id):
        return [e for e in self.on(f"ack/{dev}") if e.payload["cmd_id"] == cmd_id]


def make(seed=0):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed)
    return sim, bus, home, Recorder(bus)


def confirmed(rec, dev, expected, after_ts):
    return [e for e in rec.on(f"state/{dev}")
            if e.ts > after_ts and all(e.payload.get(k) == v for k, v in expected.items())]


def strip_ids(log):
    # Envelope event_ids come from a process-global counter in islab.core.bus, so they
    # differ between two runs in the same process. Everything else must be identical.
    return [(t, k, {kk: vv for kk, vv in d.items() if kk != "event_id"}) for t, k, d in log]


# ---------------------------------------------------------------- determinism
def _day(seed):
    sim, bus, home, _ = make(seed)
    home.run_script(DEFAULT_DAY)
    home.faults.link("state/", drop=0.1, jitter=0.5, duplicate=0.1)
    sim.after(100, home.command, "valve.main", "close")
    sim.run(until=parse_time("23:00"))
    return sim.log


def test_same_seed_same_log():
    assert strip_ids(_day(7)) == strip_ids(_day(7))


def test_different_seed_different_log():
    assert strip_ids(_day(7)) != strip_ids(_day(8))


def test_registry_covers_contract_ids():
    _, _, home, _ = make()
    ids = ("valve.main lock.front lock.back shade.living thermo.kitchen thermo.study window.kitchen "
           "garage.door leak.sink smoke.hall motion.kitchen camera.front light.kitchen light.living "
           "temp.kitchen").split()
    assert sorted(home.registry) == sorted(ids) == sorted(LAYOUT)
    for entry in home.registry.values():
        assert set(entry) >= {"friendly_name", "room", "kind", "capabilities"}


# ---------------------------------------------------------------- command cycles
CYCLES = [
    ("valve.main", "close", {}, 4, 8),
    ("lock.front", "unlock", {}, 1, 2),
    ("lock.back", "unlock", {}, 1, 2),
    ("shade.living", "set_position", {"position": 30}, 5, 10),
    ("thermo.kitchen", "set_setpoint", {"setpoint": 23.0}, 0.3, 1),
    ("thermo.study", "set_mode", {"mode": "off"}, 0.3, 1),
    ("garage.door", "open", {}, 11, 13),
    ("light.kitchen", "on", {}, 0.2, 0.5),
    ("light.living", "set_brightness", {"brightness": 40}, 0.2, 0.5),
]


@pytest.mark.parametrize("dev,action,args,lo,hi", CYCLES)
def test_command_ack_state_cycle(dev, action, args, lo, hi):
    sim, _, home, rec = make(3)
    sim.run(until=5)
    t0 = sim.now
    cmd = home.command(dev, action, args)
    sim.run(until=t0 + hi + 1)
    acks = rec.acks(dev, cmd)
    assert len(acks) == 1 and acks[0].payload["status"] == "accepted"
    ok = confirmed(rec, dev, home.expected_state(dev, action, args), t0)
    assert ok, f"{dev} never confirmed"
    assert lo <= ok[0].ts - t0 <= hi + 0.1
    assert acks[0].ts < ok[0].ts            # ack strictly before the confirming state


def test_unsupported_and_invalid_commands_rejected():
    sim, _, home, rec = make()
    c1 = home.command("leak.sink", "close")
    c2 = home.command("valve.main", "set_position", {"position": 150})
    sim.run(until=2)
    assert rec.acks("leak.sink", c1)[0].payload == {"cmd_id": c1, "status": "rejected",
                                                    "reason": "unsupported_action"}
    assert rec.acks("valve.main", c2)[0].payload["status"] == "rejected"


def test_thermostat_drifts_toward_setpoint():
    sim, _, home, _ = make()
    sim.run(until=600)
    t = home["thermo.kitchen"].state["current_temp"]
    assert 19.5 < t <= 21.0


def test_sensors_and_household_script():
    sim, _, home, rec = make()
    home.run_script(DEFAULT_DAY)
    sim.run(until=parse_time("23:00"))
    assert any(e.payload["type"] == "leak" for e in rec.on("event/leak.sink"))
    cam = [e.payload for e in rec.on("event/camera.front")]
    assert {"parcel", "person", "doorbell"} <= {p["type"] for p in cam}
    assert all("clip_ref" in p and "confidence" in p for p in cam if p["type"] != "doorbell")
    opened = [e for e in rec.on("state/window.kitchen") if e.payload["state"] == "open"]
    assert opened and abs(opened[0].ts - parse_time("18:04")) < 0.1
    voice = [e.payload for e in rec.on("voice/sat.living")]
    assert voice[-1]["text"] == "Good night" and "audio_ref" in voice[-1]
    assert all("speaker" not in p for p in voice)          # ground truth stays off the bus
    assert any(k == "voice_truth" and d["speaker"] == "Mark" for _, k, d in sim.log)
    assert rec.on("event/motion.kitchen")


def test_heartbeats_and_temp_interval():
    sim, bus, home, rec = make()
    sim.run(until=600)
    assert len(rec.on("state/temp.kitchen")) >= 9          # 60 s interval
    assert 2 <= len(rec.on("state/leak.sink")) <= 4        # boot + 300 s heartbeat


def test_smoke_local_siren_independent_of_hub():
    sim, _, home, rec = make()
    home.faults.offline("smoke.hall")
    home.faults.internet_down()
    home["smoke.hall"].trigger()
    sim.run(until=10)
    assert home["smoke.hall"].state["siren"] is True       # siren sounds anyway
    assert not rec.on("event/smoke.hall")                   # but the hub heard nothing


# ---------------------------------------------------------------- faults
def test_fault_link_drop():
    sim, _, home, rec = make()
    home.faults.link("cmd/valve.main", drop=1.0)
    cmd = home.command("valve.main", "close")
    sim.run(until=30)
    assert not rec.acks("valve.main", cmd)
    assert any(k == "link_drop" for _, k, _ in sim.log)


def test_fault_link_delay():
    sim, _, home, rec = make()
    home.faults.link("ack/", delay=5.0)
    cmd = home.command("lock.front", "unlock")
    sim.run(until=10)
    ack = [e for e in rec.msgs if e.topic == "ack/lock.front"]
    assert ack and ack[0].payload["cmd_id"] == cmd
    # ack generated at ~0.01 s but delivered >5 s later; state (undelayed) arrives first
    idx_ack = rec.msgs.index(ack[0])
    idx_state = next(i for i, e in enumerate(rec.msgs)
                     if e.topic == "state/lock.front" and e.payload["state"] == "unlocked")
    assert idx_state < idx_ack


def test_fault_link_duplicate():
    sim, _, home, rec = make()
    home.faults.link("event/", duplicate=1.0)
    home["leak.sink"].set_leak(True)
    sim.run(until=5)
    evs = rec.on("event/leak.sink")
    assert len(evs) == 2 and evs[0].event_id == evs[1].event_id


def test_fault_link_reorder():
    sim, _, home, rec = make(1)
    home.faults.link("state/temp.kitchen", jitter=200.0)
    sim.run(until=3600)
    seqs = [e.seq for e in rec.on("state/temp.kitchen")]
    assert seqs != sorted(seqs)


def test_fault_stuck():
    sim, _, home, rec = make()
    home.faults.stuck("valve.main")
    cmd = home.command("valve.main", "close")
    sim.run(until=120)
    assert rec.acks("valve.main", cmd)[0].payload["status"] == "accepted"
    assert not confirmed(rec, "valve.main", {"state": "closed"}, 0)
    assert all(e.payload["position"] == 100 for e in rec.on("state/valve.main"))


def test_fault_partial():
    sim, _, home, rec = make()
    home.faults.partial("valve.main", at=40)
    home.command("valve.main", "close")
    sim.run(until=20)
    last = rec.on("state/valve.main")[-1].payload
    assert last == {"state": "partial", "position": 40}


def test_fault_partial_lock_jams():
    sim, _, home, rec = make()
    home.faults.partial("lock.front")
    home.command("lock.front", "unlock")
    sim.run(until=5)
    assert rec.on("state/lock.front")[-1].payload["state"] == "jammed"


def test_fault_offline():
    sim, _, home, rec = make()
    sim.run(until=2)
    n = len(rec.on("state/valve.main"))
    home.faults.offline("valve.main")
    cmd = home.command("valve.main", "close")
    sim.run(until=300)
    assert not rec.acks("valve.main", cmd)
    assert len(rec.on("state/valve.main")) == n


def test_fault_stale():
    sim, _, home, rec = make()
    sim.run(until=2)
    home.faults.stale("lock.front")
    t_freeze = sim.now
    cmd = home.command("lock.front", "unlock")
    sim.run(until=400)
    assert rec.acks("lock.front", cmd)[0].payload["status"] == "accepted"
    later = rec.on("state/lock.front")[-1]
    assert later.payload["state"] == "locked" and later.ts <= t_freeze   # old value, old ts
    assert home["lock.front"].state["state"] == "unlocked"              # reality moved on


def test_fault_clock_skew():
    sim, _, home, rec = make()
    home.faults.clock_skew("light.kitchen", -120.0)
    home.command("light.kitchen", "on")
    sim.run(until=5)
    st = [e for e in rec.on("state/light.kitchen") if e.payload["state"] == "on"][0]
    assert st.ts < 0                                         # behind hub time
    assert not confirmed(rec, "light.kitchen", {"state": "on"}, 0.0)   # ts rule rejects it


def test_fault_battery_dead():
    sim, _, home, rec = make()
    sim.run(until=10)
    home.faults.battery_dead("leak.sink")
    sim.run(until=3000)
    msgs = rec.on("state/leak.sink")
    assert msgs[-1].payload["battery"] == 0                  # last gasp
    assert msgs[-1].ts < 11                                   # then silence
    home["leak.sink"].set_leak(True)
    sim.run(until=3010)
    assert not rec.on("event/leak.sink")


def test_fault_internet_down_flag():
    sim, _, home, rec = make()
    home.faults.internet_down(start=100, duration=50)
    cmd = home.command("valve.main", "close")
    sim.run(until=120)
    assert not home.internet.up and not home.internet
    assert rec.acks("valve.main", cmd)                       # LAN unaffected
    sim.run(until=200)
    assert home.internet.up
    assert [up for _, up in home.internet.history] == [True, False, True]


def test_fault_hostile_name():
    sim, _, home, _ = make()
    home.faults.hostile_name("light.living")
    assert home.registry["light.living"]["friendly_name"] == HOSTILE_NAME
    assert any(k == "fault" and d["type"] == "hostile_name" for _, k, d in sim.log)


def test_fault_clear():
    sim, _, home, rec = make()
    home.faults.stuck("valve.main")
    home.faults.clear("valve.main")
    home.command("valve.main", "close")
    sim.run(until=20)
    assert confirmed(rec, "valve.main", {"state": "closed", "position": 0}, 0)
