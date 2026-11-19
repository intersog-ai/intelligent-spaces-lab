# B02 · Connect without a vendor cloud

*Intelligent Spaces · Build Track · episode 2 of 10 · by [Intersog](https://www.intersog.com)*

We moved the twin from its in-process bus onto a real MQTT broker running on the same machine: one client per device, Last Will for offline detection, retained state, QoS 0 and 1, and Home Assistant discovery topics. Then we ran the Season 1 E03 routine, "we're leaving": lights off, heating to the away setpoint, shades closed, a window check, with one shade losing power just before. We measured what the protocol changes and what it does not. Matter and Thread were not run on radios: the device schema is mapped to Matter clusters, with ids verified against the SDK's generated definitions.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | Locally, the protocol is not the slow part | A message crossed the broker in 0.5 ms (p50); the routine's live steps confirmed in 8-9 s (p50), the time a shade takes to move | [`experiments/b02_connect`](../../experiments/b02_connect) |
| 2 | Last Will turns silence into a fact, at the speed of the keepalive | Powerless shade declared offline at 15.0 s = 1.5 x keepalive, 48 of 48 trials; crashed process 0.01 s; heartbeat rule about 110 s | [`experiments/b02_connect/notes.md`](../../experiments/b02_connect/notes.md) |
| 3 | The home said which shade failed and why | "Bedroom shade did not respond: it went offline and could not act, so it may still be open", 48 of 48 trials | [`report_samples.md`](../../experiments/b02_connect/report_samples.md) |
| 4 | QoS 1 does not fix a lossy radio | Loss before the MQTT client is invisible to QoS; resend of the same command id plus confirmation by state did the recovery | [`experiments/b02_connect/results.json`](../../experiments/b02_connect/results.json) |
| 5 | QoS 1 works across a reconnect, until one setting turns it off | Command sent during a network blip: QoS 1 delivered 3/3, QoS 0 0/3; with anonymous access allowed, QoS 1 0/3, no error | [`experiments/b02_connect/notes.md`](../../experiments/b02_connect/notes.md) |
| 6 | A lost final report leaves an honest gap | 20% loss: 4 of 80 steps happened but could not be confirmed in 30 s; the assistant said so instead of "done" | [`experiments/b02_connect/results.json`](../../experiments/b02_connect/results.json) |
| 7 | Retained state gives a restarted hub the whole house | All states and the shade's "offline" in about 9 ms | [`experiments/b02_connect/results.json`](../../experiments/b02_connect/results.json) |
| 8 | Test the broker too | amqtt 0.12.2 sent clients retained messages they never subscribed to (42-182 per home) and crashed sessions on simultaneous connects | [`islab/connect/adapter.py`](../../islab/connect/adapter.py) |
| 9 | Matter counts a shade the other way round | Window Covering lift percent is closed-ness (0 = open); the twin and Home Assistant use percent open | [`islab/connect/matter_model.py`](../../islab/connect/matter_model.py) |

## Proving core: what reproduces this episode

```
git checkout b02
pip install -r requirements.txt
pytest -q tests/test_b02_connect.py         # 8 tests; broker tests skip without amqtt and paho-mqtt
python experiments/b02_connect/run.py       # 48 routine trials over a real local broker, about 3 minutes
```

## Documents in this episode

- [`docs/paper_b02.md`](../../docs/paper_b02.md): question, method, results, limits.
- [`experiments/b02_connect/notes.md`](../../experiments/b02_connect/notes.md): what is real and what is modelled; facts taken from our own knowledge, to be checked.
- [`islab/connect/`](../../islab/connect/): broker, adapter, hub, Home Assistant discovery, Matter mapping.
- [`script.txt`](script.txt): the narration, word for word.
- Downloadable pack: [`IS_B02_pack.zip`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/b02) (release asset, same content as tag `b02`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
