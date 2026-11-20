# B08 · Cameras and the alarm path

*Intelligent Spaces · Build Track · episode 8 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode eight, "Safe at home", keeps what the home observed apart from what it might mean. The home reports; a person or the monitoring centre decides; the centre, not the home, calls 911. We built that in the twin. Cameras state an observation with its confidence and ask; no footage leaves except a clip a person chooses to share. And we closed the gap B01 and B04 left open: with the internet down, alarms now have a second, modelled cellular path to the centre. 3,720 seeded alarms, 40 simulated days, 208 simulated camera weeks.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is. This is not a certified alarm system. The cellular link, the camera detector and the people are models with labelled assumptions.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | A second path closes the outage gap | Internet down 10 min: alarms at the centre within 60 s 13.0% with one path, 100% with two (p95 5.79 s). Down all day: 0% vs 100% | [`experiments/b08_security`](../../experiments/b08_security) |
| 2 | It cannot help when both paths are down | Internet and cell down 10 min: 13.0% either way; the household is told to call 911 themselves if there is a fire | [`results.json`](../../experiments/b08_security/results.json) |
| 3 | A degraded cell still mostly works | 99.3% within 60 s, p95 23.9 s, but 158 duplicate copies, all suppressed at the receiver | [`islab/security/dualpath.py`](../../islab/security/dualpath.py) |
| 4 | Dedupe by incident fixes the B04 duplicate | Hub reboot: message-level dedupe opened a second incident in 200 of 200 smoke and leak runs; incident dedupe in 0 | [`experiments/b08_security/notes.md`](../../experiments/b08_security/notes.md) |
| 5 | Supervision is the cellular cost | Normal day 10,587 bytes (assumed USD 0.01); internet down all day 213,481 bytes (USD 0.21) | [`results.json`](../../experiments/b08_security/results.json) |
| 6 | A threshold trades false alerts for missed people | Threshold 0.5: 8.42 false "person" alerts a week; 0.9: 0.08, but 56.19 real persons missed | [`islab/security/camera.py`](../../islab/security/camera.py) |
| 7 | A verdict floods the centre | Automatic "intruder" while armed: 34.81 signals a week to the centre, 33.92 false | [`results.json`](../../experiments/b08_security/results.json) |
| 8 | Asking keeps the decision with people | 39.62 asks a week; 1.46 reached the centre, each with a person's yes and that one clip; 0 clips without consent | [`islab/security/camera.py`](../../islab/security/camera.py) |
| 9 | Asking costs time | Unexpected person reached the centre in 69.2% of tests, p50 40.7 s; in 5 of 26 nobody answered | [`experiments/b08_security/notes.md`](../../experiments/b08_security/notes.md) |
| 10 | Open: a flapping backup is noisy | Internet down all day with a degraded cell: 87.6 path messages a day to the household | [`experiments/b08_security/notes.md`](../../experiments/b08_security/notes.md) |

## Proving core: what reproduces this episode

```
git checkout v1.0
pip install -r requirements.txt
pytest -q tests/test_b08_security.py         # cellular model, dual path, dedupe, consent, asks
python experiments/b08_security/run.py      # alarms, cellular days, camera weeks, about 2-4 minutes
```

## Documents in this episode

- [`docs/paper_b08.md`](../../docs/paper_b08.md): method, results, limits.
- [`experiments/b08_security/notes.md`](../../experiments/b08_security/notes.md): results tables and what we fixed along the way.
- [`experiments/b08_security/results.json`](../../experiments/b08_security/results.json): every number in this episode, with every assumption.
- [`islab/security/`](../../islab/security/): the cellular model, dual-path hub and receiver, camera observer.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): scene by scene visuals for the video.
- Downloadable: the complete research file and pack, release [`v1.0`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/v1.0) (`intelligent-spaces-research.pdf`, `IS_series_pack.zip`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
