"""Speaker identification experiment on the Intelligent Spaces S1 voice-over (ElevenLabs TTS voices).

Run:  python experiments/speaker_id/run.py   (~2-4 min on 2 CPUs)
Writes experiments/speaker_id/results.json (contract fields) and prints a summary.

Protocols
  P1 "household as specified": Anna, Mark, Sam enrolled; everyone else is an impostor.
     There is only ~9 s of Anna, ~6 s of Mark and ~1.4 s of Sam in total, so 10 s / 30 s enrollment
     is impossible. Enrollment = one file, test = the other file, both directions (2-fold).
  P2 "surrogate household": the three voices with enough audio (David, Amanda, Ollie) are enrolled with
     the first 10 s and 30 s of their speech; all other voices are impostors. Thresholds come from here.
Replay proxy: enrolled person's held-out test windows through a phone-speaker simulation, scored
     against their own profile.
Noise: held-out target windows + kitchen-like noise at 20/10/5 dB SNR -> FRR (and FAR for impostors).
"""
from __future__ import annotations
import datetime, glob, json, os, sys, time
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from islab.voice import audio                       # noqa: E402
from islab.voice.speaker import ResemblyzerEncoder  # noqa: E402

VO = os.environ.get("ISLAB_VO", "audio")
SEED = 117
WIN, HOP, MIN_WIN = 2.0, 1.0, 1.0
SNRS = [20, 10, 5]
HOUSEHOLD = ["Anna", "Mark", "Sam"]
SURROGATE = ["David", "Amanda", "Ollie"]


def speaker_of(fname: str) -> str | None:
    b = os.path.basename(fname)
    if b.startswith(("sfx", "music")):
        return None
    tag = b.split("_", 1)[1].rsplit(".", 1)[0]
    for name in ("AnnaAssist", "Anna", "Mark", "Sam", "David", "Amanda", "Ollie"):
        if tag.startswith(name):
            return name
    return None


def file_order(f):
    # chronological: episode number, v1 before v2
    b = os.path.basename(f)
    ep = b[1:3]
    return (ep, "/v2/" in f, b)


def collect():
    files = sorted(glob.glob(f"{VO}/*.mp3") + glob.glob(f"{VO}/v2/*.mp3"), key=file_order)
    data = {}  # speaker -> list of (file, [segment arrays])
    for f in files:
        spk = speaker_of(f)
        if spk is None:
            continue
        w = audio.load(f)
        segs = [w[int(s * audio.SR):int(e * audio.SR)] for s, e in audio.silence_segments(f, -40, 0.3)]
        data.setdefault(spk, []).append((os.path.relpath(f, VO), segs))
    return data


def split_by_seconds(segs, enroll_s):
    """First enroll_s seconds of speech -> enrollment; the rest -> test (segment at the cut is split)."""
    need = int(enroll_s * audio.SR)
    enr, tst = [], []
    for s in segs:
        if need <= 0:
            tst.append(s); continue
        if len(s) <= need:
            enr.append(s); need -= len(s)
        else:
            enr.append(s[:need]); tst.append(s[need:]); need = 0
    return enr, tst


def win_all(segs, win=WIN, hop=HOP):
    out = []
    for s in segs:
        out += audio.windows(s, win, hop, MIN_WIN)
    return out


class Embedder:
    def __init__(self):
        self.enc = ResemblyzerEncoder()
        self.n = 0
        self.t = 0.0

    def __call__(self, wavs):
        t0 = time.perf_counter()
        E = np.array([self.enc.embed(w) for w in wavs]) if wavs else np.zeros((0, 256), np.float32)
        self.t += time.perf_counter() - t0; self.n += len(wavs)
        return E


def unit(v):
    return v / (np.linalg.norm(v) + 1e-12)


def det(tgt, non):
    """EER and threshold (scores: higher = same speaker)."""
    tgt, non = np.asarray(tgt), np.asarray(non)
    ths = np.unique(np.concatenate([tgt, non]))
    far = np.array([(non >= t).mean() for t in ths])
    frr = np.array([(tgt < t).mean() for t in ths])
    i = int(np.argmin(np.abs(far - frr)))
    thr_far1 = float(next((t for t, fa in zip(ths, far) if fa <= 0.01), ths[-1]))
    return {"eer": float((far[i] + frr[i]) / 2), "eer_threshold": float(ths[i]), "far1_threshold": thr_far1}


def rates(tgt, non, thr):
    r = {"threshold": round(float(thr), 4)}
    if len(tgt):
        r["FRR"] = round(float((np.asarray(tgt) < thr).mean()), 4); r["n_target"] = len(tgt)
    if len(non):
        r["FAR"] = round(float((np.asarray(non) >= thr).mean()), 4); r["n_nontarget"] = len(non)
    return r


def score_trials(profiles, test):
    """profiles: name->emb; test: name->array of embeddings. Returns target, nontarget score lists,
    plus per-pair breakdown and open-set identification stats."""
    tgt, non, pairs = [], [], {}
    names = list(profiles)
    P = np.array([profiles[n] for n in names])
    ident = []  # (true_speaker, best_model, best_score)
    for spk, E in test.items():
        if len(E) == 0:
            continue
        S = E @ P.T
        for j, m in enumerate(names):
            (tgt if spk == m else non).extend(S[:, j].tolist())
            pairs[f"{spk}->{m}"] = round(float(S[:, j].mean()), 3)
        for row in S:
            k = int(np.argmax(row)); ident.append((spk, names[k], float(row[k])))
    return tgt, non, pairs, ident


def ident_stats(ident, enrolled, thr):
    tg = [x for x in ident if x[0] in enrolled]
    im = [x for x in ident if x[0] not in enrolled]
    out = {}
    if tg:
        out["target_correct_and_accepted"] = round(np.mean([b == s and sc >= thr for s, b, sc in tg]), 4)
        out["target_misidentified_as_other_member"] = round(np.mean([b != s and sc >= thr for s, b, sc in tg]), 4)
        out["target_rejected"] = round(np.mean([sc < thr for s, b, sc in tg]), 4)
        out["closed_set_top1"] = round(np.mean([b == s for s, b, sc in tg]), 4)
        out["n_target_windows"] = len(tg)
    if im:
        out["impostor_accepted_as_member"] = round(np.mean([sc >= thr for s, b, sc in im]), 4)
        out["n_impostor_windows"] = len(im)
    return out


def main():
    t_start = time.perf_counter()
    rng = np.random.default_rng(SEED)
    emb = Embedder()
    data = collect()
    speech_s = {k: round(sum(len(s) for _, segs in v for s in segs) / audio.SR, 2) for k, v in data.items()}
    print("speech seconds per voice:", speech_s)

    noise_bank = audio.kitchen_noise(audio.SR * 60, rng)

    def degrade_sets(wavs):
        """Return dict cond -> list of wavs (replay + noise SNRs)."""
        out = {"replay_phone": [audio.phone_replay(w, rng) for w in wavs]}
        for snr in SNRS:
            ws = []
            for w in wavs:
                o = rng.integers(0, len(noise_bank) - len(w))
                ws.append(audio.mix_snr(w, noise_bank[o:o + len(w)], snr))
            out[f"noise_{snr}dB"] = ws
        return out

    results = {"speech_seconds_per_voice": speech_s}

    # ---------------- P2 surrogate household (thresholds) ----------------
    p2 = {}
    imp_wins = {s: win_all([x for _, segs in data[s] for x in segs]) for s in data if s not in SURROGATE}
    imp_emb = {s: emb(w) for s, w in imp_wins.items()}
    imp_deg = {s: degrade_sets(w) for s, w in imp_wins.items()}
    imp_deg_emb = {s: {c: emb(ws) for c, ws in d.items()} for s, d in imp_deg.items()}
    for enroll_s in (10, 30):
        profiles, test_w = {}, {}
        for s in SURROGATE:
            segs = [x for _, sg in data[s] for x in sg]
            e, t = split_by_seconds(segs, enroll_s)
            profiles[s] = unit(emb(win_all(e)).mean(0))
            test_w[s] = win_all(t)
        test_e = {s: emb(w) for s, w in test_w.items()}
        test_e.update(imp_emb)
        tgt, non, pairs, ident = score_trials(profiles, test_e)
        d = det(tgt, non)
        # per-condition (targets degraded; impostors degraded the same way)
        tgt_deg = {s: degrade_sets(test_w[s]) for s in SURROGATE}
        cond = {}
        for c in ["replay_phone"] + [f"noise_{x}dB" for x in SNRS]:
            te = {s: emb(tgt_deg[s][c]) for s in SURROGATE}
            te.update({s: imp_deg_emb[s][c] for s in imp_deg_emb})
            ct, cn, cp, ci = score_trials(profiles, te)
            cond[c] = {"strict": rates(ct, cn, d["far1_threshold"]), "eer_thr": rates(ct, cn, d["eer_threshold"]),
                       "mean_target_score": round(float(np.mean(ct)), 3)}
        p2[f"enroll_{enroll_s}s"] = {
            "enrolled": SURROGATE,
            "impostors": sorted(imp_emb),
            "n_target_trials": len(tgt), "n_nontarget_trials": len(non),
            "EER": round(d["eer"], 4), "eer_threshold": round(d["eer_threshold"], 4),
            "far1_threshold": round(d["far1_threshold"], 4),
            "at_eer_threshold": rates(tgt, non, d["eer_threshold"]),
            "at_strict_threshold": rates(tgt, non, d["far1_threshold"]),
            "at_fixed_thresholds": {str(t): rates(tgt, non, t) for t in (0.75, 0.80, 0.85, 0.90)},
            "identification_at_strict": ident_stats(ident, SURROGATE, d["far1_threshold"]),
            "mean_score_matrix": pairs,
            "conditions": cond,
        }
        print(f"P2 enroll {enroll_s}s: EER={d['eer']:.3f} thr={d['eer_threshold']:.3f} far1thr={d['far1_threshold']:.3f}")
    results["P2_surrogate_household"] = p2
    thr_eer = p2["enroll_10s"]["eer_threshold"]
    thr_strict = p2["enroll_10s"]["far1_threshold"]

    # ---------------- P2-room: "deployed" scenario, everything heard through the satellite channel ----------------
    # Enrollment, genuine tests and impostors all pass through the same simulated room + mic, so the
    # threshold is calibrated for the channel the hub actually hears. Replay = recording played through a
    # phone or a Bluetooth speaker *in that room*. This is the fair test of "voice match != identity".
    room = lambda ws: [audio.satellite_channel(w, rng) for w in ws]
    profiles, test_w = {}, {}
    for s in SURROGATE:
        segs = [x for _, sg in data[s] for x in sg]
        e, t = split_by_seconds(segs, 10)
        profiles[s] = unit(emb(room(win_all(e))).mean(0))
        test_w[s] = win_all(t)
    te = {s: emb(room(w)) for s, w in test_w.items()}
    te.update({s: emb(room(w)) for s, w in imp_wins.items()})
    rt, rn, rpairs, rident = score_trials(profiles, te)
    dr = det(rt, rn)
    room_res = {"n_target_trials": len(rt), "n_nontarget_trials": len(rn), "EER": round(dr["eer"], 4),
                "eer_threshold": round(dr["eer_threshold"], 4), "far1_threshold": round(dr["far1_threshold"], 4),
                "at_eer_threshold": rates(rt, rn, dr["eer_threshold"]),
                "at_strict_threshold": rates(rt, rn, dr["far1_threshold"]),
                "mean_score_matrix": rpairs, "replay": {}, "noise": {}}
    for name, fn in (("phone_speaker", audio.phone_replay), ("bluetooth_speaker", audio.speaker_replay)):
        sc, top1 = [], []
        P = np.array([profiles[n] for n in SURROGATE])
        for s in SURROGATE:
            E = emb(room([fn(w, rng) for w in test_w[s]]))
            S = E @ P.T
            sc += S[:, SURROGATE.index(s)].tolist()
            top1 += (np.argmax(S, 1) == SURROGATE.index(s)).tolist()
        room_res["replay"][name] = {
            "n": len(sc), "mean_score_vs_victim_profile": round(float(np.mean(sc)), 3),
            "accepted_as_victim_at_eer_thr": round(float(np.mean(np.array(sc) >= dr["eer_threshold"])), 4),
            "accepted_as_victim_at_strict_thr": round(float(np.mean(np.array(sc) >= dr["far1_threshold"])), 4),
            "victim_is_top1": round(float(np.mean(top1)), 4)}
    for snr in SNRS:
        ct = []
        P = np.array([profiles[n] for n in SURROGATE])
        for s in SURROGATE:
            ws = []
            for w in room(test_w[s]):
                o = rng.integers(0, len(noise_bank) - len(w))
                ws.append(audio.mix_snr(w, noise_bank[o:o + len(w)], snr))
            ct += (emb(ws) @ profiles[s]).tolist()
        room_res["noise"][f"{snr}dB"] = {"strict": rates(ct, [], dr["far1_threshold"]),
                                         "eer_thr": rates(ct, [], dr["eer_threshold"]),
                                         "mean_target_score": round(float(np.mean(ct)), 3)}
    results["P2_room_channel_enroll_10s"] = room_res
    print(f"P2-room: EER={dr['eer']:.3f} thr={dr['eer_threshold']:.3f}; replay {room_res['replay']}")

    # ---------------- P1 household as specified ----------------
    files = {s: data[s] for s in HOUSEHOLD}
    p1_t, p1_n, p1_ident, p1_pairs = [], [], [], {}
    p1_deg = {c: ([], []) for c in ["replay_phone"] + [f"noise_{x}dB" for x in SNRS]}
    folds = []
    for fold in (0, 1):
        profiles, test_w = {}, {}
        for s in HOUSEHOLD:
            fl = files[s]
            if len(fl) >= 2:
                e_segs = fl[fold][1]; t_segs = fl[1 - fold][1]
            else:  # Sam: single 1.4 s utterance -> enrollment only, no held-out target audio
                e_segs = fl[0][1]; t_segs = []
            profiles[s] = unit(emb(win_all(e_segs)).mean(0))
            test_w[s] = win_all(t_segs)
        folds.append({s: {"enroll_s": None, "test_windows": len(test_w[s])} for s in HOUSEHOLD})
        te = {s: emb(w) for s, w in test_w.items()}
        te.update({s: e for s, e in imp_emb.items() if s not in HOUSEHOLD})
        for s in SURROGATE:  # surrogate voices are impostors here
            te[s] = emb(win_all([x for _, sg in data[s] for x in sg])[::4])  # subsample long narrators
        t, n, pairs, ident = score_trials(profiles, te)
        p1_t += t; p1_n += n; p1_ident += ident
        for k, v in pairs.items():
            p1_pairs.setdefault(k, []).append(v)
        for s in ("Anna", "Mark"):
            dg = degrade_sets(test_w[s])
            for c, ws in dg.items():
                E = emb(ws)
                p1_deg[c][0].extend((E @ profiles[s]).tolist())
    d1 = det(p1_t, p1_n)
    results["P1_household"] = {
        "enrolled": HOUSEHOLD,
        "enrollment": "2-fold: Anna/Mark enrolled on one file (~2-7 s), tested on the other; Sam enrolled on his only 1.4 s utterance (no target test audio)",
        "n_target_trials": len(p1_t), "n_nontarget_trials": len(p1_n),
        "EER_own_data_not_reliable": round(d1["eer"], 4), "eer_threshold_own": round(d1["eer_threshold"], 4),
        "target_scores": [round(x, 3) for x in sorted(p1_t)],
        "max_nontarget_score": round(float(max(p1_n)), 3),
        "at_strict_threshold_from_P2": rates(p1_t, p1_n, thr_strict),
        "at_eer_threshold_from_P2": rates(p1_t, p1_n, thr_eer),
        "identification_at_strict": ident_stats(p1_ident, HOUSEHOLD, thr_strict),
        "identification_at_eer_thr": ident_stats(p1_ident, HOUSEHOLD, thr_eer),
        "mean_score_matrix": {k: round(float(np.mean(v)), 3) for k, v in p1_pairs.items()},
        "conditions_target_only": {c: {"strict": rates(v[0], [], thr_strict), "eer_thr": rates(v[0], [], thr_eer),
                                        "mean_target_score": round(float(np.mean(v[0])), 3)} for c, v in p1_deg.items()},
    }
    print(f"P1: EER(own)={d1['eer']:.3f}; at strict {rates(p1_t, p1_n, thr_strict)}")

    results["operating_points"] = {
        "studio_eer": round(thr_eer, 4), "studio_strict": round(thr_strict, 4),
        "room_eer": results["P2_room_channel_enroll_10s"]["eer_threshold"],
        "room_strict": results["P2_room_channel_enroll_10s"]["far1_threshold"],
        "chosen_default": "room_strict",
        "why": ("the hub hears people through a room, so thresholds calibrated on the room channel apply; 'strict' "
                "(FAR<=1 % on these trials) because attributing a request to the wrong person is worse than asking"),
        "derived_from": "P2_surrogate_household.enroll_10s (studio) and P2_room_channel_enroll_10s (room)"}
    # ---------------- export precomputed embeddings for the twin (islab/voice/data) ----------------
    hh = {"_doc": ("Resemblyzer GE2E embeddings of the S1 voice-over voices, for twin scenarios without torch. "
                   "profiles = mean of all 2 s windows of each household member (so 'utterances' overlap the "
                   "profile data and score optimistically). replay = Bluetooth-speaker replay proxy of the same windows. "
                   "Generated by experiments/speaker_id/run.py."),
          "encoder": "resemblyzer-0.1.4-ge2e", "window_s": WIN,
          "threshold": results["operating_points"], "profiles": {}, "utterances": {}, "replay": {}, "impostors": {},
          "scores_vs_own_profile": {}}
    for s in HOUSEHOLD:
        ws = win_all([x for _, sg in data[s] for x in sg])
        E = emb(ws); prof = unit(E.mean(0))
        R = emb([audio.speaker_replay(w, rng) for w in ws])
        hh["profiles"][s] = [round(float(x), 5) for x in prof]
        hh["utterances"][s] = [[round(float(x), 5) for x in e] for e in E]
        hh["replay"][s] = [[round(float(x), 5) for x in e] for e in R]
        hh["scores_vs_own_profile"][s] = {"utterances": [round(float(x), 3) for x in E @ prof],
                                          "replay": [round(float(x), 3) for x in R @ prof]}
    for s in ("Amanda", "David", "AnnaAssist", "Ollie"):
        ws = win_all([x for _, sg in data[s] for x in sg])[:4]
        hh["impostors"][s] = [[round(float(x), 5) for x in e] for e in emb(ws)]
    os.makedirs(os.path.join(ROOT, "islab", "voice", "data"), exist_ok=True)
    with open(os.path.join(ROOT, "islab", "voice", "data", "household_embeddings.json"), "w") as f:
        json.dump(hh, f)
    results["household_export_scores"] = hh["scores_vs_own_profile"]

    runtime = time.perf_counter() - t_start
    results["compute"] = {"embeddings": emb.n, "embed_seconds": round(emb.t, 1),
                          "ms_per_2s_window": round(1000 * emb.t / max(1, emb.n), 1),
                          "total_runtime_s": round(runtime, 1)}

    out = {
        "experiment": "speaker_id",
        "date": datetime.date.today().isoformat(),
        "method": ("Resemblyzer 0.1.4 GE2E d-vector encoder (pretrained weights bundled in the pip wheel; 256-d), "
                   "cosine scoring against mean-of-windows enrollment profile. Segmentation: ffmpeg silencedetect "
                   "-40 dB / 0.3 s, then 2 s windows with 1 s hop (min 1 s). Audio decoded to 16 kHz mono."),
        "params": {"seed": SEED, "window_s": WIN, "hop_s": HOP, "min_window_s": MIN_WIN,
                   "silencedetect": {"noise_db": -40, "min_silence_s": 0.3},
                   "enrollment_seconds_P2": [10, 30], "snr_db": SNRS,
                   "replay_model": {"phone_speaker": "Butterworth band-pass 300-3400 Hz (4th order) + tanh soft-clip (drive 2) + room IR RT60 0.3 s + pink noise 30 dB",
                                    "bluetooth_speaker": "band-pass 120-7500 Hz (2nd order) + room IR RT60 0.2 s + pink noise 35 dB"},
                   "satellite_channel": "synthetic room IR RT60 0.4 s (direct + exponential noise tail) + pink noise 30 dB SNR",
                   "noise_model": "kitchen-like: reverberant pink noise (RT60 0.5 s) + 120/240 Hz hum + ~1 clatter transient/s",
                   "voice_map": {"Anna": "ElevenLabs 'Courtney'", "Mark": "'Chris'", "Sam": "'Eric'",
                                 "David": "narrator", "Amanda/Ollie/AnnaAssist": "assistant voices"},
                   "source_dir": VO},
        "results": results,
        "limitations": [
            "All voices are ElevenLabs TTS. Synthetic voices are perfectly consistent in timbre and recording chain within a voice (easier than real people, who vary with health, mood, distance, mic), but several stock voices can be acoustically close (Anna vs Amanda mean score is high) and GE2E was trained on real speech only; the net effect on real households is unknown in both directions.",
            "P1 (the real household) has ~9 s of Anna, ~6 s of Mark and 1.4 s of Sam in total: the 10 s / 30 s enrollment requested is impossible, target trials are a handful, and Sam has no held-out audio. P1 EER is anecdotal; thresholds come from P2.",
            "P2 uses the narrator and assistant voices as a surrogate household (they have 70-280 s each). David's lines repeat some phrases across episodes ('Intersog presents. Intelligent Spaces.'), so some test windows share text with enrollment.",
            "Only 7 distinct voices in total, so the impostor population is tiny; FAR estimates have wide confidence intervals and a 1 % FAR threshold on a few thousand correlated window pairs is not a 1 % FAR in the field.",
            "Windows overlap 50 % and come from the same few files, so trials are correlated; no confidence intervals are reported.",
            "Replay is a DSP proxy (band-limit, clip, reverb, noise), not a real phone loudspeaker + room + mic. Real replay through a good speaker would be closer to the original, i.e. the proxy is if anything pessimistic for the attacker.",
            "Noise is synthetic (no real kitchen recordings available offline); real babble/TV/extractor-fan noise is harder than pink noise.",
            "No anti-spoofing / liveness model was available offline; none was tested.",
        ],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print("wrote", path, f"runtime {runtime:.0f}s")


if __name__ == "__main__":
    main()
