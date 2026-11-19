"""Egress gate: the only component allowed to publish egress/<purpose>.

Every outbound message carries a purpose from a fixed set and a payload class. The gate
checks destination allowlist, purpose/class compatibility and household opt-ins, counts
bytes per purpose, keeps a log of what was blocked, and answers "what left the house today".
When the internet is down it queues or drops by purpose; home control never passes through
here, so it is unaffected.
"""
from __future__ import annotations

from collections import defaultdict

from islab.core.bus import Envelope

PURPOSES = ("alarm", "backup_encrypted", "fleet_update",
            "speech_recognition_optional", "llm_text_optional")

# payload classes the gate knows about; anything else is blocked
PAYLOAD_CLASSES = ("alarm_event", "alarm_heartbeat", "camera_clip", "encrypted_blob",
                   "update_check", "speech_audio", "text", "audio", "video", "image",
                   "telemetry", "unknown")

# what each purpose may carry (before settings are applied)
ALLOWED_CLASSES = {
    "alarm": {"alarm_event", "alarm_heartbeat", "camera_clip"},
    "backup_encrypted": {"encrypted_blob"},
    "fleet_update": {"update_check"},
    "speech_recognition_optional": {"speech_audio"},
    "llm_text_optional": {"text"},
}
NEVER = {"video", "audio", "telemetry", "unknown"}   # 'audio' = ambient/recorded audio
OPTIONAL = {"speech_recognition_optional": "cloud_speech_opt_in",
            "llm_text_optional": "cloud_llm_opt_in"}

DEFAULT_ALLOWLIST = {
    "receiver.monitoring.example": {"alarm"},
    "backup.storage.example": {"backup_encrypted"},
    "updates.vendor.example": {"fleet_update"},
    "asr.provider.example": {"speech_recognition_optional"},
    "llm.provider.example": {"llm_text_optional"},
}
DEFAULT_OFFLINE_POLICY = {"alarm": "queue", "backup_encrypted": "queue", "fleet_update": "drop",
                          "speech_recognition_optional": "drop", "llm_text_optional": "drop"}
# class-level overrides: a stale heartbeat is worthless (the receiver would reject its timestamp)
DEFAULT_OFFLINE_CLASS_POLICY = {"alarm_heartbeat": "drop"}

_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
          "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen", "twenty"]


def _n(k):
    return _WORDS[k] if k < len(_WORDS) else str(k)


class EgressGate:
    NAME = "egress_gate"

    def __init__(self, sim, bus, allowlist=None, settings=None, offline_policy=None,
                 queue_limit_bytes=200_000_000):
        self.sim, self.bus = sim, bus
        self.allowlist = {k: set(v) for k, v in (allowlist or DEFAULT_ALLOWLIST).items()}
        self.settings = {"cloud_speech_opt_in": False, "cloud_llm_opt_in": False,
                         "alarm_clips_allowed": False, **(settings or {})}
        self.offline_policy = dict(DEFAULT_OFFLINE_POLICY, **(offline_policy or {}))
        self.offline_class_policy = dict(DEFAULT_OFFLINE_CLASS_POLICY)
        self.queue_limit_bytes = queue_limit_bytes
        self.internet_up = True
        self.sent = []          # dicts of everything that left
        self.blocked = []       # dicts of everything refused, with reason
        self.dropped_offline = []
        self.queue = []         # (record, data, transport)
        self.violations = []    # egress/ publications not made by the gate
        bus.subscribe("egress/#", self._watch, name="egress_watchdog")

    # ---- the one way out ------------------------------------------------------------
    def send(self, source, dest, purpose, payload_class, data: bytes = b"", size=None,
             transport=None, msg_id=None, meta=None) -> bool:
        meta = dict(meta or {})
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
        if not self.internet_up:
            policy = self.offline_class_policy.get(payload_class, self.offline_policy.get(purpose))
            if policy == "queue":
                for i, q in enumerate(self.queue):
                    if msg_id is not None and q[0]["msg_id"] == msg_id:
                        # sender retry: keep one copy, the newest (fresh DC-09 timestamp)
                        self.queue[i] = (rec, data, transport)
                        return True
                if sum(q[0]["bytes"] for q in self.queue) + nbytes > self.queue_limit_bytes:
                    rec["reason"] = "offline_queue_full"
                    self.dropped_offline.append(rec)
                    return False
                self.queue.append((rec, data, transport))
                return True
            rec["reason"] = "offline_drop"
            self.dropped_offline.append(rec)
            return False
        self._emit(rec, data, transport)
        return True

    def _check(self, dest, purpose, cls, meta):
        if purpose not in PURPOSES:
            return "unknown_purpose"
        if dest not in self.allowlist:
            return "destination_not_allowlisted"
        if purpose not in self.allowlist[dest]:
            return "purpose_not_allowed_for_destination"
        if cls not in PAYLOAD_CLASSES:
            return "unknown_payload_class"
        if cls in NEVER:
            return f"payload_class_never_leaves:{cls}"
        if cls not in ALLOWED_CLASSES[purpose]:
            return f"payload_class_not_allowed_for_purpose:{cls}"
        if cls in ("camera_clip", "image") and not self.settings["alarm_clips_allowed"]:
            return "camera_clips_disabled"
        if purpose in OPTIONAL and not self.settings[OPTIONAL[purpose]]:
            return "not_opted_in"
        if purpose == "backup_encrypted" and meta.get("encrypted") is not True:
            return "backup_not_marked_encrypted"
        if purpose == "speech_recognition_optional" and meta.get("retention") != "not_stored":
            return "speech_provider_must_not_store"
        return None

    def _emit(self, rec, data, transport):
        rec["t_out"] = self.sim.now
        self.sent.append(rec)
        self.bus.publish(Envelope(topic=f"egress/{rec['purpose']}", source=self.NAME,
                                  ts=self.sim.now,
                                  payload={"dest": rec["dest"], "bytes": rec["bytes"],
                                           "payload_class": rec["payload_class"],
                                           "msg_id": rec["msg_id"]}))
        if transport is not None:
            transport(data)

    def set_internet(self, up: bool):
        self.internet_up = up
        if up:
            q, self.queue = self.queue, []
            for rec, data, transport in q:
                self._emit(rec, data, transport)

    def _watch(self, env):
        if env.source != self.NAME:
            self.violations.append({"t": self.sim.now, "source": env.source, "topic": env.topic})
            self.sim.trace("egress_violation", source=env.source, topic=env.topic)

    # ---- reporting ------------------------------------------------------------------
    def report(self, day: int) -> dict:
        lo, hi = day * 86400.0, (day + 1) * 86400.0
        by = defaultdict(lambda: {"messages": 0, "bytes": 0, "classes": defaultdict(int)})
        for r in self.sent:
            if lo <= r["t_out"] < hi:
                b = by[r["purpose"]]
                b["messages"] += 1
                b["bytes"] += r["bytes"]
                b["classes"][r["payload_class"]] += 1
        blocked = [r for r in self.blocked if lo <= r["t"] < hi]
        offline = [r for r in self.dropped_offline if lo <= r["t"] < hi]
        camera = sum(v["classes"].get(c, 0) for v in by.values()
                     for c in ("camera_clip", "image", "video"))
        return {"day": day,
                "by_purpose": {p: {"messages": v["messages"], "bytes": v["bytes"],
                                   "classes": dict(v["classes"])} for p, v in by.items()},
                "total_bytes": sum(v["bytes"] for v in by.values()),
                "camera_items_out": camera,
                "blocked": len(blocked),
                "blocked_by_reason": _count(blocked, "reason"),
                "blocked_by_source": _count(blocked, "source"),
                "blocked_destinations": sorted({r["dest"] for r in blocked}),
                "dropped_offline": len(offline),
                "queued_now": len(self.queue),
                "violations": len([v for v in self.violations if lo <= v["t"] < hi])}

    def summary_text(self, day: int, include_routine=True) -> str:
        """Plain-language 'what left the house today' (source for the assistant's answer)."""
        r = self.report(day)
        bp = r["by_purpose"]
        parts = []
        sp = bp.get("speech_recognition_optional", {}).get("messages", 0)
        if sp:
            parts.append(f"{_n(sp)} voice request{'s' if sp != 1 else ''} for speech recognition, not stored")
        llm = bp.get("llm_text_optional", {}).get("messages", 0)
        if llm:
            parts.append(f"{_n(llm)} text question{'s' if llm != 1 else ''} to the cloud assistant")
        bk = bp.get("backup_encrypted", {}).get("messages", 0)
        if bk == 1:
            parts.append("last night's encrypted backup")
        elif bk:
            parts.append(f"{_n(bk)} encrypted backup uploads")
        al = bp.get("alarm", {}).get("classes", {})
        if al.get("alarm_event"):
            k = al["alarm_event"]
            parts.append(f"{_n(k)} alarm signal{'s' if k != 1 else ''} to the monitoring centre")
        if include_routine:
            if al.get("alarm_heartbeat"):
                parts.append(f"{_n(al['alarm_heartbeat'])} routine alarm-line check-ins")
            fu = bp.get("fleet_update", {}).get("messages", 0)
            if fu:
                parts.append(f"{_n(fu)} update check{'s' if fu != 1 else ''}")
        if not parts:
            text = "Nothing left the house today."
        else:
            text = _join(parts) + "."
            text = text[0].upper() + text[1:]
        if r["camera_items_out"] == 0 and parts:
            text += " No camera footage left the house."
        elif r["camera_items_out"] == 0:
            pass
        else:
            text += f" {_n(r['camera_items_out']).capitalize()} camera clips went to the monitoring centre with an alarm."
        if r["blocked"]:
            srcs = ", ".join(sorted(r["blocked_by_source"]))
            text += f" {_n(r['blocked']).capitalize()} attempts to send elsewhere were blocked ({srcs})."
        return text


def _count(rows, key):
    out = defaultdict(int)
    for r in rows:
        out[r[key]] += 1
    return dict(out)


def _join(parts):
    if len(parts) == 1:
        return parts[0]
    sep = ", and " if any("," in p for p in parts) or len(parts) > 2 else " and "
    return ", ".join(parts[:-1]) + sep + parts[-1]
