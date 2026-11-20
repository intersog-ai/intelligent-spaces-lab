"""A second alarm path: home internet (primary) plus a modelled cellular link (secondary).

DualPathEgressGate   the B01 egress gate with a 'cellular' route. The route carries only alarm
                     events and alarm heartbeats, does not depend on the home internet, and is
                     logged like any other egress. A camera clip leaves only with a person's
                     consent for that one clip, and never over cellular.
DualPathAlarmHub     the B01 AlarmHub with path selection and dual-path supervision:
                     primary heartbeats every 60 s; cellular heartbeats hourly, every 180 s while
                     the primary has failed supervision. An alarm goes first on the preferred path;
                     if it is not acknowledged in 5 s the next attempt goes, at once, on the other.
DualPathCentre       the B01 monitoring centre with per-path supervision and a second dedupe layer:
                     a repeat of an alarm that is already an open incident (same account, event
                     code and zone, no restore in between) is attached to that incident, not opened
                     again. That is the B04 duplicate: a rebooted hub re-raises the same leak with
                     a new sequence number, which the message-level dedupe cannot see.

Dispatch stays with the centre. The hub reports what its detectors observed and retries; it has
no way to call 911. The centre's actions are a modelled workflow, not any real centre's procedure.
"""
from __future__ import annotations

import re

from islab.alarm import dc09
from islab.alarm.egress import EgressGate
from islab.alarm.path import AlarmHub, MonitoringCentre

PRIMARY, CELLULAR = "primary", "cellular"

# ---------------------------------------------------------------------------- event parsing
_SIA_RESTORE = {"FH": "FA", "WH": "WA", "BH": "BA", "PH": "PA"}
_CATEGORY = {"FA": "fire", "WA": "water", "BA": "burglary", "PA": "panic",
             "110": "fire", "111": "fire", "154": "water", "130": "burglary", "131": "burglary",
             "132": "burglary", "120": "panic"}


def parse_event(event: str):
    """'1111 01 001' or 'Nri1/FA001' -> (code, zone, restore, category). None for anything else."""
    m = re.fullmatch(r"([136])(\d{3}) (\d{2}) (\d{3})", event or "")
    if m:
        code = m[2]
        return code, int(m[4]), m[1] == "3", _CATEGORY.get(code, "other")
    m = re.fullmatch(r"Nri\d+/([A-Z]{2})(\d{1,4})", event or "")
    if m:
        code, restore = m[1], m[1] in _SIA_RESTORE
        code = _SIA_RESTORE.get(code, code)
        return code, int(m[2]), restore, _CATEGORY.get(code, "other")
    return None


# what the centre does per category (modelled workflow; the hub never does any of this)
CENTRE_ACTIONS = {
    "fire": "centre_calls_911_with_address_sensor_time",
    "burglary": "centre_verifies_with_keyholder_then_calls_911",
    "water": "centre_calls_keyholder",
    "panic": "centre_calls_911_with_address_sensor_time",
    "other": "centre_operator_review",
}


# ---------------------------------------------------------------------------- egress
class DualPathEgressGate(EgressGate):
    CELL_CLASSES = {"alarm_event", "alarm_heartbeat"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.consents = {}        # consent_id -> {"clip_ref", "person", "dest", "t", "used"}
        self._n_consent = 0

    def record_consent(self, person, clip_ref, dest="receiver.monitoring.example"):
        """A person chose to share this one clip with the centre. Returns a single-use consent id."""
        self._n_consent += 1
        cid = f"consent{self._n_consent:05d}"
        self.consents[cid] = {"clip_ref": clip_ref, "person": person, "dest": dest,
                              "t": self.sim.now, "used": False}
        self.sim.trace("clip_consent", person=person, clip_ref=clip_ref, consent_id=cid)
        return cid

    def _check(self, dest, purpose, cls, meta):
        if cls in ("camera_clip", "image", "video"):
            c = self.consents.get(meta.get("consent_id"))
            if (c is None or c["used"] or c["clip_ref"] != meta.get("clip_ref")
                    or c["dest"] != dest):
                return "clip_needs_person_consent"
            if meta.get("route") == CELLULAR:
                return "clip_not_on_cellular"
            reason = super()._check(dest, purpose, cls, meta)
            return None if reason == "camera_clips_disabled" else reason
        reason = super()._check(dest, purpose, cls, meta)
        if reason is None and meta.get("route") == CELLULAR and (
                purpose != "alarm" or cls not in self.CELL_CLASSES):
            return "not_allowed_on_cellular"
        return reason

    def send(self, source, dest, purpose, payload_class, data: bytes = b"", size=None,
             transport=None, msg_id=None, meta=None) -> bool:
        meta = dict(meta or {})
        if meta.get("route") != CELLULAR:
            ok = super().send(source, dest, purpose, payload_class, data, size, transport,
                              msg_id, meta)
            if ok and payload_class in ("camera_clip", "image", "video"):
                self.consents[meta["consent_id"]]["used"] = True
            return ok
        # cellular route: independent of the home internet, same checks and the same log
        nbytes = len(data) if size is None else int(size)
        rec = {"t": self.sim.now, "source": source, "dest": dest, "purpose": purpose,
               "payload_class": payload_class, "bytes": nbytes, "msg_id": msg_id, "meta": meta}
        reason = self._check(dest, purpose, payload_class, meta)
        if reason:
            rec["reason"] = reason
            self.blocked.append(rec)
            self.sim.trace("egress_blocked", source=source, dest=dest, purpose=purpose,
                           payload_class=payload_class, reason=reason)
            return False
        self._emit(rec, data, transport)
        return True

    def by_route(self, lo=0.0, hi=float("inf")):
        out = {}
        for r in self.sent:
            if lo <= r["t_out"] < hi:
                route = r["meta"].get("route", PRIMARY)
                b = out.setdefault(route, {"messages": 0, "bytes": 0, "classes": {}})
                b["messages"] += 1
                b["bytes"] += r["bytes"]
                b["classes"][r["payload_class"]] = b["classes"].get(r["payload_class"], 0) + 1
        return out


# ---------------------------------------------------------------------------- hub
class DualPathAlarmHub(AlarmHub):
    """B01 AlarmHub plus a cellular path. Construct with cell_link=CellularLink(...)."""

    NAME = "alarm_hub"

    def __init__(self, sim, bus, link, centre, cell_link=None, primary_fail_s=130.0,
                 cell_hb_s=3600.0, cell_hb_failover_s=180.0, cell_probe_s=20.0,
                 cell_probes=3, tell_after_s=300.0, **kw):
        self.cell = cell_link
        self.primary_fail_s, self.cell_hb_s = primary_fail_s, cell_hb_s
        self.cell_hb_failover_s, self.cell_probe_s = cell_hb_failover_s, cell_probe_s
        self.cell_probes, self.tell_after_s = cell_probes, tell_after_s
        self.last_ack_path = {PRIMARY: sim.now, CELLULAR: sim.now}
        self.primary_ok, self.cell_ok = True, True
        self.path_log = []            # (t, path, "failed"|"restored")
        self.told = []                # (t, text) notifications this hub sent
        self._primary_failed_at = None
        self._told_primary = False
        self._told_both = False
        self._up_since = None
        self._cell_gen = 0
        self._cell_unacked = 0
        super().__init__(sim, bus, link, centre, **kw)
        if self.cell is not None and self.cell_hb_s:
            sim.after(min(30.0, self.cell_hb_s), self._cell_heartbeat, self._cell_gen)

    # ---- routing ---------------------------------------------------------------------
    def _route_for(self, attempt):
        if self.cell is None:
            return PRIMARY
        if not self.primary_ok and self.cell_ok:
            return CELLULAR
        first = PRIMARY if self.primary_ok else CELLULAR
        other = CELLULAR if first == PRIMARY else PRIMARY
        return first if attempt % 2 == 1 else other

    def _send_route(self, frame, cls, msg_id, route):
        link = self.cell if route == CELLULAR else self.link

        def transport(d):
            link.send(d, lambda raw: self.centre.receive(
                raw, lambda ack: link.send(ack, lambda a: self._on_response_route(route, a)),
                path=route))
        if self.egress is not None:
            meta = {"route": route} if route == CELLULAR else None
            return self.egress.send(source=self.NAME, dest=self.centre_host, purpose="alarm",
                                    payload_class=cls, data=frame, transport=transport,
                                    msg_id=msg_id + (":cell" if route == CELLULAR else ""),
                                    meta=meta)
        transport(frame)
        return True

    def _transmit(self, seq):
        rec = self.outstanding.get(seq)
        if rec is None or not self.alive:
            return
        rec["attempts"] += 1
        route = self._route_for(rec["attempts"])
        rec.setdefault("routes", []).append(route)
        frame = self._frame(rec["token"], seq, rec["event"])
        self._send_route(frame, "alarm_event", f"alarm:{seq}", route)
        self.sim.after(self.ack_timeout, self._check_ack, seq, rec["attempts"])

    def _check_ack(self, seq, attempt):
        rec = self.outstanding.get(seq)
        if rec is None or rec["attempts"] != attempt:
            return
        if attempt == 1 and self._route_for(2) != rec["routes"][-1]:
            self._transmit(seq)                 # first failover: switch path now, no backoff
            return
        super()._check_ack(seq, attempt)

    # ---- responses -------------------------------------------------------------------
    def _on_response_route(self, route, data):
        if not self.alive:
            return
        r = dc09.parse_response(data, self.key)
        if r.get("kind") != "ACK" or not r.get("crc_ok"):
            return
        if (r.get("account") or "").upper() != self.account:
            return
        self.last_ack_path[route] = self.sim.now
        if route == PRIMARY and not self.primary_ok:
            self._set_primary(True)
        if route == CELLULAR:
            self._cell_unacked = 0
            if not self.cell_ok:
                self._set_cell(True)
        rec = self.outstanding.get(r.get("sequence"))
        if rec is not None and rec.get("token") != dc09.NULL and rec.get("t_ack") is None:
            rec["acked_route"] = route
        self._on_response(data)

    # ---- supervision -----------------------------------------------------------------
    def _heartbeat(self):
        if not self.alive:
            return
        self._evaluate()
        seq = self._next_seq()
        self.outstanding[seq] = {"seq": seq, "token": dc09.NULL, "restore": True}
        self.sim.after(self.heartbeat_s * 3, self.outstanding.pop, seq, None)
        self._send_route(self._frame(dc09.NULL, seq, ""), "alarm_heartbeat", f"hb:{seq}", PRIMARY)
        self.heartbeats_sent += 1
        self.sim.after(self.heartbeat_s, self._heartbeat)

    def _evaluate(self):
        now = self.sim.now
        if self.primary_ok and now - self.last_ack_path[PRIMARY] > self.primary_fail_s:
            self._set_primary(False)
        if (not self.primary_ok and not self._told_primary and self._primary_failed_at is not None
                and now - self._primary_failed_at >= self.tell_after_s):
            if self.cell is not None and self.cell_ok:
                self._told_primary = True
                self._tell("The internet path to the monitoring centre is down. Alarms now go "
                           "over the cellular backup.")
        self._check_both()

    def _set_primary(self, ok):
        self.primary_ok = ok
        self.path_log.append((self.sim.now, PRIMARY, "restored" if ok else "failed"))
        self.sim.trace("path_primary", ok=ok)
        if ok:
            self._primary_failed_at = None
            if self._told_primary:
                self._told_primary = False
                self._tell("The internet path to the monitoring centre is back.")
            self._check_both()
        else:
            self._primary_failed_at = self.sim.now
            if self.cell is not None:
                self._cell_gen += 1                     # faster cellular supervision, now
                self._cell_heartbeat(self._cell_gen)

    def _set_cell(self, ok):
        self.cell_ok = ok
        self.path_log.append((self.sim.now, CELLULAR, "restored" if ok else "failed"))
        self.sim.trace("path_cellular", ok=ok)
        self._check_both()

    def _check_both(self):
        both_down = self.cell is not None and not self.primary_ok and not self.cell_ok
        if both_down:
            self._up_since = None
            if not self._told_both:
                self._told_both = True
                self._tell("Both paths to the monitoring centre are down. Alarms are kept and "
                           "retried; local sirens still work. If there is a fire, leave and call "
                           "911 yourself.")
        elif self._told_both and self._up_since is None:
            # tell "reached again" only once a path has stayed up for tell_after_s (no flapping)
            self._up_since = self.sim.now
            self.sim.after(self.tell_after_s, self._tell_reached, self._up_since)

    def _tell_reached(self, mark):
        if self._told_both and self._up_since == mark and (self.primary_ok or self.cell_ok):
            self._told_both = False
            self._up_since = None
            self._tell("The monitoring centre can be reached again.")

    def _cell_heartbeat(self, gen):
        if not self.alive or gen != self._cell_gen or self.cell is None:
            return
        seq = self._next_seq()
        self.outstanding[seq] = {"seq": seq, "token": dc09.NULL, "restore": True}
        self.sim.after(self.cell_hb_s, self.outstanding.pop, seq, None)
        self._send_route(self._frame(dc09.NULL, seq, ""), "alarm_heartbeat", f"chb:{seq}",
                         CELLULAR)
        self.sim.after(self.cell_probe_s, self._cell_probe_check, gen, self.sim.now)

    def _cell_probe_check(self, gen, t_sent):
        if not self.alive or gen != self._cell_gen:
            return
        if self.last_ack_path[CELLULAR] >= t_sent:
            self._cell_unacked = 0
            interval = self.cell_hb_s if self.primary_ok else self.cell_hb_failover_s
            self.sim.after(max(0.0, interval - self.cell_probe_s), self._cell_heartbeat, gen)
            return
        self._cell_unacked += 1
        if self._cell_unacked >= self.cell_probes and self.cell_ok:
            self._set_cell(False)
        if self._cell_unacked < self.cell_probes:
            self._cell_heartbeat(gen)              # probe again now
        else:
            interval = self.cell_hb_s if self.primary_ok else self.cell_hb_failover_s
            self.sim.after(max(0.0, interval - self.cell_probe_s), self._cell_heartbeat, gen)

    def _tell(self, text):
        self._notify(text)

    def _notify(self, text):
        self.told.append((self.sim.now, text))
        super()._notify(text)


# ---------------------------------------------------------------------------- centre
class DualPathCentre(MonitoringCentre):
    """B01 MonitoringCentre with per-path supervision and incident-level dedupe."""

    def __init__(self, sim, link, accounts, supervision=None, episode_window_s=1800.0,
                 episode_dedupe=True, **kw):
        self.path_supervision = dict({PRIMARY: 180.0, CELLULAR: 9000.0,
                                      "cellular_when_primary_down": 450.0}, **(supervision or {}))
        self.episode_window_s, self.episode_dedupe = episode_window_s, episode_dedupe
        self.incidents = []          # one per real alarm episode
        self.repeats = []            # alarms attached to an already open incident
        self.message_level_decisions = 0   # what the B01 centre would have dispatched on
        self.path_faults = []        # (t, account, path)
        self.all_paths_lost = []     # (t, account)
        self.arrivals = []           # (t, path, token, seq, event, first)
        self.pending_clips = []      # (t, clip_ref) shared clips waiting for their alarm
        self._open = {}              # (account, code, zone) -> incident
        self._path_seen = {}         # (account, path) -> t
        self._path_fault_open = set()
        self._cur_path = PRIMARY
        super().__init__(sim, link, accounts, **kw)

    def receive(self, raw, reply, path=PRIMARY):
        self._cur_path = path
        n_rec, n_dup = len(self.receipts), self.duplicates
        super().receive(raw, reply)
        if len(self.receipts) > n_rec or self.duplicates > n_dup:
            if len(self.receipts) > n_rec:
                r = self.receipts[-1]
                r["path"] = path
                first = True
            else:
                t, tok, seq = self.duplicate_log[-1]
                r = {"token": tok, "seq": seq, "event": None}
                first = False
            hdr = dc09._FRAME.match(raw.decode("ascii", "replace").strip("\n\r"))
            acct = hdr["acct"].upper() if hdr and hdr["acct"] else None
            self.arrivals.append((self.sim.now, path, r["token"], r["seq"], r.get("event"), first))
            if acct is not None:
                key = (acct, path)
                self._path_seen[key] = self.sim.now
                if key in self._path_fault_open:
                    self._path_fault_open.discard(key)
                    self.sim.trace("centre_path_restored", account=acct, path=path)

    def _decide(self, rec):
        self.message_level_decisions += 1
        p = parse_event(rec["event"])
        if p is None:
            return super()._decide(rec)
        code, zone, restore, cat = p
        key = (rec["account"], code, zone)
        if restore:
            inc = self._open.pop(key, None)
            if inc is not None:
                inc["restored_t"] = self.sim.now
            return
        inc = self._open.get(key)
        if (self.episode_dedupe and inc is not None
                and self.sim.now - inc["last_t"] <= self.episode_window_s):
            inc["last_t"] = self.sim.now
            inc["repeats"] += 1
            self.repeats.append({"t": self.sim.now, "incident": inc["id"], "seq": rec["seq"]})
            self.sim.trace("centre_repeat_of_open_incident", incident=inc["id"], seq=rec["seq"])
            return
        inc = {"id": len(self.incidents) + 1, "t": self.sim.now, "last_t": self.sim.now,
               "account": rec["account"], "event": rec["event"], "category": cat, "zone": zone,
               "action": CENTRE_ACTIONS.get(cat, CENTRE_ACTIONS["other"]), "repeats": 0,
               "path": rec.get("path", self._cur_path), "clips": []}
        if cat == "burglary":
            inc["clips"] = [c for t, c in self.pending_clips if self.sim.now - t <= 300.0]
            self.pending_clips = []
        self._open[key] = inc
        self.incidents.append(inc)
        self.dispatches.append({"t": self.sim.now, "account": rec["account"],
                                "event": rec["event"], "action": inc["action"]})
        self.sim.trace("centre_incident", **{k: inc[k] for k in ("id", "category", "action")})

    def attach_clip(self, clip_ref, nbytes):
        """A clip a person chose to share arrives through the egress gate."""
        # the clip can arrive before its alarm (different sizes, different timing): hold it until
        # the burglary incident it belongs to is open
        inc = next((i for i in reversed(self.incidents) if i["category"] == "burglary"
                    and "restored_t" not in i and self.sim.now - i["t"] <= 300.0), None)
        if inc is not None:
            inc["clips"].append(clip_ref)
        else:
            self.pending_clips.append((self.sim.now, clip_ref))
        self.sim.trace("centre_clip", clip_ref=clip_ref, bytes=nbytes)

    def _supervise(self):
        now = self.sim.now
        accts = {a for a, _ in self._path_seen}
        for acct in accts:
            prim_t = self._path_seen.get((acct, PRIMARY))
            prim_down = prim_t is None or now - prim_t > self.path_supervision[PRIMARY]
            for path in (PRIMARY, CELLULAR):
                t = self._path_seen.get((acct, path))
                if t is None:
                    continue
                win = self.path_supervision[path]
                if path == CELLULAR and prim_down:
                    win = self.path_supervision["cellular_when_primary_down"]
                if now - t > win and (acct, path) not in self._path_fault_open:
                    self._path_fault_open.add((acct, path))
                    self.path_faults.append((now, acct, path))
                    self.sim.trace("centre_path_fault", account=acct, path=path)
            seen = [p for (a, p) in self._path_seen if a == acct]
            if seen and all((acct, p) in self._path_fault_open for p in seen):
                if acct not in self._fault_open:
                    self._fault_open.add(acct)
                    self.line_faults.append((now, acct))
                    self.all_paths_lost.append((now, acct))
                    self.sim.trace("centre_all_paths_lost", account=acct)
            elif acct in self._fault_open:
                self._fault_open.discard(acct)
        self.sim.after(30.0, self._supervise)
