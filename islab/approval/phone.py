"""Phone and person doubles.

``Phone`` listens on ``notify/<person>``. For a prompt it recomputes the action digest from what it
shows; if that does not match the digest it was sent, it refuses to sign (the hub showed one
thing and asked about another). It signs replies with the key that lives on that phone, and can
be offline for a while (a reply typed while offline goes out when the phone reconnects).

``PersonModel`` decides whether and when a person answers. It is a model with labelled
assumptions, not a measurement of people:

  * availability by time of day: free, busy (work hours), asleep (no answer);
  * load L = prompts this person received in the previous 24 h;
  * P(ignore) = min(0.6, ig0 + ig1 * L)      no answer at all
  * P(hurried) = min(0.9, h0 + h1 * L)       answers without reading: yes with p_yes_hurried
  * careful answers say yes only to the action the person actually wants (an oracle ``truth``);
  * delay: lognormal, median 15 s free / 60 s busy, sigma 0.7; hurried answers are twice as fast.
"""
import hashlib
import math

from islab.core.bus import Envelope

from .ledger import action_digest, sign


class Phone:
    def __init__(self, sim, bus, person, key, decide=None, outages=(), check_display=True):
        self.sim, self.bus, self.person, self.key = sim, bus, person, key
        self.decide = decide
        self.outages = list(outages)            # [(start, end)] phone offline
        self.check_display = check_display
        self.prompts, self.tells, self.sent, self.display_refusals = [], [], [], 0
        bus.subscribe(f"notify/{person}", self._on_notify, name=f"phone.{person}")

    def offline_until(self, t):
        for a, b in self.outages:
            if a <= t < b:
                return b
        return None

    def _on_notify(self, env):
        pl = dict(env.payload or {})
        if self.offline_until(self.sim.now) is not None:
            # delivered when it reconnects (push services queue); model as delayed arrival
            self.sim.at(self.offline_until(self.sim.now), self._on_notify_now, pl)
            return
        self._on_notify_now(pl)

    def _on_notify_now(self, pl):
        pl["received"] = self.sim.now
        if pl.get("kind") not in ("ask", "approval") or not pl.get("request_id"):
            self.tells.append(pl)
            return
        if "action" in pl:
            a = pl["action"]
            pl["display_ok"] = action_digest(pl["request_id"], a.get("actor"), a.get("tool"), a.get("args")) \
                == pl.get("digest")
        self.prompts.append(pl)
        if self.decide is None:
            return
        res = self.decide(self.person, pl)
        if res is None:
            return
        answer, delay = res
        self.sim.after(max(0.0, delay), self.reply, pl, answer)

    def build_reply(self, prompt, answer, request_id=None):
        rid = request_id or prompt["request_id"]
        digest = prompt.get("digest") or hashlib.sha256(prompt.get("text", "").encode()).hexdigest()
        nonce = prompt.get("nonce", "-")
        return {"request_id": rid, "digest": digest, "nonce": nonce, "answer": answer,
                "sig": sign(self.key, rid, digest, nonce, answer)}

    def reply(self, prompt, answer, request_id=None):
        if self.check_display and prompt.get("display_ok") is False:
            self.display_refusals += 1
            return None
        pl = self.build_reply(prompt, answer, request_id)
        back = self.offline_until(self.sim.now)
        if back is not None:
            self.sim.at(back, self._publish, pl)
        else:
            self._publish(pl)
        return pl

    def _publish(self, pl):
        self.sent.append((self.sim.now, pl))
        self.bus.publish(Envelope(f"reply/{self.person}", pl, source=f"phone.{self.person}", ts=self.sim.now))


def lognormal(rng, median, sigma=0.7):
    return math.exp(math.log(median) + sigma * rng.gauss(0, 1))


class PersonModel:
    def __init__(self, rng, availability, truth, h0=0.05, h1=0.03, ig0=0.03, ig1=0.01, p_yes_hurried=0.95,
                 median_free=15.0, median_busy=60.0, prior_load=()):
        self.rng, self.availability, self.truth = rng, availability, truth
        self.h0, self.h1, self.ig0, self.ig1, self.p_yes_hurried = h0, h1, ig0, ig1, p_yes_hurried
        self.median_free, self.median_busy = median_free, median_busy
        self.received = list(prior_load)        # prompt receive times (may be negative: previous days)
        self.log = []                           # (t, request_id, avail, load, outcome, hurried, answer)

    def load(self, t):
        return sum(1 for x in self.received if t - 86400 < x <= t)

    def __call__(self, person, prompt):
        t = prompt.get("received", 0.0)
        L = self.load(t)
        self.received.append(t)
        avail = self.availability(t)
        rid = prompt.get("request_id")
        if avail == "asleep":
            self.log.append((t, rid, avail, L, "asleep", False, None))
            return None
        if self.rng.random() < min(0.6, self.ig0 + self.ig1 * L):
            self.log.append((t, rid, avail, L, "ignored", False, None))
            return None
        hurried = self.rng.random() < min(0.9, self.h0 + self.h1 * L)
        if hurried:
            answer = "yes" if self.rng.random() < self.p_yes_hurried else "no"
        else:
            answer = "yes" if self.truth(person, prompt) else "no"
        med = self.median_busy if avail == "busy" else self.median_free
        delay = lognormal(self.rng, med) * (0.5 if hurried else 1.0)
        self.log.append((t, rid, avail, L, "answered", hurried, answer))
        return answer, delay
