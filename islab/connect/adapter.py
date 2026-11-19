"""Bridge twin devices onto a real MQTT broker with paho-mqtt.

The twin devices are the unchanged classes from :mod:`islab.twin.devices`, on their own
:class:`~islab.core.sim.Sim` and in-process :class:`~islab.core.bus.Bus`. A driver thread advances
the simulator in step with the wall clock (home s = wall s x speed) and bridges the bus to MQTT.
Every device gets its OWN MQTT client, so every device has its own Last Will.

MQTT topics per device (``b = islab/<home>/<device_id>``):

* ``b/state``         twin state + ``ts`` + ``seq`` + ``matter`` view. RETAINED.
* ``b/ack``, ``b/event``
* ``b/availability``  "online" (retained) on connect; Last Will "offline" (retained, QoS 1).
* ``b/cmd``           contract command ``{"action","args","cmd_id"}`` from the hub.
* ``b/<ha subtopic>`` Home Assistant style commands (see :mod:`islab.connect.ha_discovery`).
* ``b/matter``        static Matter endpoint descriptor (retained, modelled).
* ``homeassistant/<component>/<object_id>/config`` discovery (retained).

Packet loss is emulated AT THE ADAPTER, i.e. on the leg between the device and its MQTT client
(think: the radio hop into a bridge). Each message in either direction is dropped with probability
``loss``. MQTT QoS cannot see these losses: they happen before the message reaches MQTT.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from islab.connect import ha_discovery as ha
from islab.connect import matter_model as mm
from islab.connect import SPEED
from islab.core.bus import Bus, Envelope
from islab.core.sim import Sim


class Clock:
    """Shared wall clock for a trial: home seconds since t0."""

    def __init__(self, speed: float = SPEED):
        self.speed = speed
        self.t0 = time.monotonic()

    def home(self) -> float:
        return (time.monotonic() - self.t0) * self.speed

    def wall(self, home_s: float) -> float:
        return home_s / self.speed


def new_client(client_id: str, clean_session: bool = True) -> mqtt.Client:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                    protocol=mqtt.MQTTv311, clean_session=clean_session)
    c.username_pw_set("islab")       # see broker.LocalBroker: non-anonymous sessions get offline queues
    return c


class DeviceSide:
    """Twin devices + one MQTT client each. ``layout``: {device_id: (cls, friendly_name, kind)}."""

    def __init__(self, host: str, port: int, home: str, layout: dict[str, tuple],
                 clock: Clock, seed: int = 0, qos: int = 1, loss: float = 0.0,
                 keepalive: int = 1, discovery: bool = True):
        self.host, self.port, self.home = host, port, home
        self.clock, self.qos, self.loss, self.keepalive = clock, qos, loss, keepalive
        self.discovery = discovery
        self.sim = Sim(seed)
        self.bus = Bus(self.sim)
        self.kinds: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.devices: dict[str, Any] = {}
        for dev_id, (cls, name, kind) in layout.items():
            self.devices[dev_id] = cls(self.sim, self.bus, dev_id)
            self.kinds[dev_id], self.names[dev_id] = kind, name
        self.clients: dict[str, mqtt.Client] = {}
        self.dropped: dict[str, float] = {}           # device_id -> wall time of drop
        self.inbox: queue.Queue = queue.Queue()
        self.stats = {"lost_in": 0, "lost_out": 0, "in": 0, "out": 0, "unsolicited": 0}
        self._subs: dict[str, set[str]] = {}
        self._stat_lock = threading.Lock()
        self._ha_n = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for pat in ("state/#", "ack/#", "event/#"):
            self.bus.subscribe(pat, self._forward, name="mqtt-adapter")

    # ------------------------------------------------------------------ lifecycle
    def start(self, timeout: float = 10.0) -> None:
        # One client at a time, each waiting for its SUBACK: amqtt 0.12.2 crashed client sessions
        # ("dictionary changed size during iteration", broker.py _handle_client_session) when
        # several clients connected and subscribed at once. See experiments/b02_connect/notes.md.
        connected = {d: threading.Event() for d in self.devices}
        for dev_id in self.devices:
            c = new_client(f"{self.home}-{dev_id}")
            b = ha.base(self.home, dev_id)
            c.will_set(f"{b}/availability", "offline", qos=1, retain=True)
            c.user_data_set(dev_id)
            c.on_connect = self._on_connect_factory(connected[dev_id])
            c.on_message = self._on_message
            c.connect(self.host, self.port, keepalive=self.keepalive)
            c.loop_start()
            self.clients[dev_id] = c
            if not connected[dev_id].wait(timeout):
                raise RuntimeError("device client did not connect")
        self._thread = threading.Thread(target=self._drive, name=f"twin-{self.home}", daemon=True)
        self._thread.start()

    def _on_connect_factory(self, ev: threading.Event):
        def on_connect(client, dev_id, flags, rc, props=None):
            b = ha.base(self.home, dev_id)
            subs = [(f"{b}/cmd", self.qos)] + [(f"{b}/{s}", self.qos) for s in ha.HA_SUBTOPICS]
            self._subs[dev_id] = {t for t, _ in subs}
            client.on_subscribe = lambda *a, **k: ev.set()
            client.subscribe(subs)
            client.publish(f"{b}/availability", "online", qos=1, retain=True)
            kind = self.kinds[dev_id]
            if self.discovery:
                topic = ha.config_topic(self.home, dev_id, kind)
                cfg = ha.config(self.home, dev_id, kind, self.names[dev_id], qos=self.qos)
                if topic and cfg:
                    client.publish(topic, ha.dumps(cfg), qos=1, retain=True)
                desc = mm.descriptor(kind)
                if desc:
                    client.publish(f"{b}/matter", json.dumps(desc), qos=1, retain=True)
        return on_connect

    def _drive(self) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    dev_id, sub, payload = self.inbox.get_nowait()
                except queue.Empty:
                    break
                self._deliver_cmd(dev_id, sub, payload)
            self.sim.run(until=self.clock.home())
            time.sleep(0.002)

    def retained_topics(self) -> list[str]:
        out = []
        for dev_id, kind in self.kinds.items():
            b = ha.base(self.home, dev_id)
            out += [f"{b}/state", f"{b}/availability", f"{b}/matter"]
            t = ha.config_topic(self.home, dev_id, kind)
            if t:
                out.append(t)
        return out

    def stop(self, clear_retained: bool = True) -> None:
        """Stop the twin and the clients. ``clear_retained`` publishes empty retained payloads
        (MQTT's way to delete a retained message; for Home Assistant it also removes the entity)."""
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if clear_retained:
            c = new_client(f"{self.home}-cleanup")
            c.connect(self.host, self.port, keepalive=5)
            c.loop_start()
            infos = [c.publish(t, b"", qos=1, retain=True) for t in self.retained_topics()]
            for i in infos:
                i.wait_for_publish(2)
            c.disconnect()
            c.loop_stop()
        for dev_id, c in self.clients.items():
            if dev_id in self.dropped:
                sock = c.socket()
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                continue
            c.disconnect()        # clean DISCONNECT: the broker discards the Last Will
            c.loop_stop()

    # ------------------------------------------------------------------ faults
    def drop(self, dev_id: str, mode: str = "power") -> float:
        """Take a device off the network. 'power': radio and MQTT client go silent, the TCP socket
        is left open (no FIN), so only the keepalive timeout can notice. 'crash': the socket is
        closed (FIN), as when a process dies on a live OS."""
        self.devices[dev_id].offline = True          # twin: no more publishes, commands ignored
        self.dropped[dev_id] = time.monotonic()
        c = self.clients[dev_id]
        c.loop_stop()                                 # can block up to ~1 s while paho's select returns
        t = time.monotonic()                          # the client is silent from here
        self.dropped[dev_id] = t
        if mode == "crash":
            sock = c.socket()
            if sock is not None:
                sock.close()
        return t

    # ------------------------------------------------------------------ bridging
    def _lossy(self) -> bool:
        return bool(self.loss) and self.sim.rng.random() < self.loss

    def _forward(self, env: Envelope) -> None:
        kind_topic, dev_id = env.topic.split("/", 1)
        if dev_id in self.dropped or dev_id not in self.clients:
            return
        self.stats["out"] += 1
        if self._lossy():
            self.stats["lost_out"] += 1
            return
        payload = dict(env.payload, ts=round(env.ts, 3), seq=env.seq)
        if kind_topic == "state":
            payload["matter"] = mm.to_matter(self.kinds[dev_id], env.payload)
        self.clients[dev_id].publish(f"{ha.base(self.home, dev_id)}/{kind_topic}",
                                     json.dumps(payload), qos=self.qos,
                                     retain=(kind_topic == "state"))

    def _on_message(self, client, dev_id, msg) -> None:
        if msg.topic not in self._subs.get(dev_id, ()):
            # amqtt 0.12.2 sends a newly connected client the retained messages that match ANY
            # client's subscriptions (broker.py, loop over self._subscriptions after handler.start()).
            # Never act on a topic this device did not subscribe to; count it.
            with self._stat_lock:
                self.stats["unsolicited"] += 1
            return
        sub = msg.topic.split("/", 3)[3]
        self.inbox.put((dev_id, sub, msg.payload))

    def _deliver_cmd(self, dev_id: str, sub: str, raw: bytes) -> None:
        self.stats["in"] += 1
        if self._lossy():
            self.stats["lost_in"] += 1
            return
        if sub == "cmd":
            try:
                p = json.loads(raw)
            except ValueError:
                return
            payload = {"action": p.get("action", ""), "args": p.get("args") or {},
                       "cmd_id": p.get("cmd_id", "")}
        else:
            tr = ha.ha_command(sub, raw)
            if tr is None:
                return
            self._ha_n += 1
            payload = {"action": tr[0], "args": tr[1], "cmd_id": f"ha-{self._ha_n:05d}"}
        self.bus.publish(Envelope(topic=f"cmd/{dev_id}", payload=payload, source="mqtt",
                                  ts=self.sim.now))
