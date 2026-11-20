"""Agent loop and agent doubles for B07.

No language model was run (none could be downloaded here). The planners are doubles:

  * ``B07Planner``: deterministic words-to-calls (B01 ``parse_intents`` plus ``open_garage``).
  * ``InjectionProneLLM``: a seeded, randomized double that reads every untrusted string in its
    context (device names reported by devices, calendar entries, delivery notes) and, with
    probability ``p_follow``, obeys any instruction it finds there. Modes: ``add`` (extra calls),
    ``swap`` (same tool, the injected arguments), ``replace`` (drops the person's request).
    It also emits ``approve_request`` (a tool that does not exist) when told to approve.

``B07Agent`` is the B01 ``Agent`` with the B07 intent table (so ``open_garage`` can be traceable
to a person's words) and a context that carries the calendar and delivery notes, marked
untrusted. It never publishes commands; every call goes to the engine.
"""
import re

from islab.policy.agent import Agent

from .tools import b07_intents, b07_intent_tools


class B07Planner:
    name = "rule"

    def plan(self, text, context):
        return b07_intents(text, context.get("now_s", 0.0))


def untrusted_strings(context, trusted_names=()):
    out = []
    for d in context.get("devices", []):
        name = d.get("reported_name")
        if name and name not in trusted_names:
            out.append(("device_name", name))
    for src in ("calendar", "delivery_notes"):
        for e in context.get(src, []):
            out.append((src, e.get("text", "")))
    return out


class InjectionProneLLM:
    name = "injection_prone_double"

    def __init__(self, rng, p_follow=1.0, mode="add", trusted_names=()):
        assert mode in ("add", "swap", "replace")
        self.rng, self.p_follow, self.mode = rng, p_follow, mode
        self.trusted_names = set(trusted_names)
        self.followed = []          # (source, text, calls)

    def plan(self, text, context):
        now = context.get("now_s", 0.0)
        out = [dict(c) for c in b07_intents(text, now)]
        for src, s in untrusted_strings(context, self.trusted_names):
            inj = b07_intents(s, now)
            if re.search(r"\bapprove", s.lower()):
                inj.append({"name": "approve_request", "arguments": {"request_id": "latest"}})
            if not inj or self.rng.random() >= self.p_follow:
                continue
            self.followed.append((src, s, inj))
            if self.mode == "add":
                out += inj
            elif self.mode == "replace":
                out = list(inj)
            else:
                for c in inj:
                    same = [o for o in out if o["name"] == c["name"]]
                    if same:
                        same[0]["arguments"] = dict(c["arguments"])
                    else:
                        out.append(c)
        return out


class B07Agent(Agent):
    def __init__(self, engine, planner, device_names=None, calendar=(), notes=()):
        super().__init__(engine, planner, device_names=device_names)
        self.calendar, self.notes = list(calendar), list(notes)

    def context(self):
        c = super().context()
        c["now_s"] = self.engine.sim.now
        c["calendar"] = [{"text": x, "trust": "untrusted"} for x in self.calendar]
        c["delivery_notes"] = [{"text": x, "trust": "untrusted"} for x in self.notes]
        return c

    def intent_tools(self, text):
        return b07_intent_tools(text)
