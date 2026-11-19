"""B03: readings you can trust. A seeded simulated week with injected reading faults, read by
three hub views (naive, timestamp-only, full quality layer) running side by side on the same bus.

What happens in the week (virtual time, day 0 = Monday 00:00; minute offsets drawn from sim.rng):

  all week   duplicates: 10% of state/ and event/ messages delivered twice
  all week   reordering: 0-30 s jitter on state/window.kitchen (short airings arrive out of order)
  all week   temp.kitchen clock drifts 2 s/hour behind the hub (336 s by the end)
  day 0      14:00 one temp.kitchen spike of +15 degC
  day 1      08:00-20:00 temp.kitchen stuck (keeps reporting one value with fresh timestamps)
  day 1      10:00 a real leak at the sink (state wet + event), dried after 20 min
  day 2      03:00 leak.sink sends ONE leak event, then only heartbeats saying dry; really wet 2 h;
             a person reads the phone at 07:10
  day 3      12:00 a real 5 degC step for 30 min (oven door; a legitimate fast change)
  day 5      10:00 leak.sink battery dies (last gasp, then silence)
  daily      ~14 window airings (5-60 s), ~30 motion events, 4 doorbells, cooking at 18:00

Decisions (identical code for every view; the views differ only in what they let through):
  every 60 s   kitchen heating: heat_on / heat_off / hold / pause (window open); refuse if unsure
  on reading   overheat alert if kitchen temperature > 35 degC (checked on every temperature report)
  every 60 s   after a leak shut-off: reopen the valve once the leak reading is dry for 5 min
  on event     leak -> close valve + alarm; night motion -> light on; doorbell -> tell
  22:00 daily  house check ("all clear") from leak, window, kitchen temperature

Ground truth comes from an oracle that sees every message at publish time (link hook) and at
delivery (first subscriber). The views never read the oracle.

A decision is "on bad data" if any reading it used (a) came from a lying sensor (stuck, spike,
leak heartbeat) and differs from the truth (1 degC tolerance), (b) is older than a newer honest
reading already delivered (duplicate copy, reordered message, or a real change the view held back),
(c) is truly stale (generated longer ago than the device's max age), or (d) the decision was
triggered by a duplicate copy of an event.

Usage: python experiments/b03_readings/run.py [--seeds 10] [--days 7]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from islab.core.bus import Bus, Envelope  # noqa: E402
from islab.core.sim import Sim  # noqa: E402
from islab.readings import NaiveView, QualityView, TimestampView, max_age_from_devices  # noqa: E402
from islab.twin import build_home  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DAY, HOUR, MIN = 86400.0, 3600.0, 60.0
TOL = 1.0                    # degC: a temperature reading this far from the truth is wrong
THERMO_BIAS = 0.3            # thermo.kitchen reads 0.3 degC high (an honest calibration offset)
DRIFT_S_PER_H = -2.0         # temp.kitchen clock: 2 s per hour behind
OVERHEAT_C = 35.0
DRY_HOLD_S = 300.0
NIGHT = (22, 6)
WATCH = ("temp.kitchen", "thermo.kitchen", "window.kitchen", "leak.sink")
LIES = ("stuck", "spike", "leak_oneshot")


def hhmm(day, h, m=0):
    return day * DAY + h * HOUR + m * MIN


# =============================================================================================
# Scenario: truth model, device physics overrides, fault schedule
# =============================================================================================
class Scenario:
    def __init__(self, sim, home, days):
        self.sim, self.home, self.days = sim, home, days
        rng = sim.rng
        jit = lambda: rng.uniform(-30, 30) * MIN  # noqa: E731
        self.t_spike = hhmm(0, 14) + jit()
        self.leak_real = (hhmm(1, 10) + jit(), 20 * MIN)
        self.t_oneshot = hhmm(2, 3) + jit()
        self.oneshot_wet = 2 * HOUR
        self.t_reply = hhmm(2, 7, 10)
        self.step = (hhmm(3, 12) + jit(), 30 * MIN, 5.0)
        self.stuck = (hhmm(1, 8) + jit(), 12 * HOUR)
        self.t_dead = hhmm(5, 10) + jit()
        self.leaks = [(self.leak_real[0], self.leak_real[0] + self.leak_real[1]),
                      (self.t_oneshot, self.t_oneshot + self.oneshot_wet)]
        self.spike_now = False
        self.windows: list[tuple[float, float]] = []
        self.motion: list[float] = []
        self.doorbell: list[float] = []
        for d in range(days):
            starts = sorted(rng.uniform(hhmm(d, 7), hhmm(d, 22)) for _ in range(14))
            last_end = -1.0
            for s in starts:
                dur = rng.uniform(5, 60)
                if s > last_end + 5:
                    self.windows.append((s, s + dur))
                    last_end = s + dur
            self.motion += [rng.uniform(hhmm(d, 0), hhmm(d, 24)) for _ in range(30)]
            self.doorbell += [rng.uniform(hhmm(d, 9), hhmm(d, 20)) for _ in range(4)]

    # ---- truth --------------------------------------------------------------------------
    def truth_temp(self, t):
        x = 21.0 + 2.0 * math.sin(2 * math.pi * (t / DAY - 0.375))
        c = t % DAY - 18 * HOUR                           # cooking at 18:00
        if 0 <= c < 180:
            x += 3.0 * c / 180
        elif 180 <= c < 45 * MIN:
            x += 3.0
        elif 45 * MIN <= c < 55 * MIN:
            x += 3.0 * (1 - (c - 45 * MIN) / (10 * MIN))
        s0, sd, sv = self.step
        if s0 <= t < s0 + sd:
            x += sv
        return x

    def truth_leak(self, t):
        return any(a <= t < b for a, b in self.leaks)

    def truth_window(self, t):
        return "open" if any(a <= t < b for a, b in self.windows) else "closed"

    def stuck_active(self, t):
        return self.stuck[0] <= t < self.stuck[0] + self.stuck[1]

    def dead(self, dev, t):
        return dev == "leak.sink" and t >= self.t_dead

    def label(self, env):
        """What the sensor did when it published (ground truth, oracle only)."""
        dev, t = env.source, self.sim.now
        if dev == "temp.kitchen" and env.topic.startswith("state/"):
            if self.spike_now:
                return "spike"
            if self.stuck_active(t):
                return "stuck"
        if (dev == "leak.sink" and env.topic.startswith("state/") and self.truth_leak(t)
                and (env.payload or {}).get("leak") is False):
            return "leak_oneshot"
        return "good"

    # ---- install --------------------------------------------------------------------------
    def install(self):
        sim, h, rng = self.sim, self.home, self.sim.rng
        tk, th, ts = h["temp.kitchen"], h["thermo.kitchen"], h["thermo.study"]

        def temp_tick():
            if not self.stuck_active(sim.now):
                tk.state["temperature"] = round(self.truth_temp(sim.now) + rng.gauss(0, 0.05), 2)
        tk.tick = temp_tick
        th.tick = lambda: th.state.__setitem__(
            "current_temp", round(self.truth_temp(sim.now) + THERMO_BIAS + rng.gauss(0, 0.05), 2))
        study = lambda t: 20.0 + 2.0 * math.sin(2 * math.pi * (t / DAY - 0.375))  # noqa: E731
        ts.tick = lambda: ts.state.__setitem__("current_temp", round(study(sim.now) + rng.gauss(0, 0.05), 2))
        tk.state["temperature"] = round(self.truth_temp(0.0), 2)      # boot with a true reading
        th.state["current_temp"] = round(self.truth_temp(0.0) + THERMO_BIAS, 2)
        ts.state["current_temp"] = round(study(0.0), 2)

        f = h.faults
        f.link(["state/", "event/"], duplicate=0.10)
        f.link(["state/window.kitchen"], jitter=30.0)
        f.clock_skew("temp.kitchen", 0.0)

        def drift():   # continuous drift: set the device clock directly (one fault record above)
            tk.clock_skew = DRIFT_S_PER_H * sim.now / HOUR
            sim.after(MIN, drift)
        sim.at(0.0, drift)

        def spike():
            self.spike_now = True
            tk.state["temperature"] = round(self.truth_temp(sim.now) + 15.0, 2)
            tk.publish_state()
            self.spike_now = False
            sim.trace("fault", type="spike", device="temp.kitchen")
        sim.at(self.t_spike, spike)

        leak = h["leak.sink"]
        sim.at(self.leak_real[0], leak.set_leak, True)
        sim.at(self.leak_real[0] + self.leak_real[1], leak.set_leak, False)

        def oneshot():
            leak.emit_event("leak", severity="high")    # state stays {"leak": False}
            sim.trace("fault", type="leak_oneshot", device="leak.sink")
        sim.at(self.t_oneshot, oneshot)
        sim.at(self.stuck[0], sim.trace, "fault", type="stuck_sensor", device="temp.kitchen")
        sim.at(self.t_dead, f.battery_dead, "leak.sink")

        win = h["window.kitchen"]
        for a, b in self.windows:
            sim.at(a, win.set_open, True)
            sim.at(b, win.set_open, False)
        for t in self.motion:
            sim.at(t, h["motion.kitchen"].trigger)
        for t in self.doorbell:
            sim.at(t, h["camera.front"].ring)


# =============================================================================================
# Oracle: ground truth about every message (views never read it)
# =============================================================================================
class Oracle:
    def __init__(self, sim, bus, scen, max_age):
        self.sim, self.scen, self.max_age = sim, scen, max_age
        self.pub: dict[str, dict] = {}
        self.deliv: dict[str, list[float]] = {}
        self.first_good: dict[str, bool] = {}
        self.newest: dict[str, dict] = {}            # dev -> newest-seq delivered honest state
        self.newest_any: dict[str, int] = {}
        self.last_pub: dict[str, float] = {}
        bus.link_hooks.insert(0, self._hook)
        bus.subscribe("state/#", self._on, name="oracle")
        bus.subscribe("event/#", self._on, name="oracle")

    def _hook(self, env):
        if env.event_id not in self.pub and env.topic.startswith(("state/", "event/")):
            self.pub[env.event_id] = {"t": self.sim.now, "dev": env.source, "seq": env.seq,
                                      "topic": env.topic, "payload": dict(env.payload or {}),
                                      "label": self.scen.label(env)}
            self.last_pub[env.source] = self.sim.now
        return [(0.0, env)]

    def _on(self, env):
        lst = self.deliv.setdefault(env.event_id, [])
        lst.append(self.sim.now)
        info = self.pub[env.event_id]
        dev = info["dev"]
        if len(lst) == 1:
            in_order = env.seq >= self.newest_any.get(dev, 0)
            self.first_good[env.event_id] = in_order and info["label"] == "good"
        if env.topic.startswith("state/"):
            self.newest_any[dev] = max(env.seq, self.newest_any.get(dev, 0))
            nw = self.newest.get(dev)
            if info["label"] == "good" and (nw is None or env.seq > nw["seq"]):
                self.newest[dev] = {"seq": env.seq, "eid": env.event_id, "first": lst[0]}

    def is_copy(self, eid):
        return len(self.deliv.get(eid, [])) > 1

    def rejected_good(self, rec):
        eid = rec.get("event_id")
        if rec.get("why") == "duplicate" or eid not in self.first_good:
            return False
        return self.first_good[eid] and abs(self.deliv[eid][0] - rec["t"]) < 1e-9

    @staticmethod
    def _differs(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            return abs(a - b) > TOL
        return a != b

    def _lied(self, dev, attr, value, now):
        if attr in ("temperature", "current_temp"):
            bias = THERMO_BIAS if dev == "thermo.kitchen" else 0.0
            return abs(value - self.scen.truth_temp(now) - bias) > TOL
        if attr == "leak":
            return bool(value) != self.scen.truth_leak(now)
        return False

    def bad_label(self, dev, attr, eid, value, now):
        """None if the reading was fit to use at `now`, else the fault that made it bad."""
        info = self.pub.get(eid)
        if info is None:
            return None
        if info["label"] in LIES and self._lied(dev, attr, value, now):
            return info["label"]
        if not info["topic"].startswith("state/"):
            return None                                    # latched alarm events do not age
        nw = self.newest.get(dev)
        if nw and nw["seq"] > info["seq"]:
            newer = self.pub[nw["eid"]]["payload"]
            if attr in newer and self._differs(newer[attr], value):
                d = self.deliv.get(eid, [])
                if d and max(d) < nw["first"]:
                    return "held_back"
                return "duplicate" if len(d) > 1 and d[-1] > nw["first"] else "reorder"
        if now - info["t"] > self.max_age.get(dev, 150.0):
            return "dead_battery" if self.scen.dead(dev, now) else "stale"
        return None

    def is_good(self, dev, attr, eid, value, now):
        """An honest, current reading: not from a lying sensor and fit to use."""
        info = self.pub.get(eid)
        return (info is not None and info["label"] == "good"
                and self.bad_label(dev, attr, eid, value, now) is None)

    def issue_is_real(self, kind, dev, now):
        s, h = self.scen, self.scen.home
        if kind == "stale":
            return now - self.last_pub.get(dev, 0.0) > self.max_age.get(dev, 150.0) or s.dead(dev, now)
        if kind in ("battery_empty", "battery_low"):
            return h[dev].state.get("battery", 100) <= 5
        if kind == "stuck":
            return dev == "temp.kitchen" and s.stuck_active(now)
        if kind == "clock_skew":
            return abs(h[dev].clock_skew) > 30.0
        if kind == "disagreement":
            return s.stuck_active(now) or abs(now - s.t_spike) < 900
        if kind == "disputed":
            return s.truth_leak(now) or now >= s.t_oneshot
        return True


# =============================================================================================
# Brain: identical decision code on top of each view
# =============================================================================================
class Brain:
    def __init__(self, name, view, sim, scen, oracle, bus):
        self.name, self.view, self.sim, self.scen, self.o, self.bus = name, view, sim, scen, oracle, bus
        self.decisions: list[dict] = []
        self.notices: list[dict] = []
        self.refusals: list[dict] = []
        self.valve_closed = False
        self.valve_log: list[tuple[float, str]] = [(0.0, "open")]
        self.dry_since = None
        self.last_overheat = -1e9
        self.good_primary_refused = 0
        self.last_ok: dict[str, float] = {}
        self.first_unusable: dict[str, float] = {}
        self.ts_stale_open: set = set()
        self.quality = isinstance(view, QualityView)
        view.on_event(self.on_event)
        # automations trigger on new reports too; subscribed after the views, so they see it applied
        bus.subscribe("state/temp.kitchen", self.on_temp, name=f"brain.{name}")
        bus.subscribe("state/thermo.kitchen", self.on_temp, name=f"brain.{name}")
        if self.quality:
            view.q.add_pair("kitchen_temp", ("temp.kitchen", "temperature"),
                            ("thermo.kitchen", "current_temp"), tol=1.5, hold_s=600.0)
            view.q.on_issue(self.on_issue)

    # ---- telling -------------------------------------------------------------------------
    def notify(self, kind, device, text, alarm=False, real=None, **kw):
        now = self.sim.now
        if real is None:
            real = self.o.issue_is_real(kind, device, now)
        rec = {"t": now, "kind": kind, "device": device, "alarm": alarm, "real": bool(real),
               "text": text, **kw}
        self.notices.append(rec)
        self.bus.publish(Envelope(topic="notify/anna", payload={"text": text, "kind": "tell"},
                                  source=f"hub.{self.name}", ts=now))

    def on_issue(self, issue):
        dev, kind = issue["device"], issue["kind"]
        self.notify(kind, dev, f"{dev}: {kind} {issue}")

    # ---- recording -----------------------------------------------------------------------
    def decide(self, kind, outcome, inputs, event_copy=False):
        now = self.sim.now
        bad = "duplicate" if event_copy else None
        for u in inputs:
            if bad:
                break
            if u.event_id is not None:
                bad = self.o.bad_label(u.device_id, u.attr, u.event_id, u.value, now)
        rec = {"t": now, "kind": kind, "outcome": outcome, "bad": bad,
               "inputs": [(u.device_id, u.attr, u.event_id) for u in inputs]}
        self.decisions.append(rec)
        return rec

    def refuse(self, kind, reason, devices):
        self.refusals.append({"t": self.sim.now, "kind": kind, "reason": reason, "devices": list(devices)})

    # ---- events --------------------------------------------------------------------------
    def on_event(self, env):
        dev, typ = env.topic.split("/", 1)[1], (env.payload or {}).get("type")
        copy = self.o.is_copy(env.event_id)
        from islab.readings.views import Use
        u = Use(dev, "event", typ, True, event_id=None)
        if typ == "leak":
            self.decide("close_valve", "close", [u], event_copy=copy)
            self.valve_closed, self.dry_since = True, None
            self.valve_log.append((self.sim.now, "closed"))
            real = self.scen.truth_leak(self.sim.now) and not any(
                n["kind"] == "leak" and self._same_leak(n["t"], self.sim.now) for n in self.notices)
            self.notify("leak", dev, "Water leak at the sink. I closed the main valve.", alarm=True, real=real)
        elif typ == "motion":
            h = (self.sim.now % DAY) / HOUR
            if h >= NIGHT[0] or h < NIGHT[1]:
                self.decide("night_light", "light_on", [u], event_copy=copy)
        elif typ == "doorbell":
            self.decide("tell_doorbell", "tell", [u], event_copy=copy)
            self.notify("doorbell", dev, "Someone is at the front door.", alarm=True, real=not copy)

    def on_temp(self, env):
        now, v = self.sim.now, self.view
        p = v.get("temp.kitchen", "temperature")
        f = v.get("thermo.kitchen", "current_temp")
        src = p if p.ok else (f if f.ok else None)
        if src is None or not isinstance(src.value, (int, float)):
            return
        if src.value > OVERHEAT_C and now - self.last_overheat > 30 * MIN:
            self.last_overheat = now
            self.decide("overheat_alert", "alert", [src])
            self.notify("overheat", src.device_id, f"Kitchen is {src.value:.1f} C.", alarm=True,
                        real=self.scen.truth_temp(now) > OVERHEAT_C)

    def _same_leak(self, t1, t2):
        return any(a <= t1 < b and a <= t2 < b for a, b in self.scen.leaks)

    # ---- periodic --------------------------------------------------------------------------
    def _track(self, u):
        now = self.sim.now
        was_ok = self.first_unusable.get(("prev", u.device_id), True)
        if u.ok:
            self.last_ok[u.device_id] = now
        elif was_ok:
            self.first_unusable.setdefault(("all", u.device_id), []).append(now)
        self.first_unusable[("prev", u.device_id)] = u.ok

    def tick(self):
        now, v = self.sim.now, self.view
        p = v.get("temp.kitchen", "temperature")
        f = v.get("thermo.kitchen", "current_temp")
        w = v.get("window.kitchen", "state")
        leak = v.alarm("leak.sink", "leak")
        for u in (p, f, w, leak):
            self._track(u)
        if self.name == "timestamp":
            self._ts_sweep()
        # -- heating
        src = p if p.ok else (f if f.ok else None)
        if not p.ok and p.event_id is not None and src is not None:
            if self.o.is_good(p.device_id, p.attr, p.event_id, p.value, now):
                self.good_primary_refused += 1
        disputed = False
        if self.quality and p.ok and f.ok and v.q.pair("kitchen_temp")["state"] == "disagree":
            disputed = True
        if disputed:
            self.refuse("heating", "two kitchen thermometers disagree", ["temp.kitchen", "thermo.kitchen"])
        elif src is None:
            self.refuse("heating", "no usable kitchen temperature", ["temp.kitchen", "thermo.kitchen"])
        elif not w.ok:
            self.refuse("heating", "window state unknown", ["window.kitchen"])
        else:
            t = src.value
            out = ("pause" if w.value == "open" else "heat_on" if t < 20.5
                   else "heat_off" if t > 21.5 else "hold")
            self.decide("heating", out, [src, w])
        # -- valve stand-down after a leak
        if self.valve_closed:
            if not leak.ok:
                self.refuse("reopen_valve", f"leak reading {leak.status}", ["leak.sink"])
                self.dry_since = None
            elif leak.value is False:
                self.dry_since = now if self.dry_since is None else self.dry_since
                if now - self.dry_since >= DRY_HOLD_S:
                    self.decide("reopen_valve", "open", [leak])
                    self.valve_closed = False
                    self.valve_log.append((now, "open"))
            else:
                self.dry_since = None

    def _ts_sweep(self):
        for dev in WATCH:
            u = self.view.get(dev, {"temp.kitchen": "temperature", "thermo.kitchen": "current_temp",
                                    "window.kitchen": "state", "leak.sink": "leak"}[dev])
            if not u.ok and u.event_id is not None and dev not in self.ts_stale_open:
                self.ts_stale_open.add(dev)
                self.notify("stale", dev, f"{dev} looks stale: {u.reason}")
            elif u.ok:
                self.ts_stale_open.discard(dev)

    def daily_check(self):
        v = self.view
        us = [v.alarm("leak.sink", "leak"), v.get("window.kitchen", "state"),
              v.get("temp.kitchen", "temperature")]
        bad = [u for u in us if not u.ok]
        if bad:
            devs = [u.device_id for u in bad]
            self.refuse("daily_check", "cannot vouch for " + ", ".join(devs), devs)
            self.notify("daily_check", devs[0], "Evening check: I cannot vouch for " + ", ".join(
                f"{u.device_id} ({u.reason or u.status})" for u in bad), real=True, devices=devs)
        else:
            self.decide("daily_check", "all_clear", us)

    def person_confirms_dry(self, dev):
        if self.quality and self.valve_closed:
            self.view.q.confirm_clear(dev, "leak")
            self.decide("reopen_valve", "open_by_person", [])
            self.valve_closed = False
            self.valve_log.append((self.sim.now, "open"))


# =============================================================================================
# One seed
# =============================================================================================
def run_seed(seed, days=7):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed, temp_interval_s=60.0)
    max_age = max_age_from_devices(home.devices)
    scen = Scenario(sim, home, days)
    oracle = Oracle(sim, bus, scen, max_age)          # first subscriber and first link hook
    scen.install()
    views = {"naive": NaiveView(sim, bus),
             "timestamp": TimestampView(sim, bus, max_age),
             "quality": QualityView(sim, bus, home.registry, max_age)}
    brains = {k: Brain(k, v, sim, scen, oracle, bus) for k, v in views.items()}

    def tick():
        for b in brains.values():
            b.tick()
        sim.after(MIN, tick)
    sim.at(30.0, tick)
    for d in range(days):
        sim.at(hhmm(d, 22), lambda: [b.daily_check() for b in brains.values()])
    sim.at(scen.t_reply, lambda: [b.person_confirms_dry("leak.sink") for b in brains.values()])
    sim.run(until=days * DAY)
    return summarize(sim, scen, oracle, views, brains, days)


def _first_after(notices, t0, devices, kinds=None):
    ts = [n["t"] for n in notices if n["t"] >= t0 and n["device"] in devices
          and (kinds is None or n["kind"] in kinds)]
    return round(min(ts) - t0, 1) if ts else None


def _episodes(refusals):
    eps = []
    for r in sorted(refusals, key=lambda r: r["t"]):
        if eps and eps[-1]["kind"] == r["kind"] and eps[-1]["reason"] == r["reason"] and r["t"] - eps[-1]["end"] <= MIN + 1:
            eps[-1]["end"] = r["t"]
            eps[-1]["n"] += 1
        else:
            eps.append({"kind": r["kind"], "reason": r["reason"], "devices": r["devices"],
                        "start": r["t"], "end": r["t"], "n": 1})
    return eps


def summarize(sim, scen, oracle, views, brains, days):
    out = {}
    for name, b in brains.items():
        by_fault, by_kind = {}, {}
        for d in b.decisions:
            if d["bad"]:
                by_fault[d["bad"]] = by_fault.get(d["bad"], 0) + 1
                by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
        false = [n for n in b.notices if not n["real"]]
        fa_kind = {}
        for n in false:
            fa_kind[n["kind"]] = fa_kind.get(n["kind"], 0) + 1
        # told: a notice about one of the devices, after the device was last usable, by start + 60 s
        eps = _episodes(b.refusals)
        told = 0
        for e in eps:
            since = max([b.last_ok.get(dv, -1e9) for dv in e["devices"]] + [-1e9])
            since = min(since, e["start"])
            ok = any(n["device"] in e["devices"] or set(n.get("devices", [])) & set(e["devices"])
                     for n in b.notices if e["start"] - 6 * HOUR <= n["t"] <= e["start"] + 60)
            told += ok
        rej_good = sum(1 for r in views[name].rejected if oracle.rejected_good(r))
        # water: valve open while truly leaking; valve closed while dry after a leak
        log = b.valve_log + [(days * DAY, "end")]
        wet_open = dry_closed = 0.0
        for (t0, st), (t1, _) in zip(log, log[1:]):
            for a, c in scen.leaks:
                ov = max(0.0, min(t1, c) - max(t0, a))
                if st == "open":
                    wet_open += ov
            if st == "closed":
                dry_closed += (t1 - t0) - sum(max(0.0, min(t1, c) - max(t0, a)) for a, c in scen.leaks)
        unusable = b.first_unusable.get(("all", "leak.sink"), [])
        dead_unusable = [t for t in unusable if t >= scen.t_dead]
        res = {
            "decisions": len(b.decisions),
            "bad_decisions": sum(by_fault.values()),
            "bad_by_fault": by_fault,
            "bad_by_decision": by_kind,
            "notices": len(b.notices),
            "false_alarms": len(false),
            "false_alarms_by_kind": fa_kind,
            "refused_ticks": len(b.refusals),
            "refused_by_reason": {},
            "refusal_episodes": len(eps),
            "refusal_episodes_told": told,
            "rejected_good_readings": rej_good,
            "good_primary_refused_ticks": b.good_primary_refused,
            "leak_water_min_valve_open": round(wet_open / MIN, 1),
            "valve_closed_while_dry_min": round(dry_closed / MIN, 1),
            "detect_dead_battery_notice_s": _first_after(b.notices, scen.t_dead, ["leak.sink"]),
            "detect_dead_battery_unusable_s": round(dead_unusable[0] - scen.t_dead, 1) if dead_unusable else None,
            "detect_stuck_s": _first_after(b.notices, scen.stuck[0], ["temp.kitchen"], {"stuck", "disagreement"}),
            "detect_leak_oneshot_s": _first_after(b.notices, scen.t_oneshot, ["leak.sink"], {"disputed"}),
            "overheat_alerts": sum(1 for n in b.notices if n["kind"] == "overheat"),
        }
        for r in b.refusals:
            res["refused_by_reason"][r["reason"]] = res["refused_by_reason"].get(r["reason"], 0) + 1
        if name == "quality":
            q = views[name].q
            res["layer_stats"] = dict(q.stats)
            res["skew_estimate_end_s"] = round(q.skew("temp.kitchen"), 2)
            res["skew_true_end_s"] = round(-scen.home["temp.kitchen"].clock_skew, 2)
            res["detect_skew_notice_t_h"] = next((round(n["t"] / HOUR, 1) for n in b.notices
                                                  if n["kind"] == "clock_skew"), None)
        if name == "timestamp":
            res["ts_false_stale_t_h"] = next((round(n["t"] / HOUR, 1) for n in b.notices
                                              if n["kind"] == "stale" and not n["real"]), None)
        out[name] = res
    out["_world"] = {
        "messages_published": len(oracle.pub),
        "deliveries": sum(len(v) for v in oracle.deliv.values()),
        "duplicate_copies": sum(len(v) - 1 for v in oracle.deliv.values()),
        "window_airings": len(scen.windows),
        "leak_events_truth": 2,
    }
    return out


# =============================================================================================
def aggregate(per_seed):
    names = ["naive", "timestamp", "quality"]
    agg = {}
    for n in names:
        rs = [s[n] for s in per_seed]
        a = {}
        for k in rs[0]:
            vals = [r[k] for r in rs]
            if isinstance(vals[0], dict):
                keys = sorted({kk for v in vals for kk in v})
                a[k] = {kk: sum(v.get(kk, 0) for v in vals) for kk in keys}
            elif all(isinstance(x, (int, float)) for x in vals):
                a[k] = {"total": round(sum(vals), 1), "mean": round(statistics.mean(vals), 2),
                        "min": min(vals), "max": max(vals)}
            else:
                got = [x for x in vals if x is not None]
                a[k] = {"detected_in": f"{len(got)}/{len(vals)}",
                        "median": round(statistics.median(got), 1) if got else None,
                        "max": max(got) if got else None}
        agg[n] = a
    world = per_seed[0]["_world"].keys()
    agg["_world"] = {k: round(statistics.mean(s["_world"][k] for s in per_seed), 1) for k in world}
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    t0 = time.time()
    from multiprocessing import Pool
    with Pool(min(a.jobs, a.seeds)) as pool:
        per_seed = pool.starmap(run_seed, [(s, a.days) for s in range(a.seeds)])
    agg = aggregate(per_seed)
    results = {
        "experiment": "b03_readings",
        "date": dt.date.today().isoformat(),
        "method": (
            "Seeded simulated week (virtual time) of the B01 twin (islab.twin.build_home) with faults "
            "injected through FaultInjector and scheduled sensor faults. Three hub views read the same "
            "bus at once: naive (latest arrival wins), timestamp (islab.policy.StateStore: dedupe by "
            "event id, order and freshness by device ts) and quality (islab.readings.ReadingQuality). "
            "Identical decision code runs on each view: kitchen heating every 60 s, overheat alert, leak "
            "shut-off and valve reopen, night light on motion, doorbell notice, 22:00 house check. An "
            "oracle (first link hook and first subscriber) labels every message; views never read it. "
            "A decision is on bad data if an input came from a lying sensor and differs from truth by "
            "more than 1 degC (or wrong leak state), was superseded by a newer honest reading already "
            "delivered, was truly older than the device max age (2.5 x heartbeat), or the decision was "
            "triggered by a duplicate event copy. Decisions are recorded, not executed (open loop)."),
        "params": {
            "seeds": a.seeds, "days": a.days, "decision_tick_s": 60, "temp_tolerance_c": TOL,
            "faults": {
                "duplicates": "10% of state/ and event/ messages delivered twice (0.05-0.5 s later)",
                "reordering": "0-30 s uniform jitter on state/window.kitchen; ~14 airings/day of 5-60 s",
                "clock_drift": "temp.kitchen clock 2 s/hour behind the hub, all week (336 s at day 7)",
                "spike": "day 0 ~14:00, one temp.kitchen report +15 degC",
                "leak_oneshot": "day 2 ~03:00, one leak event, state stays dry; truly wet 2 h; person reads phone 07:10",
                "stuck": "day 1 ~08:00, temp.kitchen stuck 12 h (same value, fresh timestamps)",
                "dead_battery": "day 5 ~10:00, leak.sink last gasp then silence",
            },
            "legitimate_events": {
                "real_leak": "day 1 ~10:00, 20 min, state wet + event, then dry",
                "real_step": "day 3 ~12:00, +5 degC for 30 min (fast but real)",
                "cooking": "daily 18:00, +3 degC over 3 min for 45 min",
            },
            "quality_layer": {
                "max_age": "2.5 x device heartbeat (temp 150 s, thermostat 75 s, battery sensors 750 s)",
                "skew": "rolling median of 15 (hub receive - device ts); alert above 60 s",
                "rate_limit_temp_c_per_s": 0.05, "range_temp_c": [-30, 60],
                "stuck": "identical values for 20 min (>= 15 reports)",
                "disagreement": "temp.kitchen vs thermo.kitchen > 1.5 degC for 10 min",
                "latch": "leak/smoke events clear only on device wet->dry or person confirmation",
            },
        },
        "results": agg,
        "per_seed": per_seed,
        "limitations": [
            "Simulation only; faults are scripted, one of each per week, at fixed times of day (+-30 min).",
            "Open loop: decisions are recorded, not sent to devices; heating does not change the room.",
            "Sensor resolution 0.01 degC with 0.05 degC noise makes stuck detection easy; a real 0.5 degC "
            "sensor in a stable room can repeat one value for hours and would need a longer window or a "
            "second source.",
            "The rate limit would also hold a real fire's temperature rise for one report; smoke alarms, "
            "not temperature sensors, must carry fire detection.",
            "Duplicate and reorder faults are i.i.d.; real radios lose and repeat in bursts.",
            "The person's reply time is scripted (07:10); a person who never answers keeps the valve closed.",
            "Thresholds were set by us before the run and not tuned per seed; other thresholds give other numbers.",
        ],
        "wall_time_s": round(time.time() - t0, 1),
    }
    (HERE / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(json.dumps(agg, indent=1, default=str))


if __name__ == "__main__":
    main()
