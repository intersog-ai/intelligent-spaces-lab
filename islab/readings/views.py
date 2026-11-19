"""Three ways for a hub to read the house, compared in experiments/b03_readings.

* :class:`NaiveView`      latest arrival wins. No dedupe, no ordering, no age check.
* :class:`TimestampView`  the B01 ``islab.policy.StateStore``: dedupe by event id, reject state
                          older than stored (device ts), freshness = now - device ts <= max age.
* :class:`QualityView`    the B03 :class:`~islab.readings.quality.ReadingQuality` layer.

All three answer the same two questions so the same decision code can run on each:

``get(dev, attr) -> Use``           value plus ``ok`` (may this drive a decision?) and why not.
``alarm(dev, attr) -> Use``         is the alarm condition (leak, smoke) on? Events count.
``on_event(fn)``                    fn(env) for every event the view lets through.
``rejected``                        ingest-time rejections, each with its event id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from islab.policy.state import FRESH, StateStore
from islab.readings.quality import ACCEPTED, OK, ReadingQuality


@dataclass
class Use:
    device_id: str
    attr: str
    value: Any
    ok: bool
    reason: str = ""
    event_id: str | None = None
    status: str = OK
    extra: dict = field(default_factory=dict)


class NaiveView:
    name = "naive"

    def __init__(self, sim, bus, latch: dict | None = None):
        self.sim = sim
        self.latch = latch if latch is not None else {"leak": "leak", "smoke": "smoke"}
        self._latest: dict[tuple[str, str], tuple[Any, str]] = {}
        self._fns: list[Callable] = []
        self.rejected: list[dict] = []
        bus.subscribe("state/#", self._on_state, name="view.naive")
        bus.subscribe("event/#", self._on_event, name="view.naive")

    def _on_state(self, env):
        dev = env.topic.split("/", 1)[1]
        for k, v in (env.payload or {}).items():
            self._latest[(dev, k)] = (v, env.event_id)

    def _on_event(self, env):
        dev = env.topic.split("/", 1)[1]
        attr = self.latch.get((env.payload or {}).get("type"))
        if attr:
            self._latest[(dev, attr)] = (True, env.event_id)
        for fn in list(self._fns):
            fn(env)

    def on_event(self, fn):
        self._fns.append(fn)

    def get(self, dev, attr) -> Use:
        v = self._latest.get((dev, attr))
        if v is None:
            return Use(dev, attr, None, False, "no reading", status="unknown")
        return Use(dev, attr, v[0], True, event_id=v[1])

    alarm = get


class TimestampView:
    """B01 StateStore semantics: device timestamps for order and age; nothing else."""
    name = "timestamp"

    def __init__(self, sim, bus, max_age: dict[str, float], latch: dict | None = None):
        self.sim, self.max_age = sim, max_age
        self.latch = latch if latch is not None else {"leak": "leak", "smoke": "smoke"}
        self.store = StateStore(sim, bus)
        self._fns: list[Callable] = []
        self.rejected: list[dict] = []
        self._last_event: dict[tuple[str, str], tuple[float, str]] = {}
        self.store.listen(self._on_listen)
        # runs after the StateStore for the same delivery: if it did not apply, it rejected
        bus.subscribe("state/#", self._after_state, name="view.timestamp")

    def _after_state(self, env):
        dev = env.topic.split("/", 1)[1]
        attrs = self.store.get(dev) or {}
        if not any(r.event_id == env.event_id for r in attrs.values()):
            self.rejected.append({"t": self.sim.now, "event_id": env.event_id, "topic": env.topic})

    def _on_listen(self, kind, env):
        if kind != "event":
            return
        dev = env.topic.split("/", 1)[1]
        attr = self.latch.get((env.payload or {}).get("type"))
        if attr:
            self._last_event[(dev, attr)] = (env.ts, env.event_id)
        for fn in list(self._fns):
            fn(env)

    def on_event(self, fn):
        self._fns.append(fn)

    def get(self, dev, attr) -> Use:
        r = self.store.get(dev, attr)
        if r is None:
            return Use(dev, attr, None, False, "no reading", status="unknown")
        fresh = self.store.freshness(dev, self.max_age.get(dev, 150.0), attr) == FRESH
        return Use(dev, attr, r.value, fresh, "" if fresh else f"stale by device ts ({self.sim.now - r.ts:.0f}s)",
                   r.event_id, OK if fresh else "stale")

    def alarm(self, dev, attr) -> Use:
        u = self.get(dev, attr)
        ev = self._last_event.get((dev, attr))
        r = self.store.get(dev, attr)
        if ev is not None and (r is None or ev[0] > r.ts):
            return Use(dev, attr, True, True, "event newer than state", ev[1])
        return u


class QualityView:
    name = "quality"

    def __init__(self, sim, bus, registry, max_age: dict[str, float], **kw):
        self.sim = sim
        self.q = ReadingQuality(sim, bus, registry=registry, max_age=max_age, **kw)
        self._fns: list[Callable] = []
        self.q.on_accept(self._on_accept)

    @property
    def rejected(self):
        return self.q.rejected

    def _on_accept(self, kind, env, _):
        if kind == "event":
            for fn in list(self._fns):
                fn(env)

    def on_event(self, fn):
        self._fns.append(fn)

    def get(self, dev, attr) -> Use:
        a = self.q.assess(dev, attr)
        return Use(dev, attr, a.value, a.ok, "; ".join(a.reasons),
                   a.reading.event_id if a.reading else None, a.status)

    alarm = get


__all__ = ["Use", "NaiveView", "TimestampView", "QualityView", "ACCEPTED"]
