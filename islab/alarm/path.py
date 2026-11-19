"""Alarm path in the twin: hub -> (egress gate) -> WAN -> monitoring centre -> ACK back.

Principles exercised:
- Life safety is independent: LocalSiren listens to the detectors itself (or is hard-wired)
  and never waits for the hub, the WAN or any AI.
- Dispatch is the monitoring centre's job. The hub reports and retries; it has no way to
  call emergency services.
"""
from __future__ import annotations

from islab.core.bus import Envelope
from . import dc09

# device_id -> (kind, zone)
DEFAULT_ZONES = {"smoke.hall": ("smoke", 1), "leak.sink": ("leak", 2),
                 "lock.front": ("perimeter", 3), "lock.back": ("perimeter", 4),
                 "window.kitchen": ("perimeter", 5), "garage.door": ("perimeter", 6),
                 "motion.kitchen": ("interior", 7), "camera.front": ("perimeter", 8)}
INTRUSION_TYPES = {"intrusion", "forced_entry", "glassbreak", "tamper"}
# detectors whose state carries the alarm condition (episode = attribute true until it reads false)
_EPISODE_ATTR = {"smoke.hall": "smoke", "leak.sink": "leak", "motion.kitchen": "motion"}


class WanLink:
    """Simulated WAN between the house and the monitoring centre. Same faults both ways."""

    def __init__(self, sim, latency=(0.05, 0.25), loss=0.0, outages=(), name="wan"):
        self.sim, self.latency, self.loss, self.name = sim, latency, loss, name
        self.outages = list(outages)          # [(start, end)] in sim seconds
        self.stats = {"sent": 0, "lost": 0, "outage_dropped": 0, "delivered": 0}

    def up(self, t=None):
        t = self.sim.now if t is None else t
        return not any(a <= t < b for a, b in self.outages)

    def send(self, data: bytes, deliver):
        self.stats["sent"] += 1
        if not self.up():
            self.stats["outage_dropped"] += 1
            self.sim.trace("wan_drop", link=self.name, reason="outage")
            return
        if self.loss and self.sim.rng.random() < self.loss:
            self.stats["lost"] += 1
            self.sim.trace("wan_drop", link=self.name, reason="loss")
            return
        lo, hi = self.latency
        delay = lo + (hi - lo) * self.sim.rng.random()

        def arrive():
            # a packet in flight when an outage starts is lost too
            if not self.up():
                self.stats["outage_dropped"] += 1
                return
            self.stats["delivered"] += 1
            deliver(data)
        self.sim.after(delay, arrive)


class MonitoringCentre:
    """Simulated alarm receiving centre. Decodes, de-duplicates, ACKs, decides dispatch."""

    def __init__(self, sim, link, accounts: dict, supervision_s=300.0, timeband=(40.0, 20.0),
                 dedupe_window_s=3600.0):
        self.sim, self.link = sim, link
        self.accounts = {a.upper(): k for a, k in accounts.items()}   # account -> AES key|None
        self.supervision_s, self.timeband = supervision_s, timeband
        self.dedupe_window_s = dedupe_window_s
        self.receipts = []        # first receipt of each distinct message
        self.duplicates = 0       # retransmissions that arrived after the first copy
        self.duplicate_log = []   # (t, token, seq)
        self.rejected = []        # (t, reason)
        self.dispatches = []      # the centre's decisions, NOT the hub's
        self.line_faults = []     # (t_detected, account)
        self._seen = {}           # (acct, seq, event) -> t
        self._last_seen = {}
        self._fault_open = set()
        sim.after(supervision_s, self._supervise)

    def receive(self, raw: bytes, reply):
        now = dc09.sim_timestamp(self.sim.now)
        try:
            # account is in clear text in the header; pick the key from it
            hdr = dc09._FRAME.match(raw.decode("ascii", "replace").strip("\n\r"))
            acct = hdr["acct"] if hdr else None
            if acct is None or acct.upper() not in self.accounts:
                self.rejected.append((self.sim.now, "unknown_account"))
                reply(dc09.build_nak(now))
                return
            key = self.accounts[acct.upper()]
            d = dc09.decode(raw, key)
        except dc09.DC09Error as e:
            self.rejected.append((self.sim.now, f"format:{e}"))
            reply(dc09.build_nak(now))
            return
        if not d.crc_ok or not d.length_ok:
            self.rejected.append((self.sim.now, "crc_or_length"))
            reply(dc09.build_nak(now))
            return
        if d.timestamp is not None:
            age = (now - d.timestamp).total_seconds()
            if age > self.timeband[0] or -age > self.timeband[1]:
                self.rejected.append((self.sim.now, "timestamp_out_of_band"))
                reply(dc09.build_nak(now))
                return
        self._last_seen[d.account] = self.sim.now
        if d.account in self._fault_open:
            self._fault_open.discard(d.account)
            self.sim.trace("centre_line_restored", account=d.account)
        k = (d.account, d.sequence, d.token, d.event)
        first = k not in self._seen or self.sim.now - self._seen[k] > self.dedupe_window_s
        if first:
            self._seen[k] = self.sim.now
            rec = {"t": self.sim.now, "account": d.account, "seq": d.sequence,
                   "token": d.token, "event": d.event}
            self.receipts.append(rec)
            if d.token != dc09.NULL and d.event[:1] in ("1", "N"):
                self._decide(rec)
        else:
            self.duplicates += 1
            self.duplicate_log.append((self.sim.now, d.token, d.sequence))
        reply(dc09.build_ack(d, key, now, self.sim.rng))

    def _decide(self, rec):
        # Placeholder for the operator workflow (verify, call keyholders, dispatch).
        self.dispatches.append({"t": self.sim.now, "account": rec["account"],
                                "event": rec["event"], "action": "operator_verify_then_dispatch"})
        self.sim.trace("centre_dispatch_decision", **rec)

    def _supervise(self):
        for acct, t in self._last_seen.items():
            if self.sim.now - t > self.supervision_s and acct not in self._fault_open:
                self._fault_open.add(acct)
                self.line_faults.append((self.sim.now, acct))
                self.sim.trace("centre_line_fault", account=acct)
        self.sim.after(self.supervision_s / 5, self._supervise)


class LocalSiren:
    """Sounds on smoke/leak without the hub. Subscribes to the detectors directly, and can
    also be hard-wired (hardwire()) so it works even with no message bus at all."""

    def __init__(self, sim, bus=None, detectors=("smoke.hall", "leak.sink")):
        self.sim = sim
        self.activations = []      # (t, device_id, pattern)
        self._last = {}            # device_id -> last activation time
        if bus is not None:
            for dev in detectors:
                bus.subscribe(f"event/{dev}", self._on_event, name="local_siren")

    def _on_event(self, env):
        p = env.payload or {}
        if p.get("restore") or str(p.get("type", "")).endswith("_clear"):
            return
        self.hardwire(env.source or env.topic.split("/", 1)[1])

    def interconnect(self, device):
        """Wire a detector straight to this siren (like interconnected smoke alarms): it sounds on the
        detector's own trigger, before and without any bus message (added Nov 19: a lost event/<id>
        message must not silence the siren)."""
        if getattr(device, "kind", "") == "smoke":
            orig = device.trigger

            def trigger():
                self.hardwire(device.device_id)
                orig()
            device.trigger = trigger
        elif getattr(device, "kind", "") == "leak":
            orig_leak = device.set_leak

            def set_leak(wet):
                if wet and not device.state.get("leak"):
                    self.hardwire(device.device_id)
                orig_leak(wet)
            device.set_leak = set_leak

    def hardwire(self, device_id):
        # one activation per episode: the bus copy of an interconnected detector is ignored for 120 s
        last = self._last.get(device_id)
        if last is not None and self.sim.now - last < 120.0:
            return
        self._last[device_id] = self.sim.now
        pattern = "temporal3" if device_id.startswith("smoke") else "chime"
        self.activations.append((self.sim.now, device_id, pattern))
        self.sim.trace("siren", device=device_id, pattern=pattern)


class AlarmHub:
    """Hub component: turns detector events into DC-09 messages and gets them ACKed."""

    NAME = "alarm_hub"

    def __init__(self, sim, bus, link, centre, account="1A2B3C", key=None, fmt=dc09.ADM_CID,
                 egress=None, centre_host="receiver.monitoring.example", heartbeat_s=60.0,
                 ack_timeout=5.0, backoff_base=1.0, backoff_cap=16.0, link_fault_after=180.0,
                 person="owner", zones=None, armed=False, receiver="0", line="1"):
        self.sim, self.bus, self.link, self.centre = sim, bus, link, centre
        self.account, self.key, self.fmt = account.upper(), key, fmt
        self.egress, self.centre_host = egress, centre_host
        self.heartbeat_s, self.ack_timeout = heartbeat_s, ack_timeout
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.link_fault_after, self.person = link_fault_after, person
        self.zones = dict(DEFAULT_ZONES, **(zones or {}))
        self.armed, self.receiver, self.line = armed, receiver, line
        self.alive = True
        self._seq = 0
        self.outstanding = {}      # seq -> record
        self.alarms = []           # every alarm record (acked or not)
        self.errors = []
        self._seen_events = set()
        self.last_ack = 0.0
        self.link_down_notified = False
        self.heartbeats_sent = 0
        self._episodes = {}        # device_id -> t opened: one alarm per detector episode
        bus.subscribe("event/+", self._on_event, name=self.NAME)
        # Nov 19: also raise from detector STATE, so a lost event/<id> is recovered by the state copy
        # sent with it or by the next heartbeat (smoke/leak true, or motion true while armed).
        bus.subscribe("state/+", self._on_state, name=self.NAME)
        self._lock_state = {}      # lock device -> last reported state
        self._unlock_cmd = {}      # lock device -> time of the last hub unlock command
        bus.subscribe("cmd/+", self._on_cmd, name=self.NAME)
        if heartbeat_s:
            sim.after(heartbeat_s, self._heartbeat)

    # ---- inputs -------------------------------------------------------------------
    def crash(self):
        self.alive = False

    def _on_event(self, env):
        if not self.alive:
            return
        try:
            self._handle(env)
        except Exception as e:      # never let the hub break the bus for other subscribers
            self.errors.append((self.sim.now, repr(e)))

    def _classify(self, dev, p):
        typ = str(p.get("type", ""))
        restore = bool(p.get("restore")) or typ.endswith("_clear")
        base = typ[:-6] if typ.endswith("_clear") else typ
        if dev in self.zones and base in ("smoke", "fire", "leak"):
            return base, self.zones[dev][1], restore
        if base in INTRUSION_TYPES:
            kind = self.zones.get(dev, ("intrusion", 99))[0]
            kind = kind if kind in ("perimeter", "interior") else "intrusion"
            return kind, self.zones.get(dev, (None, 99))[1], restore
        # Integration (islab/suites/house.py, Nov 19): a door opened by key/thumb-turn while armed
        # away is a perimeter alarm. The twin has no door contacts; a lock's manual unlock event is
        # the only "door opened" signal it emits.
        if (base == "lock_operation" and p.get("operation") == "unlock" and self.armed
                and self.zones.get(dev, ("", 0))[0] == "perimeter"):
            return "perimeter", self.zones[dev][1], False
        if base == "motion" and self.armed and dev in self.zones:
            return "interior", self.zones[dev][1], restore
        return None

    def _handle(self, env):
        if env.event_id in self._seen_events:       # bus-level duplicate
            return
        self._seen_events.add(env.event_id)
        dev = env.topic.split("/", 1)[1]
        c = self._classify(dev, env.payload or {})
        if c is None:
            return
        kind, zone, restore = c
        if dev in _EPISODE_ATTR or kind == "perimeter":
            if restore:
                self._episodes.pop(dev, None)
            elif dev in self._episodes:
                self.sim.trace("alarm_episode_dedupe", device=dev, via="event")
                return
            else:
                self._episodes[dev] = self.sim.now
        self.raise_alarm(kind, zone, restore=restore, t_event=self.sim.now, source=dev)

    def _on_cmd(self, env):
        if (env.payload or {}).get("action") == "unlock":
            self._unlock_cmd[env.topic.split("/", 1)[1]] = self.sim.now

    def _lock_from_state(self, dev, st):
        """A perimeter lock that reports locked -> unlocked while armed, with no unlock command from the
        hub in the last 60 s, was opened by hand: same alarm as the manual-unlock event (one per episode)."""
        prev = self._lock_state.get(dev)
        self._lock_state[dev] = st
        if st != "unlocked":
            self._episodes.pop(dev, None)
            return
        if prev != "locked" or not self.armed or dev in self._episodes:
            return
        if self.sim.now - self._unlock_cmd.get(dev, -1e9) < 60.0:
            return
        self._episodes[dev] = self.sim.now
        self.sim.trace("alarm_from_state", device=dev)
        self.raise_alarm("perimeter", self.zones[dev][1], t_event=self.sim.now, source=dev)

    def _on_state(self, env):
        if not self.alive:
            return
        try:
            dev = env.topic.split("/", 1)[1]
            if self.zones.get(dev, ("",))[0] == "perimeter" and dev.startswith("lock.") \
                    and "state" in (env.payload or {}):
                self._lock_from_state(dev, env.payload["state"])
                return
            attr = _EPISODE_ATTR.get(dev)
            if attr is None or attr not in (env.payload or {}):
                return
            active = env.payload[attr] is True
            if not active:
                self._episodes.pop(dev, None)
                return
            kind = self.zones[dev][0] if dev in self.zones else None
            if kind == "interior" and not self.armed:
                return
            if kind is None or dev in self._episodes:
                return
            self._episodes[dev] = self.sim.now
            self.sim.trace("alarm_from_state", device=dev, event_id=env.event_id)
            self.raise_alarm(kind, self.zones[dev][1], t_event=self.sim.now, source=dev)
        except Exception as e:
            self.errors.append((self.sim.now, repr(e)))

    # ---- transmission ---------------------------------------------------------------
    def _next_seq(self):
        self._seq = self._seq % 9999 + 1
        return self._seq

    def raise_alarm(self, kind, zone, restore=False, t_event=None, source=None):
        seq = self._next_seq()
        if self.fmt == dc09.ADM_CID:
            event = dc09.adm_cid_event(kind, zone, restore=restore)
        else:
            event = dc09.sia_dcs_event(kind, zone, restore=restore)
        rec = {"seq": seq, "kind": kind, "zone": zone, "restore": restore, "event": event,
               "source": source, "t_event": self.sim.now if t_event is None else t_event,
               "attempts": 0, "t_ack": None, "token": self.fmt}
        self.outstanding[seq] = rec
        self.alarms.append(rec)
        self.sim.trace("alarm_raised", seq=seq, alarm_kind=kind, zone=zone)
        self._transmit(seq)
        return rec

    def _frame(self, token, seq, event):
        msg = dc09.DC09Message(mtype=token, account=self.account, sequence=seq,
                               receiver=self.receiver, line=self.line, event=event,
                               timestamp=dc09.sim_timestamp(self.sim.now),
                               encrypted=self.key is not None)
        return dc09.encode(msg, key=self.key, rng=self.sim.rng)

    def _uplink(self, frame: bytes):
        self.link.send(frame, lambda d: self.centre.receive(d, self._reply))

    def _reply(self, ack: bytes):
        self.link.send(ack, self._on_response)

    def _send(self, frame, purpose_class, msg_id):
        if self.egress is not None:
            return self.egress.send(source=self.NAME, dest=self.centre_host, purpose="alarm",
                                    payload_class=purpose_class, data=frame,
                                    transport=self._uplink, msg_id=msg_id)
        self._uplink(frame)
        return True

    def _transmit(self, seq):
        rec = self.outstanding.get(seq)
        if rec is None or not self.alive:
            return
        rec["attempts"] += 1
        # a fresh timestamp each attempt (receivers reject stale ones); same sequence number
        frame = self._frame(rec["token"], seq, rec["event"])
        self._send(frame, "alarm_event", f"alarm:{seq}")
        self.sim.after(self.ack_timeout, self._check_ack, seq, rec["attempts"])

    def _check_ack(self, seq, attempt):
        rec = self.outstanding.get(seq)
        if rec is None or rec["attempts"] != attempt:
            return
        n = rec["attempts"]
        backoff = min(self.backoff_cap, self.backoff_base * 2 ** min(n - 1, 30))
        backoff *= 0.5 + self.sim.rng.random()          # jitter
        self.sim.after(backoff, self._transmit, seq)

    def _on_response(self, data: bytes):
        if not self.alive:
            return
        r = dc09.parse_response(data, self.key)
        if r.get("kind") != "ACK" or not r.get("crc_ok"):
            return              # NAK / garbage: the timeout path retries
        if (r.get("account") or "").upper() != self.account:
            return
        self.last_ack = self.sim.now
        if self.link_down_notified:
            self.link_down_notified = False
            self._notify("Monitoring centre link restored.")
        rec = self.outstanding.pop(r["sequence"], None)
        if rec is not None and rec["token"] != dc09.NULL:
            rec["t_ack"] = self.sim.now
            self.sim.trace("alarm_acked", seq=rec["seq"], latency=self.sim.now - rec["t_event"])
            if not rec["restore"]:
                self._notify(f"{rec['kind'].capitalize()} alarm (zone {rec['zone']}) received "
                             f"by the monitoring centre. They handle dispatch.")

    # ---- supervision -----------------------------------------------------------------
    def _heartbeat(self):
        if not self.alive:
            return
        seq = self._next_seq()
        # heartbeats are not retried; the next one replaces a lost one
        self.outstanding[seq] = {"seq": seq, "token": dc09.NULL, "restore": True}
        self.sim.after(self.heartbeat_s * 3, self.outstanding.pop, seq, None)
        self._send(self._frame(dc09.NULL, seq, ""), "alarm_heartbeat", f"hb:{seq}")
        self.heartbeats_sent += 1
        if (self.sim.now - self.last_ack > self.link_fault_after
                and not self.link_down_notified):
            self.link_down_notified = True
            self._notify("Monitoring centre link is down. Alarms are queued and retried; "
                         "local sirens work without it.")
        self.sim.after(self.heartbeat_s, self._heartbeat)

    def _notify(self, text):
        self.bus.publish(Envelope(topic=f"notify/{self.person}",
                                  payload={"text": text, "kind": "tell"},
                                  source=self.NAME, ts=self.sim.now))
