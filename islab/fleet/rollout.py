"""A fleet of simulated homes and a staged software update rollout. A lightweight model: no twin
inside each home. What a home's health report shows after a bad update comes from the full-test
twin (experiments/b10_fleet calibrates it); everything else here is a labelled ASSUMPTION.

    res = simulate(FleetConfig(n_homes=1000, design="rings_auto_halt"), Update(...), seed=0)

Model, hourly ticks over ``horizon_h``:
  * availability (ASSUMED): most homes are always online; some drop off for spells of hours
    ("intermittent"); a few are offline for the whole rollout ("away");
  * a home checks for updates every ``check_every_h`` (the 6 h update check of the B09 day) and
    installs when its ring is open and the rollout is not halted. With ``install="maintenance"``
    it downloads at the check and installs at the next 03:00, even if the plane halted in between
    or the home is offline then (installing needs no internet);
  * each online home sends a health report every hour: counts only (see cloud.py). After a bad
    update a home starts failing at a time drawn from ``Update.fail_after`` and shows it in its
    next report while online;
  * the plane halts automatically when ``halt_min_failing`` new-version homes report failing, or,
    without automatic halt, an operator halts ``operator_delay_h`` after the first failing report;
  * after a halt the plane publishes a rollback: homes on the bad version return to the old one at
    their next check while online.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

AVAILABILITY_ASSUMPTIONS = {
    "always_online": 0.88,          # share of homes
    "intermittent": 0.10,           # drop off with p_drop per hour, spells exponential with mean spell_h
    "away": 0.02,                   # offline for the whole rollout
    "p_drop_per_h": 0.02,
    "spell_h_mean": 12.0,
}
PLAN = (("canary", 0.01), ("ring1", 0.05), ("ring2", 0.20), ("ring3", 0.74))


@dataclass
class FleetConfig:
    n_homes: int = 1000
    horizon_h: int = 14 * 24
    check_every_h: int = 6
    design: str = "rings_auto_halt"       # all_at_once | rings_operator | rings_auto_halt
    install: str = "on_check"             # on_check | maintenance (03:00 window)
    plan: tuple = PLAN
    soak_h: int = 24
    halt_min_failing: int = 2
    operator_delay_h: int = 24
    availability: dict = field(default_factory=lambda: dict(AVAILABILITY_ASSUMPTIONS))


@dataclass
class Update:
    name: str
    p_affect: float                       # share of installed homes that start failing
    fail_after: object = None             # callable(rng, install_hour_of_day array) -> hours after install
    p_false_per_day: float = 0.0          # chance a healthy home's report shows a failing count, per day


def _availability(rng, cfg):
    n, H, a = cfg.n_homes, cfg.horizon_h, cfg.availability
    kind = rng.choice(3, size=n, p=[a["always_online"], a["intermittent"], a["away"]])
    online = np.ones((H, n), dtype=bool)
    online[:, kind == 2] = False
    inter = np.where(kind == 1)[0]
    for i in inter:
        h = 0
        while h < H:
            if rng.random() < a["p_drop_per_h"]:
                spell = max(1, int(rng.exponential(a["spell_h_mean"])))
                online[h:h + spell, i] = False
                h += spell
            else:
                h += 1
    return kind, online


def simulate(cfg: FleetConfig, upd: Update, seed=0):
    rng = np.random.default_rng(seed)
    n, H = cfg.n_homes, cfg.horizon_h
    kind, online = _availability(rng, cfg)
    phase = rng.integers(0, cfg.check_every_h, size=n)          # each home's check hour offset
    order = rng.permutation(n)
    ring = np.empty(n, dtype=int)
    if cfg.design == "all_at_once":
        ring[:] = 0
        plan = (("all", 1.0),)
    else:
        plan = cfg.plan
        edges = np.cumsum([max(1, int(round(f * n))) for _, f in plan])
        edges[-1] = n
        start = 0
        for k, e in enumerate(edges):
            ring[order[start:e]] = k
            start = e
    ring_open_at = [0] + [None] * (len(plan) - 1)
    version = np.zeros(n, dtype=int)            # 0 old, 1 new, 2 rolled back to old
    installed_at = np.full(n, -1)
    downloaded = np.zeros(n, dtype=bool)
    affected = rng.random(n) < upd.p_affect
    fail_at = np.full(n, np.inf)
    halted_at, first_fail_report, rollback_at = None, None, None
    ever_new = np.zeros(n, dtype=bool)
    installed_after_halt = 0
    reported_failing = np.zeros(n, dtype=bool)
    false_flags = np.zeros(n, dtype=bool)
    p_false_h = 1 - (1 - upd.p_false_per_day) ** (1 / 24) if upd.p_false_per_day else 0.0

    def install(idx, h):
        nonlocal installed_after_halt
        if idx.size == 0:
            return
        version[idx] = 1
        installed_at[idx] = h
        ever_new[idx] = True
        if halted_at is not None:
            installed_after_halt += idx.size
        aff = idx[affected[idx]]
        if aff.size:
            delay = upd.fail_after(rng, np.full(aff.size, h % 24)) if upd.fail_after else np.zeros(aff.size)
            fail_at[aff] = h + np.asarray(delay, dtype=float)

    for h in range(H):
        on = online[h]
        # rings open after a quiet soak
        if halted_at is None and cfg.design != "all_at_once":
            for k in range(1, len(plan)):
                if ring_open_at[k] is None and ring_open_at[k - 1] is not None and h >= ring_open_at[k - 1] + cfg.soak_h:
                    ring_open_at[k] = h
                    break
        open_rings = np.array([o is not None and o <= h for o in ring_open_at])
        checking = on & ((h - phase) % cfg.check_every_h == 0)
        if rollback_at is not None:
            back = np.where(checking & (version == 1))[0]
            version[back] = 2
        elif halted_at is None:
            cand = np.where(checking & (version == 0) & open_rings[ring])[0]
            if cfg.install == "on_check":
                install(cand, h)
            else:
                downloaded[cand] = True
        if cfg.install == "maintenance" and h % 24 == 3:
            # the 03:00 window installs what was downloaded, online or not, halted or not
            idx = np.where(downloaded & (version == 0))[0]
            downloaded[idx] = False
            install(idx, h)
        # hourly health reports (counts only), online homes only
        failing_now = (version == 1) & (fail_at <= h)
        if p_false_h:
            false_flags |= (version == 1) & (rng.random(n) < p_false_h)
        rep = on & (version == 1) & (failing_now | false_flags)
        reported_failing |= rep
        if first_fail_report is None and rep.any():
            first_fail_report = h
        if halted_at is None:
            if cfg.design == "rings_auto_halt" and reported_failing.sum() >= cfg.halt_min_failing:
                halted_at = h
            elif cfg.design != "rings_auto_halt" and first_fail_report is not None and \
                    h >= first_fail_report + cfg.operator_delay_h:
                halted_at = h
            if halted_at is not None:
                rollback_at = h
    bad_installs = int(ever_new.sum())
    failed_homes = int(((installed_at >= 0) & affected & (fail_at < H)).sum())
    first_install = int(installed_at[installed_at >= 0].min()) if (installed_at >= 0).any() else None
    res = {
        "homes": n, "availability_kinds": {"always_online": int((kind == 0).sum()),
                                           "intermittent": int((kind == 1).sum()), "away": int((kind == 2).sum())},
        "installed_new": bad_installs,
        "installed_new_before_halt": int(((installed_at >= 0) & ((installed_at <= halted_at) if halted_at is not None
                                                                 else True)).sum()),
        "installed_after_halt": installed_after_halt,
        "homes_failing": failed_homes,
        "first_install_h": first_install,
        "first_failing_report_h": first_fail_report,
        "halted": halted_at is not None,
        "halt_h": halted_at,
        "halt_after_first_failure_report_h": (halted_at - first_fail_report) if halted_at is not None and
        first_fail_report is not None else None,
        "on_new_at_end": int((version == 1).sum()),
        "on_old_at_end": int((version != 1).sum()),
        "never_updated": int((~ever_new).sum()),
        "never_updated_away": int(((~ever_new) & (kind == 2)).sum()),
        "on_new_at_end_offline_at_end": int(((version == 1) & ~online[-1]).sum()),
        "rings_opened": sum(o is not None for o in ring_open_at),
    }
    if upd.p_affect == 0 and not upd.p_false_per_day:
        res["hours_to_95pct_on_new"] = _hours_to_share(installed_at, 0.95, n)
    elif upd.p_affect == 0:
        res["hours_to_95pct_on_new"] = _hours_to_share(installed_at, 0.95, n)
    return res


def _hours_to_share(installed_at, share, n):
    t = np.sort(installed_at[installed_at >= 0])
    k = int(np.ceil(share * n))
    return int(t[k - 1]) if t.size >= k else None


def summarize(rows, keys):
    out = {}
    for k in keys:
        xs = [r[k] for r in rows if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        if xs:
            xs = sorted(xs)
            out[k] = {"mean": round(float(np.mean(xs)), 2), "p50": xs[len(xs) // 2], "min": xs[0], "max": xs[-1]}
        else:
            out[k] = None
    out["halted_runs"] = sum(1 for r in rows if r.get("halted"))
    out["runs"] = len(rows)
    return out
