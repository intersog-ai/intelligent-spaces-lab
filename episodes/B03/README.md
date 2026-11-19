# B03 · Readings you can trust

*Intelligent Spaces · Build Track · episode 3 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode 4 gave the home its senses and one rule: fresh, sourced readings. Every reading carries a device ID, a timestamp and an event ID, and stale or duplicate data must not drive a decision. We built that rule as a reading-quality layer, ran the twin for a simulated week with faulty sensors, and compared it with two simpler hubs: one that trusts the latest message, and one that trusts device timestamps.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | A stuck sensor looks perfect and drives the most bad decisions | About 603 heating decisions per week on a stuck thermometer for both simpler hubs; the layer caught it in 19 minutes and switched to the second thermometer | [`experiments/b03_readings`](../../experiments/b03_readings) |
| 2 | Timestamps fix the transport, not the sensor | Timestamp hub: 0 decisions on duplicate or reordered data (naive: 30 per week), but the same 603 stuck-sensor decisions | [`experiments/b03_readings/notes.md`](../../experiments/b03_readings/notes.md) |
| 3 | A leak alarm must not be cleared by a heartbeat that never said wet | One-shot leak: both simpler hubs reopened the valve and water ran for 112 minutes; the layer kept it closed and disputed the reading within 143 s on average | [`experiments/b03_readings`](../../experiments/b03_readings) |
| 4 | The last gasp is the earliest signal of a dead sensor | Dead battery noticed at 0 s (battery 0 report) versus 782 s by missed heartbeats; the naive hub never noticed and said "all clear" twice | [`experiments/b03_readings/results.json`](../../experiments/b03_readings/results.json) |
| 5 | Device timestamps punish a drifting clock | 2 s per hour of drift: the timestamp hub declared a good sensor stale after 45 to 67 hours and discarded about 110 hours of good readings per week; the layer estimated the offset at 335.7 s against a true 336 s | [`islab/readings/quality.py`](../../islab/readings/quality.py) |
| 6 | Trust has a cost, and the person is told | Valve closed 134 minutes after the floor was dry, waiting for a person; a real 5 degree change held one report, 2 decisions a minute late; 30 of 30 refusals told | [`experiments/b03_readings/notes.md`](../../experiments/b03_readings/notes.md) |
| 7 | False alarms | Per week: naive 4.7, timestamp 2, quality layer 0 | [`experiments/b03_readings`](../../experiments/b03_readings) |
| 8 | Still open | Stuck detection relies on fine sensor resolution; the rate limit would delay a real fire's temperature rise by one report, so fire detection stays with smoke alarms | [`docs/paper_b03.md`](../../docs/paper_b03.md) §6 |

## Proving core: what reproduces this episode

```
git checkout b03
pip install -r requirements.txt
pytest -q tests/test_b03_readings.py        # 19 tests for the layer and the experiment
python experiments/b03_readings/run.py      # 10 seeds x 7 simulated days, about 2 minutes on 2 cores
```

## Documents in this episode

- [`docs/paper_b03.md`](../../docs/paper_b03.md): question, method, results, limits.
- [`islab/readings/`](../../islab/readings/): the reading-quality layer and the three hub views.
- [`experiments/b03_readings/`](../../experiments/b03_readings/): `run.py`, `results.json` (every seed), `notes.md`.
- [`script.txt`](script.txt): the narration, word for word.
- Downloadable pack: [`IS_B03_pack.zip`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/b03) (release asset, same content as tag `b03`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
