"""Trust policy: what a home may do with knowing who is speaking.

    pol = TrustPolicy()                                   # household.yaml
    ev = pol.evidence({"anna": 0.84, "mark": 0.61, "sam": 0.58}, live=True)
    pol.tier(ev)                                          # "named" | "comfort" | "guest"
    v = pol.personal(ev, "calendar")                      # Verdict(kind, person, say)
    p = pol.proposal(ev, "unlock_door", {"door": "front"}, text="unlock the front door")
    engine.propose(p)                                     # level 3 still needs a phone approval

Rules (fixed in code, not in the YAML):
  * Voice can make a request *personal* and can raise a speaker from guest to a named person.
  * Voice never authorizes a level-3 action. ``proposal`` only names the requester; the decision and the
    phone approval belong to ``islab.policy.PolicyEngine``. ``authorizes_level3`` is always False.
  * A failed liveness check or overlapping voices caps the tier at "comfort": the request is treated
    as the household's, not as a named person's.
  * When a family member is not recognised for private data, the home does not read it out loud; it
    offers to send it to that person's phone, where the phone (not the voice) decides who sees it.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

from islab.policy.engine import Proposal, load_policy

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "household.yaml")
TIERS = ("guest", "comfort", "named")          # increasing trust
LEVEL3 = ("unlock_door", "open_garage", "disarm_alarm", "open_valve", "share_camera_clip")


def load_config(path: str = CONFIG_PATH) -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


@dataclass
class Evidence:
    """What the hub knows about one utterance. ``speaker`` is the best-scoring enrolled person (or None)."""
    speaker: str | None
    score: float
    margin: float = 1.0           # best score minus second-best score
    live: bool | None = None      # liveness check result; None = not run
    overlap: bool = False         # two voices suspected
    scores: dict = field(default_factory=dict)


@dataclass
class Verdict:
    kind: str                     # serve | generic | to_phone | refuse
    person: str | None            # whose data / preferences were used (None = house defaults)
    say: str                      # what the home says out loud
    tier: str = "guest"


class TrustPolicy:
    def __init__(self, config: dict | None = None):
        self.cfg = copy.deepcopy(config if config is not None else load_config())
        self.people = dict(self.cfg.get("people", {}))
        self.tiers = self.cfg.get("tiers", {})

    # ---- identity ------------------------------------------------------------------------------
    def evidence(self, scores: dict, live: bool | None = None, overlap: bool | None = None) -> Evidence:
        """Evidence from cosine scores against every enrolled profile. If ``overlap`` is None it is
        inferred: the second-best score is at or above ``overlap.second_score`` (two known voices at once)."""
        if not scores:
            return Evidence(None, 0.0, 0.0, live, bool(overlap), {})
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best, s1 = ranked[0]
        s2 = ranked[1][1] if len(ranked) > 1 else -1.0
        if overlap is None:
            thr = (self.cfg.get("overlap") or {}).get("second_score")
            overlap = thr is not None and s2 >= float(thr)
        return Evidence(best, float(s1), float(s1 - s2), live, bool(overlap), dict(scores))

    def _passes(self, ev: Evidence, tier: str) -> bool:
        t = self.tiers[tier]
        return ev.speaker is not None and ev.score >= float(t["threshold"]) and ev.margin >= float(t.get("margin", 0))

    def tier(self, ev: Evidence) -> str:
        """Highest tier the evidence supports."""
        need_live = set(self.cfg.get("require_live_for", ["named"]))
        if self._passes(ev, "named") and not ev.overlap and not (ev.live is False and "named" in need_live):
            return "named"
        if self._passes(ev, "comfort") and not (ev.live is False and "comfort" in need_live):
            return "comfort"
        return "guest"

    def who(self, ev: Evidence, tier: str) -> str | None:
        """The person the home treats as speaking at a given tier (None = guest / house)."""
        return ev.speaker if TIERS.index(self.tier(ev)) >= TIERS.index(tier) else None

    # ---- personalization (no device action) --------------------------------------------------
    def personal(self, ev: Evidence, scope: str, claimed: str | None = None) -> Verdict:
        need = self.cfg.get("scopes", {}).get(scope, "named")
        t = self.tier(ev)
        person = ev.speaker if TIERS.index(t) >= TIERS.index(need) else None
        d = self.cfg.get("house_defaults", {})
        if scope == "music":
            pl = self.people.get(person, {}).get("playlist") if person else None
            if pl:
                return Verdict("serve", person, f"Playing {pl}.", t)
            return Verdict("generic", None, f"Playing {d.get('playlist')}.", t)
        if scope == "temperature":
            if person:
                c = self.people[person].get("climate_c", d.get("climate_c"))
                return Verdict("serve", person, f"Setting it to {c} degrees, the way {person.capitalize()} likes it.", t)
            return Verdict("generic", None, f"Setting it to the house default, {d.get('climate_c')} degrees.", t)
        # private scopes: calendar, messages
        if person:
            return Verdict("serve", person, f"{person.capitalize()}, here is your {scope}.", t)
        if ev.overlap:
            return Verdict("refuse", None, "I heard more than one voice. Ask me again on your own, "
                                           f"or open your {scope} in the app.", t)
        if ev.live is False:
            return Verdict("refuse", None, f"I can't read a {scope} out loud for that request. "
                                           f"Open it in the app on your phone.", t)
        # Not sure who it is: do not read it out loud; offer the person's own phone.
        return Verdict("to_phone", None, f"I'm not sure who's asking, so I won't read a {scope} out loud. "
                                         f"Tell me your name and I'll send it to your phone.", t)

    # ---- actions (handed to the policy engine) -----------------------------------------------
    def proposal(self, ev: Evidence, tool: str, args: dict, text: str = "", origin: str = "b06.trust") -> Proposal:
        """Build the PolicyEngine proposal. The requester is the named person only at the 'named' tier;
        otherwise the engine sees an unrecognised voice (guest). Confidence is passed through unchanged
        so the engine's own min_confidence also applies. Level 3 is decided by the engine (phone approval)."""
        named = self.who(ev, "named")
        return Proposal(tool=tool, args=dict(args), requester=named, confidence=(ev.score if named else None),
                        channel="voice", explicit=True, text=text, origin=origin)

    @staticmethod
    def authorizes_level3(ev: Evidence, tool: str) -> bool:
        """Voice evidence alone authorizes a level-3 action: never."""
        return False

    # ---- engine configuration ----------------------------------------------------------------
    def engine_config(self, base: dict | None = None) -> dict:
        """policy.yaml (or ``base``) with this household's people, permissions, approvers and levels,
        and voice.min_confidence = the 'named' threshold."""
        cfg = copy.deepcopy(base if base is not None else load_policy())
        cfg["people"] = {p: {"role": v.get("role", "adult")} for p, v in self.people.items()}
        cfg.setdefault("levels", {}).update(self.cfg.get("levels", {}))
        perms = cfg.setdefault("permissions", {})
        for who, rule in (self.cfg.get("permissions") or {}).items():
            perms[who] = copy.deepcopy(rule)
        cfg.setdefault("approvers", {}).update(copy.deepcopy(self.cfg.get("approvers", {})))
        cfg.setdefault("voice", {})["min_confidence"] = float(self.tiers["named"]["threshold"])
        return cfg
