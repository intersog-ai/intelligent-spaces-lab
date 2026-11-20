"""B05 audio measurements on this CPU: real wake word, end of speech, speech to text and TTS on synthetic
voices, plus the failure cases that need audio. Writes measured.json next to this file.

    ISLAB_VO=/path/to/vo ISLAB_MODELS=/path/to/models python experiments/b05_pipeline/measure.py

Components (all CPU, PyPI wheels; STT/TTS weights from the sherpa-onnx GitHub releases):
  wake word   openWakeWord 0.4.0 'hey_jarvis' (model bundled in the wheel), threshold 0.5
  endpoint    silero-vad 6.x (bundled) with 300 ms hangover; webrtcvad 2.0.10 for comparison
  STT         sherpa-onnx streaming zipformer en-20M int8; sherpa-onnx Whisper tiny.en int8
  TTS         Piper VITS low voices (amy, ryan, alan) via onnxruntime + espeak-ng phonemes
Test audio: commands spoken by three Piper voices, plus the Season 1 household lines (E06 Anna, Mark,
Sam; E07-09 Anna) where they are commands. "TV-like speech" = the series narrator (David) and assistant
voices, played through a TV-speaker EQ. Everything is synthetic speech: no human voices, no real room.
"""
from __future__ import annotations
import glob, json, os, sys, time, platform, statistics
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from islab.voice import audio                                    # noqa: E402
from islab.pipeline.intent import Grammar, devices_of            # noqa: E402
from islab.pipeline import real                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VO = os.environ.get("ISLAB_VO", "audio")
MODELS = os.environ.get("ISLAB_MODELS", "models")
SR = 16000
SEED = 2026
HANGOVER_MS = 300
LISTEN_CAP_S = 8.0             # the pipeline stops listening this long after the command starts
TV_EQ = "highpass=f=180,lowpass=f=6000,acompressor=threshold=-20dB:ratio=3"

COMMANDS = [   # (text, kind)  kind: named = names its device; context = needs the room or the dialogue
    ("turn on the kitchen light", "named"), ("turn off the kitchen light", "named"),
    ("turn on the living room light", "named"), ("switch off the living room light", "named"),
    ("set the kitchen light to fifty percent", "named"), ("set the kitchen to twenty one degrees", "named"),
    ("set the study to nineteen degrees", "named"), ("set the study thermostat to twenty degrees", "named"),
    ("close the living room shade", "named"), ("open the living room blinds", "named"),
    ("close the garage door", "named"), ("shut the garage", "named"),
    ("lock the front door", "named"), ("lock the back door", "named"), ("unlock the front door", "named"),
    ("shut off the water", "named"), ("turn off the main water valve", "named"),
    ("what's the temperature in the kitchen", "named"), ("is the back door locked", "named"),
    ("turn off the light", "context"), ("turn it off", "context"), ("turn it on", "context"),
    ("close it", "context"), ("make it a bit warmer in here", "context"), ("same in my study please", "context"),
]
SPLITS = [   # (first part, second part): a pause in the middle of a command
    ("set the kitchen to", "twenty one degrees"), ("turn on the kitchen light", "at fifty percent"),
    ("lock the", "back door"), ("close the garage", "door"), ("set the study", "to nineteen degrees"),
    ("turn off the", "living room light"),
]
PAUSES = (0.3, 0.5, 0.7, 0.9)
HANGOVERS = (300, 500, 800)
EXTEND_MS = 700               # grammar-aware endpoint: wait this much longer when the text is incomplete
VOICES = [("en_US-amy-low", "en-us"), ("en_US-ryan-low", "en-us"), ("en_GB-alan-low", "en-gb")]
SERIES_LINES = [   # (file, segment index or None, text, voice used for the wake word)
    ("v2/E06_Anna.mp3", None, "make it a bit warmer in here", "en_US-amy-low"),
    ("v2/E06_Mark.mp3", None, "same in my study please", "en_US-ryan-low"),
    ("v2/E06_Sam.mp3", None, "unlock the front door", "en_US-ryan-low"),
    ("v2/E0709_Anna.mp3", 0, "keep it on", "en_US-amy-low"),
    ("v2/E0709_Anna.mp3", 1, "i'm closing it now", "en_US-amy-low"),
]
CONDITIONS = {"clean": None, "tv_10db": 10.0, "tv_0db": 0.0}
REPLIES = ["Done. The kitchen light is on.", "Done. The garage door is closed.",
           "Sent. Waiting for the garage door to confirm.", "Which one: the kitchen light or the living room light?",
           "Only Anna or Mark can unlock the front door. I've sent a request.",
           "The garage door got the command but never showed it happened, so it may still be open."]


def pct(xs, q):
    return round(float(np.percentile(np.asarray(xs, dtype=float), q)), 2) if len(xs) else None


def rms_db(w):
    return 20 * np.log10(np.sqrt(np.mean(w.astype(np.float64) ** 2)) + 1e-12)


def gain_to(w, db):
    return (w * 10 ** ((db - rms_db(w)) / 20)).astype(np.float32)


def sil(s):
    return np.zeros(int(s * SR), np.float32)


class Bed:
    """Background: mic noise floor (-60 dBFS) plus optional TV speech at a given SNR."""

    def __init__(self, tv_bank, rng):
        self.tv, self.rng = tv_bank, rng

    def mix(self, x, speech_db, snr):
        y = x + self.rng.normal(0, 10 ** (-60 / 20), len(x)).astype(np.float32)
        if snr is not None:
            o = int(self.rng.integers(0, len(self.tv) - len(x)))
            y = y + gain_to(self.tv[o:o + len(x)], speech_db - snr)
        return y.astype(np.float32)


def score(g, truth, hyp, sat="sat.kitchen"):
    """Compare the grammar's reading of the transcript with its reading of what was said."""
    a, b = g.parse(truth, sat), g.parse(hyp, sat)
    if a.status != "ok":
        return "n/a" if b.status != "ok" else "acted_on_unclear"
    if b.status != "ok":
        return "asked" if b.status in ("ambiguous", "incomplete") else "not_understood"
    if a.calls == b.calls:
        return "correct"
    da = sorted(d for c in a.calls for d in devices_of(c))
    db = sorted(d for c in b.calls for d in devices_of(c))
    return "wrong_device" if da != db else "wrong_value"


def main(only=None):
    t_start = time.perf_counter()
    rng = np.random.default_rng(SEED)
    g = Grammar()
    out = {"_machine": {"cpu": platform.machine(), "cpus": os.cpu_count(), "python": platform.python_version()},
           "_env": {"SANDBOX_SHERPA_ONNX_WHISPER_FILES": os.environ.get("SANDBOX_SHERPA_ONNX_WHISPER_FILES")},
           "blocked": {}}

    # ---------------- components
    ww = real.WakeWord("hey_jarvis", 0.5)
    ep = real.SileroEndpointer(HANGOVER_MS)
    wrtc = real.WebrtcEndpointer(HANGOVER_MS, 2)
    zip_ = real.ZipformerSTT(MODELS)
    wsp = real.WhisperSTT(MODELS)
    voices = {n: real.PiperVoice(MODELS, n, v) for n, v in VOICES}
    for name, fn in (("vosk", lambda: __import__("vosk").Model(lang="en-us")),
                     ("faster_whisper", lambda: __import__("faster_whisper").WhisperModel("tiny.en", device="cpu"))):
        try:
            fn()
            out["blocked"][name] = "loaded (unexpected)"
        except BaseException as e:   # noqa: BLE001 - record any failure verbatim
            out["blocked"][name] = f"{type(e).__name__}: {str(e)[:200]}"

    # ---------------- TV bank: narrator + assistant voices through a TV-speaker EQ
    tv_files = sorted(glob.glob(f"{VO}/E0*_David.mp3"))
    tv_bank = np.concatenate([audio.load(f, af=TV_EQ) for f in tv_files])
    bed = Bed(gain_to(tv_bank, -23), rng)

    # ---------------- 1. command clips through the real chain
    clips = []
    for vname, tts in voices.items():
        hj = gain_to(tts.synth("Hey Jarvis."), -23)
        for text, kind in COMMANDS:
            clips.append({"voice": vname, "text": text, "kind": kind, "wake": hj,
                          "cmd": gain_to(tts.synth(text + "."), -23)})
    for f, seg, text, wv in SERIES_LINES:
        w = audio.load(os.path.join(VO, f))
        if seg is not None:
            segs = audio.silence_segments(os.path.join(VO, f), -40, 0.15)
            s0, e0 = segs[seg]
            w = w[int(s0 * SR):int(e0 * SR)]
        clips.append({"voice": "series:" + os.path.basename(f).split(".")[0], "text": text, "kind": "series",
                      "wake": gain_to(voices[wv].synth("Hey Jarvis."), -23), "cmd": gain_to(real.trim(w), -23)})

    if only == "cutoffs":
        out = json.load(open(os.path.join(HERE, "measured.json")))
        clips = []
    recs, wake_delay, ep_delay, zip_ms, wsp_ms, wrtc_delay = [], [], [], [], [], []
    for cond, snr in (CONDITIONS.items() if only != "cutoffs" else []):
        for c in clips:
            lead = 1.0
            x = np.concatenate([sil(lead), c["wake"], sil(0.3), c["cmd"], sil(LISTEN_CAP_S)])
            t_wake_end = lead + len(c["wake"]) / SR
            t_cmd0 = t_wake_end + 0.3
            t_cmd_end = t_cmd0 + len(c["cmd"]) / SR
            y = bed.mix(x, -23, snr)
            hits = ww.detect(y[: int((t_cmd0 + 0.6) * SR)])
            det = [t for t, s in hits if t >= lead]
            r = {"voice": c["voice"], "text": c["text"], "kind": c["kind"], "condition": cond,
                 "speech_s": round(len(c["cmd"]) / SR, 3), "wake_detected": bool(det)}
            if det:
                r["wake_delay_ms"] = round((det[0] - t_wake_end) * 1000)
            # end of speech: VAD runs from the end of the wake word
            i0 = int(t_wake_end * SR)
            cap = t_cmd0 + LISTEN_CAP_S
            ends = [i0 / SR + e for e in ep.end_events(y[i0:int(cap * SR)])]
            ends = [e for e in ends if e > t_cmd0 + 0.15]
            t_ep = ends[0] if ends else cap
            r["endpoint_timeout"] = not ends
            r["endpoint_after_end_ms"] = round((t_ep - t_cmd_end) * 1000)
            r["cut"] = t_ep < t_cmd_end - 0.05
            seg = y[i0:int(t_ep * SR)]
            r["hyp_zipformer"], zm = zip_.transcribe(seg)
            r["hyp_whisper"], wm = wsp.transcribe(seg)
            r["zipformer_ms"], r["whisper_ms"] = round(zm, 1), round(wm, 1)
            r["score_zipformer"] = score(g, c["text"], r["hyp_zipformer"])
            r["score_whisper"] = score(g, c["text"], r["hyp_whisper"])
            we = [i0 / SR + e for e in wrtc.end_events(y[i0:int(cap * SR)])]
            we = [e for e in we if e > t_cmd0 + 0.15]
            r["webrtc_endpoint_after_end_ms"] = round(((we[0] if we else cap) - t_cmd_end) * 1000)
            r["webrtc_timeout"] = not we
            recs.append(r)
            if cond == "clean":
                if det:
                    wake_delay.append(r["wake_delay_ms"])
                if not r["cut"] and not r["endpoint_timeout"]:
                    ep_delay.append(r["endpoint_after_end_ms"])
                if not r["webrtc_timeout"]:
                    wrtc_delay.append(r["webrtc_endpoint_after_end_ms"])
                zip_ms.append(zm)
                wsp_ms.append(wm)
    if only != "cutoffs":
        out["clips"] = recs
    print(f"clips done {time.perf_counter() - t_start:.0f}s", flush=True)

    # ---------------- 2. cut-off commands: a pause inside the command vs the endpoint hangover
    cuts = []
    for vname, tts in voices.items():
        for a, b in SPLITS:
            wa, wb = gain_to(tts.synth(a), -23), gain_to(tts.synth(b + "."), -23)
            truth = f"{a} {b}"
            for p in PAUSES:
                x = np.concatenate([sil(0.5), wa, sil(p), wb, sil(3.0)])
                y = bed.mix(x, -23, None)
                t0, t_end = 0.5, 0.5 + (len(wa) + len(wb)) / SR + p
                for h in HANGOVERS:
                    ends = [e for e in ep.end_events(y, h) if e > t0 + 0.15]
                    t_ep = ends[0] if ends else len(y) / SR
                    r = {"voice": vname, "text": truth, "pause_s": p, "hangover_ms": h,
                         "cut": t_ep < t_end - 0.05, "endpoint_after_end_ms": round((t_ep - t_end) * 1000)}
                    if r["cut"]:
                        hyp, _ = wsp.transcribe(y[: int(t_ep * SR)])
                        r["hyp"], r["score"] = hyp, score(g, truth, hyp)
                        # grammar-aware endpoint: if the text so far is incomplete, keep listening EXTEND_MS more;
                        # if speech resumes inside that window, take the next end-of-speech event instead
                        r["incomplete_at_endpoint"] = g.parse(hyp).status == "incomplete"
                        if r["incomplete_at_endpoint"]:
                            t_resume = 0.5 + len(wa) / SR + p
                            later = [e for e in ends if e > t_resume]
                            r["extended"] = bool(later) and t_resume - t_ep <= EXTEND_MS / 1000.0
                            if r["extended"]:
                                hyp2, _ = wsp.transcribe(y[: int(later[0] * SR)])
                                r["hyp_extended"], r["score_extended"] = hyp2, score(g, truth, hyp2)
                                r["extended_endpoint_after_end_ms"] = round((later[0] - t_end) * 1000)
                    cuts.append(r)
    out["cutoffs"] = cuts
    print(f"cutoffs done {time.perf_counter() - t_start:.0f}s", flush=True)
    if only == "cutoffs":
        out["cutoffs_wall_s"] = round(time.perf_counter() - t_start, 1)
        with open(os.path.join(HERE, "measured.json"), "w") as f:
            json.dump(out, f, indent=1)
        print("measured.json cutoffs updated")
        return

    # ---------------- 3. wake word on TV-like speech
    tv_sets = {"narrator_tv_eq": tv_files,
               "assistant_voices_tv_eq": sorted(glob.glob(f"{VO}/E0*_Amanda.mp3") + glob.glob(f"{VO}/E0*_Ollie*.mp3")
                                                + glob.glob(f"{VO}/v2/E0*_Ollie*.mp3")),
               "music_tv_eq": [f"{VO}/v2/music_s1.mp3"]}
    fa = {}
    for name, files in tv_sets.items():
        n_hit, dur, peaks = 0, 0.0, []
        for f in files:
            w = gain_to(audio.load(f, af=TV_EQ), -23)
            sc = ww.scores(w)
            peaks.append(max(s for _, s in sc))
            n_hit += len(ww.detect(w))
            dur += len(w) / SR
        fa[name] = {"files": len(files), "minutes": round(dur / 60, 2), "activations": n_hit,
                    "max_score": round(max(peaks), 3)}
    tot_min = sum(v["minutes"] for v in fa.values())
    tot_hits = sum(v["activations"] for v in fa.values())
    out["tv_false_accepts"] = {"sets": fa, "minutes": round(tot_min, 2), "activations": tot_hits,
                               "per_hour": round(tot_hits / (tot_min / 60), 2),
                               "upper_95_per_hour_if_zero": round(3.0 / (tot_min / 60), 1) if tot_hits == 0 else None}

    # the TV itself says the wake word, then keeps talking
    tv_says = []
    bank = bed.tv
    for k in range(12):
        vname = list(voices)[k % 3]
        hj = gain_to(voices[vname].synth("Hey Jarvis."), -23)
        hj = gain_to(audio.ffmpeg_filter(hj, TV_EQ), -23)
        o = int(rng.integers(SR * 5, len(bank) - SR * 15))
        x = np.concatenate([bank[o - SR * 3:o], hj, bank[o:o + SR * 10]])
        x = bed.mix(x, -23, None)
        t_w_end = 3.0 + len(hj) / SR
        hits = [t for t, s in ww.detect(x) if 3.0 <= t <= t_w_end + 1.0]
        r = {"voice": vname, "wake_detected": bool(hits)}
        if hits:
            i0 = int(t_w_end * SR)
            ends = [t_w_end + e for e in ep.end_events(x[i0:i0 + int(LISTEN_CAP_S * SR)])]
            ends = [e for e in ends if e > t_w_end + 0.4]
            t_ep = ends[0] if ends else t_w_end + LISTEN_CAP_S
            hyp, _ = wsp.transcribe(x[i0:int(t_ep * SR)])
            it = g.parse(hyp, "sat.living")
            r.update(listened_s=round(t_ep - t_w_end, 2), hyp=hyp, intent=it.status,
                     calls=it.calls, question=it.question)
        tv_says.append(r)
    out["tv_says_wake_word"] = tv_says
    print(f"tv done {time.perf_counter() - t_start:.0f}s", flush=True)

    # ---------------- 4. TTS, grammar and policy timings
    amy = voices["en_US-amy-low"]
    amy.synth("Warm up.")
    tts_ms = []
    for _ in range(3):
        for ln in REPLIES:
            first = ln.split(". ")[0] + "."
            t0 = time.perf_counter()
            amy.synth(first)
            tts_ms.append((time.perf_counter() - t0) * 1000)
    gr_ms = []
    texts = [c[0] for c in COMMANDS] * 20
    for t in texts:
        t0 = time.perf_counter()
        g.parse(t, "sat.kitchen")
        gr_ms.append((time.perf_counter() - t0) * 1000)
    pol_ms = policy_timing()

    stt_engine = "whisper"
    zip_ok = sum(r["score_zipformer"] == "correct" for r in recs if r["kind"] == "named")
    wsp_ok = sum(r["score_whisper"] == "correct" for r in recs if r["kind"] == "named")
    if zip_ok > wsp_ok:
        stt_engine = "zipformer"
    stt_lat = wsp_ms if stt_engine == "whisper" else zip_ms
    ww_ms = ww.frame_ms
    out["stages"] = {
        "wake_detect": {"p50": pct(wake_delay, 50), "p95": pct(wake_delay, 95),
                        "source": f"measured: openWakeWord hey_jarvis, delay from end of wake phrase to activation, "
                                  f"clean clips, n={len(wake_delay)}"},
        "vad_endpoint": {"p50": pct(ep_delay, 50), "p95": pct(ep_delay, 95),
                         "source": f"measured: silero-vad, {HANGOVER_MS} ms hangover, end of command to end-of-speech "
                                   f"event, clean clips not cut, n={len(ep_delay)}"},
        "stt": {"p50": pct(stt_lat, 50), "p95": pct(stt_lat, 95),
                "source": f"measured: {stt_engine} ({'Whisper tiny.en int8, 2 threads, decode after endpoint' if stt_engine == 'whisper' else 'zipformer streaming, final result after endpoint'}), clean clips, n={len(stt_lat)}"},
        "tts_first_audio": {"p50": pct(tts_ms, 50), "p95": pct(tts_ms, 95),
                            "source": f"measured: Piper en_US-amy-low first sentence, 2 threads, n={len(tts_ms)}"},
        "intent_rules": {"p50": pct(gr_ms, 50), "p95": pct(gr_ms, 95),
                         "source": f"measured: B05 grammar parse, n={len(gr_ms)}"},
        "policy": {"p50": pct(pol_ms, 50), "p95": pct(pol_ms, 95),
                   "source": f"measured: B01 PolicyEngine.propose wall time for level-1 voice calls, n={len(pol_ms)}"},
    }
    out["stt_engine_chosen"] = stt_engine
    out["component_compute"] = {
        "wake_ms_per_80ms_frame": {"p50": pct(ww_ms, 50), "p95": pct(ww_ms, 95)},
        "silero_ms_per_32ms_chunk": {"p50": pct(ep.chunk_ms, 50), "p95": pct(ep.chunk_ms, 95)},
        "zipformer_final_ms": {"p50": pct(zip_ms, 50), "p95": pct(zip_ms, 95)},
        "whisper_decode_ms": {"p50": pct(wsp_ms, 50), "p95": pct(wsp_ms, 95)},
        "webrtcvad_endpoint_after_end_ms": {"p50": pct(wrtc_delay, 50), "p95": pct(wrtc_delay, 95), "n": len(wrtc_delay)},
    }
    out["wall_s"] = round(time.perf_counter() - t_start, 1)
    with open(os.path.join(HERE, "measured.json"), "w") as f:
        json.dump(out, f, indent=1)
    print("measured.json written", out["wall_s"], "s")


def policy_timing(n=200):
    from islab.core.sim import Sim
    from islab.core.bus import Bus
    from islab.twin.home import build_home
    from islab.policy.state import StateStore
    from islab.policy.engine import PolicyEngine, Proposal
    sim = Sim(seed=1)
    bus = Bus(sim)
    build_home(sim, bus, seed=1)
    eng = PolicyEngine(sim, bus, StateStore(sim, bus))
    sim.run(until=5)
    ms = []
    for i in range(n):
        p = Proposal(tool="set_light", args={"room": "kitchen", "on": bool(i % 2)}, requester="anna", confidence=0.9,
                     channel="voice", text="turn on the kitchen light")
        t0 = time.perf_counter()
        eng.propose(p)
        ms.append((time.perf_counter() - t0) * 1000)
        sim.run(until=sim.now + 3)
    return ms


if __name__ == "__main__":
    main(only=sys.argv[1] if len(sys.argv) > 1 else None)   # "cutoffs" re-runs only section 2
