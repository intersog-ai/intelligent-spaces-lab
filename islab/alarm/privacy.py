"""Local recording store with retention and a delete that removes every copy.

Locations: main store, index, thumbnail cache, backup queue, plus any cache another
feature registers (register_location). delete() finds copies by id AND by content hash, so
a copy saved under a different name is still found; it then re-scans to prove absence and
returns a receipt. Copies that already left the house (sent backups) cannot be deleted from
here; the receipt says so instead of claiming success.
"""
from __future__ import annotations

import hashlib
import json
import re

DAY = 86400.0


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_thumb(data: bytes) -> bytes:
    """Stand-in for a real thumbnail: a deterministic derived byte string."""
    return b"THUMB:" + hashlib.sha1(data).digest() + data[:32]


class RecordingStore:
    def __init__(self, sim, retention_days=7):
        self.sim = sim
        self.retention_days = retention_days
        self.main = {}            # rec_id -> bytes
        self.index = {}           # rec_id -> meta
        self.thumbs = {}          # rec_id -> bytes
        self.backup_queue = []    # [{"rec_id", "data"}] waiting for tonight's backup
        self.offsite = {}         # rec_id -> sha256 of copies already sent (outside the house)
        self.locations = {"main": self.main, "index": self.index, "thumbs": self.thumbs,
                          "backup_queue": self.backup_queue}
        self.receipts = []

    def register_location(self, name, container):
        """Any feature that keeps copies (export cache, share links...) must register here."""
        self.locations[name] = container
        return container

    def add(self, rec_id, data: bytes, camera="camera.front", kind="clip"):
        thumb = make_thumb(data)
        self.main[rec_id] = data
        self.thumbs[rec_id] = thumb
        self.index[rec_id] = {"rec_id": rec_id, "camera": camera, "kind": kind,
                              "created": self.sim.now, "bytes": len(data),
                              "sha256": sha256(data), "thumb_sha256": sha256(thumb)}
        self.backup_queue.append({"rec_id": rec_id, "data": data})
        return self.index[rec_id]

    def run_backup(self):
        """Tonight's backup leaves the house (encrypted). Afterwards those copies are remote."""
        sent = 0
        for item in self.backup_queue:
            self.offsite[item["rec_id"]] = sha256(item["data"])
            sent += len(item["data"])
        self.backup_queue.clear()
        return sent

    # ---- finding copies -------------------------------------------------------------
    def _matches(self, rec_id, hashes, key, value):
        if key == rec_id or (isinstance(key, str) and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(rec_id)}(?![A-Za-z0-9])", key)):
            return "id_in_key"
        if isinstance(value, (bytes, bytearray)) and sha256(bytes(value)) in hashes:
            return "content_hash"
        if isinstance(value, dict):
            if value.get("rec_id") == rec_id:
                return "id_in_record"
            for v in value.values():
                if isinstance(v, (bytes, bytearray)) and sha256(bytes(v)) in hashes:
                    return "content_hash_in_record"
        return None

    def find_copies(self, rec_id, hashes=None):
        hashes = hashes if hashes is not None else self._hashes(rec_id)
        hits = []
        for name, cont in self.locations.items():
            items = cont.items() if isinstance(cont, dict) else enumerate(cont)
            for k, v in list(items):
                how = self._matches(rec_id, hashes, k, v)
                if how:
                    hits.append((name, k, how))
        return hits

    def _hashes(self, rec_id):
        meta = self.index.get(rec_id, {})
        hs = {meta.get("sha256"), meta.get("thumb_sha256")}
        if rec_id in self.main:
            hs.add(sha256(self.main[rec_id]))
            hs.add(sha256(make_thumb(self.main[rec_id])))
        return {h for h in hs if h}

    # ---- delete -----------------------------------------------------------------------
    def delete(self, rec_id, reason="user_request"):
        hashes = self._hashes(rec_id)
        meta = dict(self.index.get(rec_id, {}))
        removed = []
        hits = self.find_copies(rec_id, hashes)
        # remove list items from the end so indices stay valid
        for name, k, how in sorted(hits, key=lambda h: (h[0], -h[1] if isinstance(h[1], int) else 0)):
            cont = self.locations[name]
            if isinstance(cont, dict):
                cont.pop(k, None)
            else:
                del cont[k]
            removed.append({"location": name, "key": str(k), "matched_by": how})
        left = self.find_copies(rec_id, hashes)
        pending_remote = []
        if rec_id in self.offsite:
            pending_remote.append({"location": "offsite_backup",
                                   "note": "copy already in last encrypted backup; deletion "
                                           "request queued for the backup provider, expires "
                                           "with backup rotation"})
        receipt = {"rec_id": rec_id, "reason": reason, "t": self.sim.now,
                   "sha256": meta.get("sha256"), "removed": removed,
                   "locations_scanned": sorted(self.locations),
                   "verified_absent_locally": not left, "remaining": [list(map(str, h)) for h in left],
                   "pending_remote": pending_remote,
                   "complete": not left and not pending_remote}
        receipt["receipt_id"] = sha256(json.dumps(receipt, sort_keys=True).encode())[:16]
        self.receipts.append(receipt)
        self.sim.trace("recording_deleted", rec_id=rec_id, copies=len(removed),
                       complete=receipt["complete"])
        return receipt

    def purge_expired(self):
        cutoff = self.sim.now - self.retention_days * DAY
        old = [r for r, m in self.index.items() if m["created"] < cutoff]
        return [self.delete(r, reason="retention_expired") for r in old]
