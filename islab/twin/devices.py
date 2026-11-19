"""Device models for the smart-home digital twin.

Every device talks to the hub only through the bus, using the topics in
``islab/core/CONTRACT.md``:

* ``state/<id>``  full attribute snapshot, ``ts`` = device clock (``sim.now + clock_skew``)
* ``cmd/<id>``    ``{"action", "args", "cmd_id"}`` from the hub
* ``ack/<id>``    ``{"cmd_id", "status": "accepted"|"rejected", "reason"?}``
* ``event/<id>``  discrete happenings, ``{"type": ..., ...}``

The command cycle is deliberately two-step, like real Matter/Zigbee gear:
the device *acks* as soon as it has parsed the command, and only later --
after a physical actuation delay -- publishes the new state. "accepted" means
"I heard you", never "it is done". The fault flags on :class:`Device`
(``offline``, ``stuck``, ``partial_at``, ``stale``, ``battery_dead``,
``clock_skew``) are set by :mod:`islab.twin.faults`; devices themselves never
decide to misbehave.

All randomness comes from ``sim.rng`` so a run is reproducible from its seed.
"""
from __future__ import annotations

from typing import Any

from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim

Payload = dict[str, Any]


class Device:
    """Base class: state publishing, heartbeat, command/ack/actuate cycle.

    Subclasses override :meth:`initial_state`, :meth:`validate`,
    :meth:`target_state`, :meth:`transition_state`, :meth:`actuation_delay`
    and optionally :meth:`tick` (called before every heartbeat).
    """

    kind = "device"
    cluster = ""                         # Matter-like cluster name, for the registry
    capabilities: tuple[str, ...] = ()   # actions accepted on cmd/<id>
    power = "mains"                      # "mains" | "battery"
    heartbeat_s: float | None = 60.0     # periodic state report; None = never

    def __init__(self, sim: Sim, bus: Bus, device_id: str, clock_skew: float = 0.0,
                 heartbeat_s: float | None = None):
        self.sim, self.bus, self.device_id = sim, bus, device_id
        self.clock_skew = clock_skew
        if heartbeat_s is not None:
            self.heartbeat_s = heartbeat_s
        self.state: Payload = self.initial_state()
        if self.power == "battery":
            self.state["battery"] = 100
        # --- fault flags (set by islab.twin.faults) ---
        self.offline = False              # radio gone: no publish, commands ignored
        self.stuck = False                # acks "accepted", never moves
        self.partial_at: float | None = None   # position actuators stop here
        self.battery_dead = False         # last gasp, then silence
        self._stale: tuple[Payload, float] | None = None  # frozen (payload, ts)
        self._seq = 0
        self._cmd_acks: dict[str, tuple[str, str | None]] = {}   # cmd_id -> (status, reason), bounded
        bus.subscribe(f"cmd/{device_id}", self._on_cmd, name=device_id)
        sim.after(sim.rng.uniform(0.0, 1.0), self.publish_state)   # boot announce
        if self.heartbeat_s:
            sim.after(sim.rng.uniform(1.0, self.heartbeat_s), self._heartbeat)

    # ------------------------------------------------------------------ clock / io
    def clock(self) -> float:
        """The device's own idea of 'now' (may be skewed)."""
        return self.sim.now + self.clock_skew

    @property
    def silent(self) -> bool:
        return self.offline or self.battery_dead

    @property
    def stale(self) -> bool:
        return self._stale is not None

    def set_stale(self, on: bool = True) -> None:
        """Freeze the published state: keep resending the current snapshot with its old ts."""
        self._stale = (dict(self.state), self.clock()) if on else None

    def _publish(self, kind: str, payload: Payload, ts: float | None = None) -> None:
        self._seq += 1
        self.bus.publish(Envelope(topic=f"{kind}/{self.device_id}", payload=payload,
                                  source=self.device_id,
                                  ts=self.clock() if ts is None else ts, seq=self._seq))

    def publish_state(self) -> None:
        """Report the full state snapshot (or the frozen one if stale)."""
        if self.silent:
            return
        if self._stale is not None:
            payload, ts = self._stale
            self._publish("state", dict(payload), ts)
        else:
            self._publish("state", dict(self.state))

    def emit_event(self, type_: str, **data: Any) -> None:
        """Publish a discrete event on event/<id>. Silent and stale devices emit nothing."""
        if self.silent or self.stale:
            return
        self._publish("event", {"type": type_, **data})

    def _ack(self, cmd_id: str, status: str, reason: str | None = None) -> None:
        payload: Payload = {"cmd_id": cmd_id, "status": status}
        if reason:
            payload["reason"] = reason
        self._publish("ack", payload)

    def _heartbeat(self) -> None:
        self.tick()
        self.publish_state()
        if self.heartbeat_s:
            self.sim.after(self.heartbeat_s, self._heartbeat)

    def update(self, **changes: Any) -> None:
        """Change state locally (user/physical action) and report it on change."""
        if any(self.state.get(k) != v for k, v in changes.items()):
            self.state.update(changes)
            self.publish_state()

    # ------------------------------------------------------------------ commands
    def _on_cmd(self, env: Envelope) -> None:
        p = env.payload
        action, args, cmd_id = p.get("action", ""), p.get("args") or {}, p.get("cmd_id", "")
        if self.silent:
            self.sim.trace("dev_ignored", device=self.device_id, cmd_id=cmd_id)
            return
        # Idempotent on cmd_id (Nov 19, for ActionTracker resends and link duplicates): a repeated
        # cmd_id is acked again with the first answer and NOT actuated a second time.
        if cmd_id and cmd_id in self._cmd_acks:
            status, why = self._cmd_acks[cmd_id]
            self.sim.trace("dev_duplicate_cmd", device=self.device_id, cmd_id=cmd_id)
            self._ack(cmd_id, status, why)
            return
        if action not in self.capabilities:
            self._remember(cmd_id, "rejected", "unsupported_action")
            self._ack(cmd_id, "rejected", "unsupported_action")
            return
        reason = self.validate(action, args)
        if reason:
            self._remember(cmd_id, "rejected", reason)
            self._ack(cmd_id, "rejected", reason)
            return
        self._remember(cmd_id, "accepted", None)
        self._ack(cmd_id, "accepted")
        if self.stuck:
            self.sim.trace("dev_stuck", device=self.device_id, cmd_id=cmd_id)
            return
        transition = self.transition_state(action, args)
        if transition:
            self.state.update(transition)
            self.publish_state()
        target = self.target_state(action, args)
        self.sim.after(self.actuation_delay(action), self._finish, target, cmd_id)

    def _remember(self, cmd_id: str, status: str, reason: str | None) -> None:
        if not cmd_id:
            return
        self._cmd_acks[cmd_id] = (status, reason)
        if len(self._cmd_acks) > 256:
            self._cmd_acks.pop(next(iter(self._cmd_acks)))

    def _finish(self, target: Payload, cmd_id: str) -> None:
        if self.partial_at is not None:
            target = self.partial_result(target)
        self.state.update(target)
        self.sim.trace("dev_actuated", device=self.device_id, cmd_id=cmd_id)
        self.publish_state()

    # ------------------------------------------------------------------ overridables
    def initial_state(self) -> Payload:
        return {}

    def validate(self, action: str, args: Payload) -> str | None:
        """Return a rejection reason, or None if the command is acceptable."""
        return None

    def target_state(self, action: str, args: Payload) -> Payload:
        """State attributes after the action has physically completed."""
        return {}

    def expected_state(self, action: str, args: Payload) -> Payload:
        """Attributes a client should see in a later state/<id> to call the action CONFIRMED."""
        return self.target_state(action, args)

    def transition_state(self, action: str, args: Payload) -> Payload | None:
        """Optional intermediate state published right after the ack (e.g. 'closing')."""
        return None

    def actuation_delay(self, action: str) -> float:
        return 0.0

    def partial_result(self, target: Payload) -> Payload:
        """What happens under the 'partial actuation' fault. Default: stop at partial_at."""
        if "position" in target:
            return {**target, "position": self.partial_at, "state": "partial"}
        return {}

    def tick(self) -> None:
        """Physics step run before each heartbeat (drift, random walk...)."""


# =============================================================================
# Actuators
# =============================================================================
class _PositionActuator(Device):
    """Shared logic for valve / shade / garage: open, close, set_position (0..100)."""

    capabilities = ("open", "close", "set_position")
    delay_range = (4.0, 8.0)

    def validate(self, action: str, args: Payload) -> str | None:
        if action == "set_position":
            pos = args.get("position")
            if not isinstance(pos, (int, float)) or not 0 <= pos <= 100:
                return "position_out_of_range"
        return None

    def target_state(self, action: str, args: Payload) -> Payload:
        pos = {"open": 100, "close": 0}.get(action, args.get("position", 0))
        state = "open" if pos == 100 else "closed" if pos == 0 else "partial"
        return {"state": state, "position": pos}

    def transition_state(self, action: str, args: Payload) -> Payload | None:
        target = self.target_state(action, args)["position"]
        cur = self.state["position"]
        if target == cur:
            return None
        return {"state": "opening" if target > cur else "closing"}

    def actuation_delay(self, action: str) -> float:
        return self.sim.rng.uniform(*self.delay_range)


class Valve(_PositionActuator):
    """Main water shut-off valve. Normally open; ~4-8 s to travel."""
    kind, cluster = "valve", "ValveConfigurationAndControl"
    delay_range = (4.0, 8.0)

    def initial_state(self) -> Payload:
        return {"state": "open", "position": 100}


class Shade(_PositionActuator):
    """Window shade, position = percent open."""
    kind, cluster = "shade", "WindowCovering"
    delay_range = (5.0, 10.0)

    def initial_state(self) -> Payload:
        return {"state": "open", "position": 100}


class GarageDoor(_PositionActuator):
    """Garage door opener, ~12 s travel."""
    kind, cluster = "garage_door", "Closure"
    capabilities = ("open", "close")
    delay_range = (11.0, 13.0)

    def initial_state(self) -> Payload:
        return {"state": "closed", "position": 0}


class Lock(Device):
    """Battery door lock, ~1-2 s bolt throw. Can also be operated by hand (manual())."""
    kind, cluster, power = "lock", "DoorLock", "battery"
    capabilities = ("lock", "unlock")

    def initial_state(self) -> Payload:
        return {"state": "locked"}

    def target_state(self, action: str, args: Payload) -> Payload:
        return {"state": "locked" if action == "lock" else "unlocked"}

    def actuation_delay(self, action: str) -> float:
        return self.sim.rng.uniform(1.0, 2.0)

    def partial_result(self, target: Payload) -> Payload:
        return {"state": "jammed"}          # bolt did not fully throw

    def manual(self, locked: bool) -> None:
        """Someone turned the thumb-turn / key."""
        if self.silent:
            self.state["state"] = "locked" if locked else "unlocked"
            return
        self.update(state="locked" if locked else "unlocked")
        self.emit_event("lock_operation", operation="lock" if locked else "unlock", source="manual")


class Light(Device):
    """Dimmable light."""
    kind, cluster = "light", "OnOff"
    capabilities = ("on", "off", "set_brightness")

    def initial_state(self) -> Payload:
        return {"state": "off", "brightness": 0}

    def validate(self, action: str, args: Payload) -> str | None:
        if action == "set_brightness":
            b = args.get("brightness")
            if not isinstance(b, (int, float)) or not 0 <= b <= 100:
                return "brightness_out_of_range"
        return None

    def target_state(self, action: str, args: Payload) -> Payload:
        if action == "on":
            return {"state": "on", "brightness": 100}
        if action == "off":
            return {"state": "off", "brightness": 0}
        b = args["brightness"]
        return {"state": "on" if b > 0 else "off", "brightness": b}

    def actuation_delay(self, action: str) -> float:
        return self.sim.rng.uniform(0.2, 0.5)


class Thermostat(Device):
    """Setpoint + current temperature that drifts toward the setpoint on each heartbeat."""
    kind, cluster = "thermostat", "Thermostat"
    capabilities = ("set_setpoint", "set_mode")
    heartbeat_s = 30.0
    drift_per_tick = 0.1            # degC per heartbeat while heating/cooling

    def initial_state(self) -> Payload:
        return {"mode": "heat", "setpoint": 21.0, "current_temp": 19.5}

    def validate(self, action: str, args: Payload) -> str | None:
        if action == "set_setpoint":
            sp = args.get("setpoint")
            if not isinstance(sp, (int, float)) or not 5 <= sp <= 30:
                return "setpoint_out_of_range"
        if action == "set_mode" and args.get("mode") not in ("heat", "cool", "off"):
            return "bad_mode"
        return None

    def target_state(self, action: str, args: Payload) -> Payload:
        if action == "set_setpoint":
            return {"setpoint": float(args["setpoint"])}
        return {"mode": args["mode"]}

    def actuation_delay(self, action: str) -> float:
        return self.sim.rng.uniform(0.3, 1.0)

    def tick(self) -> None:
        cur, sp, mode = self.state["current_temp"], self.state["setpoint"], self.state["mode"]
        if mode == "off":
            return
        diff = sp - cur
        if (mode == "heat" and diff > 0) or (mode == "cool" and diff < 0):
            step = max(-self.drift_per_tick, min(self.drift_per_tick, diff))
            self.state["current_temp"] = round(cur + step, 2)


# =============================================================================
# Sensors (no commands; report on change + heartbeat)
# =============================================================================
class Sensor(Device):
    power = "battery"
    heartbeat_s = 300.0


class WindowContact(Sensor):
    """Open/closed contact, user-driven via set_open()."""
    kind, cluster = "contact", "BooleanState"

    def initial_state(self) -> Payload:
        return {"state": "closed"}

    def set_open(self, is_open: bool) -> None:
        self.update(state="open" if is_open else "closed")


class LeakSensor(Sensor):
    kind, cluster = "leak", "BooleanStateConfiguration"

    def initial_state(self) -> Payload:
        return {"leak": False}

    def set_leak(self, wet: bool) -> None:
        was = self.state["leak"]
        self.update(leak=wet)
        if wet and not was:
            self.emit_event("leak", severity="high")


class SmokeSensor(Sensor):
    """Relay of a listed (certified) smoke alarm.

    The alarm has its OWN siren which sounds locally the moment smoke is sensed,
    whether or not the hub, the network or the internet is alive. ``state["siren"]``
    reflects the physical siren even while the radio is offline.
    """
    kind, cluster = "smoke", "SmokeCOAlarm"
    listed = True

    def initial_state(self) -> Payload:
        return {"smoke": False, "siren": False, "listed": True}

    def trigger(self) -> None:
        self.state["siren"] = True           # local siren: independent of everything else
        if not self.state["smoke"]:
            self.update(smoke=True)
            self.emit_event("smoke", listed=True, siren_local=True)

    def clear(self) -> None:
        self.state.update(smoke=False, siren=False)
        self.publish_state()


class MotionSensor(Sensor):
    kind, cluster = "motion", "OccupancySensing"
    clear_after_s = 60.0

    def initial_state(self) -> Payload:
        return {"motion": False}

    def trigger(self) -> None:
        self._last = self.sim.now
        if not self.state["motion"]:
            self.update(motion=True)
        self.emit_event("motion")
        self.sim.after(self.clear_after_s, self._maybe_clear)

    def _maybe_clear(self) -> None:
        if self.sim.now - self._last >= self.clear_after_s - 1e-9:
            self.update(motion=False)


class TempSensor(Sensor):
    """Temperature/humidity with a configurable report interval and a small random walk."""
    kind, cluster = "temperature", "TemperatureMeasurement"

    def __init__(self, sim: Sim, bus: Bus, device_id: str, clock_skew: float = 0.0,
                 report_interval_s: float = 60.0):
        super().__init__(sim, bus, device_id, clock_skew, heartbeat_s=report_interval_s)

    def initial_state(self) -> Payload:
        return {"temperature": 21.0, "humidity": 45.0}

    def tick(self) -> None:
        self.state["temperature"] = round(self.state["temperature"] + self.sim.rng.gauss(0, 0.05), 2)

    def set_temperature(self, value: float) -> None:
        self.update(temperature=round(value, 2))


class Camera(Device):
    """Front camera / doorbell. Emits event/<id> {type: person|parcel|vehicle|doorbell, ...}."""
    kind, cluster = "camera", "Camera"
    detection_types = ("person", "parcel", "vehicle")

    def initial_state(self) -> Payload:
        self._clips = 0
        return {"online": True, "recording": False}

    def detect(self, type_: str, confidence: float | None = None) -> str:
        """Fire a detection; returns the clip_ref."""
        if type_ not in self.detection_types:
            raise ValueError(f"unknown detection type {type_!r}")
        self._clips += 1
        clip = f"clip/{self.device_id}/{self._clips:04d}"
        conf = round(self.sim.rng.uniform(0.6, 0.99), 2) if confidence is None else confidence
        self.emit_event(type_, confidence=conf, clip_ref=clip)
        return clip

    def ring(self) -> None:
        self.emit_event("doorbell")
