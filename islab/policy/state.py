"""State store: the hub's view of the house, built only from sourced readings (principle 1).

Subscribes to ``state/#`` and ``event/#``. For every device it keeps the latest value of each
attribute together with where it came from (device_id + event_id), the device's own timestamp
and the hub's receive time. Duplicates (same event_id) are dropped; out-of-order messages
(older source ts than what is stored) are rejected and counted, never applied.
"""
from dataclasses import dataclass, asdict

FRESH, STALE, UNKNOWN = "fresh", "stale", "unknown"


@dataclass(frozen=True)
class Reading:
    device_id: str
    attr: str
    value: object
    ts: float            # source (device) clock
    received: float      # hub sim time when applied
    event_id: str

    def source(self):
        return {"device_id": self.device_id, "event_id": self.event_id, "ts": self.ts}

    def to_dict(self):
        return asdict(self)


class StateStore:
    def __init__(self, sim, bus, max_seen=100_000):
        self.sim, self.bus = sim, bus
        self._state = {}          # device_id -> {attr: Reading}
        self._last_ts = {}        # device_id -> latest applied source ts (state topic)
        self._events = {}         # device_id -> list[Reading-like dict]
        self._seen = {}           # event_id -> None (insertion ordered, bounded)
        self._max_seen = max_seen
        self.stats = {"applied": 0, "duplicates": 0, "out_of_order": 0, "events": 0}
        self._listeners = []      # callables(kind, env)
        bus.subscribe("state/#", self._on_state, name="policy.state")
        bus.subscribe("event/#", self._on_event, name="policy.state")

    # ---- ingestion -------------------------------------------------------------------
    def _dedupe(self, env):
        if env.event_id in self._seen:
            self.stats["duplicates"] += 1
            return False
        self._seen[env.event_id] = None
        if len(self._seen) > self._max_seen:
            self._seen.pop(next(iter(self._seen)))
        return True

    @staticmethod
    def _device(env):
        return env.topic.split("/", 1)[1]

    def _on_state(self, env):
        if not self._dedupe(env):
            return
        dev = self._device(env)
        last = self._last_ts.get(dev)
        if last is not None and env.ts < last:
            self.stats["out_of_order"] += 1
            self.sim.trace("state_reject_ooo", device=dev, ts=env.ts, stored_ts=last)
            return
        self._last_ts[dev] = env.ts
        attrs = self._state.setdefault(dev, {})
        for k, v in (env.payload or {}).items():
            attrs[k] = Reading(dev, k, v, env.ts, self.sim.now, env.event_id)
        self.stats["applied"] += 1
        for fn in list(self._listeners):
            fn("state", env)

    def _on_event(self, env):
        if not self._dedupe(env):
            return
        dev = self._device(env)
        self._events.setdefault(dev, []).append(
            {"device_id": dev, "event_id": env.event_id, "ts": env.ts,
             "received": self.sim.now, "payload": dict(env.payload or {})})
        self.stats["events"] += 1
        for fn in list(self._listeners):
            fn("event", env)

    def listen(self, fn):
        self._listeners.append(fn)

    # ---- queries ---------------------------------------------------------------------
    def get(self, device_id, attr=None):
        """Latest Reading for an attribute, or a dict attr->Reading for the device. None if unknown."""
        attrs = self._state.get(device_id)
        if attrs is None:
            return None
        return dict(attrs) if attr is None else attrs.get(attr)

    def last_ts(self, device_id):
        return self._last_ts.get(device_id)

    def events(self, device_id):
        return list(self._events.get(device_id, []))

    def age(self, device_id, attr=None):
        r = self._reading_for_age(device_id, attr)
        return None if r is None else self.sim.now - r.ts

    def _reading_for_age(self, device_id, attr):
        if attr is not None:
            return self.get(device_id, attr)
        attrs = self._state.get(device_id)
        if not attrs:
            return None
        return max(attrs.values(), key=lambda r: r.ts)

    def freshness(self, device_id, max_age, attr=None):
        """'fresh' | 'stale' | 'unknown'. Age is measured on the source timestamp.
        A reading from the future (source clock ahead by more than max_age) is treated as stale:
        it cannot be trusted either."""
        r = self._reading_for_age(device_id, attr)
        if r is None:
            return UNKNOWN
        age = self.sim.now - r.ts
        return FRESH if -max_age <= age <= max_age else STALE

    def snapshot(self, device_ids=None):
        """Sourced view for an agent's context: device -> attr -> {value, ts, age_s, event_id}."""
        out = {}
        for dev, attrs in self._state.items():
            if device_ids is not None and dev not in device_ids:
                continue
            out[dev] = {k: {"value": r.value, "ts": r.ts, "age_s": round(self.sim.now - r.ts, 3),
                            "source": r.device_id, "event_id": r.event_id}
                        for k, r in attrs.items()}
        return out
