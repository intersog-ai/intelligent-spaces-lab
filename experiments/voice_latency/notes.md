# voice_latency — notes (2026-11-19)

Files: `measure.py` (local measurements -> `measured.json`), `run.py` (Monte Carlo budget -> `results.json`),
`fetch_models.sh` (downloads the 3 sherpa-onnx release models), model config `islab/voice/latency_config.json`.

Machine: 2 vCPU x86_64 cloud VM, no GPU, Python 3.13. Network: PyPI and github.com release downloads worked;
huggingface.co and alphacephei.com were blocked (HTTP 403 from the proxy).

## What worked (measured here)

| Stage | Component | Where the weights came from | Result |
|---|---|---|---|
| Wake word | openWakeWord 0.4.0 (onnx) | bundled in the wheel | 2.0 ms compute per 80 ms frame (RTF 0.025). Fired on espeak-ng "Hey Jarvis" 153 ms and "Alexa" 121 ms after the phrase ended; missed "Hey Mycroft" (peak 0.001). Only 3 synthetic phrases: not a detection-rate result. |
| VAD / endpoint | silero-vad 6.2.3 (onnx) | bundled in the wheel | 0.19 ms per 32 ms chunk. End-of-speech event p50 324 / 425 / 620 ms after true speech end for 200 / 300 / 500 ms hangover. The latency is the hangover setting, not compute. p95 for 300/500 ms (1.6-2.0 s) is inflated by intra-sentence pauses that silencedetect counts as speech ends. |
| STT streaming | sherpa-onnx 2.0.2, zipformer-transducer en-20M int8, 1 thread | github.com release | final text 15 ms (p50) after end of audio; RTF 0.03; WER 14 % on whole VO files. On tightly cut short segments it loses onsets (e.g. "ULIN FROM ABOUT EIGHT"). |
| STT offline | sherpa-onnx 2.0.2 + Whisper tiny.en int8, 2 threads | github.com release | 166 ms p50 / 194 ms p95 decode after endpoint for 1.6 s (p50) commands; RTF 0.10; WER 6.3 %. |
| STT classical | pocketsphinx 5.1.1 | bundled en-us model in the wheel | RTF 0.17, WER 20 %. Not competitive. |
| Intent (rules) | 6 Python regexes | none | < 0.001 ms per utterance |
| TTS neural | Piper VITS en_US-amy-low through onnxruntime 1.29 + espeak-ng IPA phonemes | github.com release (sherpa-onnx tts-models) | first sentence audio 87 ms p50 / 107 ms p95, RTF 0.036. Spot check: Whisper transcribed the output as "Done the garage is close." |
| TTS formant | espeak-ng (espeakng-loader 0.2.4) | bundled lib + data | 0.2 ms to first 50 ms buffer, RTF 0.001. Robotic voice, but a zero-latency fallback for confirmations. |
| Speaker embedding | resemblyzer GE2E | bundled in the wheel | 21.5 ms per 2 s utterance |

## What was blocked or failed

- **faster-whisper 1.2.1**: installs, but `WhisperModel("tiny.en")` fails because the model downloads from huggingface.co, which returns 403.
- **vosk 0.3.45**: installs. `Model(lang="en-us")` fails on the model list at alphacephei.com (403). The wheel has no model.
- **sherpa-onnx TTS (`OfflineTts`)**: the wheel has no espeak frontend ("phoneme_codepoints is empty, no lexicon, and no frontend available"), so it returned 0 samples. Worked around by running the same Piper model directly in onnxruntime.
- **LLM intent**: not measured. GGUF and transformers weights are hosted on huggingface.co. Every LLM number in the config is an assumption.
- **Hailo-10H, Jetson Orin Nano Super, x86 + 12 GB GPU**: no hardware and no web access. Every tier number is labelled `source: assumption`.
- **Sandbox model substitution**: when it loads the Whisper ONNX files, this sandbox's sherpa_onnx can swap in "rewritten copies". measure.py sets `SANDBOX_SHERPA_ONNX_WHISPER_FILES=0` and records that in measured.json, so the published files are what gets measured. With the rewritten copies the p50 was about 130-155 ms; with the published files it is 166 ms.
- Installing resemblyzer pulled in torch 2.15 with the CUDA wheels (about 3 GB of `nvidia-*`). It also pulled in the PyPI `typing` backport, which does no harm because the 3.13 stdlib `typing` shadows it.

## Budget (results.json; p50 / p95, ms; 20k draws, seed 2026)

| Tier | Earcon after wake word | Reply audio after end of command: rules | Reply audio after end of command: LLM |
|---|---|---|---|
| Pi 5 + Hailo-10H (assumed) | 179 / 331 | 1185 / 1586 | 6126 / 11006 |
| Jetson Orin Nano Super (assumed) | 179 / 331 | 931 / 1240 | 3036 / 5004 |
| x86 + 12 GB GPU (assumed) | 179 / 331 | 725 / 958 | 1497 / 2184 |
| This CPU (measured STT/TTS; LLM assumed) | 179 / 331 | 740 / 923 | 7898 / 13973 |

## Reading
- With rules intents, the endpoint hangover (about 425 ms) dominates the budget, followed by STT. A streaming STT such as zipformer leaves almost nothing to decode once speech ends. The design choices that matter here are the hangover length and whether STT streams.
- The LLM path costs seconds on edge tiers because of time-to-first-token plus the tokens needed before the first speakable sentence. Recommended: rules or a small classifier first, the LLM only as a fallback, and an earcon or short espeak acknowledgement within 200 ms.
- Before using any tier number in a decision, replace it with a bench measurement.
