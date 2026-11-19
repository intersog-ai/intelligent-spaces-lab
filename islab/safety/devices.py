"""Simulated devices added for B04: a water flow meter on the main pipe, and ground truth.

``FlowSensor`` ("flow.main") is mains powered and sits after the main valve. Its physics reads the
valve's PHYSICAL position and whether water is escaping (the leak), never the bus:
    flow = leak_lpm * position / 100 while the leak is active, else 0 (plus small noise).
It reports ``state/flow.main {"flow_lpm": x}`` on a change of more than ``report_delta`` and on a
heartbeat. It is an independent second witness: a valve that reports "closed" while water still
flows is caught by the meter.

``GroundTruth`` records the physical valve position over time (from the device object, not from
messages) so experiments can check claims against what really happened.
"""
from __future__ import annotations

from islab.twin.devices import Device

FLOW_ID = "flow.main"


class FlowSensor(Device):
    kind, cluster, power = "flow", "FlowMeasurement", "mains"
    heartbeat_s = 10.0          # mains powered: reports every 10 s and on change

    def __init__(self, sim, bus, valve, leak, device_id=FLOW_ID, leak_lpm=6.0, tick_s=1.0,
                 report_delta=0.3, noise=0.03):
        self.valve, self.leak = valve, leak
        self.leak_lpm, self.tick_s, self.report_delta, self.noise = leak_lpm, tick_s, report_delta, noise
        super().__init__(sim, bus, device_id)
        self._reported = self.state["flow_lpm"]
        sim.after(tick_s, self._physics)

    def initial_state(self):
        return {"flow_lpm": 0.0}

    def true_flow(self) -> float:
        if not self.leak.state.get("leak"):
            return 0.0
        return self.leak_lpm * float(self.valve.state.get("position", 100)) / 100.0

    def _physics(self):
        v = self.true_flow()
        if v > 0:
            v = max(0.0, v + self.sim.rng.gauss(0, self.noise))
        self.state["flow_lpm"] = round(v, 2)
        if abs(self.state["flow_lpm"] - self._reported) > self.report_delta:
            self._reported = self.state["flow_lpm"]
            self.publish_state()
        self.sim.after(self.tick_s, self._physics)

    def publish_state(self):
        super().publish_state()
        self._reported = self.state["flow_lpm"]


class GroundTruth:
    """Physical valve position timeline: [(t, position)], from the device object itself."""

    def __init__(self, sim, valve):
        self.sim, self.valve = sim, valve
        self.timeline = [(0.0, valve.state["position"])]
        orig = valve._finish

        def finish(target, cmd_id):
            orig(target, cmd_id)
            if valve.state["position"] != self.timeline[-1][1]:
                self.timeline.append((sim.now, valve.state["position"]))
        valve._finish = finish

    def position_at(self, t: float) -> float:
        pos = self.timeline[0][1]
        for ti, p in self.timeline:
            if ti <= t:
                pos = p
            else:
                break
        return pos

    def first_closed(self, after: float = 0.0):
        for ti, p in self.timeline:
            if ti >= after and p == 0:
                return ti
        return None
