"""Append-only audit log: every proposal, decision, reason, approval and outcome as JSON lines."""
import json


class AuditLog:
    def __init__(self, sim, path=None):
        self.sim = sim
        self.path = path
        self.records = []
        if path:
            open(path, "w").close()

    def write(self, kind, **fields):
        rec = {"t": round(self.sim.now, 4), "kind": kind, **fields}
        self.records.append(rec)
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
        return rec

    def of(self, kind=None, request_id=None):
        return [r for r in self.records
                if (kind is None or r["kind"] == kind) and (request_id is None or r.get("request_id") == request_id)]

    def jsonl(self):
        return "\n".join(json.dumps(r, default=str, sort_keys=True) for r in self.records)
