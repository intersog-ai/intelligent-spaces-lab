"""In-process MQTT-like bus on the simulator. Topics use '/' and '+' '#' wildcards.
Every message is an Envelope. Link faults (drop, delay, duplicate, reorder) are hooks
set per topic prefix by the fault injector."""
from dataclasses import dataclass, field
import itertools

_ids = itertools.count(1)

def reset_ids():
    """Restart event ids so two runs in one process produce identical traces."""
    global _ids
    _ids = itertools.count(1)

@dataclass
class Envelope:
    topic: str
    payload: dict
    source: str                 # device_id or component name
    ts: float                   # source clock timestamp (may be skewed)
    event_id: str = field(default_factory=lambda: f"ev{next(_ids):07d}")  # see reset_ids()
    seq: int = 0

def match(pattern, topic):
    p, t = pattern.split('/'), topic.split('/')
    for i, seg in enumerate(p):
        if seg == '#':
            return True
        if i >= len(t) or (seg != '+' and seg != t[i]):
            return False
    return len(p) == len(t)

class Bus:
    def __init__(self, sim, base_latency=0.01):
        self.sim = sim
        self.base_latency = base_latency
        self.subs = []          # (pattern, callback, name)
        self.link_hooks = []    # callables(env) -> list of (delay, env) ; [] = drop
        self.delivered = 0
        self.errors = []        # (t, subscriber, topic, error) - checked by the test suites

    def subscribe(self, pattern, cb, name='?'):
        self.subs.append((pattern, cb, name))

    def publish(self, env: Envelope):
        self.sim.trace('pub', topic=env.topic, source=env.source, event_id=env.event_id)
        deliveries = [(self.base_latency, env)]
        for hook in self.link_hooks:
            nxt = []
            for d, e in deliveries:
                for d2, e2 in hook(e):
                    nxt.append((d + d2, e2))
            deliveries = nxt
        for d, e in deliveries:
            self.sim.after(d, self._deliver, e)

    def _deliver(self, env):
        for pattern, cb, name in list(self.subs):
            if match(pattern, env.topic):
                self.delivered += 1
                try:
                    cb(env)
                except Exception as exc:   # one broken subscriber must not stop the home
                    self.errors.append((self.sim.now, name, env.topic, repr(exc)))
                    self.sim.trace('subscriber_error', name=name, topic=env.topic, error=repr(exc))
