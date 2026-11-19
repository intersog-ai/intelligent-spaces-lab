"""'What happened', built only from journal records, and an automatic checker for it.

``explain(records)`` returns statements ``{"text", "refs"}``. Every statement cites the record ids
(``rid``) it was built from; nothing is added that is not in a record. Times are shown on the house
clock (``origin`` = the house time at virtual t = 0).

``check(statements, records)`` re-reads each statement independently of the generator: what kind of
fact it states (from keywords), every clock time, every percentage, every flow figure, every quoted
message and every count must match a field of a cited record of the right kind. A statement with no
citation, a citation to a missing record, or any token that no cited record supports is flagged.
It checks faithfulness to the record, not truth about the world: a record that is itself a claim
(``claimed_closed``) yields a faithful statement that may still be false. Experiments check truth
against the twin separately.
"""
from __future__ import annotations

import re

CLOCK0 = 9 * 3600 + 10 * 60          # virtual t = 0 is 09:10:00 house time

STATE_WORDS = {"leak_detected": "leak detected", "closing": "closing the valve",
               "close_failed": "valve not confirmed", "escalated": "escalated",
               "closed_confirmed": "closed and confirmed", "idle": "idle"}


def hms(t, origin=CLOCK0):
    s = int(origin + t)
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def hm(t, origin=CLOCK0):
    return hms(t, origin)[:5]


def _num(x):
    return f"{x:g}"


def _flow(x):
    return f"{x:.1f}"


def explain(records, origin=CLOCK0):
    out = []
    S = lambda text, *recs: out.append({"text": text, "refs": [r["rid"] for r in recs]})  # noqa: E731
    sends = [r for r in records if r["kind"] in ("cmd_sent", "cmd_resent")]
    first_send = sends[0] if sends else None
    more = sends[1:]
    for r in records:
        k, t = r["kind"], r["t"]
        if k == "leak_detected":
            how = "in a status report" if r.get("via") == "state" else "in a leak alert"
            S(f"At {hm(t, origin)} the sink sensor reported water ({how}).", r)
        elif r is first_send:
            S(f"The valve was told to close at {hms(t, origin)}.", r)
            if more:
                S(f"The close command was sent {len(more)} more time{'s' if len(more) != 1 else ''}, the last at "
                  f"{hms(more[-1]['t'], origin)}.", *more)
        elif k == "claimed_closed":
            how = ("when it sent the command" if r.get("basis") == "sent"
                   else "when the valve acknowledged the command")
            S(f"The hub marked the valve closed at {hms(t, origin)}, {how}, without reading the valve.", r)
        elif k == "closed_confirmed":
            S(f"It was confirmed closed at {hms(t, origin)}: the valve reported position "
              f"{_num(r['position'])}% and the flow meter read {_flow(r['flow_lpm'])} litres a minute.", r)
        elif k == "close_failed":
            if r.get("reason") == "no_ack":
                S(f"By {hms(t, origin)} the valve had not answered the command.", r)
            elif r.get("reason") == "rejected":
                S(f"At {hms(t, origin)} the valve refused the command.", r)
            else:
                parts = []
                if r.get("last_position") is not None:
                    parts.append(f"position {_num(r['last_position'])}%")
                if r.get("last_flow_lpm") is not None:
                    parts.append(f"flow {_flow(r['last_flow_lpm'])} litres a minute")
                last = f" (last readings: {', '.join(parts)})" if parts else ""
                S(f"By {hms(t, origin)} the valve had not confirmed closed{last}.", r)
        elif k == "told":
            if r.get("tag") == "escalated":
                S(f"At {hms(t, origin)} people were told to turn off the water by hand: \"{r['text']}\"", r)
            else:
                S(f"People were told at {hms(t, origin)}: \"{r['text']}\"", r)
        elif k == "alarm_raised":
            what = "A second leak alarm, for the valve," if r["zone"] == 12 else "The leak alarm"
            S(f"{what} was raised to the monitoring centre at {hms(t, origin)}.", r)
        elif k == "alarm_acked":
            what = "valve alarm" if r["zone"] == 12 else "leak alarm"
            S(f"The monitoring centre acknowledged the {what} at {hms(r['t_ack'], origin)}.", r)
        elif k == "resumed":
            S(f"The hub restarted and at {hms(t, origin)} resumed from its journal "
              f"({STATE_WORDS.get(r['from_state'], r['from_state'])}).", r)
    return out


def render(statements):
    return " ".join(s["text"] for s in statements)


# ---- checker -------------------------------------------------------------------------------
# (pattern, record kinds that can support it). Order matters: negative forms first.
_FACTS = [
    (r"had not confirmed closed|had not answered|refused the command", {"close_failed"}),
    (r"reported water|showed water", {"leak_detected"}),
    (r"told to close|close command was sent", {"cmd_sent", "cmd_resent"}),
    (r"marked the valve closed", {"claimed_closed"}),
    (r"confirmed closed at", {"closed_confirmed"}),
    (r"people were told|People were told", {"told"}),
    (r"alarm.* was raised", {"alarm_raised"}),
    (r"acknowledged the (leak|valve) alarm", {"alarm_acked"}),
    (r"restarted", {"resumed"}),
]
_TIME = re.compile(r"\b(\d\d):(\d\d)(?::(\d\d))?\b")
_PCT = re.compile(r"(\d+(?:\.\d+)?)%")
_LPM = re.compile(r"(\d+(?:\.\d+)?) litres a minute")
_QUOTE = re.compile(r"\"(.*)\"")
_COUNT = re.compile(r"sent (\d+) more times?")


def check(statements, records, origin=CLOCK0):
    by_id = {r["rid"]: r for r in records}
    bad = []
    for st in statements:
        text, refs = st["text"], st.get("refs") or []
        why = []
        recs = [by_id.get(x) for x in refs]
        if not refs:
            why.append("no citation")
        if any(r is None for r in recs):
            why.append("cites a missing record")
        recs = [r for r in recs if r]
        kinds = {r["kind"] for r in recs}
        body = _QUOTE.sub("", text)        # quoted messages are checked as a whole, below
        fact = next((ks for pat, ks in _FACTS if re.search(pat, body)), None)
        if fact is None:
            why.append("states no known fact")
        elif not (kinds & fact):
            why.append(f"fact needs {sorted(fact)}, cites {sorted(kinds)}")
        times = {hms(r["t"], origin) for r in recs} | {hms(r["t_ack"], origin) for r in recs if "t_ack" in r}
        for m in _TIME.finditer(body):
            tok = m.group(0)
            ok = tok in times if m.group(3) else any(x.startswith(tok) for x in times)
            if not ok:
                why.append(f"time {tok} not in cited records")
        nums = set()
        for r in recs:
            for f in ("position", "last_position"):
                if isinstance(r.get(f), (int, float)):
                    nums.add(_num(r[f]))
        flows = {_flow(r[f]) for r in recs for f in ("flow_lpm", "last_flow_lpm")
                 if isinstance(r.get(f), (int, float))}
        for m in _PCT.finditer(body):
            if m.group(1) not in nums:
                why.append(f"{m.group(0)} not in cited records")
        for m in _LPM.finditer(body):
            if m.group(1) not in flows:
                why.append(f"flow {m.group(1)} not in cited records")
        q = _QUOTE.search(text)
        if q and q.group(1) not in {r.get("text") for r in recs}:
            why.append("quoted message not in cited records")
        c = _COUNT.search(body)
        if c and int(c.group(1)) != len(recs):
            why.append("count does not match cited records")
        if why:
            bad.append({"text": text, "why": why})
    n = len(statements)
    return {"statements": n, "supported": n - len(bad), "unsupported": bad}
