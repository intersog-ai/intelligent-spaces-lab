"""A modelled cellular (LTE) alarm path. No real network, no real modem.

Every number below is an ASSUMPTION chosen for the twin, not a measurement of any operator,
modem or plan. They are kept in one dict so results.json can print them next to the results.

Model, per message, one direction:
  - the radio is idle when nothing was sent or received for `rrc_idle_after_s`; the next message
    pays a connection setup delay (`rrc_setup_s`) before it goes out;
  - one-way latency is uniform in `latency_s`;
  - with probability `tail_p` a message is late by uniform `tail_s` (retransmission, scheduling);
  - with probability `loss` it is lost;
  - in an outage (`outages`, list of (start, end)) nothing goes through;
  - every message sent with service is billed: its bytes plus `overhead_bytes` (IP/TCP headers and
    TCP acks on a kept-alive connection), in both directions, whether or not it arrives.
A "degraded" cell multiplies latency and setup, and raises loss and tail probability.
"""
from __future__ import annotations

CELL_ASSUMPTIONS = {
    "latency_s": (0.05, 0.20),          # one-way, radio connected (LTE Cat-1 / Cat-M class, assumed)
    "rrc_idle_after_s": 10.0,           # inactivity timer before the radio drops to idle (assumed)
    "rrc_setup_s": (0.10, 0.80),        # idle -> connected (assumed)
    "tail_p": 0.02, "tail_s": (1.0, 6.0),
    "loss": 0.01,
    "overhead_bytes": 120,              # per message, IP/TCP headers + TCP acks (assumed)
    "usd_per_mb": 1.00,                 # low-volume IoT data tariff, order of magnitude (assumed)
}

DEGRADED = {"latency_mult": 4.0, "loss": 0.30, "tail_p": 0.10}


class CellularLink:
    """Same interface as islab.alarm.path.WanLink: send(data, deliver), up(), stats."""

    def __init__(self, sim, outages=(), degraded=False, name="cellular", **overrides):
        self.sim, self.name = sim, name
        self.p = dict(CELL_ASSUMPTIONS, **overrides)
        self.outages = list(outages)
        self.degraded = degraded
        if degraded:
            self.p["loss"] = DEGRADED["loss"]
            self.p["tail_p"] = DEGRADED["tail_p"]
        self.stats = {"sent": 0, "lost": 0, "outage_dropped": 0, "delivered": 0,
                      "bytes": 0, "payload_bytes": 0}
        self.billed = []          # (t, bytes) for per-day accounting
        self._last_activity = -1e9

    def up(self, t=None):
        t = self.sim.now if t is None else t
        return not any(a <= t < b for a, b in self.outages)

    def cost_usd(self, nbytes=None):
        nbytes = self.stats["bytes"] if nbytes is None else nbytes
        return nbytes / 1e6 * self.p["usd_per_mb"]

    def bytes_between(self, lo, hi):
        rows = [b for t, b in self.billed if lo <= t < hi]
        return len(rows), sum(rows)

    def _delay(self):
        rng, p = self.sim.rng, self.p
        mult = DEGRADED["latency_mult"] if self.degraded else 1.0
        lo, hi = p["latency_s"]
        d = lo + (hi - lo) * rng.random()
        if self.sim.now - self._last_activity > p["rrc_idle_after_s"]:
            a, b = p["rrc_setup_s"]
            d += a + (b - a) * rng.random()
        if rng.random() < p["tail_p"]:
            a, b = p["tail_s"]
            d += a + (b - a) * rng.random()
        return d * mult

    def send(self, data: bytes, deliver):
        self.stats["sent"] += 1
        if not self.up():                     # no service: the modem cannot send, nothing billed
            self.stats["outage_dropped"] += 1
            self.sim.trace("cell_drop", reason="outage")
            return
        nb = len(data) + self.p["overhead_bytes"]
        self.stats["bytes"] += nb
        self.stats["payload_bytes"] += len(data)
        self.billed.append((self.sim.now, nb))
        delay = self._delay()
        self._last_activity = self.sim.now + delay
        if self.sim.rng.random() < self.p["loss"]:
            self.stats["lost"] += 1
            self.sim.trace("cell_drop", reason="loss")
            return

        def arrive():
            if not self.up():
                self.stats["outage_dropped"] += 1
                return
            self.stats["delivered"] += 1
            deliver(data)
        self.sim.after(delay, arrive)
