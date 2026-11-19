# B04 · Sent is not closed

*Intelligent Spaces · Build Track · episode 4 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode five has a leak under the sink. A local rule closes the main water valve with no AI in the loop. We built that rule as a state machine on the hub, with a journal that survives a reboot, and compared it in the twin with two common designs: report "closed" when the command is sent, and report "closed" when the valve acknowledges. Then we broke the valve, the network, the hub and the internet, 500 seeded leaks per condition.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is. This is not a certified leak-protection system.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | "Closed" on send or on ack is a guess | Stuck or half-closed valve: 100% false "closed" messages from both baselines; 0% from the state machine | [`experiments/b04_valve`](../../experiments/b04_valve) |
| 2 | Even when the guess is right, it is early | Normal valve: every baseline "closed" was sent while water still flowed; confirmed close took p50 6.5 s, p95 8.4 s | [`results.json`](../../experiments/b04_valve/results.json) |
| 3 | A failed close gets plain words and a second alarm | Stuck or partial valve: people told to shut off by hand at 30.0 s in 100% of runs; valve alarm at the centre within 60 s in 100% | [`islab/safety/controller.py`](../../islab/safety/controller.py) |
| 4 | Lost commands need resends with the same id | 50% command loss: fire-and-forget left the valve open in 51.6% of runs and said it was closed; the state machine closed it in 100%, p95 16.5 s | [`experiments/b04_valve/notes.md`](../../experiments/b04_valve/notes.md) |
| 5 | The journal is what survives a reboot | Hub reboot mid-action: confirmed p50 34.1 s with the journal, 204.6 s without it | [`islab/safety/journal.py`](../../islab/safety/journal.py) |
| 6 | The valve works without the internet; the alarm does not | Internet down: confirmed p95 8.4 s; leak alarm reached the centre only when the internet returned, 270 s later; the person away was told 0% within 60 s | [`experiments/b04_valve`](../../experiments/b04_valve) |
| 7 | Listen to state, not only to events | Leak event lost: both baselines never closed the valve and told nobody; the state machine closed it in 100% of runs | [`results.json`](../../experiments/b04_valve/results.json) |
| 8 | Explanations come from the record | 64,768 generated statements, 100% matched a cited record; "closed" statements true against the twin: 100% state machine, 0% baselines | [`islab/safety/explain.py`](../../islab/safety/explain.py) |
| 9 | Our own bug: a rebooted alarm hub repeats itself | The B01 alarm hub sent a duplicate leak alarm after 81.8% of reboots; 9.2% with the controller's fix | [`experiments/b04_valve/notes.md`](../../experiments/b04_valve/notes.md) |

## Proving core: what reproduces this episode

```
git checkout b04
pip install -r requirements.txt
pytest -q tests/test_b04_safety.py           # the controller, journal, explanation checker, egress
python experiments/b04_valve/run.py         # 8 conditions x 3 designs x 500 leaks, about 1-3 minutes
```

## Documents in this episode

- [`docs/paper_b04.md`](../../docs/paper_b04.md): method, results, limits.
- [`experiments/b04_valve/notes.md`](../../experiments/b04_valve/notes.md): results table and what we fixed along the way.
- [`experiments/b04_valve/results.json`](../../experiments/b04_valve/results.json): every number in this episode.
- [`islab/safety/`](../../islab/safety/): the controller, journal, flow meter, notifier and explanation checker.
- [`script.txt`](script.txt): the narration, word for word.
- Downloadable pack: `IS_B04_pack.zip` (release asset, same content as tag `b04`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
