# Connecting a home without a vendor cloud: the twin over real local MQTT

Intelligent Spaces Lab, Build Track B02 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced on one computer, with a real MQTT broker and clients but simulated devices, emulated packet loss and no radios, by AI agents under human direction. It is not a product and not safety advice. Use it at your own risk and for your own benefit.

## 1. Question

Season 1 E03: one phrase, "we're leaving", runs an away routine across lights, climate and shades and checks a window. One shade does not respond, and the home says so. B01 tested this on an in-process bus. This study asks: when the same twin talks over a real local protocol, with no vendor cloud in the path, what changes? How fast is a routine, how fast is a dead device noticed, and what do MQTT's QoS levels, retained messages and Last Will actually buy?

## 2. Design under test

- **Broker** (`islab/connect/broker.py`): amqtt 0.12.2, a pure-Python MQTT 3.1.1 broker, started in-process on 127.0.0.1. Mosquitto is used if amqtt is missing.
- **Adapter** (`islab/connect/adapter.py`): the unchanged twin devices from B01, each bridged to its own paho-mqtt 2.2.0 client, so each device has its own Last Will. State is retained. Commands arrive as contract JSON or as Home Assistant style payloads. Packet loss is emulated at the adapter, on the leg between a device and its MQTT client, as a radio hop into a bridge would lose it.
- **Hub** (`islab/connect/hub.py`): sends commands, resends the same command id after 2 s without an ack (at most 3 times), confirms only by a later state message, marks a device offline only on its Last Will, and writes the plain-words report.
- **Home Assistant discovery** (`islab/connect/ha_discovery.py`): retained `homeassistant/<component>/<object_id>/config` payloads for light, cover, climate and binary_sensor.
- **Matter view** (`islab/connect/matter_model.py`): each state message carries its Matter attributes (OnOff, LevelControl, Thermostat, WindowCovering, BooleanState). Cluster and attribute ids are checked against `chip.clusters.Objects` from `home-assistant-chip-clusters` 2025.7.0, generated from the connectedhomeip SDK.

## 3. Method

1. Seven devices: two lights, two thermostats, two shades, a window contact. The bedroom shade loses power 0-1 s before the routine: its client goes silent and its TCP socket stays open, as with a power cut.
2. The twin runs on the wall clock compressed ten times: one wall second is ten home seconds for device movement, heartbeats, keepalive (10 s) and timeouts (30 s).
3. Conditions: QoS 0 and 1, adapter loss 0, 5 and 20%, 8 seeded trials each, 48 in total.
4. Side measurements: transport latency on loopback; Last Will after power loss and after a process crash; a command sent during a 4.5 s network blip, with a persistent session, against two broker settings; whether a client receives retained messages it did not subscribe to; what a restarted hub learns from retained messages.
5. Everything is in `experiments/b02_connect/run.py`; `results.json` holds every trial.

## 4. Results

### 4.1 The routine (`experiments/b02_connect`)

| condition | all 5 live steps confirmed | time to confirm them, p50 / p95 | routine closed, p50 / p95 | happened, not confirmed |
|---|---|---|---|---|
| QoS 0, no loss | 8/8 | 8.3 / 9.5 s | 15.0 / 15.0 s | 0 |
| QoS 0, 5% | 7/8 | 8.3 / 22.8 s | 15.0 / 28.1 s | 1 |
| QoS 0, 20% | 6/8 | 9.3 / 18.2 s | 15.0 / 30.0 s | 2 |
| QoS 1, no loss | 8/8 | 9.0 / 10.0 s | 15.0 / 15.0 s | 0 |
| QoS 1, 5% | 8/8 | 8.0 / 9.2 s | 15.0 / 15.0 s | 0 |
| QoS 1, 20% | 6/8 | 21.4 / 27.1 s | 23.4 / 30.0 s | 2 |

In 48 of 48 trials the report named the bedroom shade as offline and possibly still open. A typical report: "Leaving mode is mostly set: 5 of 6 steps confirmed in 15 seconds. 1 did not. Confirmed by the devices: Kitchen light off; Living room light off; Kitchen thermostat to 16 °C; Study thermostat to 16 °C; Living room shade closed. Bedroom shade did not respond: it went offline and could not act, so it may still be open. Check it when you can. The kitchen window is open (reading 24 seconds old). I can't close it; someone needs to."

### 4.2 Transport and offline detection

| Measure | Result |
|---|---|
| Loopback, publish to receive, wall time | QoS 0 p50 0.45 ms, p95 2.28 ms; QoS 1 p50 0.51 ms, p95 0.80 ms |
| Last Will after power loss (socket open) | 15.0 s, every probe and every trial: 1.5 x the 10 s keepalive |
| Last Will after a process crash (socket closed) | 0.01 s |
| Heartbeat rule, 2 missed 60 s heartbeats (computed) | about 110 s |

### 4.3 QoS, sessions and retained messages

| Measure | Result |
|---|---|
| Command during a network blip, persistent session, username required | QoS 0 delivered 0/3, QoS 1 3/3 |
| Same, amqtt with anonymous access allowed | QoS 0 0/3, QoS 1 0/3 |
| Restarted hub: all states and availability from retained messages | p50 8-10 ms (wall); the shade's retained "offline" seen in every trial |
| Restarted hub missing a device's state | 3 of 8 trials at QoS 0, 20% loss: the device's only report was lost before the broker |
| Client receives retained messages it did not subscribe to | yes: 1 of 1 probe; 42-182 per home in the routine trials |

## 5. Findings in our own code and our stack

1. **The hub's MQTT thread died on an empty message.** An empty retained payload is how MQTT deletes a retained message and how Home Assistant removes an entity. Our hub parsed it as JSON and its network thread stopped. Fixed: empty payloads clear state; malformed JSON is ignored.
2. **Twin ids are not valid Home Assistant object ids.** `shade.living` became `<home>_shade_living`.
3. **amqtt 0.12.2 delivers retained messages to clients that did not subscribe to them**, by looping over every client's subscriptions when a new client connects. A device client received other devices' states and configurations. We drop any message on a topic the device did not subscribe to. The same loop crashed sessions when seven clients connected at once; we connect one at a time.
4. **One broker setting silently disables QoS 1 for offline devices.** With anonymous access allowed, amqtt marks every session anonymous and queues nothing for it. We require a username.
5. **Matter counts shades the other way round.** Window Covering lift percent is closed-ness (0 = fully open); the twin and Home Assistant count percent open. A Matter contact sensor reports true when closed. The adapter inverts both; tests check it.

## 6. Limits

One machine, TCP on loopback: no Wi-Fi, Thread or Zigbee radio, no real bridge. TCP does not lose messages on a live connection, so loss was emulated at the adapter, i.i.d.; real radios lose packets in bursts. Time is compressed ten times, so transport and scheduling overhead count ten times in home-second figures; section 4.2 gives the unscaled cost. Keepalive was 10 s; many devices use 30-60 s, and Last Will detection scales with it. Heartbeat detection was computed, not waited for. Eight trials per condition: p95 values are indicative and QoS 0 versus QoS 1 differences in 4.1 are within trial noise. Matter is a data-model mapping: no controller, no commissioning, no fabric. Home Assistant discovery fields come from our own knowledge and were rendered with Jinja2, not loaded by a running Home Assistant. Facts taken from our own knowledge are listed in `experiments/b02_connect/notes.md` for the facts ledger.

## 7. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b02_connect.py
python experiments/b02_connect/run.py       # about 3 minutes on 2 cores
```

## 8. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
