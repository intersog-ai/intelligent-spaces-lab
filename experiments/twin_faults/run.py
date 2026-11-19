"""Baseline: how often does 'ack' mean 'done' under each fault type?

For every fault type we run N seeded trials. Each trial builds a fresh home,
injects one fault, lets the house boot, then a tiny client sends ONE command and
waits up to 30 virtual seconds for a confirming ``state/<device>`` (target
attributes, ts after the command -- the contract's definition of CONFIRMED).

Outcomes per trial (mutually exclusive):
  confirmed               confirming state seen within the timeout
  ack_not_confirmed       accepted ack seen, no confirming state  <- "ack != done"
  no_ack                  neither

Usage:  python experiments/twin_faults/run.py [--trials 200]
Writes results.json and notes.md next to this file. Virtual time only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from islab.core.bus import Bus  # noqa: E402
from islab.core.sim import Sim  # noqa: E402
from islab.twin import build_home  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
TIMEOUT = 30.0
BOOT = 5.0
DEFAULT_TARGET = ("valve.main", "close", {})


def _links(dev: str) -> list[str]:
    return [f"cmd/{dev}", f"ack/{dev}", f"state/{dev}"]


# name -> (setup(home, dev), target device/action/args, human description)
FAULTS = {
    "none":           (lambda h, d: None, DEFAULT_TARGET, "no fault"),
    "link_drop":      (lambda h, d: h.faults.link(_links(d), drop=0.3), DEFAULT_TARGET,
                       "30% drop on cmd/ack/state of the device"),
    "link_delay":     (lambda h, d: h.faults.link(_links(d), delay=5.0), DEFAULT_TARGET,
                       "+5 s on cmd/ack/state"),
    "link_duplicate": (lambda h, d: h.faults.link(_links(d), duplicate=0.5), DEFAULT_TARGET,
                       "50% of cmd/ack/state messages delivered twice"),
    "link_reorder":   (lambda h, d: h.faults.link(_links(d), jitter=10.0), DEFAULT_TARGET,
                       "uniform 0-10 s jitter on cmd/ack/state (reorders)"),
    "stuck":          (lambda h, d: h.faults.stuck(d), DEFAULT_TARGET,
                       "actuator acks, never moves"),
    "partial":        (lambda h, d: h.faults.partial(d, 40), DEFAULT_TARGET,
                       "valve stops at 40%"),
    "offline":        (lambda h, d: h.faults.offline(d), DEFAULT_TARGET,
                       "device silent, ignores commands"),
    "stale":          (lambda h, d: h.faults.stale(d), DEFAULT_TARGET,
                       "repeats last snapshot with old ts; acks commands"),
    "clock_skew_behind": (lambda h, d: h.faults.clock_skew(d, -120.0), DEFAULT_TARGET,
                          "device clock 120 s behind hub"),
    "clock_skew_ahead":  (lambda h, d: h.faults.clock_skew(d, +120.0), DEFAULT_TARGET,
                          "device clock 120 s ahead of hub"),
    "battery_dead":   (lambda h, d: h.faults.battery_dead(d), ("lock.front", "unlock", {}),
                       "battery lock: last gasp then silence"),
    "internet_down":  (lambda h, d: h.faults.internet_down(), DEFAULT_TARGET,
                       "WAN down; local bus intact"),
    "hostile_name":   (lambda h, d: h.faults.hostile_name(d), DEFAULT_TARGET,
                       "prompt-injection friendly name (agent-layer fault)"),
}


class ConfirmClient:
    """Minimal hub client: send one command, watch ack + state, apply the CONFIRMED rule."""

    def __init__(self, home, dev: str):
        self.home, self.dev = home, dev
        self.cmd_id: str | None = None
        self.t_cmd = self.t_ack = self.t_confirm = None
        self.acks = 0
        self.expected: dict = {}
        home.bus.subscribe(f"ack/{dev}", self._on_ack, name="client")
        home.bus.subscribe(f"state/{dev}", self._on_state, name="client")

    def send(self, action: str, args: dict) -> None:
        self.expected = self.home.expected_state(self.dev, action, args)
        self.t_cmd = self.home.sim.now
        self.cmd_id = self.home.command(self.dev, action, args, source="client")

    def _on_ack(self, env) -> None:
        if env.payload.get("cmd_id") == self.cmd_id and env.payload.get("status") == "accepted":
            self.acks += 1
            if self.t_ack is None:
                self.t_ack = self.home.sim.now

    def _on_state(self, env) -> None:
        if self.t_cmd is None or self.t_confirm is not None:
            return
        if env.ts > self.t_cmd and all(env.payload.get(k) == v for k, v in self.expected.items()):
            if self.home.sim.now - self.t_cmd <= TIMEOUT:
                self.t_confirm = self.home.sim.now


def trial(fault: str, seed: int) -> dict:
    setup, (dev, action, args), _ = FAULTS[fault]
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed)
    setup(home, dev)
    client = ConfirmClient(home, dev)
    sim.run(until=BOOT)
    client.send(action, args)
    sim.run(until=BOOT + TIMEOUT)
    if client.t_confirm is not None:
        outcome = "confirmed"
    elif client.t_ack is not None:
        outcome = "ack_not_confirmed"
    else:
        outcome = "no_ack"
    return {"outcome": outcome,
            "ttc": None if client.t_confirm is None else client.t_confirm - client.t_cmd,
            "dup_ack": client.acks > 1,
            "confirmed_without_ack": client.t_confirm is not None and client.t_ack is None}


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    share = lambda f: round(100.0 * sum(1 for r in rows if f(r)) / n, 1)
    ttc = [r["ttc"] for r in rows if r["ttc"] is not None]
    return {"trials": n,
            "confirmed_pct": share(lambda r: r["outcome"] == "confirmed"),
            "ack_not_confirmed_pct": share(lambda r: r["outcome"] == "ack_not_confirmed"),
            "no_ack_pct": share(lambda r: r["outcome"] == "no_ack"),
            "confirmed_without_ack_pct": share(lambda r: r["confirmed_without_ack"]),
            "duplicate_ack_pct": share(lambda r: r["dup_ack"]),
            "ttc_p50_s": pct(ttc, 0.5), "ttc_p95_s": pct(ttc, 0.95)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=200)
    trials = ap.parse_args().trials
    results = {}
    for fault in FAULTS:
        results[fault] = summarize([trial(fault, seed) for seed in range(trials)])
    out = {
        "experiment": "twin_faults",
        "date": dt.date.today().isoformat(),
        "method": ("Per fault type, N seeded trials (seed = trial index) of a fresh twin home. "
                   f"Fault injected at t=0; after {BOOT:g} s boot a minimal client sends one command "
                   f"and waits {TIMEOUT:g} virtual s. CONFIRMED = a state/<device> message with the "
                   "expected attributes and device ts > command time (contract rule). Outcomes are "
                   "mutually exclusive: confirmed / ack_not_confirmed / no_ack. ttc = time from "
                   "command to confirming state, virtual seconds."),
        "params": {"trials": trials, "timeout_s": TIMEOUT, "boot_s": BOOT,
                   "faults": {k: {"target": f"{v[1][0]} {v[1][1]}", "setup": v[2]}
                              for k, v in FAULTS.items()},
                   "base_latency_s": 0.01},
        "results": results,
        "limitations": [
            "Synthetic device models; delay distributions are uniform guesses, not measured hardware.",
            "One command per trial on one device; no retries, no concurrent load.",
            "Link faults are i.i.d. per message; real radio loss is bursty.",
            "Client trusts device ts per the contract; clock_skew_behind shows that rule "
            "rejects real confirmations when the device clock lags.",
            "internet_down and hostile_name do not touch the local transport, so they look "
            "healthy here; their effects show only in the agent/egress layers.",
        ],
    }
    (HERE / "results.json").write_text(json.dumps(out, indent=2) + "\n")
    print(f"{'fault':20s} {'conf%':>6s} {'ack!conf%':>9s} {'noack%':>7s} {'p50':>6s} {'p95':>6s}")
    for k, r in results.items():
        print(f"{k:20s} {r['confirmed_pct']:6.1f} {r['ack_not_confirmed_pct']:9.1f} "
              f"{r['no_ack_pct']:7.1f} {str(r['ttc_p50_s']):>6s} {str(r['ttc_p95_s']):>6s}")


if __name__ == "__main__":
    main()
