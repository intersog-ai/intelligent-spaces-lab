# b05_pipeline: notes (2026-11-19)

Files: `measure.py` (real audio components on this CPU -> `measured.json`), `run.py` (twin sessions -> `results.json`).
Package: `islab/pipeline/` (`pipeline.py`, `intent.py`, `tiers.py` + `tiers.json`, `real.py`).

```
ISLAB_VO=/path/to/vo ISLAB_MODELS=/path/to/models python experiments/b05_pipeline/measure.py   # ~10 min here
python experiments/b05_pipeline/measure.py cutoffs                                              # section 2 only, ~3 min
python experiments/b05_pipeline/run.py                                                          # ~20 s
```

Machine: 2 vCPU x86_64 cloud VM, no GPU, Python 3.13, shared with other jobs while measuring. measure.py took 585 s
end to end, longer than the 3-minute target; run.py reads its output and takes about 20 s.

## What ran here, from PyPI wheels

| Stage | Component | Weights from | Measured (clean, this CPU) |
|---|---|---|---|
| Wake word | openWakeWord 0.4.0 `hey_jarvis` | in the wheel | activation 40 ms p50 / 60 ms p95 after the phrase ends; 6.1 ms compute per 80 ms frame |
| End of speech | silero-vad 6.2.3, 300 ms hangover | in the wheel | 412 / 464 ms after the command ends |
| End of speech | webrtcvad 2.0.10, 300 ms hangover | none needed | 400 / 420 ms |
| STT | sherpa-onnx Whisper tiny.en int8, 2 threads | github.com release | decode 396 / 654 ms after the endpoint (B01 measured 166 ms on the idle machine) |
| STT | sherpa-onnx streaming zipformer en-20M | github.com release | final 15 / 28 ms after the endpoint, but 42.1% correct on named commands vs 93.0% for Whisper |
| TTS | Piper low voices (amy, ryan, alan) via onnxruntime | github.com release | first sentence 360 / 894 ms (B01 idle: 87 ms) |
| Intent | B05 grammar | none | 0.02 / 0.04 ms |
| Policy | B01 PolicyEngine.propose | none | 0.03 / 0.09 ms |

## Blocked
- `vosk` 0.3.45: `Model(lang="en-us")` fails: alphacephei.com unreachable through the proxy.
- `faster-whisper`: model download from huggingface.co, 403.
- No LLM weights (huggingface.co). `LLMStub` is a difflib matcher with assumed LLM latency, labelled as such.
- No Pi 5, mini PC or GPU: those tiers scale this CPU's numbers or reuse B01 assumptions (`islab/pipeline/tiers.json`).

## Findings in our own code and in B01 (B01 code not edited, wrapped in `islab/pipeline/intent.py`)
1. **B01 `parse_intents` turns a question into a command.** "is the back door locked", which Whisper often hears as "is the back door lock", parses as `lock_door`. The B05 grammar answers questions with `get_state` before calling the B01 parser.
2. **B01 `parse_intents` opens the shade when there is no verb.** Whisper heard "close the living room shade" as "'cuss the living room shade"; the B01 default position is 100 (open). The B05 grammar asks "Open or close the living room shade?".
3. **B01 `parse_intents` picks the kitchen for a light with no room.** The B05 grammar uses the satellite's room or asks.
4. **Our grammar-aware endpoint never fired in the first measurement**: the "speech resumed" test used the end of the next segment instead of its start. Fixed; the cut-off section was re-run.
5. **STT punctuation hid cut-offs**: "turn off the..." did not end in "the" for the grammar. Fixed by stripping STT punctuation.
6. **The simulated person said yes to every spoken level-2 question**, including "Should I close the garage?" after a TV-garbled "shut off the water". Now the person says yes only if the question matches what they asked for.
7. The pipeline imports two private helpers from `islab.policy.engine` (`_intent`, `_summary`) for its spoken lines. A public name for them would be cleaner (shared code, not changed here).

## Reading
- With rules intents, compute is small: the end-of-speech hangover (about 0.4 s) and STT decode set the time to a decision. After that the device decides: waiting for confirmation costs 0.44 s at p50 against replying on the ack (1.69 s vs 1.25 s, mini PC, clean audio), and the ack replies were true when heard only 70.3% of the time.
- A TV under the command is the main breaker: wake word detection drops to 48.8% at 0 dB, the endpoint stays open (p50 2.1 s after the command at 10 dB), and named commands are understood correctly in 50.9% (10 dB) and 14.0% (0 dB) of clips. A real satellite would use echo cancellation against its own speaker; nothing here models a TV's own audio reference.
- Every number for real hardware tiers is an assumption until measured on that hardware.
