"""Approval tickets: single-use, expiring, bound to one exact action and its arguments.

A ticket is issued when the hub asks a person. The prompt carries the request id, the action
(tool + arguments), a digest of that action and a fresh nonce. The phone recomputes the digest
from what it shows, and signs ``request_id|digest|nonce|answer`` with a key that lives on that
phone (HMAC-SHA256 here; a real phone would keep the key in its hardware key store). The hub accepts a reply
only if every one of these holds:

  known request · not used before · from a person who was asked · signature valid for that
  person's key · digest equals the ticket's · nonce equals the ticket's · before expiry

and executes only if the action it is about to run still hashes to the approved digest, inside
the action's own time window. Every refusal is counted by reason.
"""
import collections
import hashlib
import hmac
import json
from dataclasses import dataclass, field

REASONS = ("unknown", "replayed", "wrong_person", "bad_signature", "digest_mismatch", "nonce_mismatch",
           "expired", "bad_answer")


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def action_digest(request_id, actor, tool, args):
    return hashlib.sha256(canon([request_id, actor, tool, args]).encode()).hexdigest()


def sign(key, request_id, digest, nonce, answer):
    return hmac.new(key, f"{request_id}|{digest}|{nonce}|{answer}".encode(), hashlib.sha256).hexdigest()


def make_keys(rng, people):
    """Per-phone secret keys from the seeded sim rng (simulation only)."""
    return {p: rng.getrandbits(256).to_bytes(32, "big") for p in people}


@dataclass
class Ticket:
    request_id: str
    kind: str                    # "ask" | "approval" | "rule"
    actor: str
    tool: str
    args: dict
    digest: str
    nonce: str
    issued_at: float
    expires_at: float
    asked: list = field(default_factory=list)   # people prompted so far
    used: bool = False
    answer: str = None
    by: str = None
    via: str = None
    answered_at: float = None
    executed: bool = False


class ApprovalLedger:
    def __init__(self, sim, keys, audit=None):
        self.sim, self.keys, self.audit = sim, dict(keys), audit
        self.tickets = {}
        self.refused = collections.Counter()      # reply refusals by reason
        self.exec_refused = collections.Counter() # execution refusals by reason
        self.refusal_log = []

    def issue(self, request_id, kind, actor, tool, args, expires_at, asked=()):
        nonce = f"{self.sim.rng.getrandbits(64):016x}"
        snap = json.loads(canon(args or {}))     # a frozen copy: later edits to the args object do not count
        t = Ticket(request_id, kind, actor, tool, snap, action_digest(request_id, actor, tool, snap), nonce,
                   self.sim.now, expires_at, list(asked))
        self.tickets[request_id] = t
        return t

    def grant_by_rule(self, request_id, actor, tool, args, rule_id, owner):
        t = self.issue(request_id, "rule", actor, tool, args, self.sim.now, [owner])
        t.used, t.answer, t.by, t.via, t.answered_at = True, "yes", owner, f"standing_rule:{rule_id}", self.sim.now
        return t

    def _refuse(self, reason, request_id, person, where="reply"):
        (self.refused if where == "reply" else self.exec_refused)[reason] += 1
        rec = {"t": self.sim.now, "where": where, "reason": reason, "request_id": request_id, "by": person}
        self.refusal_log.append(rec)
        if self.audit is not None:
            self.audit.write("approval_refused", **rec)
        return False, reason, self.tickets.get(request_id)

    def verify(self, person, payload, now=None):
        """Check a phone reply. Marks the ticket used on success (single use)."""
        now = self.sim.now if now is None else now
        pl = payload or {}
        rid = pl.get("request_id")
        t = self.tickets.get(rid)
        if t is None:
            return self._refuse("unknown", rid, person)
        if t.used:
            return self._refuse("replayed", rid, person)
        if person not in t.asked:
            return self._refuse("wrong_person", rid, person)
        key = self.keys.get(person)
        sig = pl.get("sig")
        want = sign(key, rid, pl.get("digest"), pl.get("nonce"), pl.get("answer")) if key else None
        if not (isinstance(sig, str) and want and hmac.compare_digest(sig, want)):
            return self._refuse("bad_signature", rid, person)
        if pl.get("digest") != t.digest:
            return self._refuse("digest_mismatch", rid, person)
        if pl.get("nonce") != t.nonce:
            return self._refuse("nonce_mismatch", rid, person)
        if now > t.expires_at:
            return self._refuse("expired", rid, person)
        if pl.get("answer") not in ("yes", "no"):
            return self._refuse("bad_answer", rid, person)
        t.used, t.answer, t.by, t.via, t.answered_at = True, pl["answer"], person, "phone", now
        return True, "ok", t

    def mark_voice(self, request_id, person):
        t = self.tickets.get(request_id)
        if t and not t.used and t.kind == "ask":
            t.used, t.answer, t.by, t.via, t.answered_at = True, "yes", person, "voice", self.sim.now

    def check_execution(self, request_id, actor, tool, args):
        """Right before execution: the action must still be exactly what was approved, once."""
        t = self.tickets.get(request_id)
        if t is None or not t.used or t.answer != "yes":
            return self._refuse("not_approved", request_id, actor, "execute")
        if t.executed:
            return self._refuse("already_executed", request_id, actor, "execute")
        if action_digest(request_id, actor, tool, args) != t.digest:
            return self._refuse("action_changed_after_approval", request_id, actor, "execute")
        t.executed = True
        return True, "ok", t
