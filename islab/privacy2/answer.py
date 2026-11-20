""""What did you send out today?" at three levels, from the ledger only.

* ``short(report)``: one or two spoken sentences. Every category that occurred is mentioned.
  Things a person did or was told about are named with a count; background traffic may be
  grouped, but it is always said, with its message count.
* ``phone(report)``: the full answer for the phone: every purpose with messages, bytes,
  destination, who would notice it and the promised retention; what was blocked and why;
  what is waiting for the internet or was dropped.
* ``raw(gate, day)``: the ledger rows themselves, JSON lines.

``categories(report)`` lists what occurred; ``coverage(text, report)`` checks a short answer
against it (used by the tests and by the experiment to grade the Season 1 line).
"""
from __future__ import annotations

import json
import re

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def words(n: int) -> str:
    """Spoken number, 0..999,999."""
    n = int(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + words(rest))
    if n < 1_000_000:
        rest = n % 1000
        return words(n // 1000) + " thousand" + ("" if rest == 0 else " " + words(rest))
    return str(n)


def _cap(s):
    return s[0].upper() + s[1:] if s else s


def _pl(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _kb(b):
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    if b >= 1000:
        return f"{b / 1000:.1f} KB"
    return f"{b} bytes"


# (purpose, payload class) -> category name; categories are what the short answer must cover
CATEGORY = {
    ("speech_recognition_optional", "speech_audio"): "speech",
    ("llm_text_optional", "text"): "llm_text",
    ("notify_push", "notify_text"): "push",
    ("alarm", "alarm_event"): "alarm_signal",
    ("alarm", "camera_clip"): "alarm_clip",
    ("alarm", "alarm_heartbeat"): "alarm_checkin",
    ("fleet_update", "update_check"): "update_check",
    ("backup_encrypted", "encrypted_blob"): "backup",
}
BACKGROUND = {"alarm_checkin", "update_check", "backup"}
# words that show a category was mentioned; background ones also count as said by "background"
KEYWORDS = {
    "speech": r"voice request|speech",
    "llm_text": r"text question|assistant",
    "push": r"message[s]? to (a |your )?phone",
    "alarm_signal": r"alarm signal",
    "alarm_clip": r"camera clip",
    "alarm_checkin": r"check-in|background",
    "update_check": r"update check|background",
    "backup": r"backup|background",
    "blocked": r"blocked",
    "queued": r"waiting for the internet",
    "dropped": r"not sent|dropped",
    "bypass": r"around the gate|bypass",
}


def _cat_counts(report):
    out = {}
    for purpose, v in report["by_purpose"].items():
        for cls, k in v["classes"].items():
            cat = CATEGORY.get((purpose, cls), f"other:{purpose}:{cls}")
            out[cat] = out.get(cat, 0) + k
    return out


def categories(report) -> dict:
    """Every category that occurred today, with its message count."""
    c = _cat_counts(report)
    if report["blocked"]:
        c["blocked"] = report["blocked"]
    if report.get("waiting_now"):
        c["queued"] = report["waiting_now"]
    if report["dropped_offline"]:
        c["dropped"] = report["dropped_offline"]
    if report.get("violations"):
        c["bypass"] = report["violations"]
    return c


def coverage(text: str, report) -> dict:
    """Which categories a short answer mentions; background grouped must carry its count."""
    cats = categories(report)
    low = text.lower()
    missing = []
    for cat in cats:
        pat = KEYWORDS.get(cat)
        if pat is None or not re.search(pat, low):
            missing.append(cat)
    bg = sum(n for c, n in cats.items() if c in BACKGROUND)
    grouped_ok = True
    if bg and "background" in low:
        grouped_ok = words(bg) in low or str(bg) in low
    return {"categories": sorted(cats), "missing": missing, "background_messages": bg,
            "background_count_said": grouped_ok, "ok": not missing and grouped_ok}


def short(report) -> str:
    c = _cat_counts(report)
    parts = []
    if c.get("speech"):
        parts.append(f"{words(c['speech'])} voice {_pl(c['speech'], 'request')} for speech "
                     f"recognition, not stored")
    if c.get("llm_text"):
        parts.append(f"{words(c['llm_text'])} text {_pl(c['llm_text'], 'question')} to the "
                     f"cloud assistant, not stored")
    if c.get("push"):
        parts.append(f"{words(c['push'])} {_pl(c['push'], 'message')} to a phone away from home")
    if c.get("alarm_signal"):
        parts.append(f"{words(c['alarm_signal'])} alarm {_pl(c['alarm_signal'], 'signal')} to "
                     f"the monitoring centre")
    if c.get("alarm_clip"):
        parts.append(f"{words(c['alarm_clip'])} camera {_pl(c['alarm_clip'], 'clip')} with an alarm")
    others = sorted(k for k in c if k.startswith("other:"))
    for k in others:
        parts.append(f"{words(c[k])} other {_pl(c[k], 'message')} ({k.split(':', 1)[1]})")
    bg = sum(c.get(k, 0) for k in BACKGROUND)
    sentences = []
    if parts:
        sentences.append(_cap(_join(parts)) + ".")
    if bg:
        kinds = []
        if c.get("backup"):
            kinds.append("the nightly encrypted backup" if c["backup"] == 1 else
                         f"{words(c['backup'])} encrypted backups")
        if c.get("alarm_checkin"):
            kinds.append("alarm-line check-ins")
        if c.get("update_check"):
            kinds.append("update checks")
        lead = "In the background" if parts else "Only background traffic"
        sentences.append(f"{lead}: {words(bg)} {_pl(bg, 'message')}, {_join(kinds)}.")
    if not parts and not bg:
        sentences.append("Nothing left the house today.")
    if not c.get("alarm_clip"):
        sentences.append("No camera footage left.")
    if report["blocked"]:
        srcs = sorted(report["blocked_by_source"])
        who = srcs[0] if len(srcs) == 1 else f"{words(len(srcs))} devices"
        sentences.append(f"{_cap(words(report['blocked']))} {_pl(report['blocked'], 'attempt')} "
                         f"to send elsewhere {_pl(report['blocked'], 'was', 'were')} blocked, "
                         f"from {who}.")
    if report.get("violations"):
        v = report["violations"]
        sentences.append(f"{_cap(words(v))} {_pl(v, 'try', 'tries')} to go around the gate, logged.")
    w = report.get("waiting_now", 0)
    if w:
        sentences.append(f"{_cap(words(w))} {_pl(w, 'message is', 'messages are')} "
                         f"waiting for the internet.")
    if report["dropped_offline"]:
        d = report["dropped_offline"]
        sentences.append(f"{_cap(words(d))} {_pl(d, 'message was', 'messages were')} not sent "
                         f"because the internet was down.")
    return " ".join(sentences)


def phone(report) -> str:
    lines = [f"Sent out today: {report['messages']} messages, {_kb(report['bytes'])}."]
    vis = report["by_visibility"]
    if report["messages"]:
        bm = vis.get("background", {}).get("messages", 0)
        bb = vis.get("background", {}).get("bytes", 0)
        lines.append(f"Background (nobody asked for it, nobody is told): {bm} messages "
                     f"({100 * bm / report['messages']:.1f}%), {_kb(bb)} "
                     f"({100 * bb / report['bytes']:.1f}% of bytes).")
    for p, v in report["by_purpose"].items():
        who = "you would notice" if "person" in v["visibility"] else "background"
        lines.append(f"- {p}: {v['messages']} messages, {_kb(v['bytes'])}, to "
                     f"{', '.join(v['destinations'])}; {who}; promised: {'; '.join(v['retention'])}.")
    lines.append(f"Camera items out: {report['camera_items_out']}.")
    if report["blocked"]:
        lines.append(f"Blocked: {report['blocked']} attempts.")
        for s, n in report["blocked_by_source"].items():
            lines.append(f"- from {s}: {n}")
        for r, n in report["blocked_by_reason"].items():
            lines.append(f"- reason {r}: {n}")
    for n in report["notices"]:
        lines.append(f"Notice at {_clock(n['t'])}: {n['text']}")
    if report.get("violations"):
        lines.append(f"Tries to go around the gate: {report['violations']} (logged).")
    if report.get("waiting_now"):
        lines.append(f"Waiting for the internet: {report['waiting_now']} messages "
                     f"({report['queued']} send attempts were held, retries replace older copies).")
    if report["dropped_offline"]:
        lines.append(f"Not sent, internet down: {report['dropped_offline']} "
                     f"({', '.join(f'{k} {v}' for k, v in report['dropped_offline_by_purpose'].items())}).")
    lines.append("Full ledger: every row, on request.")
    return "\n".join(lines)


def raw(gate, day=0) -> str:
    return "\n".join(json.dumps(r, sort_keys=True) for r in gate.rows(day))


def _clock(t):
    t = t % 86400
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}"


def _join(parts):
    if len(parts) == 1:
        return parts[0]
    sep = ", and " if any("," in p for p in parts) or len(parts) > 2 else " and "
    return ", ".join(parts[:-1]) + sep + parts[-1]
