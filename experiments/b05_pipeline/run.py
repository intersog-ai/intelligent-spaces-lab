"""B05 "The voice pipeline": staged local pipeline in the twin, four hub tiers, failure cases.

Two steps:
  measure.py  (audio, real models on this CPU)  -> measured.json   about 10 min on a shared 2 vCPU box
  run.py      (twin, virtual time)              -> results.json    under 1 min

run.py reads measured.json and
  1. re-scores every measured transcript with the current grammar (audio is not re-run);
  2. builds the four tiers (this CPU measured; Pi 5 class, mini PC, mini PC + GPU from labelled assumptions);
  3. runs N seeded household sessions per tier through islab.pipeline.VoicePipeline on a fresh twin
     (B01 devices, B01 PolicyEngine and ActionTracker). Each session: one satellite, three utterances
     25 s apart, drawn from the measured clips (their real transcripts, wake results and endpoint delays),
     the cut-off clips, and the TV-said-the-wake-word clips; device faults injected at labelled rates;
  4. repeats the mini PC tier with the 'reply on ack' baseline on the same seeds.

    python experiments/b05_pipeline/run.py              # 400 sessions per tier
    python experiments/b05_pipeline/run.py --sessions 50
"""
from __future__ import annotations
import argparse, collections, datetime as dt, json, math, pathlib, random, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from islab.core.sim import Sim                                       # noqa: E402
from islab.core.bus import Bus                                       # noqa: E402
from islab.twin.home import build_home                               # noqa: E402
from islab.policy.state import StateStore                            # noqa: E402
from islab.policy.engine import PolicyEngine                         # noqa: E402
from islab.pipeline import Grammar, StageModel, Utterance, VoicePipeline, build_tiers, TIER_ORDER  # noqa: E402
from islab.pipeline.intent import devices_of                         # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
COND_MIX = {"clean": 0.70, "tv_10db": 0.25, "tv_0db": 0.05}       # assumption: how often a TV plays under a command
P_CUTOFF_CLIP = 0.10              # share of utterances drawn from the paused-command set (assumption)
P_TV_WAKE = 0.05                  # share of "utterances" that are the TV saying the wake word (assumption)
FAULTS = {"stuck": 0.08, "offline": 0.04, "state_lost": 0.04}      # per session, on one device the session addresses (assumption)
ACTUATORS = ["light.kitchen", "light.living", "thermo.kitchen", "thermo.study", "garage.door", "lock.front",
             "lock.back", "shade.living", "valve.main"]
DEPLOYED_HANGOVER = 300
VOICE_PEOPLE = {"en_US-amy-low": "anna", "en_US-ryan-low": "mark", "en_GB-alan-low": "mark",
                "series:E06_Anna": "anna", "series:E06_Mark": "mark", "series:E06_Sam": "sam",
                "series:E0709_Anna": "anna"}


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, math.ceil(q * len(s)) - 1)], 1)


def share(k, n):
    return round(100.0 * k / n, 1) if n else None


def score(g, truth, hyp):
    a, b = g.parse(truth, "sat.kitchen"), g.parse(hyp, "sat.kitchen")
    if a.status != "ok":
        return "n/a"
    if b.status != "ok":
        return "asked" if b.status in ("ambiguous", "incomplete") else "not_understood"
    if a.calls == b.calls:
        return "correct"
    da = sorted(d for c in a.calls for d in devices_of(c))
    db = sorted(d for c in b.calls for d in devices_of(c))
    return "wrong_device" if da != db else "wrong_value"


# ------------------------------------------------------------------ audio results (re-scored)
def audio_results(m, g):
    out = {"stt_on_named_commands": {}, "wake": {}, "endpoint": {}}
    for cond in ("clean", "tv_10db", "tv_0db"):
        rs = [r for r in m["clips"] if r["condition"] == cond]
        named = [r for r in rs if r["kind"] == "named"]
        for eng in ("whisper", "zipformer"):
            c = collections.Counter(score(g, r["text"], r[f"hyp_{eng}"]) for r in named)
            n = len(named)
            out["stt_on_named_commands"].setdefault(eng, {})[cond] = {
                "n": n, **{k: share(c[k], n) for k in ("correct", "wrong_device", "wrong_value", "asked", "not_understood")}}
        out["wake"][cond] = {"n": len(rs), "detected_pct": share(sum(r["wake_detected"] for r in rs), len(rs))}
        out["endpoint"][cond] = {
            "n": len(rs), "cut_pct": share(sum(r["cut"] for r in rs), len(rs)),
            "listen_cap_hit_pct": share(sum(r["endpoint_timeout"] for r in rs), len(rs)),
            "silero_after_end_ms_p50": pct([r["endpoint_after_end_ms"] for r in rs], 0.5),
            "silero_after_end_ms_p95": pct([r["endpoint_after_end_ms"] for r in rs], 0.95),
            "webrtc_after_end_ms_p50": pct([r["webrtc_endpoint_after_end_ms"] for r in rs], 0.5),
            "webrtc_after_end_ms_p95": pct([r["webrtc_endpoint_after_end_ms"] for r in rs], 0.95),
            "webrtc_listen_cap_hit_pct": share(sum(r["webrtc_timeout"] for r in rs), len(rs))}
    # device-name errors: named commands, whisper, all conditions, which words went wrong
    ex = []
    for r in m["clips"]:
        if r["kind"] == "named":
            s = score(g, r["text"], r["hyp_whisper"])
            if s in ("wrong_device", "wrong_value"):
                ex.append({"condition": r["condition"], "said": r["text"], "heard": r["hyp_whisper"][:90], "score": s})
    out["stt_action_errors_whisper"] = ex
    # cut-offs
    cut = {}
    for h in (300, 500, 800):
        rs = [r for r in m["cutoffs"] if r["hangover_ms"] == h]
        for p in sorted({r["pause_s"] for r in rs}):
            rp = [r for r in rs if r["pause_s"] == p]
            cut.setdefault(str(h), {})[str(p)] = share(sum(r["cut"] for r in rp), len(rp))
    outcomes = {}
    for h in (300, 500, 800):
        cs = [r for r in m["cutoffs"] if r["hangover_ms"] == h and r["cut"]]
        plain = collections.Counter(score(g, r["text"], r["hyp"]) for r in cs)
        ext = collections.Counter((score(g, r["text"], r["hyp_extended"]) if r.get("extended") else
                                   score(g, r["text"], r["hyp"])) for r in cs)
        outcomes[str(h)] = {"n_cut": len(cs), "plain_endpoint": {k: share(v, len(cs)) for k, v in plain.items()},
                            "grammar_aware_endpoint": {k: share(v, len(cs)) for k, v in ext.items()},
                            "extended": sum(1 for r in cs if r.get("extended"))}
    out["cutoffs"] = {"cut_pct_by_hangover_and_pause": cut, "outcomes_of_cut_commands": outcomes,
                      "n_per_cell": len([r for r in m["cutoffs"] if r["hangover_ms"] == 300 and r["pause_s"] == 0.5])}
    tvw = m["tv_says_wake_word"]
    its = [g.parse(r.get("hyp", ""), "sat.living") for r in tvw if r["wake_detected"]]
    out["tv"] = {"no_wake_word": m["tv_false_accepts"],
                 "tv_says_wake_word": {"n": len(tvw), "accepted": sum(r["wake_detected"] for r in tvw),
                                       "actions_from_following_tv_speech": sum(1 for i in its if i.ok),
                                       "clarifying_questions": sum(1 for i in its if i.status in ("ambiguous", "incomplete")),
                                       "listened_s_p50": pct([r["listened_s"] for r in tvw if r["wake_detected"]], 0.5),
                                       "examples": [r.get("hyp") for r in tvw[:4]]}}
    return out


# ------------------------------------------------------------------ twin sessions
def build_pool(m, g):
    pool = {c: [r for r in m["clips"] if r["condition"] == c] for c in COND_MIX}
    cuts = [r for r in m["cutoffs"] if r["hangover_ms"] == DEPLOYED_HANGOVER]
    tvw = [r for r in m["tv_says_wake_word"] if r["wake_detected"]]
    return pool, cuts, tvw


def pick(rng, pool, cuts, tvw, sat, grammar_aware=True):
    u = rng.random()
    if u < P_TV_WAKE and tvw:
        r = rng.choice(tvw)
        return dict(case="tv_wake", person=None, text="(TV)", hyp=r["hyp"], speech_s=r["listened_s"],
                    wake=True, endpoint_ms=0.0, source="tv")
    if u < P_TV_WAKE + P_CUTOFF_CLIP:
        r = rng.choice(cuts)
        hyp = r.get("hyp_extended") if (grammar_aware and r.get("extended")) else r.get("hyp", r["text"])
        ep = r.get("extended_endpoint_after_end_ms") if (grammar_aware and r.get("extended")) else r["endpoint_after_end_ms"]
        return dict(case="paused_cut" if r["cut"] else "paused_ok", person=VOICE_PEOPLE[r["voice"]], text=r["text"],
                    hyp=hyp if r["cut"] else r["text"], speech_s=1.6 + r["pause_s"], wake=True,
                    endpoint_ms=ep, source="person", cut=r["cut"])
    x, cond = rng.random(), "clean"
    acc = 0.0
    for c, w in COND_MIX.items():
        acc += w
        if x < acc:
            cond = c
            break
    r = rng.choice(pool[cond])
    return dict(case=r["kind"], condition=cond, person=VOICE_PEOPLE[r["voice"]], text=r["text"], hyp=r["hyp_whisper"],
                speech_s=r["speech_s"], wake=r["wake_detected"], endpoint_ms=r["endpoint_after_end_ms"],
                source="person", cut=r["cut"])


def session(seed, tier, table, pool, cuts, tvw, reply_on="confirm", grammar_aware=True):
    crng = random.Random(seed * 7919 + 1)          # content draws, identical across tiers and designs
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed)
    store = StateStore(sim, bus)
    eng = PolicyEngine(sim, bus, store)
    g = Grammar()

    def answer(utt, d):          # the person says yes only if the question matches what they asked for
        return "yes" if d.tool in [c["name"] for c in g.parse(utt.text, utt.satellite).calls] else "no"
    pipe = VoicePipeline(sim, bus, eng, StageModel(tier, table), home=home, reply_on=reply_on, answer_fn=answer)
    sat = "sat.kitchen" if crng.random() < 0.7 else "sat.living"
    picks = [pick(crng, pool, cuts, tvw, sat, grammar_aware) for _ in range(3)]
    # the faulty device (if any) is one the session's requests address, so faults meet commands
    targets = sorted({d for c in picks for call in g.parse(c["text"], sat).calls for d in devices_of(call)
                      if d in ACTUATORS}) or ACTUATORS
    fault, fdev = None, None
    u = crng.random()
    acc = 0.0
    for f, p in FAULTS.items():
        acc += p
        if u < acc:
            fault, fdev = f, crng.choice(targets)
            break
    if fault == "stuck":
        home.faults.stuck(fdev)
    elif fault == "offline":
        home.faults.offline(fdev)
    elif fault == "state_lost":
        home.faults.link(f"state/{fdev}", drop=1.0)
    utts = []
    for k, c in enumerate(picks):
        utt = Utterance(f"s{seed}u{k}", sat, c["person"], 0.9 if c["person"] else None, c["text"], c["hyp"],
                        c["speech_s"], wake_detected=c["wake"], source=c["source"],
                        meta={"endpoint_ms": c["endpoint_ms"], "case": c["case"], "condition": c.get("condition"),
                              "cut": c.get("cut", False)})
        pipe.speak(utt, at=20.0 + 25.0 * k)
        utts.append(utt.uid)
    sim.run(until=150.0)
    rows = []
    for uid in utts:
        rec = pipe.records[uid]
        said = [l for l in rec["said"]]
        first = min((l["t_audio"] for l in said), default=None)
        devs = [d for r in rec["requests"] for d in devices_of({"name": r["tool"], "arguments": r["args"] or {}})]
        rows.append({
            "uid": uid, "case": rec["meta"]["case"], "condition": rec["meta"].get("condition"), "source": rec["source"],
            "cut": rec["meta"].get("cut"), "outcome": rec["outcome"], "interp": (rec["interp"] or {}).get("status"),
            "resolved_by": (rec["interp"] or {}).get("resolved_by"), "llm": bool(rec.get("llm_used")),
            "ttfa_ms": None if first is None else round((first - rec["t_speech_end"]) * 1000, 1),
            "decided_ms": (round((rec["stages"]["policy"]["t"] - rec["t_speech_end"]) * 1000, 1)
                           if "policy" in rec["stages"] else None),
            "earcon_ms": None if "t_earcon" not in rec else round((rec["t_earcon"] - rec["t_wake_end"]) * 1000, 1),
            "done_ms": next((round((l["t_audio"] - rec["t_speech_end"]) * 1000, 1) for l in said if l["kind"] == "done"), None),
            "said": [(l["kind"], l["text"]) for l in said], "checks": rec["done_checks"],
            "fault_hit": bool(fault and fdev in devs), "fault": fault if fdev in devs else None,
            "executed": any(r["status"] == "executing" or r["final"] in ("confirmed", "failed", "timed_out")
                            for r in rec["requests"]),
            "finals": [r["final"] for r in rec["requests"]], "tools": [r["tool"] for r in rec["requests"]],
            "calls": [{"name": r["tool"], "arguments": r["args"]} for r in rec["requests"]],
            "statuses": [r["status"] for r in rec["requests"]], "truth": rec["text"], "hyp": rec["hyp"]})
    return rows


def summarize(rows, g):
    people = [r for r in rows if r["source"] == "person"]
    heard = [r for r in people if r["outcome"] != "missed_wake"]
    tt = [r["ttfa_ms"] for r in heard if r["ttfa_ms"] is not None]
    tt_rules = [r["ttfa_ms"] for r in heard if r["ttfa_ms"] is not None and not r["llm"]]
    tt_llm = [r["ttfa_ms"] for r in heard if r["ttfa_ms"] is not None and r["llm"]]
    done = [r["done_ms"] for r in heard if r["done_ms"] is not None]
    executed = [r for r in heard if r["executed"]]
    dones = [c for r in rows for c in r["checks"]]
    exec_done_confirmed = sum(1 for r in executed if r["checks"] and all(c["confirmed_before"] for c in r["checks"]))
    exec_failure_told = sum(1 for r in executed if any(k == "failed" for k, _ in r["said"]))
    exec_silent = sum(1 for r in executed if not any(k in ("done", "failed") for k, _ in r["said"]))
    # wrong actions: executed commands whose tools/args differ from what was said
    wrong = 0
    for r in executed:
        a = g.parse(r["truth"], "sat.kitchen")
        if a.ok and r["case"] in ("named", "paused_cut", "paused_ok") and a.calls != r["calls"]:
            wrong += 1
    s = {"utterances": len(rows), "person_utterances": len(people), "heard": len(heard),
         "ttfa_ms": {"p50": pct(tt, 0.5), "p95": pct(tt, 0.95), "n": len(tt)},
         "ttfa_ms_rules_path": {"p50": pct(tt_rules, 0.5), "p95": pct(tt_rules, 0.95), "n": len(tt_rules)},
         "ttfa_ms_llm_stub_path": {"p50": pct(tt_llm, 0.5), "p95": pct(tt_llm, 0.95), "n": len(tt_llm)},
         "speech_end_to_policy_decision_ms": {"p50": pct([r["decided_ms"] for r in heard if r["decided_ms"] is not None], 0.5),
                                              "p95": pct([r["decided_ms"] for r in heard if r["decided_ms"] is not None], 0.95)},
         "ttfa_ms_executed": {"p50": pct([r["ttfa_ms"] for r in heard if r["executed"] and r["ttfa_ms"]], 0.5),
                              "p95": pct([r["ttfa_ms"] for r in heard if r["executed"] and r["ttfa_ms"]], 0.95)},
         "ttfa_ms_clean_rules_path": {"p50": pct([r["ttfa_ms"] for r in heard if r["ttfa_ms"] and not r["llm"] and r["condition"] in (None, "clean")], 0.5),
                                      "p95": pct([r["ttfa_ms"] for r in heard if r["ttfa_ms"] and not r["llm"] and r["condition"] in (None, "clean")], 0.95)},
         "earcon_ms": {"p50": pct([r["earcon_ms"] for r in heard if r["earcon_ms"] is not None], 0.5),
                       "p95": pct([r["earcon_ms"] for r in heard if r["earcon_ms"] is not None], 0.95)},
         "confirmed_done_ms": {"p50": pct(done, 0.5), "p95": pct(done, 0.95), "n": len(done)},
         "executed_commands": len(executed),
         "executed_done_after_device_confirmed_pct": share(exec_done_confirmed, len(executed)),
         "executed_failure_told_pct": share(exec_failure_told, len(executed)),
         "executed_no_outcome_said_pct": share(exec_silent, len(executed)),
         "done_claims": len(dones),
         "done_claims_confirmed_before_pct": share(sum(c["confirmed_before"] for c in dones), len(dones)),
         "done_claims_true_at_audio_pct": share(sum(bool(c["true_at_audio"]) for c in dones), len(dones)),
         "executed_wrong_tool": wrong,
         "approval_requests": sum(r["statuses"].count("awaiting_approval") for r in rows),
         "approval_requests_not_asked_for": sum(
             1 for r in rows for t, st in zip(r["tools"], r["statuses"]) if st == "awaiting_approval"
             and t not in [c["name"] for c in g.parse(r["truth"], "sat.kitchen").calls])}
    return s


def failure_cases(rows, g):
    people = [r for r in rows if r["source"] == "person"]
    tv = [r for r in rows if r["source"] == "tv"]

    def said_top(rs, kinds=None):
        c = collections.Counter(t for r in rs for k, t in r["said"] if kinds is None or k in kinds)
        return [t for t, _ in c.most_common(2)]
    named = [r for r in people if r["case"] == "named" and r["outcome"] != "missed_wake"]
    cut = [r for r in people if r["case"] == "paused_cut"]
    amb = [r for r in people if r["case"] in ("context", "series")]
    fault = [r for r in people if r["fault_hit"] and r["executed"]]
    missed = [r for r in people if r["outcome"] == "missed_wake"]
    out = {
        "wake_missed": {"n": len(missed), "of": len(people), "pct": share(len(missed), len(people)),
                        "says": "nothing (no earcon; the person has to repeat)"},
        "tv_wake_word": {"n": len(tv), "actions": sum(r["executed"] for r in tv),
                         "spoken_replies_pct": share(sum(bool(r["said"]) for r in tv), len(tv)), "says": said_top(tv)},
        "cut_off": {"n": len(cut), "of": len(people), "pct": share(len(cut), len(people)),
                    "outcomes": dict(collections.Counter(r["outcome"] for r in cut)),
                    "executed_not_what_was_said": sum(1 for r in cut if r["executed"] and
                                                      g.parse(r["truth"], "sat.kitchen").calls != r["calls"]),
                    "says": said_top(cut, ("clarify", "not_understood"))},
        "stt_on_named": {"n": len(named),
                         "not_understood_pct": share(sum(r["outcome"] == "not_understood" for r in named), len(named)),
                         "clarify_pct": share(sum(r["outcome"] == "clarify" for r in named), len(named)),
                         "says": said_top(named, ("not_understood", "clarify"))},
        "ambiguous": {"n": len(amb),
                      "resolved_by_context_pct": share(sum(r["resolved_by"] == "context" and r["interp"] == "ok" for r in amb), len(amb)),
                      "resolved_by_room_or_relative_pct": share(sum(r["resolved_by"] in ("room", "relative") and r["interp"] == "ok" for r in amb), len(amb)),
                      "asked_pct": share(sum(r["outcome"] == "clarify" for r in amb), len(amb)),
                      "not_a_request_pct": share(sum(r["outcome"] == "not_understood" for r in amb), len(amb)),
                      "says": said_top(amb, ("clarify",))},
        "device_not_confirming": {"n": len(fault),
                                  "by_fault": dict(collections.Counter(r["fault"] for r in fault)),
                                  "failure_told_pct": share(sum(any(k == "failed" for k, _ in r["said"]) for r in fault), len(fault)),
                                  "done_said_pct": share(sum(any(k == "done" for k, _ in r["said"]) for r in fault), len(fault)),
                                  "done_said_true_pct": share(sum(1 for r in fault for c in r["checks"] if c["true_at_audio"]),
                                                              sum(len(r["checks"]) for r in fault)),
                                  "done_note": "a 'Done' here means the device already showed the target state (e.g. a stuck light that was already off)",
                                  "says": said_top(fault, ("failed",))},
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=400)
    a = ap.parse_args()
    t0 = time.perf_counter()
    m = json.load(open(HERE / "measured.json"))
    g = Grammar()
    audio = audio_results(m, g)
    tiers = build_tiers(m["stages"])
    pool, cuts, tvw = build_pool(m, g)
    res = {"audio": audio, "tiers": {}, "baseline_ack": {}, "failure_cases": {}}
    all_rows = {}
    for tier in TIER_ORDER:
        rows = [r for s in range(a.sessions) for r in session(s, tier, tiers[tier], pool, cuts, tvw)]
        all_rows[tier] = rows
        res["tiers"][tier] = {"label": tiers[tier]["label"], **summarize(rows, g),
                              "stage_sources": {k: v.get("source") for k, v in tiers[tier]["stages"].items()},
                              "stage_ms": {k: {"p50": v["p50"], "p95": v["p95"]} for k, v in tiers[tier]["stages"].items()}}
        print(tier, res["tiers"][tier]["ttfa_ms"], round(time.perf_counter() - t0, 1), "s", flush=True)
    res["failure_cases"] = failure_cases(all_rows["minipc_cpu"], g)
    rows = [r for s in range(a.sessions) for r in session(s, "minipc_cpu", tiers["minipc_cpu"], pool, cuts, tvw, reply_on="ack")]
    res["baseline_ack"] = summarize(rows, g)
    bad = [r for r in rows if r["fault_hit"] and r["executed"]]
    res["baseline_ack"]["device_not_confirming"] = {
        "n": len(bad), "done_said_pct": share(sum(any(k == "done" for k, _ in r["said"]) for r in bad), len(bad))}
    rows = [r for s in range(a.sessions) for r in session(s, "minipc_cpu", tiers["minipc_cpu"], pool, cuts, tvw,
                                                          grammar_aware=False)]
    cut = [r for r in rows if r["case"] == "paused_cut"]
    res["ablation_plain_endpoint"] = {"cut_utterances": len(cut),
                                      "outcomes": dict(collections.Counter(r["outcome"] for r in cut)),
                                      "executed_not_what_was_said": sum(1 for r in cut if r["executed"] and
                                                                        g.parse(r["truth"], "sat.kitchen").calls != r["calls"])}
    res["examples"] = [{"truth": r["truth"], "heard": r["hyp"], "said": r["said"], "ttfa_ms": r["ttfa_ms"]}
                       for r in all_rows["minipc_cpu"][:12]]
    doc = {
        "experiment": "b05_pipeline", "date": dt.date.today().isoformat(),
        "method": ("Audio (measure.py -> measured.json): real local components on this 2 vCPU x86 VM: openWakeWord "
                   "hey_jarvis, silero-vad (300 ms hangover) and webrtcvad, sherpa-onnx Whisper tiny.en and streaming "
                   "zipformer, Piper low voices. Test audio: 25 commands x 3 Piper voices + 5 Season 1 household lines, "
                   "each after a synthetic 'Hey Jarvis', in three conditions (clean; series narration through a TV-speaker "
                   "EQ at 10 dB and 0 dB below the command); 6 commands split by a 0.3-0.9 s pause, endpointed at 300/500/800 ms "
                   "hangover; 8.7 min of narration, assistant voices and music through the TV EQ for wake-word false "
                   "accepts; 12 cases of the TV itself saying the wake word. Transcripts are re-scored by the B05 grammar "
                   "in run.py. Twin (run.py): per tier, N seeded sessions on a fresh B01 twin; three utterances 25 s apart "
                   "drawn from the measured clips (their transcript, wake result and endpoint delay), the cut-off set and "
                   "the TV-wake set; stage times drawn from the tier table (this CPU measured; other tiers labelled "
                   "assumptions); B01 PolicyEngine and ActionTracker decide and confirm; ground truth = the twin device's "
                   "physical state when 'Done' is heard. Time to first audio = end of the person's speech to the first "
                   "reply audio (question, hold line, answer or 'Done'); the earcon after the wake word is reported apart."),
        "params": {"sessions_per_tier": a.sessions, "utterances_per_session": 3, "condition_mix": COND_MIX,
                   "p_cutoff_clip": P_CUTOFF_CLIP, "p_tv_wake": P_TV_WAKE, "faults_per_session": FAULTS,
                   "deployed_hangover_ms": DEPLOYED_HANGOVER, "hold_s": 1.5, "stt_engine_in_twin": m["stt_engine_chosen"],
                   "tiers": {k: tiers[k]["label"] for k in TIER_ORDER}, "measured_stages_this_cpu": m["stages"],
                   "blocked": m["blocked"], "machine": m["_machine"], "measure_wall_s": m.get("wall_s")},
        "results": res,
        "limitations": [
            "All speech is synthetic (Piper, espeak phonemes, and the Season 1 synthetic voices); no human speakers, no real room, no far-field microphone. Accuracy numbers will be worse with real voices and rooms.",
            "TV-like speech is the series narrator and assistant voices through an EQ, mixed digitally; no acoustic echo cancellation was modelled, which a real satellite would use against its own TV/speaker output.",
            "Only 8.7 minutes of TV-like audio: zero false accepts bounds the rate only below about 21 per hour (95%).",
            "The Pi 5, mini PC and GPU tiers are not measured: they scale this CPU's measured numbers or reuse B01 assumptions, as labelled per stage.",
            "This CPU was shared with other jobs during measure.py: Whisper tiny decode (p50 396 ms) and Piper first audio (p50 360 ms) are slower than B01's unloaded measurement on the same machine (166 ms, 87 ms).",
            "No LLM was run: model weights are hosted on huggingface.co, blocked here. The LLM path is a deterministic fuzzy matcher with assumed LLM latency.",
            "Every tier uses the same Whisper tiny transcripts; a larger STT model on the GPU tier would likely make fewer errors.",
            "Voice identity is a fixed confidence of 0.9 for household voices (speaker ID is episode B06).",
            "Fault, TV and cut-off rates in the twin are scenario parameters; the per-case outcome shares are what the run measures.",
        ]}
    with open(HERE / "results.json", "w") as f:
        json.dump(doc, f, indent=1, default=str)
    print("results.json written", round(time.perf_counter() - t0, 1), "s")


if __name__ == "__main__":
    main()
