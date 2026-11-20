"""A full egress ledger on top of the B01 gate (and the B04 push purpose).

``LedgerGate`` keeps every rule of ``islab.alarm.egress.EgressGate`` and adds three things:

* a ledger row for every attempt to send anything out: time, source, destination, purpose,
  payload class, bytes, outcome (sent, blocked, queued, dropped_offline), reason, whether a
  person would notice it ("person") or not ("background"), and the destination's declared
  retention promise;
* source manifests: each sender declares which purposes it uses, the largest message it sends
  and how many messages a day. A sender outside its manifest is blocked, even towards an
  allowlisted destination with an allowed payload class (the base gate lets that through);
* a notice to the household the first time a source tries an undeclared destination or breaks
  its manifest (detection), with the source then quarantined for the rest of the day.

Everything here is declared by people (allowlist, retention promises, manifests). The gate
cannot check what a provider really keeps; it records what was promised.
"""
from __future__ import annotations

from collections import defaultdict

from islab.core.bus import Envelope
from islab.safety.notify import PUSH_CLASS, PUSH_PURPOSE, PUSH_RELAY, SafetyEgressGate

DAY = 86400.0

# Who each destination is and what it promised to keep (declared, not verified).
DESTINATIONS = {
    "receiver.monitoring.example": {"purposes": {"alarm"}, "operator": "monitoring centre",
                                    "retention": "alarm records kept per monitoring contract"},
    "backup.storage.example": {"purposes": {"backup_encrypted"}, "operator": "backup storage",
                               "retention": "encrypted, rotated after 30 days"},
    "updates.vendor.example": {"purposes": {"fleet_update"}, "operator": "hub vendor",
                               "retention": "request logs kept 90 days, no content"},
    "asr.provider.example": {"purposes": {"speech_recognition_optional"},
                             "operator": "speech provider", "retention": "not stored"},
    "llm.provider.example": {"purposes": {"llm_text_optional"}, "operator": "assistant provider",
                             "retention": "not stored"},
    PUSH_RELAY: {"purposes": {PUSH_PURPOSE}, "operator": "push relay",
                 "retention": "text deleted after delivery"},
}

# Would a person notice this message? "person": someone asked for it, or is told about it.
VISIBILITY = {
    ("speech_recognition_optional", "speech_audio"): "person",
    ("llm_text_optional", "text"): "person",
    (PUSH_PURPOSE, PUSH_CLASS): "person",
    ("alarm", "alarm_event"): "person",
    ("alarm", "camera_clip"): "person",
    ("alarm", "alarm_heartbeat"): "background",
    ("fleet_update", "update_check"): "background",
    ("backup_encrypted", "encrypted_blob"): "background",
}

# What each sender may do: purpose -> (max bytes per message, max messages per day).
DEFAULT_MANIFESTS = {
    "alarm_hub": {"alarm": (2_000, 2_000)},
    "voice.pipeline": {"speech_recognition_optional": (64_000, 500),
                       "llm_text_optional": (8_000, 500)},
    "backup.agent": {"backup_encrypted": (200_000_000, 2)},
    "fleet.agent": {"fleet_update": (2_000, 6)},
    "safety.notify": {PUSH_PURPOSE: (2_000, 200)},
    "privacy.notify": {PUSH_PURPOSE: (2_000, 200)},
}


def visibility(purpose, payload_class):
    return VISIBILITY.get((purpose, payload_class), "background")


class LedgerGate(SafetyEgressGate):
    """B01 gate + B04 push purpose + manifests, ledger, detection notices."""

    def __init__(self, sim, bus, settings=None, manifests=None, enforce_manifests=True,
                 notify_person="anna", **kw):
        super().__init__(sim, bus, settings=settings, **kw)
        self.manifests = {k: dict(v) for k, v in (manifests or DEFAULT_MANIFESTS).items()}
        self.enforce_manifests = enforce_manifests
        self.notify_person = notify_person
        self.ledger: list[dict] = []
        self.notices: list[dict] = []
        self.quarantined: dict[str, float] = {}
        self._day_counts = defaultdict(int)       # (day, source, purpose) -> messages sent

    # ---- the one way out, with a ledger row for every attempt -------------------------
    def send(self, source, dest, purpose, payload_class, data: bytes = b"", size=None,
             transport=None, msg_id=None, meta=None) -> bool:
        nbytes = len(data) if size is None else int(size)
        row = {"t": self.sim.now, "source": source, "dest": dest, "purpose": purpose,
               "payload_class": payload_class, "bytes": nbytes, "msg_id": msg_id,
               "visibility": visibility(purpose, payload_class),
               "retention": DESTINATIONS.get(dest, {}).get("retention", "undeclared destination")}
        reason = self._manifest_check(source, dest, purpose, nbytes)
        if reason:
            rec = dict(row, meta=dict(meta or {}), reason=reason)
            self.blocked.append(rec)
            self.sim.trace("egress_blocked", source=source, dest=dest, purpose=purpose,
                           payload_class=payload_class, reason=reason)
            self._log(row, "blocked", reason)
            self._detect(source, dest, purpose, reason)
            return False
        n_block, n_drop, n_sent = len(self.blocked), len(self.dropped_offline), len(self.sent)
        ok = super().send(source, dest, purpose, payload_class, data=data, size=size,
                          transport=transport, msg_id=msg_id, meta=meta)
        if len(self.blocked) > n_block:
            reason = self.blocked[-1]["reason"]
            self._log(row, "blocked", reason)
            self._detect(source, dest, purpose, reason)
        elif len(self.dropped_offline) > n_drop:
            self._log(row, "dropped_offline", self.dropped_offline[-1]["reason"])
        elif len(self.sent) == n_sent and ok:
            self._log(row, "queued", "internet_down")
        if ok and len(self.dropped_offline) == n_drop:
            day = int(self.sim.now // DAY)
            self._day_counts[(day, source, purpose)] += 1
        return ok

    def _emit(self, rec, data, transport):
        super()._emit(rec, data, transport)
        row = {"t": rec["t"], "source": rec["source"], "dest": rec["dest"],
               "purpose": rec["purpose"], "payload_class": rec["payload_class"],
               "bytes": rec["bytes"], "msg_id": rec["msg_id"],
               "visibility": visibility(rec["purpose"], rec["payload_class"]),
               "retention": DESTINATIONS.get(rec["dest"], {}).get("retention",
                                                                  "undeclared destination")}
        self._log(row, "sent", None, t_out=self.sim.now)

    def _log(self, row, outcome, reason, t_out=None):
        r = dict(row, outcome=outcome, reason=reason)
        if t_out is not None:
            r["t_out"] = t_out
        self.ledger.append(r)

    # ---- manifests and detection ---------------------------------------------------------
    def _manifest_check(self, source, dest, purpose, nbytes):
        if not self.enforce_manifests:
            return None
        if source in self.quarantined:
            return "source_quarantined"
        if dest not in self.allowlist:
            return None                      # the base gate blocks it with its own reason
        man = self.manifests.get(source)
        if man is None:
            return "source_has_no_manifest"
        if purpose not in man:
            return "purpose_not_in_source_manifest"
        max_bytes, max_day = man[purpose]
        if nbytes > max_bytes:
            return "over_declared_message_size"
        if self._day_counts[(int(self.sim.now // DAY), source, purpose)] >= max_day:
            return "over_declared_daily_count"
        return None

    def _detect(self, source, dest, purpose, reason):
        """First suspicious attempt by a source: tell the household once, quarantine the source."""
        if not self.enforce_manifests:
            return                           # B01 behaviour: blocked and logged, nobody told
        suspicious = reason in ("destination_not_allowlisted", "source_has_no_manifest",
                                "purpose_not_in_source_manifest", "over_declared_message_size",
                                "over_declared_daily_count", "purpose_not_allowed_for_destination",
                                "unknown_purpose") or reason.startswith("payload_class_never_leaves")
        if not suspicious or source in self.quarantined:
            return
        if source in self.manifests and reason in ("over_declared_daily_count",):
            # a declared sender over its daily count: tell, but do not cut its declared traffic
            if any(n["source"] == source for n in self.notices):
                return
        else:
            self.quarantined[source] = self.sim.now
        text = (f"{source} tried to send to {dest} ({purpose}). That is not what it declared. "
                f"Blocked; the device is held at home until you review it.")
        self.notices.append({"t": self.sim.now, "source": source, "dest": dest,
                             "purpose": purpose, "reason": reason, "text": text})
        self.bus.publish(Envelope(topic=f"notify/{self.notify_person}",
                                  payload={"text": text, "kind": "tell", "tag": "egress_notice"},
                                  source="privacy.ledger", ts=self.sim.now))

    def _watch(self, env):
        before = len(self.violations)
        super()._watch(env)
        if len(self.violations) > before and self.enforce_manifests:
            self.quarantined.setdefault(env.source, self.sim.now)
            text = (f"{env.source} tried to send without going through the gate. Logged; "
                    f"in a real home the firewall must stop it.")
            self.notices.append({"t": self.sim.now, "source": env.source, "dest": env.topic,
                                 "purpose": None, "reason": "bypass_attempt", "text": text})
            self.bus.publish(Envelope(topic=f"notify/{self.notify_person}",
                                      payload={"text": text, "kind": "tell", "tag": "egress_notice"},
                                      source="privacy.ledger", ts=self.sim.now))

    def release(self, source):
        """A person reviewed the device and lets it send again (inside its manifest)."""
        self.quarantined.pop(source, None)

    # ---- day views -------------------------------------------------------------------------
    def rows(self, day=None, outcome=None):
        out = []
        for r in self.ledger:
            t = r.get("t_out", r["t"])
            if day is not None and not (day * DAY <= t < (day + 1) * DAY):
                continue
            if outcome is not None and r["outcome"] != outcome:
                continue
            out.append(r)
        return out

    def day_report(self, day=0) -> dict:
        sent = self.rows(day, "sent")
        by = defaultdict(lambda: {"messages": 0, "bytes": 0, "destinations": set(),
                                  "visibility": set(), "retention": set(), "classes": defaultdict(int)})
        for r in sent:
            b = by[r["purpose"]]
            b["messages"] += 1
            b["bytes"] += r["bytes"]
            b["destinations"].add(r["dest"])
            b["visibility"].add(r["visibility"])
            b["retention"].add(r["retention"])
            b["classes"][r["payload_class"]] += 1
        vis = defaultdict(lambda: {"messages": 0, "bytes": 0})
        for r in sent:
            vis[r["visibility"]]["messages"] += 1
            vis[r["visibility"]]["bytes"] += r["bytes"]
        tot_m = sum(v["messages"] for v in vis.values())
        tot_b = sum(v["bytes"] for v in vis.values())
        blocked = self.rows(day, "blocked")
        dropped = self.rows(day, "dropped_offline")
        queued = self.rows(day, "queued")
        return {
            "day": day,
            "by_purpose": {p: {"messages": v["messages"], "bytes": v["bytes"],
                               "destinations": sorted(v["destinations"]),
                               "visibility": sorted(v["visibility"]),
                               "retention": sorted(v["retention"]),
                               "classes": dict(v["classes"])} for p, v in sorted(by.items())},
            "messages": tot_m, "bytes": tot_b,
            "by_visibility": {k: dict(v) for k, v in vis.items()},
            "background_share_messages": round(vis["background"]["messages"] / tot_m, 4) if tot_m else 0.0,
            "background_share_bytes": round(vis["background"]["bytes"] / tot_b, 4) if tot_b else 0.0,
            "camera_items_out": sum(1 for r in sent if r["payload_class"] in ("camera_clip", "image", "video")),
            "blocked": len(blocked),
            "blocked_by_source": _count(blocked, "source"),
            "blocked_by_reason": _count(blocked, "reason"),
            "blocked_destinations": sorted({r["dest"] for r in blocked}),
            "dropped_offline": len(dropped),
            "dropped_offline_by_purpose": _count(dropped, "purpose"),
            "queued": len(queued),
            "waiting_now": len(self.queue),
            "notices": [n for n in self.notices if day * DAY <= n["t"] < (day + 1) * DAY],
            "violations": len([v for v in self.violations if day * DAY <= v["t"] < (day + 1) * DAY]),
        }


def _count(rows, key):
    out = defaultdict(int)
    for r in rows:
        out[r[key]] += 1
    return dict(sorted(out.items()))


__all__ = ["LedgerGate", "DESTINATIONS", "VISIBILITY", "DEFAULT_MANIFESTS", "visibility",
           "PUSH_RELAY", "PUSH_PURPOSE", "PUSH_CLASS"]
