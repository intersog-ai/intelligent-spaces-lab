# B10 · Fleet, cloud and the full test

*Intelligent Spaces · Build Track · episode 10 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode ten, the season finale, makes the home one of many. Here we switch on every layer from B01 to B09 in one home on one day, run the five principle suites over it under no, light and heavy faults, and fix what breaks between the layers. Then we put the home in a fleet of 1,000 simulated homes. We ship a bad update through canary and rings with an automatic halt, and check what a cloud management plane is allowed to see. The complete research, B01 to B10, is one file: [`docs/intelligent-spaces-research.md`](../../docs/intelligent-spaces-research.md).

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | Every layer on, the safety and trust suites hold | P1-P4, ALARM, BUS passed in 18 of 18 days (6 per mix); 0 unapproved level-3, 0 false "done", 0 silent failures; 100% of alarms within 60 s | [`experiments/b10_fleet`](../../experiments/b10_fleet) |
| 2 | Layers that pass alone can break each other | 10 interaction bugs, one fix each; 3 changed what the home did (double actions, a weaker teen rule, a silent phone-home) | [`notes.md`](../../experiments/b10_fleet/notes.md) |
| 3 | A green test from episode one fails the newer design | B01's P5, unchanged, failed 18 of 18 days (cellular alarm path, a consented clip, the push purpose); the route-aware P5r passed 18 of 18 | [`islab/fleet/checks.py`](../../islab/fleet/checks.py) |
| 4 | The core stays at home | home control in the 2 h outage: 100% / 100% / 58.3% (full) vs 100% / 95.8% / 45.8% (B01 only); 41-42 cellular alarm-line messages through the outage | [`results.json`](../../experiments/b10_fleet/results.json) |
| 5 | A bad update in one home | a manifest typo blocked the alarm path 2.87-3.23 h after midnight in 9 of 9 days; ALARM failed in all 9; every health report flagged it | [`results.json`](../../experiments/b10_fleet/results.json) |
| 6 | Rings and an automatic halt | 1,000 homes: all at once 979.0 homes got the bad update; rings with operator halt 43.8; rings with automatic halt 7.5 (at most 10), halted 3.8 h in | [`islab/fleet/rollout.py`](../../islab/fleet/rollout.py) |
| 7 | A quiet bad update gets past the canary | 5% of homes affected (assumed): passed the canary in 20 of 20 rollouts; 142.1 installs before the halt at 46.9 h | [`results.json`](../../experiments/b10_fleet/results.json) |
| 8 | A 03:00 install window ignores the halt | 48.0 homes installed the quiet bad update after the halt | [`results.json`](../../experiments/b10_fleet/results.json) |
| 9 | The cloud gets counts, not the home | health report 265 bytes, sent; raw bundle 4,094,716 bytes with 14 requests in people's own words, refused by the B09 manifest | [`islab/fleet/cloud.py`](../../islab/fleet/cloud.py) |
| 10 | Still open | an unconfirmed courier unlock that really happened was not relocked: door unlocked at 19:05 in 2 of 6 heavy days | [`docs/intelligent-spaces-research.md`](../../docs/intelligent-spaces-research.md) |

## Proving core: what reproduces this episode

```
git checkout v1.0
pip install -r requirements.txt
pytest -q tests/test_b10_fleet.py            # full day, each fix, fast bus, gate order, plane rules, rollout
python experiments/b10_fleet/run.py         # full test, fleet, cloud; about 2-3 minutes on 2 cores
```

## Documents in this episode

- [`docs/intelligent-spaces-research.md`](../../docs/intelligent-spaces-research.md): **the complete research, B01-B10, in one file.** Start here.
- [`docs/paper_b10.md`](../../docs/paper_b10.md): method, results, limits for this episode.
- [`experiments/b10_fleet/notes.md`](../../experiments/b10_fleet/notes.md): what is real and what is modelled, every interaction bug.
- [`experiments/b10_fleet/results.json`](../../experiments/b10_fleet/results.json): every number in this episode.
- [`islab/fleet/`](../../islab/fleet/): the full house, the checks, the rollout model, the cloud plane rules and mapping.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): one moving visual per paragraph.
- Downloadable: the complete research file and pack, release [`v1.0`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/v1.0) (`intelligent-spaces-research.pdf`, `IS_series_pack.zip`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
