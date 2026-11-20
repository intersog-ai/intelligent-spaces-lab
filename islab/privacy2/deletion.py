"""Deletion with a receipt, for data that may belong to more than one person.

``SharedStore`` extends the B01 ``RecordingStore`` (islab/alarm/privacy.py, unchanged):

* Owners. A record can belong to several people. A delete request from one owner asks every
  other owner first (``notify/<person>`` at home, a push through the egress gate when away).
  Nothing shared is deleted without a yes from each other owner. "No" or no answer within the
  timeout keeps the record, and the requester gets a receipt that says so.
* Every copy. The B01 delete scans registered locations by id and content hash. Here the scan
  covers the whole hub disk (``disk``: registered and unregistered containers), by id, content
  hash, containment inside a larger blob, and recorded lineage (``derive``) for copies whose
  bytes differ (re-encoded, cropped). A copy made outside ``derive`` with different bytes cannot
  be found by any of these; the receipt lists that as not checked instead of claiming it.
* Backups. Each record is encrypted for the backup with its own key, kept at home
  (``keys="per_record"``). Deleting the record destroys the key, so the copy that already left
  becomes unreadable at once; the ciphertext itself expires with backup rotation. With one key
  per night (``keys="nightly"``, the B01 shape) a backup copy stays readable until rotation and
  the receipt says so.

Scan time on the hub is modelled (bytes scanned / ``disk_rate`` + a per-location cost); it is
an assumption, not a measurement.
"""
from __future__ import annotations

import hashlib
import json

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from islab.alarm.privacy import RecordingStore, make_thumb, sha256
from islab.core.bus import Envelope
from islab.safety.notify import PUSH_CLASS, PUSH_PURPOSE, PUSH_RELAY

DAY = 86400.0
NOT_CHECKED = [
    "copies with different bytes made without recorded lineage (re-encoded outside the store)",
    "copies on phones, laptops or anywhere off the hub disk",
    "copies a person exported or shared before the request",
]


class BackupProvider:
    """Outside the house: keeps ciphertext only, rotates after ``rotation_days``."""

    def __init__(self, rotation_days=30):
        self.rotation_days = rotation_days
        self.blobs = {}       # rec_id -> {"nonce", "ct", "key_id", "t"}

    def put(self, rec_id, nonce, ct, key_id, t):
        self.blobs[rec_id] = {"nonce": nonce, "ct": ct, "key_id": key_id, "t": t}

    def rotate(self, now):
        cut = now - self.rotation_days * DAY
        for r in [r for r, b in self.blobs.items() if b["t"] < cut]:
            del self.blobs[r]


class SharedStore(RecordingStore):
    NAME = "privacy.store"

    def __init__(self, sim, bus=None, gate=None, people_home=None, keys="per_record",
                 retention_days=7, ask_timeout_s=DAY, disk_rate=200e6, per_location_s=0.005,
                 provider=None):
        super().__init__(sim, retention_days=retention_days)
        self.bus, self.gate = bus, gate
        self.people_home = dict(people_home or {"anna": True, "mark": False, "sam": True})
        self.keys_mode = keys
        self.ask_timeout_s = ask_timeout_s
        self.disk_rate, self.per_location_s = disk_rate, per_location_s
        self.provider = provider or BackupProvider()
        self.keystore = {}          # key_id -> key bytes (at home; never in the backup)
        self.owners = {}            # rec_id -> set of people
        self.lineage = {}           # rec_id -> [{"location", "key", "sha256"}]
        self.disk = dict(self.locations)     # everything on the hub disk, registered or not
        self.requests = {}          # request_id -> state
        self.push_handlers = {}     # person -> callable(text, request_id) for away phones
        self._req_n = 0
        if bus is not None:
            bus.subscribe("reply/+", self._on_reply, name=self.NAME)

    # ---- storage ------------------------------------------------------------------------
    def register_location(self, name, container):
        self.disk[name] = container
        return super().register_location(name, container)

    def unregistered_location(self, name, container):
        """A container on the disk that no feature registered (a forgotten cache, a debug dump)."""
        self.disk[name] = container
        return container

    def add(self, rec_id, data: bytes, owners=("anna",), camera="camera.front", kind="clip"):
        meta = super().add(rec_id, data, camera=camera, kind=kind)
        self.owners[rec_id] = set(owners)
        meta["owners"] = sorted(owners)
        return meta

    def derive(self, rec_id, location, key, data: bytes):
        """Store a derived copy (re-encoded, cropped) and record where it went."""
        cont = self.disk[location]
        if isinstance(cont, dict):
            cont[key] = data
        else:
            cont.append({"key": key, "data": data})
        self.lineage.setdefault(rec_id, []).append({"location": location, "key": key,
                                                    "sha256": sha256(data)})

    # ---- backup -------------------------------------------------------------------------
    def _new_key(self):
        return bytes(self.sim.rng.getrandbits(8) for _ in range(16))

    def run_backup(self):
        night = int(self.sim.now // DAY)
        sent = 0
        night_key_id = f"night:{night}"
        if self.keys_mode == "nightly" and night_key_id not in self.keystore:
            self.keystore[night_key_id] = self._new_key()
        for item in self.backup_queue:
            rid, data = item["rec_id"], item["data"]
            key_id = night_key_id if self.keys_mode == "nightly" else f"rec:{rid}"
            if key_id not in self.keystore:
                self.keystore[key_id] = self._new_key()
            nonce = bytes(self.sim.rng.getrandbits(8) for _ in range(12))
            ct = AESGCM(self.keystore[key_id]).encrypt(nonce, data, rid.encode())
            self.provider.put(rid, nonce, ct, key_id, self.sim.now)
            self.offsite[rid] = sha256(data)
            sent += len(ct)
        self.backup_queue.clear()
        if self.gate is not None and sent:
            self.gate.send("backup.agent", "backup.storage.example", "backup_encrypted",
                           "encrypted_blob", size=sent, meta={"encrypted": True},
                           msg_id=f"backup:{night}")
        return sent

    def restore(self, rec_id):
        """Try to read a record back from the provider with the keys still at home."""
        b = self.provider.blobs.get(rec_id)
        if b is None or b["key_id"] not in self.keystore:
            return None
        try:
            return AESGCM(self.keystore[b["key_id"]]).decrypt(b["nonce"], b["ct"], rec_id.encode())
        except InvalidTag:
            return None

    # ---- requests with consent ------------------------------------------------------------
    def request_delete(self, rec_id, requester, reason="user_request"):
        self._req_n += 1
        rid = f"del{self._req_n:05d}"
        others = sorted(self.owners.get(rec_id, set()) - {requester})
        st = {"request_id": rid, "rec_id": rec_id, "requester": requester, "reason": reason,
              "t_request": self.sim.now, "asked": others, "answers": {}, "done": False}
        self.requests[rid] = st
        if requester not in self.owners.get(rec_id, {requester}):
            return self._finish(st, "refused_not_an_owner")
        if not others:
            return self._delete_after_scan(st)
        text = (f"{requester.capitalize()} asked to delete a recording you share "
                f"({self.index.get(rec_id, {}).get('kind', 'item')}). Delete it? yes or no")
        for person in others:
            self._ask(person, text, rid)
        self.sim.after(self.ask_timeout_s, self._timeout, rid)
        return rid

    def _ask(self, person, text, rid):
        if self.bus is not None:
            self.bus.publish(Envelope(topic=f"notify/{person}",
                                      payload={"text": text, "kind": "ask", "request_id": rid,
                                               "tag": "delete_ask"},
                                      source=self.NAME, ts=self.sim.now))
        if not self.people_home.get(person, True) and self.gate is not None:
            handler = self.push_handlers.get(person)
            self.gate.send("privacy.notify", PUSH_RELAY, PUSH_PURPOSE, PUSH_CLASS,
                           data=text.encode(), meta={"person": person},
                           transport=(lambda _d, h=handler: h(text, rid)) if handler else None)

    def _on_reply(self, env):
        p = env.payload or {}
        st = self.requests.get(p.get("request_id"))
        person = env.topic.split("/", 1)[1]
        if st is None or st["done"] or person not in st["asked"] or person in st["answers"]:
            return
        st["answers"][person] = {"answer": p.get("answer"), "t": self.sim.now}
        if p.get("answer") != "yes":
            self._finish(st, "declined")
        elif all(st["answers"].get(o, {}).get("answer") == "yes" for o in st["asked"]):
            self._delete_after_scan(st)

    def _timeout(self, rid):
        st = self.requests[rid]
        if not st["done"] and not st.get("scanning"):
            self._finish(st, "no_answer")

    # ---- the delete itself ----------------------------------------------------------------
    def _delete_after_scan(self, st):
        st["scanning"] = True
        disk_bytes = sum(_size(c) for c in self.disk.values())
        dt = disk_bytes / self.disk_rate + self.per_location_s * len(self.disk)
        self.sim.after(dt, self._do_delete, st)
        return st["request_id"]

    def _hashes(self, rec_id):
        hs = super()._hashes(rec_id)
        hs |= {c["sha256"] for c in self.lineage.get(rec_id, [])}
        return hs

    def _match_full(self, rec_id, hashes, data, key, value):
        how = self._matches(rec_id, hashes, key, value)
        if how:
            return how
        blobs = []
        if isinstance(value, (bytes, bytearray)):
            blobs = [bytes(value)]
        elif isinstance(value, dict):
            blobs = [bytes(v) for v in value.values() if isinstance(v, (bytes, bytearray))]
        if data and len(data) >= 64 and any(data in b for b in blobs):
            return "contained_in_blob"
        return None

    def scan(self, rec_id, hashes=None, data=None):
        hashes = hashes if hashes is not None else self._hashes(rec_id)
        data = data if data is not None else self.main.get(rec_id)
        hits = []
        for name, cont in self.disk.items():
            items = cont.items() if isinstance(cont, dict) else enumerate(cont)
            for k, v in list(items):
                how = self._match_full(rec_id, hashes, data, k, v)
                if how:
                    hits.append((name, k, how))
        return hits

    def _do_delete(self, st):
        rec_id = st["rec_id"]
        data = self.main.get(rec_id)
        hashes = self._hashes(rec_id)
        meta = dict(self.index.get(rec_id, {}))
        hits = self.scan(rec_id, hashes, data)
        removed = []
        by_cont = {}
        for name, k, how in hits:
            by_cont.setdefault(name, []).append((k, how))
        for name, lst in by_cont.items():
            cont = self.disk[name]
            if isinstance(cont, dict):
                for k, how in lst:
                    if how == "contained_in_blob":
                        cont[k] = _cut(cont[k], data)
                    else:
                        cont.pop(k, None)
                    removed.append({"location": name, "key": str(k), "matched_by": how,
                                    "registered": name in self.locations})
            else:
                for k, how in sorted(lst, key=lambda x: -x[0]):
                    if how == "contained_in_blob":
                        cont[k] = _cut(cont[k], data)
                    else:
                        del cont[k]
                    removed.append({"location": name, "key": str(k), "matched_by": how,
                                    "registered": name in self.locations})
        self.lineage.pop(rec_id, None)
        self.owners.pop(rec_id, None)
        left = self.scan(rec_id, hashes, data)
        backup = self._backup_statement(rec_id)
        st["removed"] = removed
        st["remaining"] = [list(map(str, h)) for h in left]
        st["backup"] = backup
        st["sha256"] = meta.get("sha256")
        return self._finish(st, "deleted")

    def _backup_statement(self, rec_id):
        b = self.provider.blobs.get(rec_id)
        if b is None:
            return {"in_backup": False, "statement": "No copy had left in a backup."}
        expires = b["t"] + self.provider.rotation_days * DAY
        if b["key_id"].startswith("rec:"):
            self.keystore.pop(b["key_id"], None)
            readable = self.restore(rec_id) is not None
            return {"in_backup": True, "key_destroyed": True, "restore_after_delete": readable,
                    "ciphertext_expires_t": expires,
                    "statement": ("A copy is in an encrypted backup at backup.storage.example. "
                                  "Its key was destroyed at home, so it can no longer be read. "
                                  f"The unreadable copy expires with rotation on day {int(expires // DAY)}.")}
        readable = self.restore(rec_id) is not None
        return {"in_backup": True, "key_destroyed": False, "restore_after_delete": readable,
                "ciphertext_expires_t": expires,
                "statement": ("A copy is in an encrypted backup at backup.storage.example. It shares "
                              "a key with the rest of that night's backup, so it stays readable "
                              f"until rotation on day {int(expires // DAY)}.")}

    def _finish(self, st, status):
        if st["done"]:
            return st["request_id"]
        st["done"] = True
        st["scanning"] = False
        rec = {"request_id": st["request_id"], "rec_id": st["rec_id"], "status": status,
               "requester": st["requester"], "reason": st["reason"],
               "t_request": st["t_request"], "t_receipt": self.sim.now,
               "asked": st["asked"], "answers": st["answers"]}
        if status == "deleted":
            rec.update({"sha256": st.get("sha256"), "removed": st["removed"],
                        "locations_scanned": sorted(self.disk),
                        "matched_by": ["id", "content hash", "contained in a larger file",
                                       "recorded lineage"],
                        "remaining_found": st["remaining"],
                        "no_copy_found_on_hub_disk": not st["remaining"],
                        "backup": st["backup"],
                        "not_checked": list(NOT_CHECKED)})
        elif status == "declined":
            who = [p for p, a in st["answers"].items() if a["answer"] != "yes"]
            rec["statement"] = f"Not deleted: {', '.join(who)} said no. Nothing was removed."
        elif status == "no_answer":
            rec["statement"] = "Not deleted: no answer from every other owner in time. Nothing was removed."
        else:
            rec["statement"] = "Not deleted: the requester does not own this recording."
        rec["receipt_id"] = sha256(json.dumps(rec, sort_keys=True, default=str).encode())[:16]
        self.receipts.append(rec)
        self.sim.trace("privacy_receipt", rec_id=st["rec_id"], status=status)
        if self.bus is not None:
            self.bus.publish(Envelope(topic=f"notify/{st['requester']}",
                                      payload={"text": f"Deletion receipt {rec['receipt_id']}: {status}.",
                                               "kind": "tell", "tag": "delete_receipt"},
                                      source=self.NAME, ts=self.sim.now))
        return st["request_id"]

    def receipt(self, request_id):
        for r in self.receipts:
            if r.get("request_id") == request_id:
                return r
        return None


class ConsentResponder:
    """A person who answers delete requests: ``answer`` is "yes", "no" or None (never answers)."""

    def __init__(self, sim, bus, person, store, delay_s, answer="yes"):
        self.sim, self.bus, self.person = sim, bus, person
        self.delay_s, self.answer = delay_s, answer
        self.asked = []
        if store.people_home.get(person, True):
            bus.subscribe(f"notify/{person}", self._on_notify, name=f"person.{person}")
        else:
            store.push_handlers[person] = self._on_push

    def _on_notify(self, env):
        p = env.payload or {}
        if p.get("kind") == "ask" and p.get("tag") == "delete_ask":
            self._got(p["request_id"])

    def _on_push(self, text, request_id):
        self._got(request_id)

    def _got(self, rid):
        self.asked.append((self.sim.now, rid))
        if self.answer is None:
            return
        self.sim.after(self.delay_s, lambda: self.bus.publish(Envelope(
            topic=f"reply/{self.person}", payload={"request_id": rid, "answer": self.answer},
            source=f"phone.{self.person}", ts=self.sim.now)))


def _size(cont):
    vals = cont.values() if isinstance(cont, dict) else cont
    n = 0
    for v in vals:
        if isinstance(v, (bytes, bytearray)):
            n += len(v)
        elif isinstance(v, dict):
            n += sum(len(x) for x in v.values() if isinstance(x, (bytes, bytearray)))
    return n


def _cut(value, data):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).replace(data, b"")
    if isinstance(value, dict):
        return {k: (bytes(v).replace(data, b"") if isinstance(v, (bytes, bytearray)) else v)
                for k, v in value.items()}
    return value


def transcode(data: bytes) -> bytes:
    """Stand-in for a re-encode: different bytes, same picture."""
    return b"RECODE:" + hashlib.sha256(data).digest() + data[::2]


__all__ = ["SharedStore", "BackupProvider", "ConsentResponder", "transcode", "make_thumb",
           "NOT_CHECKED"]
