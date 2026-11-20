# B05 · The voice pipeline

*Intelligent Spaces · Build Track · episode 5 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode six has the family talking to the home. We built the whole voice path on a local hub in the twin: wake word, end of speech, speech to text, a rule grammar with an LLM stand-in behind it, the policy engine, the device, and a spoken reply that says "Done" only after the device confirms. The wake word, end-of-speech, speech-to-text and TTS models ran for real on a 2-core CPU with synthetic voices, clean and with a TV playing. Three hub classes we could not test are modelled with labelled assumptions.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | It runs locally on a 2-core CPU | Clean audio, rules path: first reply p50 1.69 s, p95 2.98 s after speech ends; earcon 66 ms after the wake word | [`results.json`](../../experiments/b05_pipeline/results.json) `tiers.this_cpu` |
| 2 | Hub class matters, mostly for STT and the LLM (assumed tiers) | Clean rules path p50: Pi 5 class 2.97 s, mini PC 1.69 s, mini PC + GPU 1.01 s; LLM-stub path p50 10.32 / 5.96 / 2.33 s | [`islab/pipeline/tiers.json`](../../islab/pipeline/tiers.json) |
| 3 | Compute is not the bottleneck on clean audio | End of speech 412 ms, Whisper tiny 396 ms (measured, shared CPU); grammar 0.02 ms, policy 0.03 ms | [`measured.json`](../../experiments/b05_pipeline/measured.json) |
| 4 | "Done" only after the device confirms | 663 "Done" replies, 100% after device state, 100% true when heard; reply-on-ack baseline: 70.3% true, p50 1.25 s vs 1.69 s | [`experiments/b05_pipeline`](../../experiments/b05_pipeline) |
| 5 | Broken devices get plain words | Stuck, offline or silent device: told in 80.8% of 52 commands, the rest already in the asked state; the ack baseline said "Done" in 76.9% | `failure_cases.device_not_confirming` |
| 6 | A TV under the command breaks the most | Wake word caught 48.8% at 0 dB; named commands understood 93.0% clean, 50.9% at 10 dB, 14.0% at 0 dB | `audio.wake`, `audio.stt_on_named_commands` |
| 7 | The TV can wake it, but did not command it | TV said the wake word: accepted 12 of 12, 0 actions; it answered the TV "Sorry, I didn't catch that." No false wakes in 8.7 min of TV audio (weak bound) | `audio.tv` |
| 8 | Pauses cut commands; the grammar can wait | 300 ms hangover cut 100% of commands with a 0.5 s pause; grammar-aware endpoint: 62 of 95 cut commands confirmed vs 26; 18 still did something else ("at fifty percent" lost) | `audio.cutoffs`, `ablation_plain_endpoint` |
| 9 | "It" needs context or a question | "Turn it off" and similar: asked 37.7%, resolved from the last device 9.7%, from the room 21.1% | `failure_cases.ambiguous` |
| 10 | Bugs found in B01's parser | "Is the back door locked" parsed as a lock command; a verbless shade phrase opened the shade. Wrapped in B05, B01 unchanged | [`notes.md`](../../experiments/b05_pipeline/notes.md) |

## Proving core: what reproduces this episode

```
git checkout b05
pip install -r requirements.txt -r requirements-voice.txt
pytest -q tests/test_b05_pipeline.py          # grammar, tiers, pipeline on the twin, results checks
python experiments/b05_pipeline/run.py        # 400 sessions x 4 tiers + baseline, about 20 s (reads measured.json)
# optional, re-measure audio (needs models and voices, about 10 min on 2 vCPU):
bash experiments/voice_latency/fetch_models.sh && bash experiments/b05_pipeline/fetch_voices.sh
ISLAB_VO=./audio ISLAB_MODELS=./models python experiments/b05_pipeline/measure.py
```

## Documents in this episode

- [`docs/paper_b05.md`](../../docs/paper_b05.md): method, results, limits.
- [`experiments/b05_pipeline/notes.md`](../../experiments/b05_pipeline/notes.md): what ran, what was blocked, what we fixed.
- [`experiments/b05_pipeline/results.json`](../../experiments/b05_pipeline/results.json): every number in this episode; [`measured.json`](../../experiments/b05_pipeline/measured.json): raw audio measurements and transcripts.
- [`islab/pipeline/`](../../islab/pipeline/): the pipeline, grammar, LLM stub, tiers and real-audio adapters.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): scene by scene.
- Downloadable pack: `IS_B05_pack.zip` (release asset, same content as tag `b05`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
