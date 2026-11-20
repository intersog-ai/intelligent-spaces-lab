"""B06 "Who is speaking": speaker identity in a household, replay, liveness, overlap, and the trust policy.

Run:  ISLAB_VO=/path/to/voice-over python experiments/b06_identity/run.py      (~1-3 min on 2 CPUs)
Writes experiments/b06_identity/results.json.

Household (islab/identity/household.yaml): anna = "Amanda" voice, mark = "David" narrator voice,
sam (teen) = "Ollie" voice. Guests, never enrolled: the S1 character voices Courtney, Chris, Eric and the
"AnnaAssist" voice (all ElevenLabs TTS), plus three LibriSpeech clips bundled with sherpa-onnx (real
human recordings) if ISLAB_REAL_GUESTS points at them.

Everything the hub hears passes through the B01 satellite channel (synthetic room IR RT60 0.4 s + pink
noise 30 dB). Enrollment = first 10 s of each member's speech. The rest is split in time: first half
-> calibration (thresholds, liveness fit, overlap rule), second half -> evaluation (every reported rate).
"""
from __future__ import annotations

import copy
import datetime
import glob
import json
import os
import re
import sys
import time

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from islab.voice import audio                                    # noqa: E402
from islab.voice.speaker import THRESHOLDS as B01_THR, ResemblyzerEncoder   # noqa: E402
from islab.identity import (FEATURES, REPLAY_DEVICES, ChallengePhrase, LivenessCheck, TrustPolicy,  # noqa: E402
                            liveness_features, load_config)
from islab.identity.home import IdentityHome                      # noqa: E402
from islab.identity.voices import (GUEST_VOICES, load_segments, score_matrix, split_seconds, unit,  # noqa: E402
                                   vo_dir, windows)

OUT = os.path.join(os.path.dirname(__file__), "results.json")
SEED = 606
ENROLL_S = 10.0
CAP = 60                       # max windows per member per split (David has ~210)
PI_GUEST = 0.2                 # share of requests from unrecognised people in the cost model
COST = {"comfort": 1.0, "named": 10.0}     # cost of a false accept relative to a false reject
MARGINS = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08]
GRID = np.round(np.arange(0.60, 0.95, 0.0025), 4)
REAL_GUESTS = os.environ.get("ISLAB_REAL_GUESTS",
                             "/home/claude/work/islab_models/x/sherpa-onnx-whisper-tiny.en/test_wavs")


def r4(x):
    return None if x is None else round(float(x), 4)


class Emb:
    def __init__(self):
        import torch
        torch.set_num_threads(1)       # 30x faster than 2 threads on a shared 2-core machine
        self.enc = ResemblyzerEncoder()
        self.n, self.t = 0, 0.0

    def __call__(self, ws):
        t0 = time.perf_counter()
        E = np.array([self.enc.embed(w) for w in ws]) if len(ws) else np.zeros((0, 256))
        self.n += len(ws); self.t += time.perf_counter() - t0
        return E


def decide(S, names, thr, margin):
    """Rows of scores -> index of accepted person or -1."""
    order = np.argsort(-S, axis=1)
    best = order[:, 0]
    s1 = S[np.arange(len(S)), best]
    s2 = S[np.arange(len(S)), order[:, 1]] if S.shape[1] > 1 else np.full(len(S), -1.0)
    ok = (s1 >= thr) & ((s1 - s2) >= margin)
    return np.where(ok, best, -1)


def id_rates(S_mem, true_idx, S_guest, thr, margin):
    dm = decide(S_mem, None, thr, margin)
    dg = decide(S_guest, None, thr, margin) if len(S_guest) else np.zeros(0, int)
    return {"FRR": float(np.mean(dm == -1)), "MIS": float(np.mean((dm != -1) & (dm != true_idx))),
            "correct": float(np.mean(dm == true_idx)),
            "guest_FAR": float(np.mean(dg != -1)) if len(dg) else None}


def cost(r, c_fa):
    return (1 - PI_GUEST) * (r["FRR"] + c_fa * r["MIS"]) + PI_GUEST * c_fa * (r["guest_FAR"] or 0.0)


def rates_out(r, n_mem, n_guest, thr, margin):
    return {"threshold": r4(thr), "margin": r4(margin), "FRR": r4(r["FRR"]), "misattributed": r4(r["MIS"]),
            "correct": r4(r["correct"]), "guest_FAR": r4(r["guest_FAR"]), "n_member_windows": n_mem,
            "n_guest_windows": n_guest}


def confusion(S, true_labels, names, thr, margin):
    d = decide(S, names, thr, margin)
    rows = sorted(set(true_labels), key=lambda x: (x not in names, x))
    out = {}
    for r in rows:
        m = np.array([t == r for t in true_labels])
        dd = d[m]
        out[r] = {**{n: int(np.sum(dd == i)) for i, n in enumerate(names)}, "unknown": int(np.sum(dd == -1)),
                  "n": int(m.sum())}
    return out


def main(seed=SEED, write=True, segs=None, verbose=True):
    t0 = time.perf_counter()
    rng = np.random.default_rng(seed)
    cfg = load_config()
    pol0 = TrustPolicy(cfg)
    members = list(cfg["people"])
    voice = {p: cfg["people"][p]["voice"] for p in members}
    emb = Emb()
    room = lambda ws: [audio.satellite_channel(w, rng) for w in ws]

    segs = segs if segs is not None else load_segments()
    speech_s = {k: round(sum(len(s) for s in v) / audio.SR, 2) for k, v in segs.items()}
    print("speech seconds:", speech_s)

    # ---------------- members: enroll / calib / eval ----------------
    profiles, calib_w, eval_w = {}, {}, {}
    for p in members:
        e, rest = split_seconds(segs[voice[p]], ENROLL_S)
        profiles[p] = unit(emb(room(windows(e))).mean(0))
        ws = windows(rest)
        h = len(ws) // 2
        calib_w[p], eval_w[p] = ws[:h][:CAP], ws[h:][:CAP]
    # guests: TTS voices + optional real recordings; split each guest's windows in time
    guest_src = {g: segs[g] for g in GUEST_VOICES if g in segs}
    real = sorted(glob.glob(os.path.join(REAL_GUESTS, "*.wav")))
    if real:
        guest_src["LibriSpeech"] = [audio.load(f) for f in real]
    g_cal, g_ev = {}, {}
    for g, s in guest_src.items():
        ws = windows(s)
        if len(ws) < 2:          # Eric (1.4 s): one window, used in evaluation only
            g_cal[g], g_ev[g] = [], ws
            continue
        h = len(ws) // 2
        g_cal[g], g_ev[g] = ws[:h], ws[h:]

    def emb_set(d, chan=room):
        return {k: emb(chan(v)) if len(v) else np.zeros((0, 256)) for k, v in d.items()}

    Ec, Ee = emb_set(calib_w), emb_set(eval_w)
    Gc, Ge = emb_set(g_cal), emb_set(g_ev)

    def stack(E, names):
        X = np.vstack([E[k] for k in names if len(E[k])]) if any(len(E[k]) for k in names) else np.zeros((0, 256))
        lab = [k for k in names for _ in range(len(E[k]))]
        return X, lab

    Xc, lc = stack(Ec, members); Xe, le = stack(Ee, members)
    XGc, lgc = stack(Gc, list(Gc)); XGe, lge = stack(Ge, list(Ge))
    Sc, names = score_matrix(Xc, profiles); Se, _ = score_matrix(Xe, profiles)
    SGc, _ = score_matrix(XGc, profiles) if len(XGc) else (np.zeros((0, 3)), names)
    SGe, _ = score_matrix(XGe, profiles)
    tc = np.array([names.index(x) for x in lc]); te = np.array([names.index(x) for x in le])

    # ---------------- threshold calibration (cost model) ----------------
    calib = {}
    for tier, c_fa in COST.items():
        best = None
        for m in (MARGINS if tier == "named" else [0.0]):
            for t in GRID:
                r = id_rates(Sc, tc, SGc, t, m)
                k = (round(cost(r, c_fa), 6), -t, m)
                if best is None or k < best[0]:
                    best = (k, t, m, r)
        _, t, m, r = best
        ev = id_rates(Se, te, SGe, t, m)
        calib[tier] = {"cost_false_accept_vs_false_reject": c_fa, "threshold": r4(t), "margin": r4(m),
                       "calibration": rates_out(r, len(tc), len(SGc), t, m),
                       "evaluation": rates_out(ev, len(te), len(SGe), t, m)}
    sweep = []
    for t in np.round(np.arange(0.70, 0.90, 0.01), 2):
        ev = id_rates(Se, te, SGe, t, 0.0)
        sweep.append({"threshold": float(t), "FRR": r4(ev["FRR"]), "misattributed": r4(ev["MIS"]),
                      "guest_FAR": r4(ev["guest_FAR"])})
    b01 = {k: rates_out(id_rates(Se, te, SGe, B01_THR[k], 0.0), len(te), len(SGe), B01_THR[k], 0.0)
           for k in ("room_eer", "room_strict")}
    T = {k: (calib[k]["threshold"], calib[k]["margin"]) for k in COST}
    # policy with the calibrated tiers (the YAML is checked against these by the tests)
    cfg_cal = copy.deepcopy(cfg)
    cfg_cal["tiers"] = {k: {"threshold": T[k][0], "margin": T[k][1]} for k in T}

    # overlap rule: second-best score threshold with <= 5 % of single-speaker calibration windows flagged
    sec_c = np.sort(Sc, axis=1)[:, -2]
    ov_thr = r4(np.quantile(sec_c, 0.95))
    cfg_cal["overlap"] = {"second_score": ov_thr}
    pol = TrustPolicy(cfg_cal)
    print("calibrated:", T, "overlap", ov_thr)

    conf = {tier: {"members": confusion(Se, le, names, *T[tier], ),
                   "guests": confusion(SGe, lge, names, *T[tier])} for tier in T}

    # ---------------- noise, retries, longer utterances ----------------
    noise_bank = audio.kitchen_noise(audio.SR * 60, rng)

    def noisy(ws, snr):
        out = []
        for w in room(ws):
            o = rng.integers(0, len(noise_bank) - len(w))
            out.append(audio.mix_snr(w, noise_bank[o:o + len(w)], snr))
        return out

    def other_room(ws):
        return [audio.satellite_channel(w, rng, rt60=0.8, snr_db=20) for w in ws]

    cond = {}
    for cname, chan in (("kitchen_noise_20dB", lambda ws: noisy(ws, 20)), ("kitchen_noise_10dB", lambda ws: noisy(ws, 10)),
                        ("other_room_rt60_0.8_snr20", other_room)):
        Wx = {p: chan(eval_w[p]) for p in members}
        Ex = {p: emb(Wx[p]) for p in members}
        X, _ = stack(Ex, members)
        S, _ = score_matrix(X, profiles)
        Fx = np.array([liveness_features(w) for p in members for w in Wx[p]])
        cond[cname] = {"embeddings": Ex, "S": S, "F": Fx,
                       "rates": {tier: rates_out(id_rates(S, te, np.zeros((0, 3)), *T[tier]), len(te), 0, *T[tier])
                                 for tier in T}}
    # retries: windows i and i+2 do not overlap (2 s windows, 1 s hop)
    retry = {}
    for tier in T:
        one, two, longer, n = 0, 0, 0, 0
        for p in members:
            S, _ = score_matrix(Ee[p], profiles)
            d = decide(S, names, *T[tier]); ip = names.index(p)
            for i in range(0, len(d) - 2, 3):
                n += 1
                a, b = d[i] == ip, d[i + 2] == ip
                one += a; two += (a or b)
                S4, _ = score_matrix(unit(Ee[p][i] + Ee[p][i + 2])[None], profiles)
                longer += decide(S4, names, *T[tier])[0] == ip
        retry[tier] = {"n_requests": n, "recognised_first_try": r4(one / n), "recognised_within_two_tries": r4(two / n),
                       "recognised_with_4s_utterance": r4(longer / n),
                       "refused_after_two_tries": r4(1 - two / n)}

    # ---------------- liveness (one-class, fitted on calibration live windows only) ----------------
    live_cal_w = [w for p in members for w in room(calib_w[p])]
    Fcal = np.array([liveness_features(w) for w in live_cal_w])
    live_ev_rooms = {p: room(eval_w[p]) for p in members}
    Fev = {p: np.array([liveness_features(w) for w in live_ev_rooms[p]]) for p in members}
    Fev_all = np.vstack([Fev[p] for p in members])
    lv = LivenessCheck(max_live_reject=0.05).fit_features(Fcal)
    lv_max = LivenessCheck(max_live_reject=0.05, rule="max").fit_features(Fcal)   # variant, reported only
    # replays: member eval windows -> replay device -> the room -> satellite
    rep = {}
    for dev, fn in REPLAY_DEVICES.items():
        ws = {p: room([fn(w, rng) for w in eval_w[p]]) for p in members}
        F = {p: np.array([liveness_features(w) for w in ws[p]]) for p in members}
        E = {p: emb(ws[p]) for p in members}
        rep[dev] = {"F": F, "E": E}
    rep_out = {}
    loo = {}
    for dev, d in rep.items():
        acc = {t: [0, 0] for t in T}; n = 0; acc_live = {t: 0 for t in T}; live_pass = 0
        for p in members:
            S, _ = score_matrix(d["E"][p], profiles)
            L = lv.is_live_features(d["F"][p])
            live_pass += int(L.sum()); n += len(L)
            for tier in T:
                dd = decide(S, names, *T[tier]) == names.index(p)
                acc[tier][0] += int(dd.sum())
                acc_live[tier] += int((dd & L).sum())
        Frep_all = np.vstack([d["F"][p] for p in members])
        rep_max = r4(lv_max.is_live_features(Frep_all).mean())
        rep_out[dev] = {"n": n, "liveness_pass": r4(live_pass / n), "liveness_pass_max_rule": rep_max,
                        **{f"accepted_as_victim_{t}_no_liveness": r4(acc[t][0] / n) for t in T},
                        **{f"accepted_as_victim_{t}_with_liveness": r4(acc_live[t] / n) for t in T}}
        # also B01's room EER threshold, to connect to the 81 % figure
        S_all = np.vstack([score_matrix(d["E"][p], profiles)[0] for p in members])
        tv = np.concatenate([np.full(len(d["E"][p]), names.index(p)) for p in members])
        Lall = lv.is_live_features(np.vstack([d["F"][p] for p in members]))
        db = decide(S_all, names, B01_THR["room_eer"], 0.0) == tv
        rep_out[dev]["accepted_as_victim_at_B01_room_eer_no_liveness"] = r4(db.mean())
        rep_out[dev]["accepted_as_victim_at_B01_room_eer_with_liveness"] = r4((db & Lall).mean())
        # leave-one-feature-out: replay liveness pass rate
        Frep = np.vstack([d["F"][p] for p in members])
        loo[dev] = {}
        for f in FEATURES:
            use = tuple(x for x in FEATURES if x != f)
            l2 = LivenessCheck(0.05, use).fit_features(Fcal)
            loo[dev][f"without_{f}"] = r4(l2.is_live_features(Frep).mean())
    # live false rejects by the liveness check
    live_fr = {"household_eval_same_room": r4(1 - lv.is_live_features(Fev_all).mean()),
               "household_eval_same_room_max_rule": r4(1 - lv_max.is_live_features(Fev_all).mean()),
               "n": int(len(Fev_all))}
    for cname in cond:
        live_fr[cname] = r4(1 - lv.is_live_features(cond[cname]["F"]).mean())
    for g in g_ev:
        F = np.array([liveness_features(w) for w in room(g_cal[g] + g_ev[g])])
        live_fr[f"guest_{g}"] = r4(1 - lv.is_live_features(F).mean())
    feat_means = {"live_calibration": dict(zip(FEATURES, [r4(x) for x in Fcal.mean(0)]))}
    for dev, d in rep.items():
        feat_means[dev] = dict(zip(FEATURES, [r4(x) for x in np.vstack(list(d["F"].values())).mean(0)]))

    # ---------------- challenge phrase (modelled with transcripts) ----------------
    lines = json.load(open(os.path.join(vo_dir(), "lines.json"))) if os.path.exists(os.path.join(vo_dir(), "lines.json")) else {}
    txt = {}
    for ep in lines.values():
        for spk, t in ep:
            txt.setdefault(spk, []).append(t)
    for f in glob.glob(os.path.join(vo_dir(), "*.txt")):
        v = os.path.basename(f).split("_", 1)[1].rsplit(".", 1)[0]
        txt.setdefault(v, []).extend(x for x in re.split(r"\n\s*\n", open(f).read()) if x.strip())
    cp = ChallengePhrase(np.random.default_rng(seed))
    chal = {}
    for p in members:
        corpus = txt.get(voice[p], [])
        n_ok = 0
        for _ in range(1000):
            ph = cp.issue()
            n_ok += any(ChallengePhrase.verify(c, ph) for c in corpus)
        chal[p] = {"recorded_lines_available_to_attacker": len(corpus), "challenges": 1000,
                   "a_recorded_line_contains_the_phrase": n_ok}

    # ---------------- two people talking ----------------
    pairs = [("sam", "anna"), ("sam", "mark"), ("anna", "mark")]
    overlap = {}
    ov_ev = {}
    for a, b in pairs:
        n = min(len(eval_w[a]), len(eval_w[b]), 20)
        for sir in (-6, 0, 6):
            mixes = []
            for i in range(n):
                wa, wb = eval_w[a][i], eval_w[b][i]
                L = min(len(wa), len(wb))
                wa, wb = wa[:L], wb[:L]
                g = np.sqrt((np.mean(wa ** 2) + 1e-12) / (np.mean(wb ** 2) + 1e-12) / 10 ** (sir / 10))
                mixes.append((wa + g * wb).astype(np.float32))
            E = emb(room(mixes))
            S, _ = score_matrix(E, profiles)
            key = f"{a}+{b}@{sir:+d}dB"
            ov_ev[key] = S
            res = {"n": n, "sir_db_first_over_second": sir}
            for tier in T:
                for label, mg in (("no_overlap_rule", None), ("with_overlap_rule", True)):
                    d = decide(S, names, *T[tier])
                    if mg:
                        flagged = np.sort(S, axis=1)[:, -2] >= ov_thr
                        if tier == "named":
                            d = np.where(flagged, -1, d)
                    res[f"{tier}_{label}"] = {"named_first": int(np.sum(d == names.index(a))),
                                              "named_second": int(np.sum(d == names.index(b))),
                                              "none": int(np.sum(d == -1))}
            res["overlap_flagged"] = int(np.sum(np.sort(S, axis=1)[:, -2] >= ov_thr))
            overlap[key] = res
    single_flag = r4(np.mean(np.sort(Se, axis=1)[:, -2] >= ov_thr))
    mix_flag = r4(np.mean(np.concatenate([np.sort(S, axis=1)[:, -2] >= ov_thr for S in ov_ev.values()])))

    # ---------------- requests through the twin's policy engine ----------------
    intents_adult = [("calendar", None), ("music", None), ("temperature", None),
                     ("set_light", {"room": "kitchen", "on": True}), ("close_garage", {}),
                     ("unlock_door", {"door": "front"}), ("open_garage", {}), ("disarm_alarm", {})]
    intents_attack = [("unlock_door", {"door": "front"}), ("disarm_alarm", {}), ("open_garage", {}), ("calendar", None)]
    reqs = []   # (kind, true_person, evidence, intent, args, legit)

    def ev_of(S_row, F_row=None, live=None):
        sc = dict(zip(names, map(float, S_row)))
        if live is None and F_row is not None:
            live = bool(lv.is_live_features(F_row[None])[0])
        return pol.evidence(sc, live=live)

    k = 0
    for p in members:
        S, _ = score_matrix(Ee[p], profiles)
        for i in range(len(S)):
            it, a = intents_adult[k % len(intents_adult)]; k += 1
            reqs.append(("family", p, ev_of(S[i], Fev[p][i]), it, a, True))
    # the same requests in kitchen noise (20 dB): realistic refusals
    Sn, Fn = cond["kitchen_noise_20dB"]["S"], cond["kitchen_noise_20dB"]["F"]
    for j, p in enumerate(le):
        it, a = intents_adult[(j + 3) % len(intents_adult)]
        reqs.append(("family_noise20", p, ev_of(Sn[j], Fn[j]), it, a, True))
    k = 0
    for g in g_ev:
        S, _ = score_matrix(Ge[g], profiles) if len(Ge[g]) else (np.zeros((0, 3)), names)
        Fg = np.array([liveness_features(w) for w in room(g_ev[g])]) if len(g_ev[g]) else None
        for i in range(len(S)):
            it, a = intents_attack[k % len(intents_attack)]; k += 1
            reqs.append(("guest", g, ev_of(S[i], Fg[i]), it, a, False))
    k = 0
    for dev, d in rep.items():
        for p in members:
            S, _ = score_matrix(d["E"][p], profiles)
            for i in range(len(S)):
                it, a = intents_attack[k % len(intents_attack)]; k += 1
                reqs.append((f"replay_{dev}", p, ev_of(S[i], d["F"][p][i]), it, a, False))
    for key, S in ov_ev.items():
        if not key.startswith("sam+"):
            continue
        for i in range(len(S)):
            it, a = [("calendar", None), ("disarm_alarm", {}), ("unlock_door", {"door": "front"})][i % 3]
            reqs.append(("teen_over_parent", "sam", ev_of(S[i], live=True), it, a, False))
    # a family member's spoken "yes" to a pending level-3 approval (must be refused)
    def run_engine(engine_cfg=None, careless=0.0, label="policy"):
        h = IdentityHome(seed=seed, policy=pol, engine_config=engine_cfg)
        crng = np.random.default_rng(seed + 1)
        out = []
        voice_yes = {"tried": 0, "accepted": 0}
        for kind, who, ev, it, a, legit in reqs:
            if it in ("calendar", "music", "temperature"):
                v = pol.personal(ev, "calendar" if it == "calendar" else it)
                wrong = v.person is not None and v.person != who
                out.append({"kind": kind, "who": who, "intent": it, "legit": legit, "tier": v.tier,
                            "result": v.kind, "served_person": v.person, "wrong_person": wrong, "say": v.say})
                continue
            o = h.request(ev, it, a, text=it.replace("_", " "))
            rec = {"kind": kind, "who": who, "intent": it, "legit": legit, "tier": o.tier, "status": o.status,
                   "reason": o.reason, "actor": o.actor, "say": o.say, "request_id": o.request_id}
            if o.status == "awaiting_approval" and kind == "family" and who in ("anna", "mark"):
                voice_yes["tried"] += 1
                voice_yes["accepted"] += int(h.voice_approval(ev, o.request_id))
            if o.status in ("awaiting_approval", "asked"):
                asked = h.engine.pending[o.request_id].asked if o.request_id in h.engine.pending else []
                responder = who if (legit and who in ("anna", "mark")) else "anna"
                if responder not in asked and asked:
                    responder = asked[0]
                if legit:
                    h.phone_reply(responder, o.request_id, "yes", after_s=float(crng.uniform(5, 40)))
                elif careless and crng.random() < careless:
                    h.phone_reply(responder, o.request_id, "yes", after_s=float(crng.uniform(5, 40)))
                    rec["careless_yes"] = True
                else:
                    h.phone_reply(responder, o.request_id, "no", after_s=float(crng.uniform(5, 40)))
            out.append(rec)
            h.sim.run(until=h.sim.now + 150)
        h.sim.run(until=h.sim.now + 300)
        ex = {e["request_id"]: e for e in h.level3_executions()}
        for rec in out:
            rec["l3_executed"] = rec.get("request_id") in ex
            rec["l3_via"] = ex[rec["request_id"]]["approved_via"] if rec["l3_executed"] else None
        return h, out, voice_yes

    def summarize(h, out, voice_yes):
        l3 = h.level3_executions()
        attacks = [r for r in out if not r["legit"]]
        by_kind = {}
        for r in attacks:
            b = by_kind.setdefault(r["kind"], {"requests": 0, "level3_requests": 0, "level3_executed": 0,
                                               "private_data_read_out_to_wrong_person": 0})
            b["requests"] += 1
            if r["intent"] in ("unlock_door", "open_garage", "disarm_alarm"):
                b["level3_requests"] += 1
                b["level3_executed"] += int(r["l3_executed"])
            if r["intent"] == "calendar" and r.get("result") == "serve":
                b["private_data_read_out_to_wrong_person"] += 1
        return {"requests": len(out), "level3_executed_total": len(l3),
                "level3_executed_by_voice_alone": h.level3_by_voice_alone(),
                "level3_executed_with_phone_approval": sum(1 for e in l3 if e["approved_via"] == "phone"),
                "attacks": by_kind, "spoken_yes_to_level3_approval": voice_yes}

    h_main, out_main, vy = run_engine()
    s_main = summarize(h_main, out_main, vy)
    h_c, out_c, vyc = run_engine(careless=0.10)
    s_care = summarize(h_c, out_c, vyc)
    weak = pol.engine_config()
    for t in ("unlock_door", "open_garage", "disarm_alarm"):
        weak["levels"][t] = 1
    # voice authorizes: level 3 acts on a voice request, but only for a recognised (named) person who is
    # permitted; an unrecognised voice can no longer even ask for a door
    weak["permissions"]["guest"]["allow"] = [t for t in weak["permissions"]["guest"]["allow"]
                                             if t not in ("unlock_door", "open_garage", "disarm_alarm")]
    h_w, out_w, vyw = run_engine(engine_cfg=weak, label="weakened")
    s_weak = summarize(h_w, out_w, vyw)

    # family outcomes under the real policy
    fam = {}
    for kind in ("family", "family_noise20"):
        rs = [r for r in out_main if r["kind"] == kind]
        priv = [r for r in rs if r["intent"] == "calendar"]
        comf = [r for r in rs if r["intent"] in ("music", "temperature")]
        acts = [r for r in rs if "status" in r]
        l3 = [r for r in acts if r["intent"] in ("unlock_door", "open_garage", "disarm_alarm")]
        not_named = [r for r in acts if r["actor"] == "guest"]
        refused = [r for r in acts if r["status"] == "denied"]
        fam[kind] = {
            "requests": len(rs),
            "calendar_requests": len(priv),
            "calendar_read_out": sum(r["result"] == "serve" and not r["wrong_person"] for r in priv),
            "calendar_offered_to_phone_instead": sum(r["result"] == "to_phone" for r in priv),
            "calendar_refused_other": sum(r["result"] == "refuse" for r in priv),
            "calendar_read_to_wrong_person": sum(r["wrong_person"] for r in priv),
            "comfort_requests": len(comf),
            "comfort_personalized": sum(r["result"] == "serve" and not r["wrong_person"] for r in comf),
            "comfort_house_default": sum(r["result"] == "generic" for r in comf),
            "comfort_wrong_person": sum(r["wrong_person"] for r in comf),
            "action_requests": len(acts),
            "actions_treated_as_guest": len(not_named),
            "actions_refused": len(refused),
            "actions_refused_because_not_recognised": sum(r["actor"] == "guest" for r in refused),
            "actions_refused_by_permission_for_recognised_person": sum(r["actor"] != "guest" for r in refused),
            "level3_requests": len(l3),
            "level3_completed_after_phone_yes": sum(r["l3_executed"] for r in l3),
        }
    teen = [r for r in out_main if r["kind"] == "family" and r["who"] == "sam"
            and r["intent"] in ("unlock_door", "open_garage", "disarm_alarm")]
    teen_out = {it: {"requests": sum(r["intent"] == it for r in teen),
                     "denied_by_permission": sum(r["intent"] == it and r["status"] == "denied" for r in teen),
                     "sent_to_parent_phone": sum(r["intent"] == it and r["status"] == "awaiting_approval" for r in teen),
                     "executed_after_parent_yes": sum(r["intent"] == it and r["l3_executed"] for r in teen)}
                for it in ("unlock_door", "open_garage", "disarm_alarm")}
    examples = {}
    for r in out_main:
        key = f"{r['kind']}|{r['intent']}|{r.get('status', r.get('result'))}|{r['tier']}"
        if key not in examples and r["kind"] in ("family", "family_noise20", "guest", "teen_over_parent",
                                                  "replay_hifi_speaker"):
            examples[key] = r["say"]

    elapsed = time.perf_counter() - t0
    results = {
        "speech_seconds_per_voice": speech_s,
        "household": {p: {"voice": voice[p], "role": cfg["people"][p]["role"]} for p in members},
        "windows": {"enroll_s": ENROLL_S, "calibration": {p: len(calib_w[p]) for p in members},
                    "evaluation": {p: len(eval_w[p]) for p in members},
                    "guest_calibration": {g: len(v) for g, v in g_cal.items()},
                    "guest_evaluation": {g: len(v) for g, v in g_ev.items()}},
        "calibration": {"chosen": {k: {"threshold": T[k][0], "margin": T[k][1]} for k in T},
                        "tiers": calib, "eval_sweep_margin0": sweep, "B01_thresholds_on_this_eval": b01,
                        "overlap_second_score_threshold": ov_thr},
        "confusion_eval": conf,
        "conditions_eval": {c: v["rates"] for c, v in cond.items()},
        "retries_eval": retry,
        "replay": {"devices": rep_out, "liveness_threshold": r4(lv.threshold),
                   "liveness_false_reject_live": live_fr, "leave_one_feature_out_replay_pass": loo,
                   "feature_means": feat_means},
        "challenge_phrase_modelled": {"combinations": ChallengePhrase.combinations, "per_victim": chal},
        "overlap": {"mixes": overlap, "single_speaker_eval_flagged_as_overlap": single_flag,
                    "mixes_flagged_as_overlap": mix_flag},
        "engine": {"policy": s_main, "careless_approver_10pct": s_care, "weakened_voice_alone_opens": s_weak},
        "family_outcomes": fam,
        "teen_level3": teen_out,
        "what_the_home_says": examples,
        "compute": {"embeddings": emb.n, "embedding_s": round(emb.t, 1), "wall_s": round(elapsed, 1)},
    }
    doc = {
        "experiment": "b06_identity",
        "date": datetime.date.today().isoformat(),
        "method": ("Resemblyzer GE2E 256-d embeddings of 2 s windows (1 s hop) of ElevenLabs TTS voices; every "
                   "window passes through the B01 satellite channel (synthetic room, RT60 0.4 s, pink noise 30 dB). "
                   "Household anna/mark/sam = Amanda/David/Ollie voices, enrolled on their first 10 s; remaining "
                   "speech split in time into calibration (first half) and evaluation (second half). Guests: 4 "
                   "TTS voices never enrolled, plus 3 LibriSpeech clips (real people) if available. Two identity "
                   "tiers chosen on calibration by minimising expected cost, (1-pi)(FRR + c*MIS) + pi*c*FAR_guest, "
                   "pi = 0.2, c = 1 (comfort) and 10 (named), named tier also with a top-1/top-2 margin. Replay = "
                   "evaluation windows through phone / Bluetooth / hi-fi speaker proxies, then the room. Liveness "
                   "= one-class z-score check on 6 channel features, fitted on calibration live windows only, "
                   "threshold at 5 % live rejection. Challenge phrase modelled with transcripts. Overlap = two "
                   "members' windows mixed at -6/0/+6 dB. All requests then go through islab.policy.PolicyEngine in "
                   "the twin (IdentityHome) with phone replies scripted: legitimate requests get a yes, others a no."),
        "params": {"seed": seed, "spread_seeds": SPREAD_SEEDS, "enroll_s": ENROLL_S, "window_s": 2.0, "hop_s": 1.0, "cap_windows_per_split": CAP,
                   "pi_guest": PI_GUEST, "cost_false_accept": COST, "margins_searched": MARGINS,
                   "threshold_grid": [float(GRID[0]), float(GRID[-1]), 0.0025], "liveness_max_live_reject": 0.05,
                   "liveness_features": list(FEATURES),
                   "replay_models": {"phone_speaker": "B01: band-pass 300-3400 Hz 4th order, tanh clip, RT60 0.3 s, 30 dB",
                                     "bluetooth_speaker": "B01: band-pass 120-7500 Hz 2nd order, RT60 0.2 s, 35 dB",
                                     "hifi_speaker": "new: 1st-order 60 Hz high-pass, RT60 0.15 s, 40 dB"},
                   "other_room": "RT60 0.8 s, pink noise 20 dB", "noise": "B01 kitchen-like noise",
                   "voice_dir": "ISLAB_VO (not distributed)", "real_guests": "LibriSpeech clips from the sherpa-onnx "
                   "whisper-tiny.en test_wavs folder (0.wav, 1.wav, 8k.wav; 8k.wav is 8 kHz audio)"},
        "results": results,
        "limitations": [
            "Every household voice is ElevenLabs TTS. A TTS voice has the same timbre, microphone and recording chain "
            "every time; real people change with colds, mood, distance, age and mic. Real false-reject rates will be "
            "higher, and teenagers' voices change over months (re-enrollment), which TTS cannot show.",
            "The household is played by narrator/assistant voices because the S1 character voices have only 1.4-6.2 s "
            "each. Only 3 enrolled voices and 4 TTS guest voices plus 3 short real clips: FAR estimates rest on "
            "dozens of guest windows and have wide uncertainty. Windows overlap 50 % and come from few files, so "
            "trials are correlated; no confidence intervals are reported.",
            "Calibration and evaluation are disjoint in time but come from the same voices and recordings.",
            "Rooms, noise and replay devices are DSP proxies. The liveness check is fitted and tested on the same "
            "synthetic room; its low false-reject rate in that room does not transfer (see other_room).",
            "The liveness check learns 'how this household sounds through this microphone'. It catches band-limited "
            "or clipped replays, not a good full-range speaker; and nothing here detects a voice clone.",
            "The challenge phrase is modelled on transcripts; no speech recogniser checked spoken phrases, so its "
            "own false rejects (ASR errors, accents) are not measured.",
            "Phone approvals are scripted: legitimate requests get a yes, others a no (and 10 % careless yes in a "
            "sensitivity run). Real approval fatigue is not measured.",
            "Overlap = two clean windows summed before one room; real overlapping speech differs in timing and position.",
            "Not a biometric system and not anti-spoofing. Research in simulation, provided as is.",
        ],
    }
    if verbose:
        print("calibrated:", results["calibration"]["chosen"])
        print("engine:", json.dumps(results["engine"]["policy"]))
        print("replay:", json.dumps(rep_out))
        print("liveness FR:", live_fr)
        print(f"seed {seed} done in {elapsed:.1f} s, {emb.n} embeddings")
    return doc


SPREAD_SEEDS = [607, 608, 609]


def key_numbers(doc):
    r = doc["results"]
    rep = r["replay"]["devices"]
    return {
        "named_threshold": r["calibration"]["chosen"]["named"]["threshold"],
        "comfort_threshold": r["calibration"]["chosen"]["comfort"]["threshold"],
        "named_eval_FRR": r["calibration"]["tiers"]["named"]["evaluation"]["FRR"],
        "named_eval_guest_FAR": r["calibration"]["tiers"]["named"]["evaluation"]["guest_FAR"],
        "named_eval_misattributed": r["calibration"]["tiers"]["named"]["evaluation"]["misattributed"],
        "comfort_eval_FRR": r["calibration"]["tiers"]["comfort"]["evaluation"]["FRR"],
        "comfort_eval_guest_FAR": r["calibration"]["tiers"]["comfort"]["evaluation"]["guest_FAR"],
        "liveness_false_reject_household": r["replay"]["liveness_false_reject_live"]["household_eval_same_room"],
        **{f"{d}_named_no_liveness": rep[d]["accepted_as_victim_named_no_liveness"] for d in rep},
        **{f"{d}_named_with_liveness": rep[d]["accepted_as_victim_named_with_liveness"] for d in rep},
        "level3_by_voice_alone": r["engine"]["policy"]["level3_executed_by_voice_alone"],
        "level3_by_voice_alone_weakened": r["engine"]["weakened_voice_alone_opens"]["level3_executed_by_voice_alone"],
    }


if __name__ == "__main__":
    t0 = time.perf_counter()
    segs = load_segments()
    doc = main(SEED, segs=segs)
    rows = {SEED: key_numbers(doc)}
    for sd in SPREAD_SEEDS:
        rows[sd] = key_numbers(main(sd, segs=segs, verbose=False))
        print("seed", sd, rows[sd])
    keys = list(rows[SEED])
    doc["results"]["seed_spread"] = {
        "note": ("the same pipeline with other seeds: different synthetic rooms, noise and replay draws, same "
                 "voices and splits. Primary numbers are seed %d." % SEED),
        "per_seed": {str(k): v for k, v in rows.items()},
        "min": {k: min(rows[s][k] for s in rows) for k in keys},
        "max": {k: max(rows[s][k] for s in rows) for k in keys}}
    doc["results"]["compute"]["total_wall_s_all_seeds"] = round(time.perf_counter() - t0, 1)
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    print(json.dumps(doc["results"]["seed_spread"]["min"]), json.dumps(doc["results"]["seed_spread"]["max"]))
    print("total", doc["results"]["compute"]["total_wall_s_all_seeds"])
