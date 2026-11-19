# B01 · Build the twin, then break it

*Intelligent Spaces · Build Track · episode 1 of 10 · by [Intersog](https://www.intersog.com)*

We took the home from *Intelligent Spaces* Season 1 and built it as a digital twin: 15 simulated devices, a household of three, and a fault injector. The design under test: the language model proposes, code decides, devices confirm. Then we broke it on purpose and published what failed.

Video: [CHECK: YouTube URL] · 3:44 · synthetic narration · research in simulation, no guarantee.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | An acknowledgement is not a result | Stuck, half-closed, stale and clock-skewed devices: 200 of 200 commands acknowledged, 0 confirmed | [`experiments/twin_faults`](../../experiments/twin_faults) |
| 2 | Under message loss, work gets done without a reply | 30% loss: 15% of commands carried out with no acknowledgement | [`experiments/twin_faults`](../../experiments/twin_faults) |
| 3 | The gate holds only if it is code | 1,000 adversarial scenarios: 0 unapproved level-3 actions; a weakened policy was caught 141 times | [`experiments/agent_gate`](../../experiments/agent_gate) |
| 4 | A voice is not a key | Speaker ID EER 1.2% clean, 11.9% through a room; a replayed recording accepted in up to 81% of trials | [`experiments/speaker_id`](../../experiments/speaker_id) |
| 5 | A leak can be accepted and lost | `pysiaalarm` 3.2.2 acknowledged a Contact ID 154 leak and decoded it as invalid data; 19 of 21 interoperability cases pass | [`experiments/alarm_path`](../../experiments/alarm_path) |
| 6 | A slow device clock blocked every confirmation | Fixed by ordering on device sequence numbers | [`islab/policy/actions.py`](../../islab/policy/actions.py) |
| 7 | Green tests missed the failure that mattered | Heavy faults: alarms within 60 s 81.9% → 98.0%, smoke siren within 1 s 76% → 100% after the fixes | [`experiments/scorecard/before_after.md`](../../experiments/scorecard/before_after.md) |
| 8 | Still open | With the internet down, an alarm has no second path; a cellular route is needed | [`docs/paper.md`](../../docs/paper.md) §6 |

## Proving core: what reproduces this episode

```
git checkout b01
pip install -r requirements.txt
pytest -q                                   # 117 pass, 2 voice tests skip without audio
python experiments/scorecard/run.py         # the Season 1 day, 50 seeds x 3 fault mixes, about 1 minute
```

## Documents in this episode

- [`docs/paper.md`](../../docs/paper.md): method, results, limits.
- [`docs/prompts.md`](../../docs/prompts.md): the prompts given to the six AI agents that built and checked the lab.
- [`docs/facts-ledger.md`](../../docs/facts-ledger.md): 33 facts checked against primary sources, Nov 19, 2026.
- [`docs/architecture.svg`](../../docs/architecture.svg): reference architecture.
- [`script.txt`](script.txt): the narration, word for word.
- Downloadable pack: `IS_B01_pack.zip` (release asset, same content as tag `b01`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
