# The voice pipeline: wake word to confirmed reply, local, in a digital twin

Intelligent Spaces Lab, Build Track B05 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with synthetic voices and simulated devices, by AI agents under human direction. It is not a product and not a certified system. Research in simulation, provided as is. Use it at your own risk.

## 1. Question

Season 1 episode six has the family simply talking to the home. This study asks: can the whole voice path (wake word, end of speech, speech to text, intent, policy, device, spoken reply) run on a local hub, how fast is it on three classes of hub, and what breaks? The reply follows principle 2: the home says "Done" only after the device's own report shows the change.

## 2. Design under test

- **Pipeline** (`islab/pipeline/pipeline.py`, new): a staged pipeline on the B01 twin. Every stage publishes `pipeline/<satellite>/<stage>` on the bus with its virtual time, duration and the source of that duration (measured or assumption). The transcript goes out on the contract topic `voice/<satellite>`. Decisions go through the B01 `PolicyEngine`; commands and confirmation through the B01 `ActionTracker`, unchanged.
- **Replies**: "Done" only after the tracker confirms from a device state received after the command. If nothing is confirmed after 1.5 s, a holding line that claims nothing: "Sent. Waiting for the garage door to confirm." Failures use the tracker's plain words. A level-2 action (close the garage) is asked by voice and the person answers by voice; a level-3 action (unlock) sends a phone approval request.
- **Intent** (`islab/pipeline/intent.py`, new): a rule grammar first, wrapping the B01 parser. It converts STT number words, takes a missing room from the satellite, resolves "it" only from the same satellite's last action within 60 s that touched one device, and asks when a command is ambiguous or incomplete. Second path: `LLMStub`, a deterministic fuzzy matcher standing in for an LLM, timed with assumed LLM latency. It is not a language model.
- **Grammar-aware endpoint**: when the transcript at end of speech is incomplete ("set the kitchen to"), keep listening 700 ms more.
- **Baseline**: the same pipeline replying "Done" on the device's ack.
- **Tiers** (`islab/pipeline/tiers.json`): this CPU (measured stages); a Raspberry Pi 5 class box (CPU stages 2.5x this CPU, assumed); an x86 mini PC (1.0x, assumed); a mini PC with a 12 GB GPU (STT, TTS and LLM from B01 GPU assumptions). Shared stages (earcon, network, audio out) come from B01's `latency_config.json`.

## 3. Method

1. **Audio, real components on this CPU** (`measure.py`, 2 vCPU x86 VM, no GPU). openWakeWord `hey_jarvis` and silero-vad (both bundled in their wheels), webrtcvad, sherpa-onnx Whisper tiny.en and a streaming zipformer, Piper voices. 25 commands spoken by three synthetic Piper voices plus five Season 1 household lines, each after a synthetic "Hey Jarvis", in three conditions: clean, and with series narration through a TV-speaker EQ 10 dB and 0 dB below the command. Six commands split by a 0.3-0.9 s pause, endpointed with 300, 500 and 800 ms hangover. 8.7 minutes of narration, assistant voices and music through the TV EQ for false wakes, and 12 cases where the TV itself says the wake word.
2. **Blocked**: vosk (model host unreachable), faster-whisper and every LLM (huggingface.co returns 403). The Pi 5, mini PC and GPU tiers were not available.
3. **Twin** (`run.py`): per tier 400 seeded sessions on a fresh twin; three utterances 25 s apart drawn from the measured clips with their real transcript, wake result and endpoint delay (70% clean, 25% TV at 10 dB, 5% TV at 0 dB), 10% from the paused set, 5% from the TV-says-the-wake-word set. In 16% of sessions one addressed device is stuck, offline or loses its state reports. Ground truth is the twin device's physical state at the moment "Done" is heard.
4. **Time to first audio** = end of the person's speech to the first reply audio (a question, a holding line, an answer or "Done"). The earcon after the wake word is reported apart.

## 4. Results

### 4.1 Time to first audio, all utterances heard (`results.tiers.*.ttfa_ms`)

| Tier | All, p50 / p95 | Clean audio, rules path, p50 / p95 | End of speech to policy decision, p50 / p95 | LLM-stub path, p50 / p95 |
|---|---|---|---|---|
| This CPU (measured stages) | 2.08 / 11.04 s | 1.69 / 2.98 s | 0.84 / 4.19 s | 9.82 / 15.90 s |
| Pi 5 class (assumed) | 3.35 / 11.70 s | 2.97 / 4.63 s | 1.47 / 5.10 s | 10.32 / 16.43 s |
| Mini PC (assumed) | 2.08 / 7.39 s | 1.69 / 2.98 s | 0.84 / 4.03 s | 5.96 / 9.75 s |
| Mini PC + GPU (assumed) | 1.22 / 4.28 s | 1.01 / 2.14 s | 0.49 / 3.00 s | 2.33 / 5.00 s |

The earcon followed the wake word at 66 ms p50 and 110 ms p95 on every tier. "Done" itself was heard at p50 2.38 s (this CPU and mini PC), 3.79 s (Pi 5 class) and 1.57 s (GPU); the p95 of 16.6-20.5 s is the garage: a spoken question, a spoken yes and 11-13 s of door travel.

Measured stages on this CPU (`params.measured_stages_this_cpu`): wake 40 / 60 ms, end of speech 412 / 464 ms, Whisper tiny decode 396 / 654 ms, Piper first sentence 360 / 894 ms, grammar 0.02 / 0.04 ms, policy 0.03 / 0.09 ms. The machine was shared with other jobs: B01 measured Whisper at 166 ms and Piper at 87 ms on the same VM idle.

### 4.2 Confirmed before the reply (`results.tiers.minipc_cpu`, `results.baseline_ack`)

| Design | Executed commands | "Done" after device confirmed | Failure said in plain words | "Done" true when heard | Clean rules-path TTFA p50 / p95 |
|---|---|---|---|---|---|
| Reply after confirmation | 705 | 94.0% | 6.0% | 100% of 663 | 1.69 / 2.98 s |
| Reply on ack (baseline) | 705 | 27.0% | 1.7% | 70.3% of 693 | 1.25 / 1.80 s |

With a stuck, offline or silent device, the baseline said "Done" in 76.9% of 52 such commands. The confirming pipeline said "Done" in 19.2%, all true: the device was already in the target state, such as a stuck light that was already off.

### 4.3 Failure cases

| Case | How often | What the pipeline says | Evidence |
|---|---|---|---|
| Wake word missed | Audio: detected 100% clean, 92.5% with TV at 10 dB, 48.8% at 0 dB. Run: 4.1% of person utterances | Nothing; the person has to repeat | `audio.wake`, `failure_cases.wake_missed` |
| TV, no wake word | 0 activations in 8.7 min (95% upper bound 20.6 per hour) | Nothing | `audio.tv.no_wake_word` |
| TV says the wake word | Accepted 12 of 12; 0 actions from the TV speech that followed. Run: 62 events, 0 actions | "Sorry, I didn't catch that." (100% of events: it talks back to the TV) | `audio.tv.tv_says_wake_word`, `failure_cases.tv_wake_word` |
| End of speech too early | 300 ms hangover cut 100% of commands with a 0.5 s pause; 500 ms cut 5.6% at 0.5 s and 100% at 0.7 s; 800 ms cut 94.4% at 0.9 s. Run: 8.3% of utterances | Grammar-aware endpoint: confirmed 62, asked 3 of 95. Plain endpoint: confirmed 26, asked 43 ("I think I missed the end of that. Please say the whole request.") | `audio.cutoffs`, `ablation_plain_endpoint` |
| A cut that still parses | 18 of 95 cut commands executed something other than what was said ("turn on the kitchen light ... at fifty percent" ran at full brightness), with both endpoints | "Done. The kitchen light is on." | `failure_cases.cut_off` |
| STT errors on named commands | Whisper correct: 93.0% clean, 50.9% TV 10 dB, 14.0% TV 0 dB; wrong device 0%, 3.5%, 5.3%. Zipformer clean: 42.1% | Mostly "Sorry, I didn't catch that."; one voice's "unlock the front door" was heard as "and lock the front door" and executed | `audio.stt_on_named_commands`, `audio.stt_action_errors_whisper` |
| Ambiguity ("turn it off", "same in my study", "keep it on") | 308 utterances: 9.7% resolved from context, 21.1% from the room or a relative setpoint, 37.7% asked, 25.3% not a request | "Which one: the kitchen light or the living room light?" | `failure_cases.ambiguous` |
| Device not confirming | 52 commands hit a faulty device; 80.8% told in plain words | "The study thermostat got the command but never showed it happened..." | `failure_cases.device_not_confirming` |

Across all executed commands, 33 of 705 were not what the person said. Of 9 phone approval requests, 1 was not asked for: TV speech after "set the kitchen light to fifty percent" ended in "...camera clip", and the grammar proposed sharing a camera clip. The policy engine held it for a phone approval instead of acting.

## 5. What held

- Every "Done" in 663 replies came after the device's own report and was true when heard.
- No action came from the TV's own speech, in audio or in the run.
- The whole path runs on a 2 vCPU CPU from PyPI wheels and GitHub model releases, without the internet at run time.
- Grammar and policy cost well under a millisecond. On clean audio, the end-of-speech hangover and STT decode set the time to a decision.

## 6. What broke

- A TV under the command: missed wake words, an endpoint held open by the TV (silero p50 2.1 s after the command at 10 dB, `audio.endpoint`), and transcripts with TV words mixed in.
- One fixed hangover cannot handle both a quick reply and a mid-command pause. The grammar-aware endpoint fixed incomplete cuts; it cannot see a cut that still parses.
- The LLM path, even as a stub, costs seconds on CPU tiers.
- The B01 parser read "is the back door lock(ed)" as a lock command and opened the shade when no verb was heard. B05 wraps it; B01 code is unchanged (`experiments/b05_pipeline/notes.md`).

## 7. Limits

All speech is synthetic, digitally mixed, with no real room, far-field microphone or echo cancellation. The TV set is 8.7 minutes, so zero false wakes is a weak bound. Hardware tiers other than this CPU are assumptions; the CPU was shared during measurement. The LLM is a stub with assumed latency. Every tier uses the same Whisper tiny transcripts. Voice identity is a fixed confidence (speaker ID is B06). Fault, TV and pause rates in the run are scenario parameters. This is not a product and not a certified system.

## 8. Reproduce

```
pip install -r requirements.txt -r requirements-voice.txt
bash experiments/voice_latency/fetch_models.sh          # B01 models (STT, amy voice)
bash experiments/b05_pipeline/fetch_voices.sh           # two more Piper voices
ISLAB_VO=./audio ISLAB_MODELS=./models python experiments/b05_pipeline/measure.py   # about 10 min on 2 shared vCPU
python experiments/b05_pipeline/run.py                  # about 20 s
pytest -q tests/test_b05_pipeline.py
```

## Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
