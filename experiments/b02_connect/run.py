"""B02: the E03 "we're leaving" routine over a real local MQTT broker.

Real: an MQTT 3.1.1 broker (amqtt, in-process, 127.0.0.1), paho-mqtt clients (one per device plus
the hub), QoS 0/1, retained messages, Last Will, Home Assistant discovery topics, TCP on loopback.
Modelled: the devices (islab.twin classes, unchanged), their physical timing, the radio hop
(loss emulated at the adapter), the Matter view of each state. Time: home s = wall s x SPEED (10).

Parts
  A  transport latency on loopback, QoS 0 vs 1 (wall ms, not scaled)
  B  routine matrix: QoS {0,1} x adapter loss {0, 5, 20%}; one shade loses power before the routine
  C  offline detection: power loss (socket left open) vs process crash (socket closed)
  D  QoS across a 2 s network blip with a persistent session
  E  a broker defect: retained messages delivered to clients that did not subscribe to them

Usage: python experiments/b02_connect/run.py [--trials 8]   (about 3 minutes on 2 cores)
Writes results.json and report_samples.md next to this file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from islab.connect import SPEED, broker as brk  # noqa: E402
from islab.connect import matter_model as mm  # noqa: E402
from islab.connect.adapter import Clock, DeviceSide, new_client  # noqa: E402
from islab.connect.hub import Hub, leaving_plan, plain_report  # noqa: E402
from islab.twin import devices as d  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

LAYOUT = {
    "light.kitchen":  (d.Light, "Kitchen light", "light"),
    "light.living":   (d.Light, "Living room light", "light"),
    "thermo.kitchen": (d.Thermostat, "Kitchen thermostat", "thermostat"),
    "thermo.study":   (d.Thermostat, "Study thermostat", "thermostat"),
    "shade.living":   (d.Shade, "Living room shade", "shade"),
    "shade.bedroom":  (d.Shade, "Bedroom shade", "shade"),    # extra instance; drops off
    "window.kitchen": (d.WindowContact, "Kitchen window", "contact"),
}
NAMES = {k: v[1] for k, v in LAYOUT.items()}
DROPPED = "shade.bedroom"
KEEPALIVE_WALL = 1                  # paho keepalive is whole seconds: 1 s wall = 10 home s
ROUTINE_TIMEOUT = 30.0              # home s
RESEND_AFTER = 2.0                  # home s without an ack -> resend same cmd_id (max 3)
HB_FACTOR = 2                       # heartbeat rule: offline after 2 missed heartbeats


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


# ---------------------------------------------------------------------------- A
def part_a(b, n=300):
    out = {}
    for qos in (0, 1):
        got, ev = {}, threading.Event()
        sub = new_client(f"lat-sub-{qos}")
        def on_msg(c, u, m):
            got[int(m.payload)] = time.monotonic()
            if len(got) >= n:
                ev.set()
        sub.on_message = on_msg
        ready = threading.Event()
        sub.on_subscribe = lambda *a, **k: ready.set()
        sub.on_connect = lambda c, *a, **k: c.subscribe(f"lat/{qos}", qos)
        sub.connect(b.host, b.port, 30); sub.loop_start(); ready.wait(5)
        pub = new_client(f"lat-pub-{qos}")
        pub.connect(b.host, b.port, 30); pub.loop_start(); time.sleep(0.1)
        sent = {}
        for i in range(n):
            sent[i] = time.monotonic()
            pub.publish(f"lat/{qos}", str(i), qos=qos)
            time.sleep(0.003)
        ev.wait(10)
        lat = [(got[i] - sent[i]) * 1000 for i in got]
        out[f"qos{qos}"] = {"n": n, "received": len(got), "p50_ms": pct(lat, 50),
                            "p95_ms": pct(lat, 95), "max_ms": round(max(lat), 2)}
        for c in (sub, pub):
            c.disconnect(); c.loop_stop()
    return out


# ---------------------------------------------------------------------------- B
def one_trial(b, qos, loss, seed, window_open):
    clock = Clock(SPEED)
    home = f"b02q{qos}l{int(loss * 100)}s{seed}"
    hub = Hub(b.host, b.port, home, clock, qos=qos, resend_after=RESEND_AFTER)
    ds = DeviceSide(b.host, b.port, home, LAYOUT, clock, seed=seed, qos=qos, loss=loss,
                    keepalive=KEEPALIVE_WALL)
    for light in ("light.kitchen", "light.living"):
        ds.devices[light].state.update(state="on", brightness=100)
    if window_open:
        ds.devices["window.kitchen"].state["state"] = "open"
    ds.start()
    time.sleep(0.15 + ds.sim.rng.uniform(0, 0.1))          # boot (1.5-2.5 home s), state settles
    t_drop = ds.drop(DROPPED, "power")
    drop_h = (t_drop - clock.t0) * clock.speed
    steps = leaving_plan(NAMES)
    t_start = clock.home()
    closed = hub.run(steps, timeout=ROUTINE_TIMEOUT)
    now = clock.home()
    report = plain_report(steps, t_start, hub.latest("window.kitchen"), NAMES["window.kitchen"],
                          now, NAMES)
    # Last Will may arrive after a timed-out routine: wait for it (bounded) to measure detection
    end = time.monotonic() + 3.0
    while time.monotonic() < end and not (hub.avail.get(DROPPED, ("",))[0] == "offline"):
        time.sleep(0.01)
    av = hub.avail.get(DROPPED)
    lwt = round(av[1] - drop_h, 2) if av and av[0] == "offline" else None
    last = hub.last_rx.get(DROPPED)
    hb = round(last + HB_FACTOR * d.Shade.heartbeat_s - drop_h, 2) if last is not None else None
    # a hub that restarts now: what do retained messages tell it, and how fast?
    clock2_t = time.monotonic()
    hub2 = Hub(b.host, b.port, home, clock, qos=1, client_id=f"{home}-hub2")
    end = time.monotonic() + 2.0
    while time.monotonic() < end and (len(hub2.states) < len(LAYOUT) or len(hub2.avail) < len(LAYOUT)):
        time.sleep(0.005)
    complete = len(hub2.states) == len(LAYOUT) and len(hub2.avail) == len(LAYOUT)
    restart = {"wall_ms": round((time.monotonic() - clock2_t) * 1000, 1) if complete else None,
               "states": len(hub2.states), "availability": len(hub2.avail),
               "dropped_seen_offline": hub2.avail.get(DROPPED, ("",))[0] == "offline",
               "dropped_retained_position": (hub2.latest(DROPPED) or {}).get("position")}
    hub2.close()
    discovery_seen = len(hub.discovery)
    ds.stop()
    truth = {k: dict(v.state) for k, v in ds.devices.items()}
    hub.close()
    live = [s for s in steps if s.device_id != DROPPED]
    live_conf = [s.at - t_start for s in live if s.outcome == "confirmed"]
    acted_unconfirmed = sum(1 for s in live if s.outcome != "confirmed"
                            and all(truth[s.device_id].get(k) == v for k, v in s.expect.items()))
    return {
        "seed": seed, "routine_closed_s": round(closed, 2),
        "all_live_confirmed_s": round(max(live_conf), 2) if len(live_conf) == len(live) else None,
        "outcomes": {s.device_id: s.outcome for s in steps},
        "confirm_s": {s.device_id: round(s.at - t_start, 2) for s in steps if s.outcome == "confirmed"},
        "sends": {s.device_id: len(s.sent) for s in steps},
        "lwt_detect_s": lwt, "heartbeat_detect_s": hb,
        "dropped_in_report_as_offline": steps[-1].outcome == "offline",
        "acted_but_unconfirmed": acted_unconfirmed,
        "adapter": dict(ds.stats), "restart": restart, "window_open": window_open,
        "discovery_configs_seen": discovery_seen, "report": report,
    }


def part_b(b, trials):
    conds, samples = {}, {}
    for qi, qos in enumerate((0, 1)):
        for li, loss in enumerate((0.0, 0.05, 0.20)):
            key = f"qos{qos}_loss{int(loss * 100)}"
            rows = [one_trial(b, qos, loss, seed=1000 * (3 * qi + li) + t, window_open=bool(t % 2))
                    for t in range(trials)]
            n = len(rows)
            per_dev = {dev: round(100 * sum(r["outcomes"][dev] == "confirmed" for r in rows) / n, 1)
                       for dev in NAMES if dev != "window.kitchen"}
            conds[key] = {
                "qos": qos, "loss": loss, "trials": n,
                "routine_closed_s": {"p50": pct([r["routine_closed_s"] for r in rows], 50),
                                     "p95": pct([r["routine_closed_s"] for r in rows], 95)},
                "all_live_confirmed": {
                    "rate_pct": round(100 * sum(r["all_live_confirmed_s"] is not None for r in rows) / n, 1),
                    "p50_s": pct([r["all_live_confirmed_s"] for r in rows], 50),
                    "p95_s": pct([r["all_live_confirmed_s"] for r in rows], 95)},
                "per_device_confirmed_pct": per_dev,
                "dropped_shade_reported_offline_pct": round(
                    100 * sum(r["dropped_in_report_as_offline"] for r in rows) / n, 1),
                "lwt_detect_s": {"p50": pct([r["lwt_detect_s"] for r in rows], 50),
                                 "p95": pct([r["lwt_detect_s"] for r in rows], 95),
                                 "missing": sum(r["lwt_detect_s"] is None for r in rows)},
                "heartbeat_detect_s_computed": {"p50": pct([r["heartbeat_detect_s"] for r in rows], 50),
                                                "p95": pct([r["heartbeat_detect_s"] for r in rows], 95)},
                "discovery_configs_seen_min": min(r["discovery_configs_seen"] for r in rows),
                "acted_but_unconfirmed_total": sum(r["acted_but_unconfirmed"] for r in rows),
                "resends_to_live_devices": sum(sum(v - 1 for k, v in r["sends"].items() if k != DROPPED)
                                               for r in rows),
                "resends_to_dropped_shade": sum(r["sends"][DROPPED] - 1 for r in rows),
                "adapter_lost_in": sum(r["adapter"]["lost_in"] for r in rows),
                "adapter_lost_out": sum(r["adapter"]["lost_out"] for r in rows),
                "unsolicited_msgs_to_device_clients_per_trial": {
                    "min": min(r["adapter"]["unsolicited"] for r in rows),
                    "max": max(r["adapter"]["unsolicited"] for r in rows)},
                "hub_restart": {
                    "full_picture_ms_p50_when_complete": pct([r["restart"]["wall_ms"] for r in rows], 50),
                    "full_picture_ms_p95_when_complete": pct([r["restart"]["wall_ms"] for r in rows], 95),
                    "all_7_states_pct": round(100 * sum(r["restart"]["states"] == 7 for r in rows) / n, 1),
                    "dropped_seen_offline_pct": round(
                        100 * sum(r["restart"]["dropped_seen_offline"] for r in rows) / n, 1)},
            }
            samples[key] = [r["report"] for r in rows[:2]]
            print(f"  {key}: closed p50 {conds[key]['routine_closed_s']['p50']} s, "
                  f"live confirmed {conds[key]['all_live_confirmed']['rate_pct']}%, "
                  f"LWT p50 {conds[key]['lwt_detect_s']['p50']} s", flush=True)
            conds[key]["trial_rows"] = [{k: v for k, v in r.items() if k != "report"} for r in rows]
    return conds, samples


# ---------------------------------------------------------------------------- C
def part_c(b, n=5):
    out = {}
    for mode in ("power", "crash"):
        det = []
        for i in range(n):
            clock = Clock(SPEED)
            home = f"c{mode}{i}"
            hub = Hub(b.host, b.port, home, clock, qos=1)
            ds = DeviceSide(b.host, b.port, home, {"shade.bedroom": LAYOUT["shade.bedroom"]},
                            clock, seed=i, qos=1, keepalive=KEEPALIVE_WALL, discovery=False)
            ds.start()
            time.sleep(0.15 + 0.1 * i / n)
            t = ds.drop("shade.bedroom", mode)
            end = time.monotonic() + 4
            while time.monotonic() < end and hub.avail.get("shade.bedroom", ("",))[0] != "offline":
                time.sleep(0.002)
            av = hub.avail.get("shade.bedroom")
            det.append(round((av[1] - (t - clock.t0) * SPEED), 2) if av and av[0] == "offline" else None)
            ds.stop(); hub.close()
        out[mode] = {"n": n, "detect_home_s": det, "p50_home_s": pct(det, 50),
                     "max_home_s": max(x for x in det if x is not None) if any(det) else None,
                     "keepalive_home_s": KEEPALIVE_WALL * SPEED}
    return out


# ---------------------------------------------------------------------------- D
def part_d(b, n=3):
    """Same blip against this broker (username required) and against an amqtt broker that allows
    anonymous clients."""
    out = {"username_required": _blip(b, n)}
    with brk.LocalBroker(allow_anonymous=True) as anon:
        out["anonymous_allowed"] = _blip(anon, n)
    return out


def _blip(b, n):
    out = {}
    for qos in (0, 1):
        delivered = 0
        for i in range(n):
            topic = f"blip/{qos}/{i}/cmd"
            cid = f"blip-dev-{qos}-{i}"
            got = threading.Event()
            dev = new_client(cid, clean_session=False)
            ready = threading.Event()
            dev.on_subscribe = lambda *a, **k: ready.set()
            dev.connect(b.host, b.port, 5); dev.loop_start()
            dev.subscribe(topic, qos); ready.wait(5)
            dev.loop_stop(); dev.socket().close()          # Wi-Fi blip: connection gone
            time.sleep(0.3)                                 # broker has noticed (3 home s)
            hub = new_client(f"blip-hub-{qos}-{i}")
            hub.connect(b.host, b.port, 5); hub.loop_start(); time.sleep(0.05)
            hub.publish(topic, "close", qos=qos).wait_for_publish(2)
            time.sleep(0.1)                                 # blip lasts ~0.45 s wall = ~4.5 home s
            dev2 = new_client(cid, clean_session=False)     # same id, persistent session
            dev2.on_message = lambda c, u, m: got.set()
            dev2.connect(b.host, b.port, 5); dev2.loop_start()
            if got.wait(0.6):
                delivered += 1
            for c in (dev2, hub):
                c.disconnect(); c.loop_stop()
        out[f"qos{qos}"] = {"n": n, "command_delivered_after_reconnect": delivered}
    return out


# ---------------------------------------------------------------------------- E
def part_e(b):
    a = new_client("e-hub")
    ready = threading.Event()
    a.on_subscribe = lambda *x, **k: ready.set()
    a.connect(b.host, b.port, 5); a.loop_start()
    a.subscribe("eprobe/private/#", 1); ready.wait(5)
    a.publish("eprobe/private/door", "unlocked", qos=1, retain=True).wait_for_publish(2)
    time.sleep(0.1)
    got = []
    c = new_client("e-device")
    c.on_message = lambda cl, u, m: got.append(m.topic)
    c.on_connect = lambda cl, *x, **k: cl.subscribe("eprobe/own/cmd", 1)
    c.connect(b.host, b.port, 5); c.loop_start(); time.sleep(0.4)
    for cl in (a, c):
        cl.disconnect(); cl.loop_stop()
    leaked = [t for t in got if not t.startswith("eprobe/own/")]
    return {"subscribed_to": "eprobe/own/cmd", "received_topics": got, "unsolicited": len(leaked)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=8)
    args = ap.parse_args()
    impl = brk.available()
    if impl is None:
        sys.exit("needs: pip install amqtt paho-mqtt")
    import paho.mqtt
    w0 = time.monotonic()
    with brk.LocalBroker() as b:
        print(f"broker {b.version} on {b.host}:{b.port}")
        print("A transport latency"); a = part_a(b)
        print("B routine matrix"); bres, samples = part_b(b, args.trials)
        print("C offline detection"); c = part_c(b)
        print("D QoS across a blip"); dres = part_d(b)
        print("E broker retained-message check"); e = part_e(b)
    results = {
        "experiment": "b02_connect",
        "date": dt.date.today().isoformat(),
        "method": ("Real MQTT 3.1.1 broker (amqtt) on 127.0.0.1 and paho-mqtt clients: one per device "
                   "and the hub. Clients send a username (amqtt queues nothing for anonymous sessions). Twin devices (islab.twin, unchanged) run on a simulator driven by the "
                   "wall clock x10. Routine 'we're leaving': 2 lights off, 2 thermostats to 16 C, "
                   "2 shades close, kitchen window check; the bedroom shade loses power 0-1 s before. "
                   "Confirmation by later state (CONTRACT.md); resend same cmd_id after 2 s without "
                   "ack (max 3); offline from Last Will. Loss = i.i.d. drop at the adapter, both "
                   "directions (the radio hop into the bridge)."),
        "params": {"speed_home_per_wall": SPEED, "trials_per_condition": args.trials,
                   "qos": [0, 1], "adapter_loss": [0, 0.05, 0.20],
                   "keepalive_wall_s": KEEPALIVE_WALL, "keepalive_home_s": KEEPALIVE_WALL * SPEED,
                   "routine_timeout_home_s": ROUTINE_TIMEOUT, "resend_after_home_s": RESEND_AFTER,
                   "heartbeat_rule": f"offline after {HB_FACTOR} x {d.Shade.heartbeat_s:g} s shade heartbeat",
                   "broker": b.version, "paho_mqtt": paho.mqtt.__version__,
                   "matter_ids_source": mm.SOURCE, "wall_time_s": None},
        "results": {"A_transport_latency_wall": a, "B_routine": bres, "C_offline_detection": c,
                    "D_qos_across_blip": dres, "E_broker_retained_check": e},
        "limitations": [
            "Loopback TCP on one 2-core machine: no Wi-Fi, Thread or Zigbee radio. TCP does not lose "
            "messages on a live connection, so loss was emulated at the adapter (before MQTT); QoS "
            "cannot recover those losses by design.",
            "Time compression x10: device physics, heartbeats, keepalive and timeouts are scaled; "
            "transport and Python scheduling overhead is multiplied by 10 in home-second figures "
            "(see part A for the unscaled transport cost).",
            "Keepalive 10 home s (paho minimum 1 s wall). Real devices often use 30-60 s; Last Will "
            "detection scales with keepalive.",
            "Heartbeat detection is computed from the measured last message + 2 heartbeats, not "
            "waited for.",
            "Matter: data model mapping only (cluster/attribute ids from the SDK-generated package); "
            "no Matter controller, no commissioning, no Thread. Home Assistant discovery payloads "
            "were not validated against a running Home Assistant.",
            f"{args.trials} trials per condition: p95 values are indicative.",
            "i.i.d. loss; real radios lose packets in bursts.",
        ],
    }
    results["params"]["wall_time_s"] = round(time.monotonic() - w0, 1)
    (HERE / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    lines = ["# Plain-words reports from the run (first two trials per condition)\n"]
    for k, reps in samples.items():
        lines.append(f"## {k}\n")
        lines += [f"> {r}\n" for r in reps]
    (HERE / "report_samples.md").write_text("\n".join(lines))
    print(f"done in {results['params']['wall_time_s']} s")


if __name__ == "__main__":
    main()
