"""Adapters for real local audio components (all optional; imported lazily).

Every class runs on CPU from PyPI wheels; STT and Piper weights come from the sherpa-onnx GitHub
releases (see experiments/voice_latency/fetch_models.sh and experiments/b05_pipeline/notes.md).
Audio is mono float32 at 16 kHz.

    ww = WakeWord("hey_jarvis");      hits = ww.detect(wav)            # [(t_s, score)], compute ms/frame
    ep = SileroEndpointer(300);        ends = ep.end_events(wav)        # [t_s] when "end of speech" fires
    ep = WebrtcEndpointer(300, 2)
    stt = ZipformerSTT(models_dir);   text, final_ms = stt.transcribe(wav)
    stt = WhisperSTT(models_dir);     text, decode_ms = stt.transcribe(wav)
    tts = PiperVoice(models_dir, "en_US-amy-low", "en-us");  wav = tts.synth("Done.")
"""
from __future__ import annotations
import ctypes, os, time
import numpy as np

SR = 16000
os.environ.setdefault("SANDBOX_SHERPA_ONNX_WHISPER_FILES", "0")   # measure the published model files (see B01 notes)


# ---------------------------------------------------------------- espeak-ng (bundled in espeakng-loader)
class Espeak:
    _inst = None

    def __init__(self):
        import espeakng_loader
        self.lib = lib = ctypes.CDLL(espeakng_loader.get_library_path())
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
        lib.espeak_Initialize.restype = ctypes.c_int
        self.sr = lib.espeak_Initialize(2, 50, espeakng_loader.get_data_path().encode(), 0)
        self.chunks = []

        def cb(wav, n, ev):
            if n > 0:
                self.chunks.append(np.ctypeslib.as_array(wav, shape=(n,)).copy())
            return 0
        self._cb = CB(cb)
        lib.espeak_SetSynthCallback(self._cb)
        lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
        lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int]

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def ipa(self, text, voice="en-us"):
        self.lib.espeak_SetVoiceByName(voice.encode())
        buf = ctypes.c_char_p(text.encode())
        ptr = ctypes.pointer(buf)
        out = []
        while buf.value:
            out.append(self.lib.espeak_TextToPhonemes(ptr, 0, 0x02).decode())
        return " ".join(out)


# ---------------------------------------------------------------- Piper VITS via onnxruntime
class PiperVoice:
    """Piper voice run directly with onnxruntime (sherpa-onnx 2.0.2 OfflineTts lacks the espeak frontend)."""

    def __init__(self, models_dir, name="en_US-amy-low", espeak_voice="en-us", threads=2):
        import onnxruntime as ort
        d = os.path.join(models_dir, f"vits-piper-{name}")
        self.ids = {}
        for ln in open(os.path.join(d, "tokens.txt"), encoding="utf-8"):
            ln = ln.rstrip("\n")
            if ln:
                tok, i = ln[:-len(ln.split(" ")[-1]) - 1], int(ln.split(" ")[-1])
                self.ids[tok if tok else " "] = i
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(os.path.join(d, f"{name}.onnx"), so, providers=["CPUExecutionProvider"])
        self.name, self.voice = name, espeak_voice
        self.sr = 16000

    def synth(self, text, length_scale=1.0):
        ph = Espeak.get().ipa(text, self.voice)
        seq = [self.ids["^"], self.ids["_"]]
        for ch in ph:
            if ch in self.ids:
                seq += [self.ids[ch], self.ids["_"]]
        seq.append(self.ids["$"])
        x = np.array([seq], dtype=np.int64)
        y = self.sess.run(None, {"input": x, "input_lengths": np.array([x.shape[1]], dtype=np.int64),
                                 "scales": np.array([0.667, length_scale, 0.8], dtype=np.float32)})[0].squeeze()
        return trim(y.astype(np.float32))


def trim(w, db=-45.0):
    """Cut leading/trailing near-silence (20 ms frames)."""
    f = 320
    n = len(w) // f
    if n == 0:
        return w
    rms = 20 * np.log10(np.sqrt(np.mean(w[: n * f].reshape(n, f) ** 2, axis=1)) + 1e-9)
    idx = np.where(rms > db)[0]
    if len(idx) == 0:
        return w
    return w[idx[0] * f: (idx[-1] + 1) * f]


# ---------------------------------------------------------------- wake word: openWakeWord (bundled models)
class WakeWord:
    def __init__(self, model="hey_jarvis", threshold=0.5):
        import openwakeword
        from openwakeword.model import Model
        path = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", f"{model}_v0.1.onnx")
        self.m = Model(wakeword_model_paths=[path])
        self.key = list(self.m.models.keys())[0]
        self.threshold = threshold
        self.frame_ms = []

    def scores(self, wav):
        """Score per 80 ms frame; [(t_end_of_frame_s, score)]."""
        self.m.reset()
        x16 = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
        out = []
        for i in range(0, len(x16) - 1280 + 1, 1280):
            t0 = time.perf_counter()
            p = self.m.predict(x16[i:i + 1280])
            self.frame_ms.append((time.perf_counter() - t0) * 1000)
            out.append(((i + 1280) / SR, float(p.get(self.key, 0.0))))
        return out

    def detect(self, wav, refractory_s=2.0):
        """Activations (onset frames at or above threshold, then refractory_s of silence)."""
        hits, last = [], -1e9
        for t, s in self.scores(wav):
            if s >= self.threshold and t - last >= refractory_s:
                hits.append((t, s))
                last = t
        return hits


# ---------------------------------------------------------------- end of speech
class SileroEndpointer:
    def __init__(self, hangover_ms=300, threshold=0.5):
        from silero_vad import load_silero_vad
        self.model = load_silero_vad(onnx=True)
        self.hangover_ms, self.threshold = hangover_ms, threshold
        self.chunk_ms = []

    def end_events(self, wav, hangover_ms=None):
        """Times (s, at chunk end) when the VAD emits 'end of speech'."""
        import torch
        from silero_vad import VADIterator
        it = VADIterator(self.model, threshold=self.threshold, sampling_rate=SR,
                         min_silence_duration_ms=hangover_ms or self.hangover_ms, speech_pad_ms=0)
        ends = []
        for i in range(0, len(wav) - 512 + 1, 512):
            t0 = time.perf_counter()
            ev = it(torch.from_numpy(np.ascontiguousarray(wav[i:i + 512])), return_seconds=True)
            self.chunk_ms.append((time.perf_counter() - t0) * 1000)
            if ev and "end" in ev:
                ends.append((i + 512) / SR)
        return ends


class WebrtcEndpointer:
    """webrtcvad frames (30 ms); 'end' after hangover_ms of consecutive non-speech following speech."""

    def __init__(self, hangover_ms=300, aggressiveness=2):
        import webrtcvad
        self.vad = webrtcvad.Vad(aggressiveness)
        self.hangover_ms = hangover_ms

    def end_events(self, wav, hangover_ms=None):
        hang = hangover_ms or self.hangover_ms
        pcm = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
        f = 480
        in_speech, silence, ends = False, 0, []
        for i in range(0, len(pcm) - f + 1, f):
            sp = self.vad.is_speech(pcm[i:i + f].tobytes(), SR)
            if sp:
                in_speech, silence = True, 0
            elif in_speech:
                silence += 30
                if silence >= hang:
                    ends.append((i + f) / SR)
                    in_speech, silence = False, 0
        return ends


# ---------------------------------------------------------------- speech to text (sherpa-onnx)
class ZipformerSTT:
    """Streaming zipformer transducer (en-20M int8). transcribe() returns (text, ms from end of audio to final)."""

    def __init__(self, models_dir, threads=1):
        import sherpa_onnx
        z = os.path.join(models_dir, "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17")
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{z}/tokens.txt", encoder=f"{z}/encoder-epoch-99-avg-1.int8.onnx",
            decoder=f"{z}/decoder-epoch-99-avg-1.int8.onnx", joiner=f"{z}/joiner-epoch-99-avg-1.int8.onnx",
            num_threads=threads, sample_rate=SR, feature_dim=80, decoding_method="greedy_search")

    def transcribe(self, wav):
        st = self.rec.create_stream()
        w = np.concatenate([np.zeros(int(0.3 * SR), np.float32), wav])   # pre-roll, as a streaming satellite
        for i in range(0, len(w), 1600):
            st.accept_waveform(SR, w[i:i + 1600])
            while self.rec.is_ready(st):
                self.rec.decode_stream(st)
        t0 = time.perf_counter()
        st.accept_waveform(SR, np.zeros(int(0.5 * SR), np.float32))
        st.input_finished()
        while self.rec.is_ready(st):
            self.rec.decode_stream(st)
        return self.rec.get_result(st).strip().lower(), (time.perf_counter() - t0) * 1000


class WhisperSTT:
    """Whisper tiny.en int8 (offline). transcribe() returns (text, decode ms after the endpoint)."""

    def __init__(self, models_dir, threads=2):
        import sherpa_onnx
        w = os.path.join(models_dir, "sherpa-onnx-whisper-tiny.en")
        self.rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=f"{w}/tiny.en-encoder.int8.onnx", decoder=f"{w}/tiny.en-decoder.int8.onnx",
            tokens=f"{w}/tiny.en-tokens.txt", language="en", num_threads=threads)
        self.transcribe(np.zeros(SR, np.float32))

    def transcribe(self, wav):
        st = self.rec.create_stream()
        st.accept_waveform(SR, wav)
        t0 = time.perf_counter()
        self.rec.decode_stream(st)
        return st.result.text.strip().lower(), (time.perf_counter() - t0) * 1000
