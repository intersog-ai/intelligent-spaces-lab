# b02_connect: the twin over a real local MQTT broker

**Run:** `python experiments/b02_connect/run.py` (8 trials per condition, about 3.2 minutes of wall
time on 2 cores, most of it waiting for keepalive timeouts). Writes `results.json` and
`report_samples.md` (the assistant's words from the first two trials of each condition).

## What is real and what is modelled

| Part | Real | Modelled |
|---|---|---|
| Broker | amqtt 0.12.2, MQTT 3.1.1, TCP on 127.0.0.1, in-process | - |
| Clients | paho-mqtt 2.2.0: one client per device (own Last Will), one hub client | - |
| QoS 0/1, retained, Last Will, persistent sessions, keepalive | protocol behaviour of the broker and client | - |
| Home Assistant discovery | retained `homeassistant/<component>/<object_id>/config` topics on the broker | field names from our knowledge of HA's MQTT integration; no Home Assistant was run |
| Devices | - | `islab.twin` classes, unchanged: ack first, state after a physical delay |
| Time | wall clock | home seconds = wall seconds x 10 (device delays, heartbeats, keepalive, timeouts) |
| Packet loss | - | i.i.d. drop at the adapter, both directions: the radio hop into the bridge |
| Matter | cluster and attribute ids from `chip.clusters.Objects` (package `home-assistant-chip-clusters` 2025.7.0, generated from the connectedhomeip SDK), checked at import | the device schema only: no controller, no commissioning, no fabric, no Thread or Wi-Fi radio |

Matter check: `python-matter-server` 8.1.2 needs `home-assistant-chip-core` (34 MB native wheel) and
real commissioned devices to talk to. We did not pursue it. We used only the pure-Python cluster
definitions from the same SDK to verify ids.

## Setup

Seven devices: two lights (on), two thermostats (21 °C), two shades (open), the kitchen window
(open in odd trials). The bedroom shade loses power 0-1 s before the routine: its MQTT client goes
silent and its TCP socket stays open, so only the broker's keepalive timer can notice. The hub sends
all six commands at once, resends the same `cmd_id` after 2 s without an ack (at most 3 times),
confirms only by a later state message (CONTRACT.md), and marks a device offline only on its Last
Will. Routine timeout 30 s. Keepalive 10 s. All times below are home seconds unless marked.

## Results (results.json)

**A. Transport on loopback (wall time, not scaled).** 300 messages each. QoS 0: p50 0.45 ms, p95
2.28 ms. QoS 1: p50 0.51 ms, p95 0.80 ms. All delivered.

**B. "We're leaving", 48 trials.**

| condition | all 5 live steps confirmed | time to confirm them, p50 / p95 | routine closed, p50 / p95 | acted, not confirmed | shade reported offline |
|---|---|---|---|---|---|
| QoS 0, 0% | 8/8 | 8.3 / 9.5 s | 15.0 / 15.0 s | 0 | 8/8 |
| QoS 0, 5% | 7/8 | 8.3 / 22.8 s | 15.0 / 28.1 s | 1 | 8/8 |
| QoS 0, 20% | 6/8 | 9.3 / 18.2 s | 15.0 / 30.0 s | 2 | 8/8 |
| QoS 1, 0% | 8/8 | 9.0 / 10.0 s | 15.0 / 15.0 s | 0 | 8/8 |
| QoS 1, 5% | 8/8 | 8.0 / 9.2 s | 15.0 / 15.0 s | 0 | 8/8 |
| QoS 1, 20% | 6/8 | 21.4 / 27.1 s | 23.4 / 30.0 s | 2 | 8/8 |

"Acted, not confirmed": the device really reached the target (we read the twin afterwards) but its
state message was lost, so the hub said "received, no reading that it finished" instead of "done".

**C. Offline detection of the dropped shade.**

| method | detection after the drop |
|---|---|
| Last Will, power loss (socket left open) | 15.0 s in 5/5 probes and in all 48 routine trials (p95 15.03 s) = 1.5 x keepalive |
| Last Will, process crash (socket closed) | 0.01 s, 5/5 |
| Heartbeat rule (2 missed 60 s heartbeats), computed from the last message received | p50 110 s |

With the heartbeat rule alone the routine would have hit its 30 s timeout and could only say "the
bedroom shade did not answer; I don't know its state".

**D. A 4.5 s network blip on the shade, command sent during the blip.**

| broker setting | QoS 0 delivered after reconnect | QoS 1 delivered after reconnect |
|---|---|---|
| amqtt, username required, persistent session | 0/3 | 3/3 |
| amqtt, anonymous allowed (the plugin's default), persistent session | 0/3 | **0/3** |

**E. Broker defect.** A client that subscribed only to `eprobe/own/cmd` received the retained
message `eprobe/private/door` that another client had subscribed to. In the routine trials each
home's device clients received 42-182 messages they never subscribed to (other devices' discovery
configs, states and availability).

**Hub restart.** A fresh hub that connects after the routine gets every retained state and
availability, including the shade's retained "offline", in 8-10 ms (p50, wall). At 20% loss with
QoS 0, 3 of 8 restarted hubs were missing one device's state: its boot report was lost, so the broker
had nothing retained for it until the next heartbeat.

## What it shows

1. **Locally, the protocol is not the slow part.** A message crosses the broker in about half a
   millisecond. The routine takes 8-10 s because a shade takes 5-10 s to move. Nothing in this path
   needs the internet.
2. **Last Will turns silence into a fact, but only as fast as the keepalive.** The broker declared the
   powerless shade offline at exactly 1.5 x keepalive, every time. The hub could close the routine at
   15 s and say "offline, may still be open" instead of "no answer". A device that crashes on a live
   OS is seen in 10 ms. With common real keepalives of 30-60 s, expect 45-90 s.
3. **QoS 1 does not fix a lossy radio.** Loss before the MQTT client is invisible to QoS. The
   differences between QoS 0 and QoS 1 rows in table B are trial noise (8 trials); by construction the
   same losses reach both. What recovered commands was the application: resend the same `cmd_id`, and
   confirm by state.
4. **QoS 1 does work where it is meant to: across a reconnect, with a persistent session.** And one
   broker setting switches it off silently: amqtt marks every session anonymous when anonymous access
   is allowed, and anonymous sessions get no offline queue. 3/3 became 0/3 with no error anywhere.
5. **A lost final state leaves an honest gap.** At 20% loss, 4 of 80 live steps really happened but
   could not be confirmed within 30 s. The report says so. Retained state does not help when the
   message never reached the broker.
6. **Retained messages give a restarted hub the whole house in about 9 ms**, including which device is
   offline, because the Last Will is retained.
7. **Check the broker, not only your code.** amqtt 0.12.2 (a) sends a new client retained messages that
   match other clients' subscriptions (broker.py, the loop over `self._subscriptions` after
   `handler.start()`), and (b) crashed client sessions with "dictionary changed size during iteration"
   when seven clients connected at once. We connect one client at a time and drop any message on a
   topic the device did not subscribe to. For production use a broker with ACLs (for example
   Mosquitto) and per-device credentials, and re-test (a).

## Bugs found in our own code while building this

- The hub crashed its MQTT thread on an empty retained payload (how MQTT deletes a retained message,
  and how Home Assistant removes an entity). Fixed: empty payloads clear state; bad JSON is ignored.
- Dotted twin ids (`shade.living`) are not valid Home Assistant object ids. Fixed: `<home>_shade_living`.

## Facts from our own knowledge (no web access while building; to check in the facts ledger)

- MQTT 3.1.1: the server disconnects a client after 1.5 x keepalive without a packet and then
  publishes its Will; a zero-length retained message deletes the retained message. (Both matched the
  broker's measured behaviour.)
- Home Assistant MQTT discovery: topic `homeassistant/<component>/[<node_id>/]<object_id>/config`;
  fields used: `name`, `unique_id`, `availability_topic`, `payload_available`, `payload_not_available`,
  `qos`, `device` (`identifiers`, `name`, `manufacturer`, `model`), `origin`; light (default schema):
  `command_topic`, `state_topic`, `state_value_template`, `payload_on`, `payload_off`,
  `brightness_command_topic`, `brightness_state_topic`, `brightness_value_template`,
  `brightness_scale`; cover: `device_class`, `command_topic`, `payload_open`, `payload_close`,
  `payload_stop`, `state_topic`, `value_template`, `state_open|closed|opening|closing`,
  `position_topic`, `position_template`, `set_position_topic`, `position_open`, `position_closed`;
  climate: `modes`, `mode_command_topic`, `mode_state_topic`, `mode_state_template`,
  `temperature_command_topic`, `temperature_state_topic`, `temperature_state_template`,
  `current_temperature_topic`, `current_temperature_template`, `min_temp`, `max_temp`, `temp_step`,
  `temperature_unit`, `precision`; binary_sensor: `device_class`, `state_topic`, `value_template`,
  `payload_on`, `payload_off`. The templates were rendered with Jinja2 in the tests; HA itself was not run.
- Matter: device types 0x0101 Dimmable Light, 0x0301 Thermostat, 0x0202 Window Covering, 0x0015
  Contact Sensor; WindowCovering lift percent is closed-ness (0 = open, 10000 = closed), the opposite
  of the twin's and Home Assistant's percent open; Contact Sensor `StateValue` true = closed;
  thermostat temperatures in 0.01 °C; LevelControl 0-254. Cluster and attribute ids and the
  SystemMode enum were verified against the SDK package; the rest was not.

## Limitations

Loopback TCP on one machine: no radio, no Wi-Fi roaming, no real bridge. TCP does not lose messages
on a live connection, so loss was emulated at the adapter. Time compression x10 multiplies transport
and Python scheduling overhead by 10 in the home-second figures (part A gives the unscaled cost).
Keepalive 10 s is shorter than most real devices use. Heartbeat detection is computed, not waited
for. 8 trials per condition: p95 values are indicative. i.i.d. loss, while real radios lose packets in
bursts. Matter is a data-model mapping only. Home Assistant discovery was not validated against a
running Home Assistant.
