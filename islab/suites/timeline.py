"""Human-readable timeline and machine-readable trace of one House day."""
from __future__ import annotations

import json

KEEP = {"scene_step", "voice_identified", "notify", "assistant_say", "phone_reply", "phone_request",
        "alarm_raised", "alarm_acked", "centre_dispatch_decision", "siren", "egress_blocked", "alarm_mode",
        "fault", "dev_ignored", "dev_stuck", "dev_actuated", "link_drop", "state_reject_ooo"}


def hms(t):
    t = float(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def write_trace(house, path):
    """sim.log (everything, including every bus publish) + audit records, one JSON object per line."""
    rows = [{"t": t, "kind": k, **d} for t, k, d in house.sim.log]
    rows += [{**{k: v for k, v in r.items() if k != "kind"}, "kind": "audit." + r["kind"]}
             for r in house.audit.records]
    rows.sort(key=lambda r: r["t"])          # stable: sim order kept within a timestamp
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, sort_keys=True) + "\n")
    return len(rows)


def _person(p):
    return (p or "?").capitalize()


def _args(a):
    return ", ".join(f"{k}={v}" for k, v in (a or {}).items())


def rows(house):
    """(t, column, text): column in {"happened", "said", "outcome"}."""
    out = []
    acts = house.tracker.actions
    stepped = {t for t, k, _ in house.sim.log if k == "scene_step"}
    for t, k, d in house.sim.log:
        if k not in KEEP:
            continue
        if k in ("phone_request", "fault") and t in stepped:
            continue                     # the scripted step at the same instant already says it
        if k == "scene_step":
            out.append((t, "happened", f"{d['what']}"))
        elif k == "voice_identified":
            who = f"{_person(d['person'])} (voice match {d['score']:.2f})" if d["person"] else \
                f"unknown speaker (best match {d['score']:.2f})"
            out.append((t, "happened", f"{d['satellite']} hears \"{d['text']}\" -> {who}"))
        elif k == "notify":
            tag = {"ask": "question", "approval": "approval request", "tell": "message"}.get(d.get("msg_kind"), "")
            out.append((t, "said", f"to {_person(d['person'])}'s phone ({tag}): \"{d['text']}\""))
        elif k == "assistant_say":
            out.append((t, "said", f"out loud on {d['satellite']}: \"{d['text']}\""))
        elif k == "phone_reply":
            out.append((t, "happened", f"{_person(d['person'])} answers \"{d['answer']}\" on the phone"))
        elif k == "phone_request":
            out.append((t, "happened", f"{_person(d['person'])} types in the app: \"{d['text']}\""))
        elif k == "alarm_raised":
            out.append((t, "outcome", f"DC-09 alarm #{d['seq']} sent: {d['alarm_kind']} zone {d['zone']}"))
        elif k == "alarm_acked":
            out.append((t, "outcome", f"monitoring centre ACK for alarm #{d['seq']} "
                                      f"({d['latency']:.2f} s after the event)"))
        elif k == "centre_dispatch_decision":
            out.append((t, "outcome", f"monitoring centre receives {d['event']} (account {d['account']}); "
                                      f"operator verifies, then dispatches"))
        elif k == "siren":
            out.append((t, "outcome", f"local siren sounds ({d['pattern']}, from {d['device']}) - no hub, "
                                      f"no internet, no AI involved"))
        elif k == "egress_blocked":
            out.append((t, "outcome", f"egress gate blocks {d['source']} -> {d['dest']} "
                                      f"({d['purpose']}/{d['payload_class']}): {d['reason']}"))
        elif k == "alarm_mode":
            out.append((t, "outcome", "alarm armed (panel confirmed armed_away)" if d["armed"]
                        else "alarm disarmed (panel reports disarmed)"))
        elif k == "fault":
            if d.get("type") == "internet":
                out.append((t, "happened", "internet " + ("back up" if d["up"] else "goes down")))
            elif d.get("type") in ("offline", "stuck", "clock_skew", "hostile_name"):
                out.append((t, "happened", f"fault: {d['type']} {d.get('device', '')}".strip()))
        elif k in ("dev_ignored", "dev_stuck"):
            out.append((t, "outcome", f"{d['device']} {'ignores (offline)' if k == 'dev_ignored' else 'acks but does not move'} "
                                      f"command {d['cmd_id']}"))
        elif k == "dev_actuated":
            rid = str(d["cmd_id"]).rsplit(".c", 1)[0]
            if rid in acts:
                out.append((t, "outcome", f"{d['device']} physically done ({d['cmd_id']})"))
    for r in house.audit.records:
        if r["kind"] == "decision" and r.get("tool"):
            lvl = f"level {r['level']}" if r.get("level") is not None else "no level"
            d = house.engine.decisions.get(r["request_id"])
            out.append((r["t"], "outcome", f"policy: {r['request_id']} {r['tool']}({_args(d.args if d else {})}) "
                                           f"by {r.get('actor')}, {lvl} -> {r['status']} ({r['reason']})"))
        elif r["kind"] == "approval":
            out.append((r["t"], "outcome", f"approval recorded for {r['request_id']}: {r['by']} via {r['via']}"))
        elif r["kind"] == "override":
            out.append((r["t"], "outcome", f"override logged: {r['by']} overrides rule {r['rule']}"))
        elif r["kind"] == "expired":
            out.append((r["t"], "outcome", f"{r['request_id']}: nobody answered; nothing was done"))
        elif r["kind"] == "outcome":
            cmds = ", ".join(f"{c['device_id']} {c['status']}" for c in r.get("commands", []))
            out.append((r["t"], "outcome", f"{r['request_id']} {r['outcome']} ({r['detail']}): {cmds}"))
    for e in house.gate.sent:
        if e["payload_class"] != "alarm_heartbeat":
            out.append((e["t_out"], "outcome", f"egress out: {e['source']} -> {e['dest']} "
                                               f"({e['purpose']}/{e['payload_class']}, {e['bytes']:,} bytes)"))
    order = {"happened": 0, "said": 2, "outcome": 1}
    out.sort(key=lambda r: (round(r[0], 4), order[r[1]]))
    return out


def timeline_md(house, scene_results=None, title="One day in the house"):
    scene_results = scene_results or {}
    rs = rows(house)
    lines = [f"# {title}", "",
             f"Seed {house.seed}, fault mix `{house.fault_mix}`. Virtual time (00:00 = midnight). "
             "Generated by experiments/scorecard/run.py from the run itself; every line below is an event "
             "in the trace (day_trace.jsonl), nothing is paraphrased except the column labels.", "",
             "Columns: **What happened** (people, devices, faults), **Outcome** (what the house actually did, "
             "as recorded by the policy engine, action tracker, alarm path and egress gate), "
             "**Assistant said** (every message to a phone and every spoken reply, verbatim).", "",
             "Routine background not listed per line: device heartbeats, 1 alarm-line check-in per minute "
             "(DC-09 NULL message), state updates.", ""]
    for sc in house.scenes:
        lines.append(f"## {hms(sc['start'])[:5]}-{hms(sc['end'])[:5] if sc['end'] < 86400 else '24:00'}  "
                     f"{sc['title']}")
        res = scene_results.get(sc["id"])
        if res is not None:
            lines.append("")
            lines.append(f"Scene check: **{'as scripted' if res else 'DID NOT go as scripted'}**")
        lines.append("")
        lines.append("| Time | What happened | Outcome | Assistant said |")
        lines.append("|---|---|---|---|")
        n = 0
        for t, col, text in rs:
            if sc["start"] <= t < sc["end"]:
                cells = {"happened": "", "said": "", "outcome": ""}
                cells[col] = text.replace("|", "\\|")
                lines.append(f"| {hms(t)} | {cells['happened']} | {cells['outcome']} | {cells['said']} |")
                n += 1
        if not n:
            lines.append("| | (nothing) | | |")
        lines.append("")
    return "\n".join(lines) + "\n"
