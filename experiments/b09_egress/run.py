"""B09 What leaves the house. Writes results.json next to this file.

    python experiments/b09_egress/run.py        # about 1 minute

A. The Season 1 E09 day (same plan and seed as experiments/egress) through the ledger gate:
   every byte out, the answer at three levels, and how the scripted E09 line grades.
B. 30 varied days (seeds 2026-2055): bytes and messages per purpose per day, background share,
   short-answer coverage on every day.
C. Phone home: 7 ways a device tries to reach its maker, 200 seeded days each, B01 gate vs
   ledger gate with source manifests.
D. Internet down all day: what still works, cloud speech fallback, opt-out.
E. Deletion: 500 seeded trials with planted copies, B01 delete vs shared-data delete; consent
   from the other owner; backups taken before the delete (per-record keys vs one nightly key).
"""
import json
import os
import statistics
import sys
import time
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from islab.core.sim import Sim                                        # noqa: E402
from islab.core.bus import Bus, Envelope                              # noqa: E402
from islab.alarm.egress import EgressGate                             # noqa: E402
from islab.alarm.path import AlarmHub, LocalSiren, MonitoringCentre, WanLink   # noqa: E402
from islab.alarm.privacy import RecordingStore, make_thumb            # noqa: E402
from islab.privacy2 import (LedgerGate, MODES, PhoneHomeDevice, SharedStore,   # noqa: E402
                            ConsentResponder, VoiceRouter, coverage, phone, short, transcode)
from islab.privacy2.answer import categories                          # noqa: E402

DAY = 86400.0
SEED = 2026
KEY = b"0123456789ABCDEF"
SETTINGS = {"cloud_speech_opt_in": True, "cloud_llm_opt_in": False, "alarm_clips_allowed": False}
PLAN = {   # the E09 day, as in experiments/egress
    "voice_requests_total": 60,
    "voice_requests_cloud_asr": 12,
    "speech_bytes_range": (9_000, 18_000),
    "backup_bytes": 48_000_000,
    "update_checks_every_s": 6 * 3600,
    "update_check_bytes": 1_200,
    "alarm_heartbeat_s": 300.0,
    "camera_motion_clips": 25,
    "phone_home_every_s": 600,
    "home_control_commands": 200,
}
CTRL = ["light.kitchen", "light.living", "shade.living", "thermo.kitchen", "lock.front"]
E09_LINE = "Twelve speech requests, not stored. Last night's encrypted backup."
PH_TRIALS = 200
DEL_TRIALS = 500
CONSENT = {"answer_yes": 0.8, "answer_no": 0.1, "never_answers": 0.1,
           "reply_delay_lognormal_median_s": 900.0, "reply_delay_sigma": 1.0,
           "ask_timeout_s": DAY}


class StandIn:
    def __init__(self, sim, bus, dev):
        self.sim, self.bus, self.dev = sim, bus, dev
        bus.subscribe(f"cmd/{dev}", self.on_cmd, name=dev)

    def on_cmd(self, env):
        p = env.payload
        self.sim.after(0.3, lambda: self.bus.publish(Envelope(
            topic=f"state/{self.dev}", source=self.dev, ts=self.sim.now,
            payload={"value": p["args"].get("value"), "cmd_id": p["cmd_id"]})))


def pct(a, b):
    return round(100.0 * a / b, 2) if b else None


def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)


# ---------------------------------------------------------------- A, B, D: a household day
def run_day(seed, internet_down=False, variant=None, settings=None):
    sim = Sim(seed=seed)
    rng = sim.rng
    bus = Bus(sim)
    gate = LedgerGate(sim, bus, settings=settings or SETTINGS)
    link = WanLink(sim, outages=[(0, DAY)] if internet_down else [])
    centre = MonitoringCentre(sim, link, {"1A2B3C": KEY}, supervision_s=1800)
    hub = AlarmHub(sim, bus, link, centre, account="1A2B3C", key=KEY, egress=gate,
                   heartbeat_s=PLAN["alarm_heartbeat_s"], link_fault_after=900)
    siren = LocalSiren(sim, bus)
    voice = VoiceRouter(sim, gate)
    v = variant or {}
    if internet_down:
        gate.set_internet(False)
        sim.at(DAY, gate.set_internet, True)
    outage = v.get("outage")
    if outage:
        sim.at(outage[0], gate.set_internet, False)
        sim.at(outage[1], gate.set_internet, True)
        link.outages.append(outage)

    n_cloud = v.get("voice_cloud", PLAN["voice_requests_cloud_asr"])
    n_total = max(PLAN["voice_requests_total"], n_cloud)
    flags = [True] * (n_total - n_cloud) + [False] * n_cloud
    rng.shuffle(flags)
    for confident in flags:
        t = 7 * 3600 + rng.random() * 15 * 3600
        size = rng.randint(*PLAN["speech_bytes_range"])
        sim.at(t, voice.request, confident, size)
    sim.at(3 * 3600, lambda: gate.send("backup.agent", "backup.storage.example", "backup_encrypted",
                                       "encrypted_blob", size=PLAN["backup_bytes"],
                                       meta={"encrypted": True}, msg_id="backup:day0"))
    t = 1800.0
    while t < DAY:
        sim.at(t, lambda: gate.send("fleet.agent", "updates.vendor.example", "fleet_update",
                                    "update_check", size=PLAN["update_check_bytes"]))
        t += PLAN["update_checks_every_s"]
    for i in range(PLAN["camera_motion_clips"]):
        t = rng.random() * DAY
        dest = "cloud.camvendor.example" if i % 2 else "backup.storage.example"
        sim.at(t, lambda dest=dest: gate.send("camera.front", dest, "backup_encrypted",
                                              "camera_clip", size=2_000_000,
                                              meta={"encrypted": True}))
    sim.at(12 * 3600, lambda: bus.publish(Envelope(topic="egress/backup_encrypted",
                                                   payload={"dest": "cloud.camvendor.example"},
                                                   source="camera.front", ts=sim.now)))
    plug = PhoneHomeDevice(sim, bus, gate, "undeclared_destination", start_s=60.0)
    # variant extras: pushes to Mark away, cloud assistant questions, a leak alarm
    for _ in range(v.get("pushes", 0)):
        sim.at(rng.random() * DAY, lambda: gate.send(
            "privacy.notify", "push.relay.example", "notify_push", "notify_text",
            data=b"Front door unlocked by Sam at 16:02.", meta={"person": "mark"}))
    for _ in range(v.get("llm", 0)):
        sim.at(8 * 3600 + rng.random() * 12 * 3600, lambda: gate.send(
            "voice.pipeline", "llm.provider.example", "llm_text_optional", "text",
            size=rng.randint(200, 2000)))
    leak_t = v.get("leak_at")
    if internet_down and leak_t is None:
        leak_t = 14 * 3600
    if leak_t is not None:
        sim.at(leak_t, lambda: bus.publish(Envelope(topic="event/leak.sink", payload={"type": "leak"},
                                                    source="leak.sink", ts=sim.now)))
    for d in CTRL:
        StandIn(sim, bus, d)
    pending, confirmed = {}, []

    def on_state(env):
        cid = env.payload.get("cmd_id")
        if cid in pending:
            confirmed.append(sim.now - pending.pop(cid))
    bus.subscribe("state/+", on_state, name="controller")
    for i in range(PLAN["home_control_commands"]):
        dev = CTRL[i % len(CTRL)]

        def cmd(i=i, dev=dev):
            pending[f"c{i}"] = sim.now
            bus.publish(Envelope(topic=f"cmd/{dev}", source="controller", ts=sim.now,
                                 payload={"action": "set", "args": {"value": i}, "cmd_id": f"c{i}"}))
        sim.at(rng.random() * DAY, cmd)

    sim.run(until=DAY - 1e-6)
    rep = gate.day_report(0)
    out = {"report": rep, "short": short(rep), "phone": phone(rep), "gate": gate,
           "voice": voice.counts(), "home_control_confirmed": len(confirmed),
           "home_control_p50_s": q(confirmed, 0.5), "plug_attempts": len(plug.attempts),
           "plug_bytes_out": plug.bytes_out}
    if leak_t is not None:
        out["leak"] = {"siren_s": round(siren.activations[0][0] - leak_t, 3) if siren.activations else None}
    sim.run(until=DAY + 3600)
    if leak_t is not None:
        a = hub.alarms[0] if hub.alarms else None
        out["leak"]["centre_ack_s"] = round(a["t_ack"] - a["t_event"], 1) if a and a.get("t_ack") else None
    out["after_internet_back_sent"] = len([r for r in gate.ledger if r["outcome"] == "sent"
                                           and r.get("t_out", 0) >= DAY])
    return out


def compact(rep):
    return {k: rep[k] for k in ("by_purpose", "messages", "bytes", "by_visibility",
                                "background_share_messages", "background_share_bytes",
                                "camera_items_out", "blocked", "blocked_by_source",
                                "blocked_by_reason", "dropped_offline", "dropped_offline_by_purpose",
                                "queued", "waiting_now", "violations")} | {"notices": [
        {k: n[k] for k in ("t", "source", "dest", "reason")} for n in rep["notices"]]}


def part_a():
    d = run_day(SEED)
    rep = d["report"]
    g = d["gate"]
    e09 = coverage(E09_LINE, rep)
    b01_line = _b01_summary(g)
    return {
        "report": compact(rep),
        "answer_short": d["short"],
        "answer_short_words": len(d["short"].split()),
        "answer_phone": d["phone"],
        "raw_ledger_rows": len(g.rows(0)),
        "raw_ledger_rows_by_outcome": _count(g.rows(0), "outcome"),
        "raw_ledger_first_rows": g.rows(0)[:3],
        "short_answer_coverage": coverage(d["short"], rep),
        "e09_scripted_line": E09_LINE,
        "e09_scripted_line_coverage": e09,
        "b01_summary_without_routine": b01_line,
        "b01_summary_without_routine_coverage": coverage(b01_line, rep),
        "voice": d["voice"],
        "home_control_confirmed": d["home_control_confirmed"],
        "plug_attempts": d["plug_attempts"], "plug_bytes_out": d["plug_bytes_out"],
    }


def _b01_summary(gate):
    """The B01 summary_text(include_routine=False), computed on this gate's records."""
    return EgressGate.summary_text(gate, 0, include_routine=False)


def part_b():
    days = []
    import random
    vr = random.Random(99)
    for i in range(30):
        seed = SEED + i
        variant = {"voice_cloud": vr.randint(0, 25), "pushes": vr.choice([0, 0, 1, 2, 5]),
                   "llm": vr.choice([0, 0, 0, 4, 10])}
        settings = dict(SETTINGS, cloud_llm_opt_in=variant["llm"] > 0)
        if vr.random() < 0.2:
            variant["leak_at"] = vr.uniform(8, 20) * 3600
        if vr.random() < 0.2:
            s = vr.uniform(6, 18) * 3600
            variant["outage"] = (s, s + vr.uniform(2, 6) * 3600)
        d = run_day(seed, variant=variant, settings=settings)
        rep = d["report"]
        days.append({"seed": seed, "variant": variant, "report": rep,
                     "coverage": coverage(d["short"], rep),
                     "b01_coverage": coverage(_b01_summary(d["gate"]), rep),
                     "short": d["short"]})
    purposes = sorted({p for d in days for p in d["report"]["by_purpose"]})
    per_purpose = {}
    for p in purposes:
        ms = [d["report"]["by_purpose"].get(p, {}).get("messages", 0) for d in days]
        bs = [d["report"]["by_purpose"].get(p, {}).get("bytes", 0) for d in days]
        per_purpose[p] = {"messages_mean": round(statistics.mean(ms), 1), "messages_min": min(ms),
                          "messages_max": max(ms), "bytes_mean": round(statistics.mean(bs)),
                          "bytes_min": min(bs), "bytes_max": max(bs),
                          "days_present": sum(1 for m in ms if m)}
    bgm = [d["report"]["background_share_messages"] for d in days]
    bgb = [d["report"]["background_share_bytes"] for d in days]
    cats_seen = sorted({c for d in days for c in categories(d["report"])})
    return {
        "days": len(days),
        "per_purpose_per_day": per_purpose,
        "background_share_messages_mean": round(statistics.mean(bgm), 4),
        "background_share_messages_min": min(bgm), "background_share_messages_max": max(bgm),
        "background_share_bytes_mean": round(statistics.mean(bgb), 4),
        "background_share_bytes_min": min(bgb), "background_share_bytes_max": max(bgb),
        "categories_seen": cats_seen,
        "short_answer_all_categories_days": sum(1 for d in days if d["coverage"]["ok"]),
        "b01_summary_all_categories_days": sum(1 for d in days if d["b01_coverage"]["ok"]),
        "b01_summary_missing_by_category": _count([{"c": m} for d in days
                                                   for m in d["b01_coverage"]["missing"]], "c"),
        "short_answer_words_max": max(len(d["short"].split()) for d in days),
        "example_day_with_outage_and_leak": next(
            ({"seed": d["seed"], "short": d["short"]} for d in days
             if d["variant"].get("outage") and d["variant"].get("leak_at")),
            next(({"seed": d["seed"], "short": d["short"]} for d in days
                  if d["variant"].get("outage")), None)),
    }


# ---------------------------------------------------------------- C: phone home
def phone_home_trial(mode, seed, manifests_on):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    gate = LedgerGate(sim, bus, settings=SETTINGS, enforce_manifests=manifests_on)
    legit = {"sent": 0, "blocked": 0}
    t = 1800.0
    while t < DAY:
        def chk():
            ok = gate.send("fleet.agent", "updates.vendor.example", "fleet_update", "update_check",
                           size=PLAN["update_check_bytes"])
            legit["sent" if ok else "blocked"] += 1
        sim.at(t + sim.rng.random() * 60, chk)
        t += PLAN["update_checks_every_s"]
    dev = PhoneHomeDevice(sim, bus, gate, mode, start_s=sim.rng.random() * 600)
    sim.run(until=DAY - 1e-6)
    first = dev.attempts[0] if dev.attempts else None
    told = [n["t"] for n in gate.notices if n["source"] == dev.source]
    logged = ([r["t"] for r in gate.ledger if r["source"] == dev.source and r["outcome"] == "blocked"]
              + [v["t"] for v in gate.violations if v["source"] == dev.source])
    return {"attempts": len(dev.attempts), "got_out": len(dev.got_out), "bytes_out": dev.bytes_out,
            "logged": bool(logged), "told": bool(told),
            "detect_delay_s": round(min(told) - first, 3) if told else None,
            "legit_blocked": legit["blocked"], "legit_sent": legit["sent"]}


def part_c():
    out = {}
    for mode in MODES:
        out[mode] = {}
        for label, on in (("b01_gate", False), ("ledger_gate", True)):
            rs = [phone_home_trial(mode, 9000 + i, on) for i in range(PH_TRIALS)]
            att = sum(r["attempts"] for r in rs)
            got = sum(r["got_out"] for r in rs)
            dl = [r["detect_delay_s"] for r in rs if r["detect_delay_s"] is not None]
            out[mode][label] = {
                "trials": len(rs), "attempts": att, "got_out": got,
                "blocked_pct": pct(att - got, att),
                "bytes_out_per_day_mean": round(statistics.mean(r["bytes_out"] for r in rs), 1),
                "attempt_logged_pct": pct(sum(r["logged"] for r in rs), len(rs)),
                "household_told_pct": pct(sum(r["told"] for r in rs), len(rs)),
                "detect_delay_s_p50": q(dl, 0.5), "detect_delay_s_max": max(dl) if dl else None,
                "legit_update_checks_blocked": sum(r["legit_blocked"] for r in rs),
                "legit_update_checks": sum(r["legit_blocked"] + r["legit_sent"] for r in rs),
            }
    return out


# ---------------------------------------------------------------- D: internet down
def part_d():
    up = run_day(SEED)
    down = run_day(SEED, internet_down=True)
    optout = run_day(SEED, settings=dict(SETTINGS, cloud_speech_opt_in=False))
    rd = down["report"]
    # deletion with the internet down: a backed-up record, per-record keys
    sim = Sim(seed=SEED)
    bus = Bus(sim)
    gate = LedgerGate(sim, bus, settings=SETTINGS)
    store = SharedStore(sim, bus, gate)
    store.add("clip001", sim.rng.randbytes(50_000))
    store.run_backup()
    gate.set_internet(False)
    rid = store.request_delete("clip001", "anna")
    sim.run(until=60)
    rc = store.receipt(rid)
    return {
        "internet_up": {"short": up["short"], "voice": up["voice"],
                        "home_control_confirmed": up["home_control_confirmed"]},
        "internet_down": {
            "short": down["short"],
            "messages_out": rd["messages"], "bytes_out": rd["bytes"],
            "home_control_confirmed": down["home_control_confirmed"],
            "home_control_commands": PLAN["home_control_commands"],
            "home_control_p50_s": down["home_control_p50_s"],
            "voice": down["voice"],
            "leak": down["leak"],
            "waiting_at_end_of_day": rd["waiting_now"], "queued_attempts": rd["queued"],
            "dropped_offline_by_purpose": rd["dropped_offline_by_purpose"],
            "blocked": rd["blocked"],
            "sent_in_first_hour_back": down["after_internet_back_sent"],
            "delete_receipt_offline": {"status": rc["status"],
                                       "seconds": round(rc["t_receipt"] - rc["t_request"], 4),
                                       "backup_restore_after_delete": rc["backup"]["restore_after_delete"]},
        },
        "cloud_speech_opted_out": {"short": optout["short"], "voice": optout["voice"],
                                   "speech_bytes_out": optout["report"]["by_purpose"].get(
                                       "speech_recognition_optional", {}).get("bytes", 0)},
    }


# ---------------------------------------------------------------- E: deletion
KINDS = ["main", "index", "thumb", "backup_queue", "renamed_copy_registered",
         "share_link_record_registered", "transcode_with_lineage", "transcode_no_lineage",
         "exact_copy_unregistered", "embedded_in_bundle", "thumb_copy_unregistered"]


def _plant(store, shared, rid, data, others, rng):
    """Plant hidden copies of rid. Returns a checker: kind -> callable() -> still present?"""
    exports = store.register_location("exports", {})
    shares = store.register_location("share_links", {})
    previews = store.register_location("previews", {})
    dump = {}
    oldthumbs = {}
    if shared:
        store.unregistered_location("debug_dump", dump)
        store.unregistered_location("thumb_cache_old", oldthumbs)
    tag = f"{rng.getrandbits(32):08x}"
    exports[f"export_{tag}.mp4"] = data
    shares[f"tok_{tag}"] = {"payload": data, "expires": 7}
    tc = transcode(data)
    if shared:
        store.derive(rid, "previews", f"preview_{tag}.webm", tc)
    else:
        previews[f"preview_{tag}.webm"] = tc
    previews[f"p2_{tag}.webm"] = transcode(data + b"x")
    dump[f"dump_{tag}"] = data
    bundle = others[0] + data + others[1]
    exports[f"bundle_{tag}.zip"] = bundle
    oldthumbs[f"t_{tag}"] = make_thumb(data)
    p2 = transcode(data + b"x")
    return {
        "main": lambda: rid in store.main,
        "index": lambda: rid in store.index,
        "thumb": lambda: rid in store.thumbs,
        "backup_queue": lambda: any(i["rec_id"] == rid for i in store.backup_queue),
        "renamed_copy_registered": lambda: f"export_{tag}.mp4" in exports,
        "share_link_record_registered": lambda: f"tok_{tag}" in shares,
        "transcode_with_lineage": lambda: f"preview_{tag}.webm" in previews,
        "transcode_no_lineage": lambda: previews.get(f"p2_{tag}.webm") == p2,
        "exact_copy_unregistered": lambda: f"dump_{tag}" in dump,
        "embedded_in_bundle": lambda: data in exports.get(f"bundle_{tag}.zip", b""),
        "thumb_copy_unregistered": lambda: f"t_{tag}" in oldthumbs,
    }, (lambda: all(o in exports.get(f"bundle_{tag}.zip", b"") for o in others))


def deletion_trial(seed, mode):
    """mode: 'b01' (RecordingStore.delete) or 'b09' (SharedStore, solo owner request)."""
    sim = Sim(seed=seed)
    rng = sim.rng
    n = 20
    clips = {f"clip{i:03d}": rng.randbytes(100_000) for i in range(n)}
    if mode == "b01":
        store = RecordingStore(sim)
        for rid, data in list(clips.items())[:10]:
            store.add(rid, data)
        store.run_backup()
        for rid, data in list(clips.items())[10:]:
            store.add(rid, data)
    else:
        store = SharedStore(sim, None, None)
        for rid, data in list(clips.items())[:10]:
            store.add(rid, data)
        store.run_backup()
        for rid, data in list(clips.items())[10:]:
            store.add(rid, data)
    target = f"clip{rng.randrange(n):03d}"
    others = [clips[r] for r in clips if r != target][:2]
    present, bundle_ok = _plant(store, mode == "b09", target, clips[target], others, rng)
    in_backup = int(target[4:]) < 10
    w0 = time.perf_counter()
    if mode == "b01":
        rc = store.delete(target)
        claimed_done = rc["complete"]
        claimed_local = rc["verified_absent_locally"]
        modelled_s = 0.0
    else:
        rid = store.request_delete(target, "anna")
        sim.run()
        rc = store.receipt(rid)
        claimed_done = False      # the B09 receipt never claims "complete"; it lists what it did not check
        claimed_local = rc["no_copy_found_on_hub_disk"]
        modelled_s = rc["t_receipt"] - rc["t_request"]
    wall_ms = (time.perf_counter() - w0) * 1000
    left = {k: f() for k, f in present.items()}
    collateral = sum(1 for r, d in clips.items() if r != target and store.main.get(r) != d)
    collateral += 0 if bundle_ok() else 1
    return {"left": left, "in_backup": in_backup, "claimed_complete": claimed_done,
            "claimed_none_left_locally": claimed_local, "wall_ms": wall_ms,
            "modelled_s": modelled_s, "collateral": collateral, "receipt": rc}


def consent_trial(seed, keys):
    sim = Sim(seed=seed)
    rng = sim.rng
    bus = Bus(sim)
    gate = LedgerGate(sim, bus, settings=SETTINGS)
    store = SharedStore(sim, bus, gate, keys=keys, ask_timeout_s=CONSENT["ask_timeout_s"],
                        people_home={"anna": True, "mark": False, "sam": True})
    data = rng.randbytes(100_000)
    store.add("clip000", data, owners=("anna", "mark"))
    backed_up = rng.random() < 0.5
    if backed_up:
        store.run_backup()
    u = rng.random()
    answer = "yes" if u < CONSENT["answer_yes"] else ("no" if u < CONSENT["answer_yes"] + CONSENT["answer_no"] else None)
    import math
    delay = CONSENT["reply_delay_lognormal_median_s"] * math.exp(CONSENT["reply_delay_sigma"] * rng.gauss(0, 1))
    ConsentResponder(sim, bus, "mark", store, delay, answer=answer)
    sim.at(10.0, store.request_delete, "clip000", "anna")
    sim.run(until=3 * DAY)
    rc = store.receipts[-1]
    still_there = "clip000" in store.main
    restorable = store.restore("clip000") is not None
    yes = rc["answers"].get("mark", {}).get("answer") == "yes"
    pushes = len([r for r in gate.ledger if r["purpose"] == "notify_push" and r["outcome"] == "sent"])
    return {"status": rc["status"], "answer": answer, "deleted": not still_there,
            "deleted_without_yes": (not still_there) and not yes,
            "seconds": rc["t_receipt"] - rc["t_request"], "backed_up": backed_up,
            "restorable_after": restorable, "pushes_out": pushes,
            "receipt_backup": rc.get("backup")}


def part_e():
    res = {}
    for mode in ("b01", "b09"):
        rs = [deletion_trial(7000 + i, mode) for i in range(DEL_TRIALS)]
        found = {}
        for k in KINDS:
            app = [r for r in rs if k != "backup_queue" or not r["in_backup"]]
            found[k] = pct(sum(1 for r in app if not r["left"][k]), len(app))
        any_left = sum(1 for r in rs if any(r["left"][k] for k in KINDS
                                            if k != "backup_queue" or not r["in_backup"]))
        res[mode] = {
            "trials": len(rs),
            "removed_pct_by_kind": found,
            "trials_with_a_copy_left": any_left,
            "trials_with_a_copy_left_of_a_kind_the_receipt_says_it_checks": sum(
                1 for r in rs if any(r["left"][k] for k in KINDS
                                     if k != "transcode_no_lineage"
                                     and (k != "backup_queue" or not r["in_backup"]))),
            "receipt_claimed_complete": sum(r["claimed_complete"] for r in rs),
            "receipt_claimed_complete_while_copy_left": sum(
                1 for r in rs if r["claimed_complete"] and any(
                    r["left"][k] for k in KINDS if k != "backup_queue" or not r["in_backup"])),
            "receipt_said_none_left_locally_while_copy_left": sum(
                1 for r in rs if r["claimed_none_left_locally"] and any(
                    r["left"][k] for k in KINDS if k != "backup_queue" or not r["in_backup"])),
            "collateral_records_damaged": sum(r["collateral"] for r in rs),
            "wall_ms_p50": round(q([r["wall_ms"] for r in rs], 0.5), 2),
            "wall_ms_p95": round(q([r["wall_ms"] for r in rs], 0.95), 2),
            "modelled_scan_s_p50": q([r["modelled_s"] for r in rs], 0.5),
            "modelled_scan_s_p95": q([r["modelled_s"] for r in rs], 0.95),
            "target_in_backup": sum(r["in_backup"] for r in rs),
        }
    ex = deletion_trial(7000, "b09")["receipt"]
    res["b09_example_receipt"] = {k: ex[k] for k in ("receipt_id", "status", "removed",
                                                     "locations_scanned", "matched_by",
                                                     "no_copy_found_on_hub_disk", "backup",
                                                     "not_checked")}
    exb = deletion_trial(7000, "b01")["receipt"]
    res["b01_example_receipt"] = {k: exb[k] for k in ("receipt_id", "complete",
                                                      "verified_absent_locally", "removed",
                                                      "locations_scanned", "pending_remote")}
    for keys in ("per_record", "nightly"):
        rs = [consent_trial(8000 + i, keys) for i in range(DEL_TRIALS)]
        st = _count([{"s": r["status"]} for r in rs], "s")
        dl = [r for r in rs if r["status"] == "deleted"]
        bk = [r for r in dl if r["backed_up"]]
        res[f"shared_{keys}_keys"] = {
            "trials": len(rs), "status": st,
            "deleted_without_yes": sum(r["deleted_without_yes"] for r in rs),
            "kept_after_no_or_no_answer": sum(1 for r in rs if r["status"] != "deleted" and not r["deleted"]),
            "seconds_to_receipt_deleted_p50": q([r["seconds"] for r in dl], 0.5),
            "seconds_to_receipt_deleted_p95": q([r["seconds"] for r in dl], 0.95),
            "seconds_to_receipt_declined_p50": q([r["seconds"] for r in rs if r["status"] == "declined"], 0.5),
            "seconds_to_receipt_no_answer": q([r["seconds"] for r in rs if r["status"] == "no_answer"], 0.5),
            "deleted_with_copy_in_backup": len(bk),
            "backup_restorable_after_delete": sum(r["restorable_after"] for r in bk),
            "receipt_says_backup_unreadable": sum(1 for r in bk if r["receipt_backup"]
                                                  and r["receipt_backup"].get("restore_after_delete") is False),
            "ask_pushes_out": sum(r["pushes_out"] for r in rs),
            "example_backup_statement": (bk[0]["receipt_backup"]["statement"] if bk else None),
        }
    return res


def _count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


def main():
    w0 = time.perf_counter()
    a = part_a()
    b = part_b()
    c = part_c()
    d = part_d()
    e = part_e()
    out = {
        "experiment": "b09_egress",
        "date": date.today().isoformat(),
        "method": (
            "Discrete-event twin, seeded. A: the Season 1 E09 day (plan and seed of "
            "experiments/egress) through LedgerGate, which keeps every B01 gate rule, adds the B04 "
            "push purpose, a ledger row for every attempt, per-sender manifests and a notice on the "
            "first undeclared attempt; the answer to 'what did you send out today?' at three levels, "
            "graded for category coverage, and the scripted E09 line graded the same way. B: 30 "
            "varied days (seeds 2026-2055; random cloud speech count, pushes, assistant questions, "
            "leaks, 2-6 h outages). C: 7 phone-home modes x 200 seeded days, B01 gate rules vs "
            "LedgerGate. D: the E09 day with the internet down 00:00-24:00, and with cloud speech "
            "opted out. E: 500 seeded deletions with 11 kinds of planted copies, B01 delete vs B09 "
            "SharedStore; 500 shared-data deletions per backup key scheme with a modelled co-owner."),
        "params": {"seed": SEED, "settings": SETTINGS, "plan": PLAN, "e09_line": E09_LINE,
                   "phone_home_trials": PH_TRIALS, "deletion_trials": DEL_TRIALS,
                   "consent_model": CONSENT, "clip_bytes": 100_000, "clips_per_store": 20,
                   "scan_model": {"disk_rate_bytes_s": 200e6, "per_location_s": 0.005},
                   "backup_rotation_days": 30, "deletion_kinds": KINDS},
        "results": {"a_e09_day": a, "b_thirty_days": b, "c_phone_home": c,
                    "d_internet_down": d, "e_deletion": e},
        "limitations": [
            "Byte sizes are modelled (Opus voice, 48 MB backup, 100 KB clips), not measured on devices.",
            "Visibility (person vs background) is a label we assign per purpose and payload class; "
            "the nightly backup is labelled background.",
            "Retention promises are declared in DESTINATIONS; the gate records them and cannot "
            "verify what a provider keeps.",
            "Manifests are written by people. A declared sender that leaks inside its declared size "
            "and count (low and slow) is not detected; payload content is not inspected.",
            "The gate sees only traffic routed through it; a device with its own uplink needs "
            "network default-deny (VLAN, firewall), which is not simulated. 'bypass_gate' only "
            "shows the watchdog logs a direct egress/ publication on the bus.",
            "Which voice requests the local recogniser is unsure about is an input (12 of 60), "
            "not a measured recogniser; no speech model was run.",
            "The co-owner's answers and delays are modelled (80% yes, 10% no, 10% never; "
            "lognormal delay, median 15 min).",
            "Scan time on the hub is modelled (200 MB/s, 5 ms per location); wall_ms is the "
            "Python run time on the 2-core test machine, not on a hub.",
            "Copies off the hub disk (phones, exports already shared) are outside any scan; the "
            "receipt lists them as not checked.",
            "AES-GCM is real (cryptography package); key storage at home is modelled as a dict, "
            "and a key store lost with the hub makes every backup unreadable.",
        ],
        "runtime_s": round(time.perf_counter() - w0, 1),
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps(out["results"], indent=1, default=str)[:6000])
    print("runtime_s", out["runtime_s"])


if __name__ == "__main__":
    main()
