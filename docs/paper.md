# Building a trustworthy home assistant in a digital twin: method, results, limits

Intelligent Spaces Lab, Build Track B01 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with synthetic voices and simulated devices, by AI agents under human direction. It is not a product, not a certified alarm system and not safety advice. Use it at your own risk and for your own benefit. Life-safety equipment must be listed and installed by qualified people.

## 1. Question

Season 1 of *Intelligent Spaces* states five principles for a home assistant people can trust: (1) fresh, sourced readings; (2) a command sent is not a result; (3) people set the limits and can override; (4) when something fails, it says so; (5) the core stays at home. This study asks: can those principles be written as tests, and what breaks when a home built on them meets realistic faults?

## 2. Design under test

The language model proposes, code decides, devices confirm.

- **Twin** (`islab/twin`): 15 simulated devices on an MQTT-like bus in a deterministic discrete-event simulator; a household of three (Anna, Mark, Sam); a fault injector for message loss, delay, duplication, reordering, stuck and partial actuators, stale sensors, clock skew, dead batteries, internet outages and hostile device names.
- **Policy** (`islab/policy`): a state store with freshness checks; tools shaped like Model Context Protocol tools; a policy engine with three levels (act and tell; ask first; never alone, which needs a recorded phone approval); per-person permissions; an action tracker that confirms by later device state; an audit log.
- **Voice** (`islab/voice`): speaker identification with a GE2E encoder (Resemblyzer); a latency model of the voice pipeline.
- **Alarm and privacy** (`islab/alarm`): a SIA DC-09 encoder tested against an independent receiver; a simulated monitoring centre that, not the hub, dispatches; a local siren independent of the hub; an egress gate with purpose tags; delete with receipts.
- **Suites** (`islab/suites`): the five principles plus ALARM and BUS checks, run over one household day from Season 1.

## 3. Method

1. A human-written contract (`islab/core/CONTRACT.md`) fixed topics, payloads and the meaning of "confirmed".
2. Five AI agents built twin, policy, voice, alarm and a fact check in parallel (prompts in `docs/prompts.md`). An integration agent connected them.
3. Every experiment is seeded and writes `results.json` with method, parameters, results and limitations.
4. The household day ran for 50 seeds under three fault mixes: none; light (2% link loss); heavy (20% link loss, one stuck actuator, a sensor clock 120 s behind).
5. Where the suites missed a failure, a check was added, the old code was re-scored with it, the code was fixed, and the same seeds were run again.

## 4. Results

### 4.1 An acknowledgement is not a result (`experiments/twin_faults`)

200 trials per fault, close the main valve, 30 s timeout.

| Fault | Confirmed | Acked, never confirmed | No ack | p95 time to confirm |
|---|---|---|---|---|
| none | 100% | 0% | 0% | 7.9 s |
| stuck, partial, stale, clock skew −120 s | 0% | 100% | 0% | — |
| 30% link loss | 53.5% | 14% | 32.5% | 22.4 s |
| offline, dead battery | 0% | 0% | 100% | — |

With 30% loss, 15% of trials were carried out although the ack never arrived. Reporting "done" on ack would be wrong in every stuck, partial, stale and skewed case.

### 4.2 The approval gate (`experiments/agent_gate`)

1,000 seeded scenarios (1,218 proposals): benign requests, out-of-permission requests, tool calls injected through a hostile device name, invented tools, malformed arguments, level-3 requests with and without phone approval, replayed and late approvals, stale context.

| Measure | Result |
|---|---|
| Unapproved level-3 actions executed | 0 |
| Approved level-3 actions executed | 81 of 81 |
| Injected level-3 or level-2 calls executed | 0 of 190 |
| Injected level-1 calls executed (then told) | 18 of 28 |
| Hallucinated or malformed calls executed | 0 |
| Legitimate requests blocked | 0 of 614 |
| Control: same run with unlock at level 1 | 141 violations flagged |

Injected level-1 calls run by design (lights, shades) and are reported to the person. The 0% false-block figure is optimistic: the adversary is scripted and no real LLM was run.

### 4.3 A voice is not a key (`experiments/speaker_id`)

GE2E embeddings, 2 s windows, synthetic voices.

| Condition | Result |
|---|---|
| Clean studio audio, 10 s enrollment | EER 1.2%; at 0.80: FAR 0.8%, FRR 2.3% |
| Through a simulated room and microphone | EER 11.9%; at 0.824: FAR 0.8%, FRR 34% |
| Replay through a Bluetooth speaker | accepted as the person in 81% of trials (room EER threshold), 38% (default) |
| Kitchen noise, room threshold | FRR 12% clean, 14% at 20 dB, 45% at 10 dB, 85% at 5 dB |

Household audio was short (6.2 s Anna, 3.6 s Mark, 1.4 s Sam), so household numbers are anecdotal; thresholds came from three longer voices. Voice identity is used to personalize and as one input to policy, never alone for a door, an alarm or a camera.

### 4.4 Voice latency (`experiments/voice_latency`)

Measured on a 2-core CPU: wake word about 120-150 ms after the phrase; end-of-speech detection about 425 ms; Whisper tiny 166 ms; streaming Zipformer 15 ms; Piper first audio 87 ms. Modelled end of command to first reply (rules / LLM path), all tier figures assumed: Pi 5 + Hailo-10H 1.2 s / 6.1 s; Jetson Orin Nano Super 0.9 s / 3.0 s; x86 + 12 GB GPU 0.7 s / 1.5 s.

### 4.5 Alarm path and interoperability (`experiments/alarm_path`)

Against `pysiaalarm` 3.2.2 (the receiver library Home Assistant uses): 19 of 21 cases pass. Findings:

- A leak sent as Contact ID 154 is acknowledged but decoded as invalid data (`YN`): a receiver built on it would not show the leak. The same leak as SIA-DCS `WA` works.
- Two frames in one TCP write to the async server: both events lost.
- A frame without the optional R field gets an ACK containing the text `None`.
- The library uses the key text's ASCII bytes as the AES key, not hex-decoded bytes.

500 alarms per WAN condition: p95 0.25 s normal, 6.7 s at 20% loss; an event inside a 60 s outage reached the centre within 60 s in 82.4% of trials. With the internet down all day, a leak reached the centre 10 hours later: the design has no cellular backup path.

### 4.6 What leaves the house (`experiments/egress`)

A simulated day: the phone-home device was blocked and logged 144 of 144 times; no camera footage left; with the internet down all day, 200 of 200 home-control commands worked. The daily summary reproduces the Season 1 E09 answer, which is true for traffic a person would notice; the same day also carried 287 alarm-line check-ins and 4 update checks.

### 4.7 The whole day, before and after (`experiments/scorecard`)

50 seeds per mix. The five principle suites passed 100% in every mix before the fixes. An added ALARM check showed what they missed.

| Measure | light, before → after | heavy, before → after |
|---|---|---|
| ALARM suite pass rate | 92% → 100% | 54% → 92% |
| Alarms delivered within 60 s | 98.0% → 100% | 81.9% → 98.0% |
| Local siren within 1 s of smoke | 98% → 100% | 76% → 100% |
| P4 (every failure or refusal told) | 100% → 100% | 90% → 100% |
| Home control confirmed outside outages | 91.5% → 93.0% | 63.5% → 78.7% |

Fixes: the hub raises alarms from detector state as well as events (one alarm per episode); smoke and leak detectors drive the siren directly; commands with no ack are resent with the same id (devices made idempotent); refusals by local rules are told in plain words.

## 5. Findings in our own code

1. **Clock lag broke confirmation.** A device whose clock ran behind the hub could never confirm an action, because confirmations were ordered by device time against hub time. Fixed: order by the device's own sequence numbers, or its own clock against its own earlier readings.
2. **One broken subscriber stopped the home.** An exception in one component halted all later deliveries. Fixed in the bus; the BUS suite checks it.
3. **Away mode did nothing for the alarm.** `arm_away` locked doors but armed nothing. Fixed with an alarm panel device whose confirmed state arms the hub.
4. **Green tests can still miss the important failure.** All five suites passed while a quarter of smoke alarms were lost under heavy faults. The principle list needed an explicit delivery check.

## 6. Limits

Everything ran in simulation. Voices are synthetic. People's replies are scripted. Phones and satellites were not subjected to link loss. No real LLM was run; the agent was tested with a rule planner and a scripted adversary. The DC-09 format follows `pysiaalarm`'s code and public descriptions; the paywalled standard was not read. Latency figures for hub tiers are assumptions until measured on hardware. Still open: no cellular alarm path; at heavy loss the 99% alarm target is missed when both event and state copy are lost (worst case 477 s to the next heartbeat); a sensor with a lagging clock stays stale for decisions.

## 7. Reproduce

```
pip install -r requirements.txt
pytest -q                                   # 119 tests
python experiments/scorecard/run.py         # about 1 minute on 2 cores
```

Facts checked against primary sources on Nov 19, 2026: `docs/facts-ledger.md`.


## 8. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
