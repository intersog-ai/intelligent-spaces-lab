"""Standing rules: approvals a person sets in advance, with limits.

A rule names one tool, who may ask (actors), argument constraints, days and a time window, a
daily use limit, a longest window for time-bound actions, and optionally a condition from the
household's own records (``only_if_expected``: a delivery the household entered itself, never a
delivery note or a calendar invite from outside). A rule is set on a phone by an approver (its
``owner``) and is shown to that person every time it is used.

Deliberately not offered: a rule that lets a *voice* request unlock a door (B01 measured a
replayed recording accepted as the speaker in 81% of trials at the room threshold).
"""
from dataclasses import dataclass, field

from .tools import hhmm_to_s, until_abs, valid_hhmm


@dataclass
class StandingRule:
    rule_id: str
    owner: str
    tool: str
    actors: list                                   # who may ask: person names, "visitor", "automation"
    args: dict = field(default_factory=dict)       # exact values required
    weekdays: list = None                          # 0=Mon..6=Sun; None = every day
    window: tuple = None                           # ("HH:MM", "HH:MM") when the request may arrive
    max_per_day: int = None
    max_window_s: float = None                     # for open_garage: until - now at most this
    only_if_expected: bool = False
    channels: list = None                          # allowed channels; None = any

    def limited(self):
        return any([self.weekdays is not None, self.window is not None, self.max_per_day is not None,
                    self.max_window_s is not None, self.only_if_expected])


class StandingRules:
    def __init__(self, rules=(), weekday_of=None):
        self.rules = list(rules)
        self.weekday_of = weekday_of or (lambda now: int(now // 86400) % 7)
        self.uses = {}                 # (rule_id, day) -> count
        self.expected = set()          # (day, purpose) entered by the household (trusted)
        self.grants = []               # (t, rule_id, request_id)

    def expect(self, day, purpose="delivery"):
        self.expected.add((int(day), purpose))

    def match(self, actor, tool, args, now, channel=None):
        day = int(now // 86400)
        tod = now % 86400
        for r in self.rules:
            if r.tool != tool or actor not in r.actors:
                continue
            if r.channels is not None and channel not in r.channels:
                continue
            if any((args or {}).get(k) != v for k, v in r.args.items()):
                continue
            if r.weekdays is not None and self.weekday_of(now) not in r.weekdays:
                continue
            if r.window is not None:
                a, b = hhmm_to_s(r.window[0]), hhmm_to_s(r.window[1])
                if not (a <= tod <= b):
                    continue
            if r.max_per_day is not None and self.uses.get((r.rule_id, day), 0) >= r.max_per_day:
                continue
            if r.max_window_s is not None:
                u = (args or {}).get("until")
                if not valid_hhmm(u) or until_abs(u, now) - now > r.max_window_s:
                    continue
            if r.only_if_expected and (day, (args or {}).get("purpose", "delivery")) not in self.expected:
                continue
            return r
        return None

    def use(self, rule, now, request_id):
        k = (rule.rule_id, int(now // 86400))
        self.uses[k] = self.uses.get(k, 0) + 1
        self.grants.append((now, rule.rule_id, request_id))
