"""Reading-quality layer (Build Track B03, principle 1: fresh, sourced readings).

Every state or event message from a device becomes a :class:`QReading` with five provenance
fields: source device, device sequence number, device timestamp, hub receive time and event id.
Before a reading may drive a decision, the layer checks it:

* **sourced**      device id, event id and device timestamp present; topic device == source.
* **duplicate**    event id seen before, or (device, seq) seen before.
* **out of order** seq lower than the newest seq already seen from that device. Late *state* is
                   rejected (it would overwrite newer state); late *events* are kept and flagged
                   (an old leak is still a leak).
* **clock skew**   per device, rolling median of (hub receive time - device ts). Device ts is
                   corrected by that median, so a drifting device clock does not look stale.
                   A frozen timestamp (ts not advancing while messages keep coming) means a replay.
* **plausible**    range limits and rate-of-change limits per (kind, attr). A jump faster than the
                   limit is HELD, not dropped: the next reading decides. If it agrees with the jump,
                   the jump was real and is applied (one report late); if not, it was a spike.
* **stuck**        a numeric attribute whose values stay within a variance floor for too long.
* **stale**        per-device max age, on hub receive time and on corrected device time.
* **latched events** an alarm event (leak, smoke) is not cleared by a heartbeat that never said
                   "wet": a sensor that reported a leak and then only "dry" heartbeats is DISPUTED.
                   It clears when the device itself goes wet -> dry, or a person confirms.
* **disagreement** two sources for one quantity (e.g. two kitchen thermometers) that differ by
                   more than a tolerance for longer than a hold time.

The layer never decides what the home does. :meth:`ReadingQuality.assess` returns a value with a
status; decision code uses only ``status == OK`` readings and must tell the person when it refuses.
New problems are published as *issues* (see :attr:`ReadingQuality.issues` and :meth:`on_issue`) so
the hub can tell people (principle 4). All time is virtual (``sim.now``).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

OK, STALE, STUCK, DISPUTED, UNKNOWN, DEAD = "ok", "stale", "stuck", "disputed", "unknown", "dead"

# verdicts returned by ingest()
ACCEPTED = "accepted"
REJECT_UNSOURCED = "unsourced"
REJECT_DUPLICATE = "duplicate"
REJECT_OUT_OF_ORDER = "out_of_order"
REJECT_RANGE = "out_of_range"
HELD = "held_implausible"


@dataclass(frozen=True)
class SensorProfile:
    """Limits for one numeric attribute of one device kind."""
    lo: float | None = None
    hi: float | None = None
    max_rate: float | None = None        # units per second; faster jumps are held
    stuck_window_s: float | None = None  # identical-ish values for this long = stuck
    stuck_min_n: int = 10                # ...and at least this many readings
    stuck_floor: float = 0.0             # max - min within the window at or below this = stuck


# Defaults: indoor temperature may not move faster than 3 degC per minute; a sensor that reports
# the same value (0.01 degC resolution) for 20 minutes is stuck.
DEFAULT_PROFILES: dict[tuple[str, str], SensorProfile] = {
    ("temperature", "temperature"): SensorProfile(-30, 60, 0.05, 1200, 15, 0.0),
    ("temperature", "humidity"): SensorProfile(0, 100, 1.0),
    ("thermostat", "current_temp"): SensorProfile(-30, 60, 0.05, 1200, 30, 0.0),
    ("thermostat", "setpoint"): SensorProfile(5, 30),
    ("valve", "position"): SensorProfile(0, 100),
    ("shade", "position"): SensorProfile(0, 100),
    ("garage_door", "position"): SensorProfile(0, 100),
}

DEFAULT_LATCH = {"leak": "leak", "smoke": "smoke"}   # event type -> state attribute it asserts


@dataclass(frozen=True)
class QReading:
    device_id: str
    attr: str
    value: Any
    seq: int
    device_ts: float
    corrected_ts: float
    received: float
    event_id: str

    def source(self) -> dict:
        return {"device_id": self.device_id, "seq": self.seq, "device_ts": self.device_ts,
                "received": self.received, "event_id": self.event_id}


@dataclass
class Assessment:
    device_id: str
    attr: str
    status: str
    value: Any = None
    reasons: list[str] = field(default_factory=list)
    reading: QReading | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK


def max_age_from_devices(devices: dict, factor: float = 2.5, floor_s: float = 30.0) -> dict[str, float]:
    """Per-device max age = factor x the device's heartbeat (missing two reports and a bit)."""
    out = {}
    for dev_id, dev in devices.items():
        hb = getattr(dev, "heartbeat_s", None)
        out[dev_id] = max(floor_s, factor * hb) if hb else float("inf")
    return out


class ReadingQuality:
    """Ingests ``state/#`` and ``event/#``; answers "can this reading drive a decision?"."""

    def __init__(self, sim, bus=None, registry: dict | None = None, max_age: dict | None = None,
                 default_max_age: float = 150.0, profiles: dict | None = None,
                 latch: dict | None = None, skew_window: int = 15, skew_alert_s: float = 60.0,
                 battery_low: float = 5.0, sweep_s: float | None = 60.0, max_seen: int = 200_000):
        self.sim = sim
        self.registry = registry or {}
        self.max_age = dict(max_age or {})
        self.default_max_age = default_max_age
        self.profiles = DEFAULT_PROFILES if profiles is None else profiles
        self.latch_map = DEFAULT_LATCH if latch is None else latch
        self.skew_window, self.skew_alert_s, self.battery_low = skew_window, skew_alert_s, battery_low
        self._max_seen = max_seen
        self._seen_ev: dict[str, None] = {}
        self._seen_seq: dict[str, set] = {}
        self._seq_order: dict[str, deque] = {}
        self._last_seq: dict[str, int] = {}
        self._last_rx: dict[str, float] = {}
        self._last_dev_ts: dict[str, tuple[float, float]] = {}   # dev -> (device_ts, received)
        self._offsets: dict[str, deque] = {}
        self._skew: dict[str, float] = {}
        self._frozen: dict[str, bool] = {}
        self._state: dict[str, dict[str, QReading]] = {}
        self._held: dict[tuple[str, str], QReading] = {}
        self._hist: dict[tuple[str, str], deque] = {}
        self._stuck: dict[tuple[str, str], bool] = {}
        self._latch: dict[tuple[str, str], dict] = {}   # (dev, attr) -> {seq, t, seen_true, disputed}
        self._dead: dict[str, bool] = {}
        self._pairs: dict[str, dict] = {}
        self._open: set = set()                          # open issue keys (one notice per episode)
        self.issues: list[dict] = []
        self._issue_fns: list[Callable[[dict], None]] = []
        self._accept_fns: list[Callable[[str, Any, QReading | None], None]] = []
        self.stats = {k: 0 for k in ("accepted", "events", "late_events", REJECT_UNSOURCED,
                                     REJECT_DUPLICATE, REJECT_OUT_OF_ORDER, REJECT_RANGE, HELD,
                                     "held_confirmed", "held_rejected", "frozen_ts")}
        self.rejected: list[dict] = []                   # every rejection, with provenance
        if bus is not None:
            bus.subscribe("state/#", self._on_bus, name="readings.quality")
            bus.subscribe("event/#", self._on_bus, name="readings.quality")
        if sweep_s:
            self._sweep_s = sweep_s
            sim.after(sweep_s, self._sweep_loop)

    # ------------------------------------------------------------------ hooks
    def on_issue(self, fn: Callable[[dict], None]) -> None:
        self._issue_fns.append(fn)

    def on_accept(self, fn: Callable[[str, Any, QReading | None], None]) -> None:
        """fn(kind, env, reading) for every accepted message (kind 'state' or 'event')."""
        self._accept_fns.append(fn)

    def _issue(self, key: tuple, kind: str, device: str, **detail) -> None:
        if key in self._open:
            return
        self._open.add(key)
        rec = {"t": self.sim.now, "kind": kind, "device": device, **detail}
        self.issues.append(rec)
        self.sim.trace("reading_issue", issue=kind, device=device, **detail)
        for fn in list(self._issue_fns):
            fn(rec)

    def _resolve(self, key: tuple) -> None:
        if key in self._open:
            self._open.discard(key)
            self.issues.append({"t": self.sim.now, "kind": "resolved", "device": key[1], "issue": key[0]})

    # ------------------------------------------------------------------ helpers
    def _kind(self, dev: str) -> str:
        return (self.registry.get(dev) or {}).get("kind", "")

    def max_age_for(self, dev: str) -> float:
        return self.max_age.get(dev, self.default_max_age)

    def skew(self, dev: str) -> float:
        """Estimated (hub - device) clock offset in seconds; 0 if unknown."""
        return self._skew.get(dev, 0.0)

    def corrected_ts(self, dev: str, ts: float) -> float:
        return ts + self.skew(dev)

    def _reject(self, env, why: str, **kw) -> str:
        self.stats[why] += 1
        self.rejected.append({"t": self.sim.now, "event_id": getattr(env, "event_id", None),
                              "topic": getattr(env, "topic", None), "why": why, **kw})
        self.sim.trace("reading_reject", why=why, event_id=getattr(env, "event_id", None))
        return why

    # ------------------------------------------------------------------ ingest
    def _on_bus(self, env) -> None:
        self.ingest(env)

    def ingest(self, env) -> str:
        """Check one Envelope; apply it if acceptable. Returns a verdict string."""
        topic = getattr(env, "topic", "") or ""
        kind, _, dev = topic.partition("/")
        if (not dev or not getattr(env, "event_id", None) or getattr(env, "ts", None) is None
                or getattr(env, "source", None) != dev):
            return self._reject(env, REJECT_UNSOURCED)
        seq = int(getattr(env, "seq", 0) or 0)
        # duplicates: event id, then (device, seq)
        if env.event_id in self._seen_ev:
            return self._reject(env, REJECT_DUPLICATE, by="event_id")
        seqs = self._seen_seq.setdefault(dev, set())
        if seq and seq in seqs:
            return self._reject(env, REJECT_DUPLICATE, by="seq")
        self._seen_ev[env.event_id] = None
        if len(self._seen_ev) > self._max_seen:
            self._seen_ev.pop(next(iter(self._seen_ev)))
        if seq:
            seqs.add(seq)
            order = self._seq_order.setdefault(dev, deque())
            order.append(seq)
            if len(order) > 4096:
                seqs.discard(order.popleft())
        now = self.sim.now
        late = seq and seq < self._last_seq.get(dev, 0)
        self._last_rx[dev] = now
        if self._dead.get(dev) and kind == "state" and (env.payload or {}).get("battery", 1) > 0:
            self._dead[dev] = False
            self._resolve(("dead", dev))
        if late and kind == "state":
            return self._reject(env, REJECT_OUT_OF_ORDER, seq=seq, newest=self._last_seq[dev])
        if seq:
            self._last_seq[dev] = max(seq, self._last_seq.get(dev, 0))
        if kind == "event":
            if late:
                self.stats["late_events"] += 1
            return self._ingest_event(env, dev, seq, now)
        return self._ingest_state(env, dev, seq, now)

    def _update_skew(self, dev: str, ts: float, now: float) -> None:
        prev = self._last_dev_ts.get(dev)
        self._last_dev_ts[dev] = (ts, now)
        if prev is not None and ts <= prev[0] and now - prev[1] > 1.0:
            # device time did not advance while hub time did: replayed / frozen snapshot
            if not self._frozen.get(dev):
                self.stats["frozen_ts"] += 1
            self._frozen[dev] = True
            self._issue(("frozen", dev), "stale", dev, why="timestamp not advancing")
            return
        if self._frozen.get(dev):
            self._frozen[dev] = False
            self._resolve(("frozen", dev))
        q = self._offsets.setdefault(dev, deque(maxlen=self.skew_window))
        q.append(now - ts)
        srt = sorted(q)
        n = len(srt)
        self._skew[dev] = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
        if abs(self._skew[dev]) > self.skew_alert_s:
            self._issue(("skew", dev), "clock_skew", dev, skew_s=round(self._skew[dev], 1))

    def _ingest_event(self, env, dev, seq, now) -> str:
        p = env.payload or {}
        attr = self.latch_map.get(p.get("type"))
        if attr:
            cur = (self._state.get(dev) or {}).get(attr)
            self._latch[(dev, attr)] = {"seq": seq, "t": now, "event_id": env.event_id,
                                        "seen_true": bool(cur and cur.value is True), "disputed": False}
        self.stats["events"] += 1
        for fn in list(self._accept_fns):
            fn("event", env, None)
        return ACCEPTED

    def _ingest_state(self, env, dev, seq, now) -> str:
        payload = env.payload or {}
        self._update_skew(dev, env.ts, now)
        cts = self.corrected_ts(dev, env.ts)
        kind = self._kind(dev)
        # range check first: any attribute out of range rejects the whole message
        for attr, v in payload.items():
            prof = self.profiles.get((kind, attr))
            if prof and isinstance(v, (int, float)) and not isinstance(v, bool):
                if (prof.lo is not None and v < prof.lo) or (prof.hi is not None and v > prof.hi):
                    return self._reject(env, REJECT_RANGE, attr=attr, value=v)
        attrs = self._state.setdefault(dev, {})
        held_any = False
        for attr, v in payload.items():
            r = QReading(dev, attr, v, seq, env.ts, cts, now, env.event_id)
            prof = self.profiles.get((kind, attr))
            if prof and prof.max_rate and isinstance(v, (int, float)) and not isinstance(v, bool):
                verdict = self._rate_check(dev, attr, r, attrs.get(attr), prof)
                if verdict == HELD:
                    held_any = True
                    continue
            attrs[attr] = r
            if prof and prof.stuck_window_s:
                self._stuck_check(dev, attr, r, prof)
            if attr in ("battery",) and isinstance(v, (int, float)) and v <= self.battery_low:
                if v <= 0:
                    self._dead[dev] = True
                    self._issue(("dead", dev), "battery_empty", dev, battery=v)
                else:
                    self._issue(("battery", dev), "battery_low", dev, battery=v)
            self._latch_check(dev, attr, v, seq)
        if held_any:
            self.stats[HELD] += 1
            self.rejected.append({"t": now, "event_id": env.event_id, "topic": env.topic, "why": HELD})
        self.stats["accepted"] += 1
        for fn in list(self._accept_fns):
            fn("state", env, attrs)
        self._pairs_check()
        return HELD if held_any else ACCEPTED

    def _rate_check(self, dev, attr, r: QReading, prev: QReading | None, prof: SensorProfile) -> str:
        key = (dev, attr)
        held = self._held.pop(key, None)

        def rate(a: QReading, b: QReading) -> float:
            return abs(b.value - a.value) / max(b.corrected_ts - a.corrected_ts, 1.0)

        if held is not None:
            if rate(held, r) <= prof.max_rate:
                self.stats["held_confirmed"] += 1        # the jump was real: keep the new level
                return ACCEPTED
            self.stats["held_rejected"] += 1             # the jump was a spike
            self.sim.trace("reading_spike_rejected", device=dev, attr=attr, event_id=held.event_id)
            if prev is None or rate(prev, r) <= prof.max_rate:
                return ACCEPTED
        if prev is not None and rate(prev, r) > prof.max_rate:
            self._held[key] = r
            return HELD
        return ACCEPTED

    def _stuck_check(self, dev, attr, r: QReading, prof: SensorProfile) -> None:
        key = (dev, attr)
        h = self._hist.setdefault(key, deque())
        h.append((r.received, r.value))
        # keep one reading at or beyond the window edge so the span can reach the window
        while len(h) > prof.stuck_min_n and h[1][0] <= r.received - prof.stuck_window_s:
            h.popleft()
        vals = [v for _, v in h]
        span = h[-1][0] - h[0][0]
        stuck = (len(vals) >= prof.stuck_min_n and span >= prof.stuck_window_s
                 and max(vals) - min(vals) <= prof.stuck_floor)
        if stuck and not self._stuck.get(key):
            self._issue(("stuck", dev, attr), "stuck", dev, attr=attr, value=r.value,
                        since_s=round(span, 1))
        elif not stuck and self._stuck.get(key):
            self._resolve(("stuck", dev, attr))
        self._stuck[key] = stuck

    def _latch_check(self, dev, attr, v, seq) -> None:
        lt = self._latch.get((dev, attr))
        if lt is None or seq <= lt["seq"] and lt["seq"]:
            if lt is not None and v is True:
                lt["seen_true"] = True
            return
        if v is True:
            lt["seen_true"] = True
            if lt["disputed"]:
                lt["disputed"] = False
                self._resolve(("disputed", dev, attr))
        elif v is False:
            if lt["seen_true"]:
                del self._latch[(dev, attr)]            # device itself went wet -> dry
                self._resolve(("disputed", dev, attr))
            elif not lt["disputed"]:
                lt["disputed"] = True
                self._issue(("disputed", dev, attr), "disputed", dev, attr=attr,
                            event_id=lt["event_id"], since=lt["t"])

    def confirm_clear(self, dev: str, attr: str, by: str = "person") -> None:
        """A person checked and confirms the alarm condition is over."""
        if self._latch.pop((dev, attr), None) is not None:
            self.sim.trace("reading_latch_cleared", device=dev, attr=attr, by=by)
            self._resolve(("disputed", dev, attr))

    # ------------------------------------------------------------------ pairs (disagreement)
    def add_pair(self, name: str, a: tuple[str, str], b: tuple[str, str], tol: float,
                 hold_s: float = 600.0) -> None:
        self._pairs[name] = {"a": a, "b": b, "tol": tol, "hold_s": hold_s, "since": None,
                             "state": UNKNOWN, "delta": None}

    def _pairs_check(self) -> None:
        now = self.sim.now
        for name, p in self._pairs.items():
            ra, rb = self._latest(*p["a"]), self._latest(*p["b"])
            if ra is None or rb is None or not all(isinstance(r.value, (int, float)) for r in (ra, rb)):
                p["state"] = UNKNOWN
                continue
            p["delta"] = round(ra.value - rb.value, 3)
            if abs(p["delta"]) > p["tol"]:
                p["since"] = now if p["since"] is None else p["since"]
                if now - p["since"] >= p["hold_s"]:
                    if p["state"] != "disagree":
                        self._issue(("disagree", name), "disagreement", p["a"][0], pair=name,
                                    other=p["b"][0], delta=p["delta"])
                    p["state"] = "disagree"
            else:
                p["since"] = None
                if p["state"] == "disagree":
                    self._resolve(("disagree", name))
                p["state"] = "agree"

    def pair(self, name: str) -> dict:
        return dict(self._pairs[name])

    # ------------------------------------------------------------------ queries
    def _latest(self, dev: str, attr: str) -> QReading | None:
        return (self._state.get(dev) or {}).get(attr)

    def assess(self, dev: str, attr: str) -> Assessment:
        """Can (dev, attr) drive a decision right now? Status OK only if fresh, unique, plausible,
        not stuck, not disputed and not contradicted by a latched alarm event."""
        now = self.sim.now
        lt = self._latch.get((dev, attr))
        r = self._latest(dev, attr)
        if lt is not None:
            if lt["disputed"]:
                return Assessment(dev, attr, DISPUTED, True, ["alarm event not confirmed by device state"], r)
            return Assessment(dev, attr, OK, True, ["latched alarm event"], r)
        if r is None:
            return Assessment(dev, attr, UNKNOWN, None, ["no reading"])
        reasons = []
        status = OK
        if self._dead.get(dev):
            status, reasons = DEAD, ["battery empty"]
        else:
            age_rx = now - self._last_rx.get(dev, r.received)
            age_ts = now - r.corrected_ts
            lim = self.max_age_for(dev)
            if self._frozen.get(dev):
                status, reasons = STALE, ["timestamp not advancing"]
            elif age_rx > lim or age_ts > lim:
                status, reasons = STALE, [f"age {max(age_rx, age_ts):.0f}s > {lim:.0f}s"]
            elif self._stuck.get((dev, attr)):
                status, reasons = STUCK, ["value not changing"]
        return Assessment(dev, attr, status, r.value, reasons, r)

    def _sweep_loop(self) -> None:
        self.sweep()
        self.sim.after(self._sweep_s, self._sweep_loop)

    def sweep(self) -> None:
        """Periodic staleness check: a silent device is noticed even if nobody asks about it."""
        now = self.sim.now
        for dev, last in self._last_rx.items():
            lim = self.max_age_for(dev)
            key = ("silent", dev)
            if now - last > lim:
                self._issue(key, "stale", dev, silent_s=round(now - last, 1))
            elif key in self._open:
                self._resolve(key)
        self._pairs_check()
