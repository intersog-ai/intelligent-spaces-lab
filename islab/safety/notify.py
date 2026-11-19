"""Telling people, and the one extra thing allowed to leave the home: a plain-words push text.

* People at home get ``notify/<person>`` over the local network (the B01 contract): no internet.
* People away get the same text as a push through the egress gate, purpose ``notify_push``,
  payload class ``notify_text``, to one allowlisted relay. With the internet down the gate queues
  it. Nothing else about the home (device ids, readings, journal) goes in the push.
* ``Phone`` records when each person actually received each text.

``SafetyEgressGate`` extends the B01 gate with that one purpose; every other rule is unchanged.
"""
from __future__ import annotations

from islab.alarm import egress as _eg
from islab.core.bus import Envelope

PUSH_RELAY = "push.relay.example"
PUSH_PURPOSE, PUSH_CLASS = "notify_push", "notify_text"


class SafetyEgressGate(_eg.EgressGate):
    def __init__(self, sim, bus, **kw):
        allow = {k: set(v) for k, v in _eg.DEFAULT_ALLOWLIST.items()}
        allow[PUSH_RELAY] = {PUSH_PURPOSE}
        super().__init__(sim, bus, allowlist=allow,
                         offline_policy={PUSH_PURPOSE: "queue"}, **kw)

    def _check(self, dest, purpose, cls, meta):
        if purpose == PUSH_PURPOSE:
            if dest != PUSH_RELAY or PUSH_PURPOSE not in self.allowlist.get(dest, ()):
                return "destination_not_allowlisted"
            if cls != PUSH_CLASS:
                return f"payload_class_not_allowed_for_purpose:{cls}"
            if set(meta) - {"person"}:
                return "push_carries_extra_fields"
            return None
        return super()._check(dest, purpose, cls, meta)


class Phone:
    """A person's phone. ``home=True``: receives notify/<person> on the LAN. Away: via the push relay."""

    def __init__(self, sim, bus, person, home):
        self.sim, self.person, self.home = sim, person, home
        self.received: list[dict] = []        # {"t", "text", "tag"}
        if home:
            bus.subscribe(f"notify/{person}", self._on_lan, name=f"phone.{person}")

    def _on_lan(self, env):
        p = env.payload or {}
        self.received.append({"t": self.sim.now, "text": p.get("text", ""), "tag": p.get("tag"),
                              "via": "lan"})

    def on_push(self, text, tag):
        self.received.append({"t": self.sim.now, "text": text, "tag": tag, "via": "push"})


class PushRelay:
    """Cloud push service: delivers to away phones 0.5-2 s after the gate lets a text out."""

    def __init__(self, sim, phones):
        self.sim, self.phones = sim, {p.person: p for p in phones}

    def transport(self, person, text, tag):
        def deliver(_data):
            delay = self.sim.rng.uniform(0.5, 2.0)
            self.sim.after(delay, self.phones[person].on_push, text, tag)
        return deliver


class Notifier:
    """Hub side. ``tell`` publishes notify/<person> for everyone, and pushes to people away."""

    NAME = "safety.notify"

    def __init__(self, sim, bus, gate, relay, people_home: dict):
        self.sim, self.bus, self.gate, self.relay = sim, bus, gate, relay
        self.people_home = dict(people_home)      # person -> at home?
        self.alive = True

    def tell(self, text: str, tag: str) -> bool:
        if not self.alive:
            return False
        for person, home in self.people_home.items():
            self.bus.publish(Envelope(f"notify/{person}", {"text": text, "kind": "tell", "tag": tag},
                                      source=self.NAME, ts=self.sim.now))
            if not home:
                self.gate.send(source=self.NAME, dest=PUSH_RELAY, purpose=PUSH_PURPOSE,
                               payload_class=PUSH_CLASS, data=text.encode(),
                               transport=self.relay.transport(person, text, tag),
                               meta={"person": person})
        return True
