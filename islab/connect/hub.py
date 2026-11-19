"""The hub over real MQTT: subscribe, command, confirm by state, track availability, report.

Confirmation follows islab/core/CONTRACT.md: an action is CONFIRMED only when a state message
RECEIVED by the hub after the command was sent shows the target value. An ack only stops resends.
Offline comes from the device's Last Will (``availability`` = "offline"), never from silence alone;
the heartbeat rule (no message for 2 heartbeats) is computed alongside for comparison.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from islab.connect import ha_discovery as ha
from islab.connect.adapter import Clock, new_client


@dataclass
class Step:
    device_id: str
    action: str
    args: dict
    expect: dict
    label: str                      # plain words, e.g. "Kitchen light off"
    cmd_id: str = ""
    sent: list = field(default_factory=list)       # home s of each send
    acked_at: float | None = None
    outcome: str = "pending"        # confirmed | rejected | offline | not_confirmed | no_answer
    at: float | None = None         # home s when the outcome was decided


class Hub:
    def __init__(self, host: str, port: int, home: str, clock: Clock, qos: int = 1,
                 client_id: str | None = None, resend_after: float = 2.0, max_resends: int = 3):
        self.home, self.clock, self.qos = home, clock, qos
        self.resend_after, self.max_resends = resend_after, max_resends
        self.lock = threading.Lock()
        self.states: dict[str, list[tuple[float, dict]]] = {}     # dev -> [(recv home s, payload)]
        self.last_rx: dict[str, float] = {}                        # dev -> recv home s of any msg
        self.avail: dict[str, tuple[str, float, bool]] = {}        # dev -> (value, recv, retained)
        self.acks: dict[str, list[tuple[float, dict]]] = {}        # cmd_id -> [(recv, payload)]
        self.discovery: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._connected = threading.Event()
        self.client = new_client(client_id or f"{home}-hub")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()
        if not self._connected.wait(10):
            raise RuntimeError("hub did not connect")

    def _on_connect(self, client, userdata, flags, rc, props=None):
        b = f"islab/{self.home}/+"
        client.subscribe([(f"{b}/state", self.qos), (f"{b}/ack", self.qos),
                          (f"{b}/event", self.qos), (f"{b}/availability", 1),
                          (f"{ha.PREFIX}/+/+/config", 1)])
        self._connected.set()

    def _on_message(self, client, userdata, msg):
        now = self.clock.home()
        parts = msg.topic.split("/")
        if parts[0] == ha.PREFIX:
            if not parts[2].startswith(ha.object_id(self.home, "")):
                return                       # another home's config (see adapter note on amqtt)
            with self.lock:
                if msg.payload:
                    self.discovery[msg.topic] = json.loads(msg.payload)
                else:                        # empty retained payload = entity removed
                    self.discovery.pop(msg.topic, None)
            return
        if not msg.payload:
            return                           # a retained message being cleared, not a reading
        if len(parts) != 4 or parts[0] != "islab" or parts[1] != self.home:
            return
        dev, kind = parts[2], parts[3]
        with self.lock:
            if kind == "availability":
                self.avail[dev] = (msg.payload.decode(), now, bool(msg.retain))
                return
            self.last_rx[dev] = now          # device traffic only: the heartbeat rule uses this
            try:
                p = json.loads(msg.payload)
            except ValueError:
                return                       # never let one bad payload kill the client thread
            if kind == "state":
                self.states.setdefault(dev, []).append((now, p))
            elif kind == "ack":
                self.acks.setdefault(p.get("cmd_id", ""), []).append((now, p))

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    # ------------------------------------------------------------------ commands
    def send(self, step: Step) -> None:
        if not step.cmd_id:
            step.cmd_id = f"{self.home}-c{next(self._ids):04d}"
        step.sent.append(self.clock.home())
        self.client.publish(f"{ha.base(self.home, step.device_id)}/cmd",
                            json.dumps({"action": step.action, "args": step.args,
                                        "cmd_id": step.cmd_id}), qos=self.qos)

    def _check(self, step: Step, now: float) -> None:
        first = step.sent[0]
        for t, p in self.states.get(step.device_id, []):
            if t >= first and all(p.get(k) == v for k, v in step.expect.items()):
                step.outcome, step.at = "confirmed", t
                return
        acks = self.acks.get(step.cmd_id, [])
        if acks and step.acked_at is None:
            step.acked_at = acks[0][0]
        for t, p in acks:
            if p.get("status") == "rejected":
                step.outcome, step.at = "rejected", t
                return
        av = self.avail.get(step.device_id)
        if av and av[0] == "offline":
            step.outcome, step.at = "offline", max(av[1], first)
            return
        if not acks and now - step.sent[-1] >= self.resend_after and len(step.sent) <= self.max_resends:
            self.send(step)          # same cmd_id: devices are idempotent on cmd_id

    def run(self, steps: list[Step], timeout: float = 30.0, poll_wall: float = 0.005) -> float:
        """Send all steps at once; return home s when every step had an outcome (or timeout)."""
        t0 = self.clock.home()
        with self.lock:
            for s in steps:
                self.send(s)
        while True:
            now = self.clock.home()
            with self.lock:
                for s in steps:
                    if s.outcome == "pending":
                        self._check(s, now)
            if all(s.outcome != "pending" for s in steps):
                return max(s.at for s in steps) - t0
            if now - t0 >= timeout:
                for s in steps:
                    if s.outcome == "pending":
                        s.outcome = "not_confirmed" if s.acked_at is not None else "no_answer"
                        s.at = t0 + timeout
                return timeout
            time.sleep(poll_wall)

    def latest(self, dev: str) -> dict | None:
        with self.lock:
            st = self.states.get(dev)
            return dict(st[-1][1]) if st else None


# ---------------------------------------------------------------------- E03 routine
AWAY_SETPOINT = 16.0


def leaving_plan(names: dict[str, str]) -> list[Step]:
    """'We're leaving': lights off, climate to the away setpoint, shades closed."""
    steps = []
    for dev in ("light.kitchen", "light.living"):
        steps.append(Step(dev, "off", {}, {"state": "off"}, f"{names[dev]} off"))
    for dev in ("thermo.kitchen", "thermo.study"):
        steps.append(Step(dev, "set_setpoint", {"setpoint": AWAY_SETPOINT},
                          {"setpoint": AWAY_SETPOINT}, f"{names[dev]} to {AWAY_SETPOINT:g} °C"))
    for dev in ("shade.living", "shade.bedroom"):
        steps.append(Step(dev, "close", {}, {"position": 0}, f"{names[dev]} closed"))
    return steps


def _secs(x: float) -> str:
    return f"{round(x):d} second{'s' if round(x) != 1 else ''}"


def plain_report(steps: list[Step], t_start: float, window: dict | None, window_name: str,
                 now: float, names: dict[str, str]) -> str:
    """What the assistant says. Only facts the hub received; no 'done' on an ack."""
    ok = [s for s in steps if s.outcome == "confirmed"]
    bad = [s for s in steps if s.outcome != "confirmed"]
    took = max((s.at for s in steps), default=t_start) - t_start
    head = (f"Leaving mode is set. All {len(ok)} steps confirmed in {_secs(took)}."
            if not bad else
            f"Leaving mode is mostly set: {len(ok)} of {len(steps)} steps confirmed in {_secs(took)}. "
            f"{len(bad)} did not.")
    lines = [head]
    if ok:
        lines.append("Confirmed by the devices: " + "; ".join(s.label for s in ok) + ".")
    for s in bad:
        n = names[s.device_id]
        if s.outcome == "offline":
            lines.append(f"{n} did not respond: it went offline and could not act, so it may "
                         f"still be open. Check it when you can.")
        elif s.outcome == "rejected":
            lines.append(f"{n} refused the command.")
        elif s.outcome == "not_confirmed":
            lines.append(f"{n} received the command, but I have no reading that it finished. "
                         f"I'll keep checking and tell you.")
        else:
            lines.append(f"{n} did not answer. I don't know its state.")
    if window is None:
        lines.append(f"I have no reading from the {window_name.lower()}.")
    else:
        age = max(0.0, now - float(window.get("ts", now)))
        if window.get("state") == "open":
            lines.append(f"The {window_name.lower()} is open (reading {_secs(age)} old). "
                         f"I can't close it; someone needs to.")
        else:
            lines.append(f"The {window_name.lower()} is closed (reading {_secs(age)} old).")
    return " ".join(lines)
