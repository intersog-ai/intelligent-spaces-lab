"""Measure what can actually run on this CPU (2 vCPU x86, no GPU, offline except PyPI).

Run: python experiments/voice_latency/measure.py  -> experiments/voice_latency/measured.json
Each component either reports measured numbers or the exact reason it could not run."""
from __future__ import annotations
import ctypes, glob, json, os, re, statistics, sys, time, traceback
import numpy as np

# This sandbox's sherpa_onnx can silently substitute "rewritten" whisper model files. Measure the
# published models only (set before sherpa_onnx is imported; recorded in measured.json).
os.environ.setdefault("SANDBOX_SHERPA_ONNX_WHISPER_FILES", "0")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from islab.voice import audio  # noqa: E402

VO = os.environ.get("ISLAB_VO", "audio")
OUT = {}


def pct(xs, q):
    return float(np.percentile(np.asarray(xs), q))


def record(name, fn):
    t0 = time.perf_counter()
    try:
        OUT[name] = {"status": "ran", **fn()}
    except BaseException as e:  # noqa: BLE001 - we want to record *any* failure
        OUT[name] = {"status": "blocked_or_failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    OUT[name]["wall_s"] = round(time.perf_counter() - t0, 2)
    print(name, json.dumps(OUT[name])[:400])


# ---------------- TTS: espeak-ng (library + data bundled in the espeakng-loader wheel) ----------------
_ESPEAK = {}


def espeak_init():
    if _ESPEAK:
        return _ESPEAK
    import espeakng_loader
    lib = ctypes.CDLL(espeakng_loader.get_library_path())
    CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
    lib.espeak_Initialize.restype = ctypes.c_int
    sr = lib.espeak_Initialize(2, 50, espeakng_loader.get_data_path().encode(), 0)  # SYNCHRONOUS, 50 ms buffers
    lib.espeak_SetVoiceByName(b"en-us")
    state = {"chunks": [], "t_first": None}

    def cb(wav, n, ev):
        if n > 0:
            if state["t_first"] is None:
                state["t_first"] = time.perf_counter()
            state["chunks"].append(np.ctypeslib.as_array(wav, shape=(n,)).copy())
        return 0
    cbf = CB(cb)
    lib.espeak_SetSynthCallback(cbf)
    _ESPEAK.update(lib=lib, sr=sr, state=state, cbf=cbf)
    return _ESPEAK


def espeak_say(text):
    E = espeak_init()
    st = E["state"]; st["chunks"].clear(); st["t_first"] = None
    b = text.encode() + b"\0"
    t0 = time.perf_counter()
    E["lib"].espeak_Synth(b, len(b), 0, 0, 0, 0, None, None)
    t1 = time.perf_counter()
    wav = np.concatenate(st["chunks"]).astype(np.float32) / 32768 if st["chunks"] else np.zeros(0, np.float32)
    return wav, E["sr"], (st["t_first"] or t1) - t0, t1 - t0


def tts():
    lines = ["Raising the kitchen to twenty-one. Give it a few minutes.",
             "Only Anna or Mark can unlock the front door. I've sent Anna a request.",
             "Done. The garage is closed.",
             "Your study is usually nineteen, Mark. I'll keep it at twenty while you're in."]
    ttfa, rtf = [], []
    for _ in range(5):
        for ln in lines:
            wav, sr, first, total = espeak_say(ln)
            ttfa.append(first * 1000); rtf.append(total / (len(wav) / sr))
    return {"engine": "espeak-ng (espeakng-loader 0.2.4 bundled lib+data), formant synthesis, low quality",
            "time_to_first_audio_ms_p50": round(pct(ttfa, 50), 1), "time_to_first_audio_ms_p95": round(pct(ttfa, 95), 1),
            "rtf_p50": round(pct(rtf, 50), 4), "n": len(ttfa)}


# ---------------- Wake word: openWakeWord 0.4.0 (models bundled in wheel) ----------------
def wakeword():
    from openwakeword.model import Model
    from scipy.signal import resample_poly
    m = Model(wakeword_model_paths=[], inference_framework="onnx") if False else Model()
    # synthetic trigger phrase with espeak-ng (openWakeWord models were trained largely on synthetic TTS)
    res = {}
    for phrase, key in (("Hey Jarvis", "hey_jarvis"), ("Alexa", "alexa"), ("Hey Mycroft", "hey_mycroft")):
        wav, sr, _, _ = espeak_say(phrase)
        w16 = resample_poly(wav, 16000, sr).astype(np.float32)
        speech_end = 1.0 + len(w16) / 16000
        x = np.concatenate([np.zeros(16000, np.float32), w16, np.zeros(24000, np.float32)])
        x16 = (x * 32767 * 0.7).astype(np.int16)
        m.reset()
        det_t, frame_ms, peak = None, [], 0.0
        for i in range(0, len(x16) - 1280 + 1, 1280):
            t0 = time.perf_counter()
            p = m.predict(x16[i:i + 1280])
            frame_ms.append((time.perf_counter() - t0) * 1000)
            peak = max(peak, p.get(key, 0.0))
            if det_t is None and p.get(key, 0.0) >= 0.5:
                det_t = (i + 1280) / 16000
        res[key] = {"detected": det_t is not None, "peak_score": round(float(peak), 3),
                    "detection_delay_after_speech_end_ms": None if det_t is None else round((det_t - speech_end) * 1000),
                    }
        res.setdefault("_frame_ms", []).extend(frame_ms)
    fm = res.pop("_frame_ms")
    return {"engine": "openWakeWord 0.4.0 onnx (melspec+embedding+keyword heads, all bundled)",
            "frame": "80 ms", "compute_ms_per_80ms_frame_p50": round(pct(fm, 50), 2),
            "compute_ms_per_80ms_frame_p95": round(pct(fm, 95), 2),
            "rtf": round(pct(fm, 50) / 80, 4),
            "trigger_test_espeak_voice": res,
            "note": "espeak-ng is far from the TTS the models were trained on; a miss here is not a model verdict"}


# ---------------- VAD: silero-vad 6.x (onnx bundled) ----------------
def vad():
    from silero_vad import load_silero_vad, VADIterator
    import torch
    model = load_silero_vad(onnx=True)
    files = sorted(glob.glob(f"{VO}/E0*_Amanda.mp3")) + sorted(glob.glob(f"{VO}/v2/E0*_Ollie*.mp3"))
    out = {"engine": "silero-vad 6.2.3 onnx (bundled)", "chunk": "512 samples = 32 ms"}
    chunk_ms = []
    for hang in (200, 300, 500):
        delays = []
        for f in files:
            w = audio.load(f)
            truth_ends = [e for s, e in audio.silence_segments(f, -40, 0.3)]
            it = VADIterator(model, threshold=0.5, sampling_rate=16000, min_silence_duration_ms=hang, speech_pad_ms=0)
            ends = []
            for i in range(0, len(w) - 512 + 1, 512):
                t0 = time.perf_counter()
                ev = it(torch.from_numpy(w[i:i + 512]), return_seconds=True)
                chunk_ms.append((time.perf_counter() - t0) * 1000)
                if ev and "end" in ev:
                    ends.append((i + 512) / 16000)   # wall position at which the end event is emitted
            for te in truth_ends:
                later = [e for e in ends if e >= te - 0.05]
                if later and later[0] - te < 2.0:
                    delays.append((later[0] - te) * 1000)
        out[f"endpoint_delay_ms_hangover_{hang}"] = {"p50": round(pct(delays, 50)), "p95": round(pct(delays, 95)), "n": len(delays)}
    out["compute_ms_per_32ms_chunk_p50"] = round(pct(chunk_ms, 50), 3)
    out["rtf"] = round(pct(chunk_ms, 50) / 32, 4)
    out["note"] = ("endpoint delay = time from true end of utterance (ffmpeg silencedetect -40 dB) to the VAD 'end' event; "
                   "it is dominated by the configured hangover, i.e. a product choice, not compute")
    return out


# ---------------- STT ----------------
def wer(ref, hyp):
    r = re.findall(r"[a-z']+", ref.lower()); h = re.findall(r"[a-z']+", hyp.lower())
    d = np.arange(len(h) + 1)
    for i, rw in enumerate(r, 1):
        prev, d[0] = d.copy(), i
        for j, hw in enumerate(h, 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (rw != hw))
    return d[len(h)] / max(1, len(r))


def stt_pocketsphinx():
    from pocketsphinx import Decoder
    dec = Decoder(samprate=16000, loglevel="FATAL")
    rows = []
    for f in sorted(glob.glob(f"{VO}/E0*_Amanda.mp3")) + sorted(glob.glob(f"{VO}/E0*_Ollie.mp3")):
        w = audio.load(f)
        pcm = (np.clip(w, -1, 1) * 32767).astype(np.int16).tobytes()
        t0 = time.perf_counter()
        dec.start_utt(); dec.process_raw(pcm, full_utt=True); dec.end_utt()
        dt = time.perf_counter() - t0
        hyp = dec.hyp().hypstr if dec.hyp() else ""
        ref_path = f[:-4] + ".txt"
        ref = open(ref_path).read() if os.path.exists(ref_path) else ""
        ref = re.sub(r"\[.*?\]", " ", ref)
        rows.append({"file": os.path.basename(f), "audio_s": round(len(w) / 16000, 2), "decode_s": round(dt, 3),
                     "rtf": round(dt / (len(w) / 16000), 3), "wer": round(wer(ref, hyp), 3), "hyp_head": hyp[:80]})
    return {"engine": "pocketsphinx 5.1.1, bundled en-us HMM + 3-gram LM (CMU Sphinx)",
            "rtf_mean": round(statistics.mean(r["rtf"] for r in rows), 3),
            "wer_mean": round(statistics.mean(r["wer"] for r in rows), 3), "files": rows,
            "note": "batch decode of whole files; RTF on 1 thread. WER vs the voice-over scripts."}


def stt_faster_whisper():
    from faster_whisper import WhisperModel
    WhisperModel("tiny.en", device="cpu", compute_type="int8")
    return {}


def stt_vosk():
    from vosk import Model
    Model(lang="en-us")
    return {}


MODELS = os.environ.get("ISLAB_MODELS", "models")
ZIP = f"{MODELS}/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
WSP = f"{MODELS}/sherpa-onnx-whisper-tiny.en"
PIPER = f"{MODELS}/vits-piper-en_US-amy-low"


def _command_utterances():
    """Short command-like utterances: every speech segment of the household voices + Amanda E06."""
    out = []
    for f in sorted(glob.glob(f"{VO}/v2/E0*_Anna.mp3") + glob.glob(f"{VO}/v2/E0*_Mark.mp3") +
                    glob.glob(f"{VO}/v2/E06_Sam.mp3") + [f"{VO}/E06_Amanda.mp3"]):
        w = audio.load(f)
        for s0, e0 in audio.silence_segments(f, -40, 0.3):
            seg = w[int(s0 * 16000):int(e0 * 16000)]
            if len(seg) > 0.6 * 16000:
                out.append((os.path.basename(f), seg))
    return out


def _wer_files():
    fs = sorted(glob.glob(f"{VO}/E0*_Amanda.mp3")) + sorted(glob.glob(f"{VO}/E0*_Ollie.mp3"))
    return [(f, audio.load(f), re.sub(r"\[.*?\]", " ", open(f[:-4] + ".txt").read())) for f in fs]


def stt_sherpa_zipformer():
    import sherpa_onnx
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=f"{ZIP}/tokens.txt", encoder=f"{ZIP}/encoder-epoch-99-avg-1.int8.onnx",
        decoder=f"{ZIP}/decoder-epoch-99-avg-1.int8.onnx", joiner=f"{ZIP}/joiner-epoch-99-avg-1.int8.onnx",
        num_threads=1, sample_rate=16000, feature_dim=80, decoding_method="greedy_search")

    def run(w):
        # a satellite streams continuously, so the recogniser has pre-roll before the utterance
        w = np.concatenate([np.zeros(int(0.3 * 16000), np.float32), w])
        st = rec.create_stream()
        step = 1600  # 100 ms chunks, as a satellite would stream
        chunk_ms = []
        for i in range(0, len(w), step):
            t0 = time.perf_counter()
            st.accept_waveform(16000, w[i:i + step])
            while rec.is_ready(st):
                rec.decode_stream(st)
            chunk_ms.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()  # end of speech reached: flush with tail padding
        st.accept_waveform(16000, np.zeros(int(0.5 * 16000), np.float32))
        st.input_finished()
        while rec.is_ready(st):
            rec.decode_stream(st)
        final_ms = (time.perf_counter() - t0) * 1000
        return rec.get_result(st), chunk_ms, final_ms

    lat, chunks, hyps = [], [], []
    for name, seg in _command_utterances():
        txt, cm, fm = run(seg)
        lat.append(fm); chunks += cm; hyps.append((name, txt))
    wers, rtfs = [], []
    for f, w, ref in _wer_files():
        t0 = time.perf_counter(); txt, _, _ = run(w); dt = time.perf_counter() - t0
        wers.append(wer(ref, txt)); rtfs.append(dt / (len(w) / 16000))
    return {"engine": "sherpa-onnx 2.0.2 streaming zipformer-transducer en-20M int8 (model from github.com releases)",
            "threads": 1, "chunk": "100 ms",
            "compute_ms_per_100ms_chunk_p50": round(pct(chunks, 50), 2), "compute_ms_per_100ms_chunk_p95": round(pct(chunks, 95), 2),
            "final_result_after_end_of_speech_ms_p50": round(pct(lat, 50), 1),
            "final_result_after_end_of_speech_ms_p95": round(pct(lat, 95), 1),
            "rtf_whole_file": round(statistics.mean(rtfs), 3), "wer_mean": round(statistics.mean(wers), 3),
            "n_command_utterances": len(lat), "sample_hyps": hyps[:6]}


def stt_sherpa_whisper_tiny():
    import sherpa_onnx
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=f"{WSP}/tiny.en-encoder.int8.onnx", decoder=f"{WSP}/tiny.en-decoder.int8.onnx",
        tokens=f"{WSP}/tiny.en-tokens.txt", language="en", num_threads=2)

    def run(w):
        st = rec.create_stream(); st.accept_waveform(16000, w)
        t0 = time.perf_counter(); rec.decode_stream(st); return st.result.text, time.perf_counter() - t0
    run(np.zeros(16000, np.float32))
    lat, durs, hyps = [], [], []
    for name, seg in _command_utterances():
        txt, dt = run(seg); lat.append(dt * 1000); durs.append(len(seg) / 16000); hyps.append((name, txt))
    # WER on <=30 s files (whisper window)
    wers = []
    for f, w, ref in _wer_files():
        txt, _ = run(w[: 30 * 16000]); wers.append(wer(ref, txt))
    return {"engine": "sherpa-onnx 2.0.2 + whisper tiny.en int8 (model from github.com releases)", "threads": 2,
            "decode_ms_after_end_of_speech_p50": round(pct(lat, 50), 1),
            "decode_ms_after_end_of_speech_p95": round(pct(lat, 95), 1),
            "utterance_s_p50": round(pct(durs, 50), 2), "rtf_p50": round(pct(np.array(lat) / 1000 / np.array(durs), 50), 3),
            "wer_mean": round(statistics.mean(wers), 3), "n_command_utterances": len(lat), "sample_hyps": hyps[:6],
            "note": "offline (non-streaming): the whole utterance is decoded after the endpoint, so this is pure added latency"}


def _ipa(text):
    E = espeak_init(); lib = E["lib"]
    lib.espeak_SetVoiceByName(b"en-us")
    lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
    lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int]
    buf = ctypes.c_char_p(text.encode()); ptr = ctypes.pointer(buf); out = []
    while buf.value:
        out.append(lib.espeak_TextToPhonemes(ptr, 0, 0x02).decode())  # 0x02 = IPA
    return " ".join(out)


def tts_piper():
    """Piper VITS voice run directly with onnxruntime. sherpa-onnx 2.0.2's wheel has no espeak frontend
    ('phoneme_codepoints is empty, no lexicon, and no frontend available'), so phonemes come from the
    bundled espeak-ng (IPA) and are mapped to ids with tokens.txt, the standard piper recipe."""
    import onnxruntime as ort
    ids = {}
    for ln in open(f"{PIPER}/tokens.txt", encoding="utf-8"):
        ln = ln.rstrip("\n")
        if ln:
            tok, i = ln[:-len(ln.split(" ")[-1]) - 1], int(ln.split(" ")[-1])
            ids[tok if tok else " "] = i
    so = ort.SessionOptions(); so.intra_op_num_threads = 2
    sess = ort.InferenceSession(f"{PIPER}/en_US-amy-low.onnx", so, providers=["CPUExecutionProvider"])
    sr = 16000  # amy-low

    def synth(sentence):
        ph = _ipa(sentence)
        seq = [ids["^"], ids["_"]]
        for ch in ph:
            if ch in ids:
                seq += [ids[ch], ids["_"]]
        seq.append(ids["$"])
        x = np.array([seq], dtype=np.int64)
        return sess.run(None, {"input": x, "input_lengths": np.array([x.shape[1]], dtype=np.int64),
                               "scales": np.array([0.667, 1.0, 0.8], dtype=np.float32)})[0].squeeze()

    lines = ["Raising the kitchen to twenty-one. Give it a few minutes.",
             "Only Anna or Mark can unlock the front door. I've sent Anna a request.",
             "Done. The garage is closed.",
             "Your study is usually nineteen, Mark. I'll keep it at twenty while you're in."]
    synth("Warm up.")
    ttfa, rtf = [], []
    for _ in range(3):
        for ln in lines:
            sents = [x.strip() + "." for x in ln.split(".") if x.strip()]
            t0 = time.perf_counter(); n = 0; first = None
            for snt in sents:              # sentence streaming: first sentence plays while the rest renders
                y = synth(snt); n += len(y)
                if first is None:
                    first = time.perf_counter() - t0
            dt = time.perf_counter() - t0
            ttfa.append(first * 1000); rtf.append(dt / (n / sr))
    return {"engine": "Piper VITS en_US-amy-low (low quality tier) via onnxruntime 1.29, espeak-ng IPA frontend; "
                      "model from sherpa-onnx github release", "threads": 2,
            "chunking": "sentence-level", "time_to_first_audio_ms_p50": round(pct(ttfa, 50), 1),
            "time_to_first_audio_ms_p95": round(pct(ttfa, 95), 1), "rtf_p50": round(pct(rtf, 50), 3), "n": len(ttfa),
            "sherpa_onnx_tts_note": "sherpa-onnx 2.0.2 OfflineTts produced 0 samples: wheel lacks the espeak frontend"}


# ---------------- Intent: rules ----------------
RULES = [
    (re.compile(r"\b(set|raise|lower|make)\b.*\b(kitchen|study)\b.*?(\d+|twenty[- ]?\w*|nineteen|eighteen)"), "thermo.set"),
    (re.compile(r"\b(unlock|open)\b.*\bfront door\b"), "lock.front.unlock"),
    (re.compile(r"\b(lock)\b.*\b(front|back) door\b"), "lock.lock"),
    (re.compile(r"\b(close|shut)\b.*\bgarage\b"), "garage.close"),
    (re.compile(r"\b(turn|switch)\b.*\b(on|off)\b.*\blights?\b"), "light.toggle"),
    (re.compile(r"\b(shut|close|turn off)\b.*\b(water|valve)\b"), "valve.close"),
]


def intent_rules():
    utts = ["set the kitchen to twenty one", "please unlock the front door", "close the garage",
            "turn off the living room lights", "shut off the water", "what's the weather like"] * 2000
    t0 = time.perf_counter()
    hits = 0
    for u in utts:
        for rx, name in RULES:
            if rx.search(u):
                hits += 1; break
    dt = (time.perf_counter() - t0) / len(utts)
    return {"engine": "6 regex rules (python re)", "us_per_utterance": round(dt * 1e6, 2), "hit_rate": round(hits / len(utts), 3)}


def intent_llm():
    raise RuntimeError("no local LLM weights obtainable: llama.cpp/transformers models are hosted on huggingface.co (blocked); "
                       "not measured, assumed values only")


def resemblyzer_speaker():
    from islab.voice.speaker import ResemblyzerEncoder
    enc = ResemblyzerEncoder()
    w = audio.load(f"{VO}/v2/E0709_Anna.mp3")[: 2 * 16000]
    enc.embed(w)
    ts = []
    for _ in range(30):
        t0 = time.perf_counter(); enc.embed(w); ts.append((time.perf_counter() - t0) * 1000)
    return {"engine": "resemblyzer GE2E", "ms_per_2s_utterance_p50": round(pct(ts, 50), 1), "p95": round(pct(ts, 95), 1)}


if __name__ == "__main__":
    import platform
    OUT["_env"] = {"SANDBOX_SHERPA_ONNX_WHISPER_FILES": os.environ.get("SANDBOX_SHERPA_ONNX_WHISPER_FILES")}
    OUT["_machine"] = {"cpu": platform.processor() or platform.machine(), "cpus": os.cpu_count(), "python": platform.python_version()}
    record("tts_espeak_ng", tts)
    record("wake_openwakeword", wakeword)
    record("vad_silero", vad)
    record("stt_pocketsphinx", stt_pocketsphinx)
    record("stt_faster_whisper_tiny_en", stt_faster_whisper)
    record("stt_vosk", stt_vosk)
    record("stt_sherpa_zipformer_streaming", stt_sherpa_zipformer)
    record("stt_sherpa_whisper_tiny_en", stt_sherpa_whisper_tiny)
    record("tts_piper_amy_low", tts_piper)
    record("intent_rules", intent_rules)
    record("intent_llm", intent_llm)
    record("speaker_embedding", resemblyzer_speaker)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measured.json")
    json.dump(OUT, open(p, "w"), indent=1)
    print("wrote", p)
