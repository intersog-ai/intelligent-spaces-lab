# B07 · The agent and the approval gate

*Intelligent Spaces · Build Track · episode 7 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode seven: "It acts. You stay in charge." An AI agent proposes; code decides; a door, the water or the garage needs a yes on a phone. B01 showed the gate blocks 1,000 adversarial proposals. Here the approval itself is the subject: can it be replayed, forged, sent to the wrong person, stretched past its window or applied to a different action? We also ask what happens when the person is away, and how many prompts a household can live with. We simulated a month of household requests under five designs, and fed injected instructions to the agent through device names, calendar entries and delivery notes. No language model was run: the agents are scripted and randomized doubles.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | An approval is for one action, once | 10 attack types x 50 trials: B01-style engine let 6 types through (50/50 each); GatedEngine 0 | [`experiments/b07_approval`](../../experiments/b07_approval) |
| 2 | "Until 18:55" cannot open the garage at 23:00 | Old reply replayed: refused as replayed; pasted onto a new request: refused, bad signature (50/50 each); garage closed itself at the window end in 150/150 | [`islab/approval/ledger.py`](../../islab/approval/ledger.py) |
| 3 | Check the action again right before it runs | Arguments changed after the prompt: B01-style unlocked the other door 50/50; GatedEngine refused 50/50 | [`islab/approval/engine.py`](../../islab/approval/engine.py) |
| 4 | While waiting, the home does nothing | Garage moved 0 times while waiting, in 20 configurations of 100 requests | [`results.json`](../../experiments/b07_approval/results.json) |
| 5 | Away and offline cost time, not safety | Both free: p50 22.2 s, p95 34.1 s; Anna offline and Mark busy: 85% within 120 s (fan-out), 100% with a 300 s time-to-live | [`experiments/b07_approval/notes.md`](../../experiments/b07_approval/notes.md) |
| 6 | Asking for everything drowns people | 20.25 prompts a day, 260 hurried answers, 16.4% of legitimate requests blocked; by level: 3.75 a day, 3.9% blocked | [`results.json`](../../experiments/b07_approval/results.json) |
| 7 | Standing rules need limits | With limits: 1.02 prompts a day, 1 wrong garage opening in 60 household-days; without limits: 0.67 a day, 6 wrong openings | [`islab/approval/standing.py`](../../islab/approval/standing.py) |
| 8 | Injection does not reach the phone | Injected level-3 calls reaching a prompt: B01-style 11-17 per 300 scenarios, GatedEngine 0; unapproved level-3 executed: 0 in all 24 rows | [`islab/approval/agent.py`](../../islab/approval/agent.py) |
| 9 | Our own finds in B01 code | Replies trusted by topic; arguments not re-checked at execution; tool-level traceability only; untraceable level-2 questions sent to phones | [`docs/paper_b07.md`](../../docs/paper_b07.md) |

## Proving core: what reproduces this episode

```
git checkout b07
pip install -r requirements.txt
pytest -q tests/test_b07_approval.py         # tickets, gate, rules, injection double, checker
python experiments/b07_approval/run.py      # attacks, away, a month x 5 designs, injection; about 2-3 minutes
```

## Documents in this episode

- [`docs/paper_b07.md`](../../docs/paper_b07.md): method, results, limits.
- [`experiments/b07_approval/notes.md`](../../experiments/b07_approval/notes.md): results tables and the B01 gaps we found.
- [`experiments/b07_approval/results.json`](../../experiments/b07_approval/results.json): every number in this episode.
- [`islab/approval/`](../../islab/approval/): the gated engine, approval tickets, standing rules, phone and person doubles, agent doubles, independent checker.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): scene plan.
- Downloadable pack: [`IS_B07_pack.zip`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/b07) (release asset, same content as tag `b07`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
