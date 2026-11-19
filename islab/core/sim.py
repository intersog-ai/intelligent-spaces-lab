"""Deterministic discrete-event simulator. All time is virtual seconds."""
import heapq, itertools, random

class Sim:
    def __init__(self, seed=0):
        self.now = 0.0
        self._q = []
        self._n = itertools.count()
        self.rng = random.Random(seed)
        self.log = []          # (t, kind, data) trace of everything that happened
        from islab.core.bus import reset_ids
        reset_ids()            # deterministic event ids per run

    def at(self, t, fn, *a, **k):
        heapq.heappush(self._q, (max(t, self.now), next(self._n), fn, a, k))

    def after(self, dt, fn, *a, **k):
        self.at(self.now + dt, fn, *a, **k)

    def run(self, until=None):
        while self._q and (until is None or self._q[0][0] <= until):
            t, _, fn, a, k = heapq.heappop(self._q)
            self.now = t
            fn(*a, **k)
        if until is not None:
            self.now = max(self.now, until)

    def trace(self, kind, **data):
        self.log.append((round(self.now, 4), kind, data))
