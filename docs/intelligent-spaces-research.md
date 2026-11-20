# Intelligent Spaces: the complete Build Track research, B01-B10

**A trustworthy home assistant, built and broken in a digital twin**

Intersog · November 2026 · Apache-2.0 · [www.intersog.com](https://www.intersog.com)

> **Research in simulation, provided as is. Use it at your own risk.** Everything here was produced in a simulator, with simulated devices, synthetic voices, simulated people and homes, and no real cloud, by AI agents under human direction. It is not a product, not a certified alarm, leak-protection or biometric system, and not safety, security or legal advice. Life-safety equipment must be listed, installed and monitored by qualified people.

This is the canonical reading copy of the Build Track. It collects the ten episode papers (`docs/paper.md`, `docs/paper_b02.md` to `docs/paper_b10.md`) into one document you can follow from start to end. Every number comes from an experiment's `results.json`; each chapter names the file. The code, the data and the per-episode papers are in this repository: [github.com/intersog-ai/intelligent-spaces-lab](https://github.com/intersog-ai/intelligent-spaces-lab).

## Contents

1. Summary of findings
2. The architecture
3. B01 Build the twin, then break it
4. B02 Connect without a vendor cloud
5. B03 Readings you can trust
6. B04 Sent is not closed
7. B05 The voice pipeline
8. B06 Who is speaking
9. B07 The agent and the approval gate
10. B08 Cameras and the alarm path
11. B09 What leaves the house
12. B10 Fleet, cloud and the full test
13. What is still open
14. Appendix A: facts ledger
15. Appendix B: prompts and the agent contract
16. Contact

---

## 1. Summary of findings

Season 1 of *Intelligent Spaces* states five principles for a home assistant people can trust:

1. Fresh, sourced readings.
2. A command sent is not a result.
3. People set the limits and can override.
4. When something fails, it says so.
5. The core stays at home.

The Build Track writes each principle as a test, builds the home that Season 1 describes in a deterministic digital twin, and breaks it on purpose. Over ten episodes it adds one layer at a time. In the last episode every layer runs together, and the home becomes one of a thousand.

| Episode | Question | Key number | Source |
|---|---|---|---|
| B01 Build the twin, then break it | Can the five principles be tests, and what breaks under faults? | All five suites passed while 18.1% of heavy-fault alarms missed 60 s; an added ALARM check and fixes took delivery within 60 s from 81.9% to 98.0% | `experiments/scorecard/results.json` |
| B02 Connect without a vendor cloud | What does a real local protocol change? | A dead device was announced by its Last Will after 15.0 s, every time; the report named the offline shade in 48 of 48 trials | `experiments/b02_connect/results.json` |
| B03 Readings you can trust | Which sensor faults does a timestamp check miss? | Decisions on bad data per week: 637.3 naive, 605.2 with timestamps, 2 with the quality layer | `experiments/b03_readings/results.json` |
| B04 Sent is not closed | How much does a confirming state machine change a leak? | With a stuck or partial valve both common designs claimed "closed" in 100% of runs; the state machine in 0% | `experiments/b04_valve/results.json` |
| B05 The voice pipeline | Can the whole voice path run locally, and is "Done" true? | 663 of 663 "Done" replies true when heard; replying on the ack: 70.3% | `experiments/b05_pipeline/results.json` |
| B06 Who is speaking | What can a home safely do with a voice match? | 0 level-3 actions on a voice alone; a Bluetooth replay passed the named tier 48.3% of the time | `experiments/b06_identity/results.json` |
| B07 The agent and the approval gate | Can an approval be stolen, and can people keep up? | 0 of 500 attacks on bound, signed approvals succeeded; standing rules cut prompts from 20.25 to 1.02 a day | `experiments/b07_approval/results.json` |
| B08 Cameras and the alarm path | Does a second path close the outage gap? | Internet down all day: 100% of alarms at the centre within 60 s with two paths, 0% with one | `experiments/b08_security/results.json` |
| B09 What leaves the house | Is the daily answer honest about background traffic? | 96.05% of messages out were traffic a person would never notice; the B01 delete receipt said "complete" with copies left in 264 of 500 trials | `experiments/b09_egress/results.json` |
| B10 Fleet, cloud and the full test | Does it all hold together, and what does a fleet need? | Every layer on: safety suites 100% in 18 of 18 days; 10 interaction bugs found; rings with automatic halt kept a bad update to 7.5 homes of 1,000 | `experiments/b10_fleet/results.json` |

Five things the whole track found:

- **Green tests can miss the failure that matters.** B01's suites all passed while alarms were lost. In B10 an unchanged suite failed every day because the design had moved on. A test is only as good as the facts it can see.
- **Confirmation is the heart of it.** B01's stuck, partial, stale and skewed devices acknowledged 100% of commands and confirmed none. B04, B05 and B07 all depend on waiting for the device's own report.
- **The control that matters is the person, on a phone, with an approval bound to the exact action.** A voice is not a key (B01, B06). The approval must be single-use, signed and tied to that one action (B07). Asking too often makes people careless (B07).
- **The core can stay at home, and the cloud can be told only counts.** Home control, the siren, the valve and local voice all worked with the internet down (B01, B04, B09). A 265-byte counts-only report was enough to stop a bad update (B10).
- **Layers that each pass their own tests can still break each other.** B10 found ten interaction bugs. Three of them changed what the home did.

---

## 2. The architecture

**The language model proposes. Code decides. Devices confirm.**

```
 person (voice, phone)          device names, notes, calendars (untrusted text)
        |                                   |
        v                                   v
 [ voice pipeline B05 ] -> [ trust tier B06 ] -> [ agent / LLM: proposes tool calls ]
                                                            |
                                                            v
                         [ policy engine in code: levels, permissions, freshness (B01, B03),
                           approval gate with bound, signed, single-use tickets (B07) ]
                                  |                                   |
                       level 1-2: act / ask                level 3: phone approval
                                  v                                   v
                         [ action tracker: command -> ack -> CONFIRMED by a later device report ]
                                  |
                                  v
                devices (B01 twin; B02 MQTT, Matter model, Last Will; B04 valve + flow meter)

 local rules with no AI: leak -> valve (B04), smoke/leak -> local siren (B01)
 alarm hub -> internet + cellular -> monitoring centre, which dispatches (B01, B08)
 egress gate + ledger: the only way out, by declared purpose (B01, B09)
 cloud management plane: update rings, halt, counts-only health (B10)
```

Three levels decide what happens to a proposal:

- **Level 1, act and tell.** Lights, shades, heating, locking.
- **Level 2, ask first.** Close the garage, arm away.
- **Level 3, never alone.** Unlock, open the water, share a camera clip, open the garage for a visitor. Each needs a recorded phone approval from an allowed person.

A voice match can personalize and can raise trust for levels 1 and 2. It never authorizes level 3.

**Local core, cloud by purpose.** On the hub: the policy engine and approval gate, the action tracker, the leak state machine, reading quality, the voice pipeline, the trust policy, the alarm hub with both paths, and the egress gate and ledger. The outside world gets only what a declared purpose allows:

- alarm events to the monitoring centre;
- a nightly encrypted backup;
- update checks;
- plain-text pushes to people away from home;
- optional cloud speech, if the household opts in;
- in B10, counts-only health reports to a fleet plane.

The monitoring centre, not the home, calls for help.

The diagram is also in `docs/architecture.svg`. The component contract (topics, payloads, what "confirmed" means) is in `islab/core/CONTRACT.md`.

---

## 3. B01 Build the twin, then break it

Pairs with Season 1 E01-E02. Numbers from `experiments/scorecard/results.json`, `experiments/twin_faults/results.json`, `experiments/agent_gate/results.json`, `experiments/speaker_id/results.json`, `experiments/voice_latency/results.json`, `experiments/alarm_path/results.json` and `experiments/egress/results.json`.

### Question

Can the five principles be written as tests, and what breaks when a home built on them meets realistic faults?

### Method

- A human-written contract fixed topics, payloads and the meaning of "confirmed". Five AI agents then built the twin, policy, voice, alarm and a fact check in parallel, and an integration agent connected them.
- **Twin:**
  - 15 devices on an MQTT-like bus in a deterministic discrete-event simulator;
  - a household of three: Anna, Mark and Sam;
  - a fault injector: loss, delay, stuck and partial actuators, stale sensors, clock skew, dead batteries, internet outages and hostile device names.
- **Policy:**
  - a state store with freshness checks;
  - tools shaped like Model Context Protocol tools;
  - a three-level policy engine and an action tracker that confirms by later device state;
  - an audit log.
- **The household day** ran for 50 seeds under three fault mixes: none; light (2% link loss); heavy (20% loss, a stuck actuator, a sensor clock 120 s behind). Where the suites missed a failure, a check was added, the old code was re-scored, the code was fixed, and the same seeds ran again.

### Results

An acknowledgement is not a result (200 trials per fault, close the main valve, 30 s timeout):

| Fault | Confirmed | Acked, never confirmed | No ack | p95 time to confirm |
|---|---|---|---|---|
| none | 100% | 0% | 0% | 7.9 s |
| stuck, partial, stale, clock skew −120 s | 0% | 100% | 0% | — |
| 30% link loss | 53.5% | 14% | 32.5% | 22.4 s |
| offline, dead battery | 0% | 0% | 100% | — |

The approval gate (1,000 seeded scenarios, 1,218 proposals):

| Measure | Result |
|---|---|
| Unapproved level-3 actions executed | 0 |
| Approved level-3 actions executed | 81 of 81 |
| Injected level-3 or level-2 calls executed | 0 of 190 |
| Injected level-1 calls executed (then told) | 18 of 28 |
| Legitimate requests blocked | 0 of 614 |
| Control: same run with unlock at level 1 | 141 violations flagged |

A voice is not a key (GE2E embeddings, synthetic voices):

| Condition | Result |
|---|---|
| Clean studio audio, 10 s enrollment | EER 1.2% |
| Through a simulated room and microphone | EER 11.9% |
| Replay through a Bluetooth speaker | accepted as the person in 81% of trials at the room threshold |
| Kitchen noise at 10 dB, room threshold | 45% of the person's own tries refused |

The alarm path, against `pysiaalarm` 3.2.2 (the receiver library Home Assistant uses): 19 of 21 cases pass. A leak sent as Contact ID 154 is acknowledged but decoded as invalid data, so a receiver built on it would not show the leak. With the internet down all day, a leak reached the centre 10 hours later.

The whole day, before and after the fixes (50 seeds per mix):

| Measure | light, before → after | heavy, before → after |
|---|---|---|
| ALARM suite pass rate | 92% → 100% | 54% → 92% |
| Alarms delivered within 60 s | 98.0% → 100% | 81.9% → 98.0% |
| Local siren within 1 s of smoke | 98% → 100% | 76% → 100% |
| P4 (every failure or refusal told) | 100% → 100% | 90% → 100% |

What leaves the house: the phone-home device was blocked 144 of 144 times, and with the internet down all day 200 of 200 home-control commands worked.

### What held

- No unapproved level-3 action ran in 1,000 adversarial scenarios.
- Home control worked with the internet down.
- Devices that lied by acknowledging were caught by waiting for their state.

### What broke

1. A device whose clock ran behind could never confirm, because confirmations were ordered by device time against hub time.
2. One broken subscriber stopped the whole home.
3. Away mode armed nothing.
4. All five suites passed while a quarter of smoke alarms were lost under heavy faults. The principle list needed an explicit delivery check.

All four were fixed.

### Limits

- Everything ran in simulation, with synthetic voices and scripted replies.
- No real LLM was run.
- The DC-09 format follows `pysiaalarm` and public descriptions; the paywalled standard was not read.
- Hub tier latencies are assumptions.

### Reproduce

```
git checkout b01
pip install -r requirements.txt
python experiments/scorecard/run.py
python experiments/agent_gate/run.py
python experiments/twin_faults/run.py
python experiments/alarm_path/run.py
```

---

## 4. B02 Connect without a vendor cloud

Pairs with Season 1 E03. Numbers from `experiments/b02_connect/results.json`.

### Question

When the twin talks over a real local protocol, with no vendor cloud in the path, what changes? How fast is a routine, how fast is a dead device noticed, and what do MQTT's QoS levels, retained messages and Last Will actually buy?

### Method

- A real broker (amqtt 0.12.2) ran on localhost. Each twin device had its own paho-mqtt client with retained state and its own Last Will.
- The hub resends the same command id after 2 s without an ack and confirms only by a later state message.
- Home Assistant discovery payloads were generated, and a Matter attribute view was checked against SDK-generated cluster ids.
- Seven devices ran the "we're leaving" routine with the bedroom shade losing power. Time was compressed ten times.
- Conditions: QoS 0 and 1, loss of 0, 5 and 20%, 8 trials each.

### Results

| condition | all 5 live steps confirmed | time to confirm them, p50 / p95 |
|---|---|---|
| QoS 0, no loss | 8/8 | 8.3 / 9.5 s |
| QoS 0, 20% | 6/8 | 9.3 / 18.2 s |
| QoS 1, no loss | 8/8 | 9.0 / 10.0 s |
| QoS 1, 20% | 6/8 | 21.4 / 27.1 s |

| Measure | Result |
|---|---|
| Loopback publish to receive | QoS 0 p50 0.45 ms; QoS 1 p50 0.51 ms |
| Last Will after a power loss (socket open) | 15.0 s, every probe and every trial: 1.5 x the 10 s keepalive |
| Last Will after a process crash | 0.01 s |
| Command during a network blip, persistent session | QoS 0 delivered 0/3, QoS 1 3/3 |
| Same, with anonymous access allowed | QoS 1 0/3 |

In 48 of 48 trials the report named the bedroom shade as offline and possibly still open.

### What held

- Local MQTT, with no cloud, ran the routine and named the dead device every time.
- Retained messages let a restarted hub rebuild its view in about 10 ms.

### What broke

- The hub's MQTT thread died on an empty retained message (fixed).
- amqtt delivered retained messages to clients that had not subscribed to them (worked around).
- One broker setting, anonymous access, silently disabled QoS 1 for offline devices.
- Matter counts a shade's lift the other way round (fixed by inverting it).

### Limits

- One machine, TCP on loopback, with no radio; loss was emulated.
- Time was compressed ten times, with 8 trials per condition.
- Matter is a data-model mapping only, and the HA discovery payloads were not loaded by a running Home Assistant.

### Reproduce

```
git checkout b02
python experiments/b02_connect/run.py
```

---

## 5. B03 Readings you can trust

Pairs with Season 1 E04. Numbers from `experiments/b03_readings/results.json`.

### Question

Which sensor faults does a timestamp check miss? What does a fuller reading-quality layer catch, and what does the extra caution cost the people in the house?

### Method

The layer (`islab/readings/quality.py`) checks each reading before it may drive a decision:

- Is it sourced?
- Is it a duplicate, or out of order?
- How far is the device's clock skewed? The layer estimates the skew from a rolling median.
- Is the value in range, and plausible in its rate of change? A jump that is too fast is held one report.
- Is the sensor stuck?
- Is the reading stale, or has the battery gone dead?
- Is an alarm event latched until the device itself goes wet and then dry?
- Do two sensors for the same thing disagree?

The test ran 7 simulated days per seed for 10 seeds, with one of each fault scheduled. Three hub views read the same bus: naive, timestamp (B01) and quality (B03). A ground-truth oracle labelled every message.

### Results (per simulated week, mean of 10 seeds)

| Measure | naive | timestamp | quality |
|---|---|---|---|
| Decisions on bad data | 637.3 | 605.2 | 2 |
| ...from the stuck sensor | 602.8 | 602.8 | 0 |
| False alarms | 4.7 | 2 | 0 |
| Water running with the valve open during a leak | 112 min | 112 min | 0 |
| Valve closed while dry after a leak | 5.4 min | 5.4 min | 133.6 min |
| Dead battery noticed | never | 782 s | 0 s |
| Stuck sensor noticed | never | never | 19.2 min |
| Refusal episodes told within 60 s | n/a | 48 of 48 | 30 of 30 |

### What held

- The quality layer removed every decision on a stuck, spiking, dead or one-shot-leak sensor.
- Its skew estimate ended within 0.3 s of the true 336 s.

### What broke

- Timestamps fix the transport, not the sensor. The stuck sensor did the most damage, about 603 heating decisions a week, and nothing in a timestamp check fires on it.
- Caution costs time. The valve stayed closed 134 minutes after the floor was dry, until a person answered.

### Limits

- Faults came at scripted times, one of each per week.
- Decisions were recorded, not executed.
- Fine-resolution sensors make "stuck" easy to see; a coarse one can repeat a value for hours. B10 met this with thermostats.
- The rate limit delays a real fire's rise by one report, so fire detection stays with listed smoke alarms.

### Reproduce

```
git checkout b03
python experiments/b03_readings/run.py
```

---

## 6. B04 Sent is not closed

Pairs with Season 1 E05. Numbers from `experiments/b04_valve/results.json`.

### Question

A local rule closes the main water valve on a leak, with no AI in the loop. Written as code on the hub, how much does a confirming state machine change the outcome compared with two common designs, when the valve, the network, the hub and the internet fail?

### Method

The state machine runs idle → leak_detected → closing → closed_confirmed, or close_failed → escalated:

- "Closed" requires two things: a valve report showing position 0, and a fresh flow-meter reading at or below 0.2 L/min.
- 30 s after the first command without that, people are told to shut the water off by hand, and a second alarm goes to the centre.
- A write-ahead journal survives a hub reboot.

The baselines are fire-and-forget ("closed" when sent) and ack-based ("closed" on the valve's ack).

The test ran 500 seeded runs for each of 8 conditions and 3 designs. Ground truth is the valve's physical position.

### Results

| Condition | Fire-and-forget false "closed" | Ack-based false "closed" | State machine false "closed" |
|---|---|---|---|
| normal | 0% | 0% | 0% |
| valve stuck | 100% | 100% | 0% |
| valve partial | 100% | 100% | 0% |
| command lost | 51.6% | 0% | 0% |

| Condition, state machine | Confirmed close p50 | p95 |
|---|---|---|
| normal | 6.53 s | 8.40 s |
| hub reboot, with journal | 34.05 s | 49.13 s |
| hub reboot, journal lost | 204.63 s | 331.03 s |
| internet down | 6.60 s | 8.38 s |

With a stuck or partial valve, the state machine told people to shut off by hand in 100% of runs, at 30.01 s. 64,768 explanation statements matched a cited journal record in 100% of cases.

### What held

- The state machine's last message was never wrong.
- The journal cut recovery after a reboot from 204.63 s to 34.05 s at p50.
- Valve control needed no internet.

### What broke

- A rebooted alarm hub raised the leak again: a duplicate at the centre after 81.8% of reboots, cut to 9.2%.
- With the internet down, nobody away from home was told. The push waits for the internet, and so did the alarm.

### Limits

- The flow meter is an extra device most homes do not have.
- Valve travel is a jump after 4-8 s.
- One leak per run.
- Not a certified leak-protection system.

### Reproduce

```
git checkout b04
python experiments/b04_valve/run.py
```

---

## 7. B05 The voice pipeline

Pairs with Season 1 E06. Numbers from `experiments/b05_pipeline/results.json`.

### Question

Can the whole voice path run on a local hub (wake word, end of speech, speech to text, intent, policy, device, spoken reply)? How fast is it on three classes of hub, and is "Done" true?

### Method

- **Measured on a 2 vCPU CPU:** openWakeWord, silero-vad, sherpa-onnx Whisper tiny.en and Piper, on synthetic voices, clean and with a TV underneath.
- **Twin runs:** 400 seeded sessions per tier. In 16% of sessions an addressed device is stuck, offline or silent.
- **Replies:** "Done" only after the device's own report. A holding line after 1.5 s that claims nothing. The baseline says "Done" on the ack.

### Results

| Tier | All utterances, p50 / p95 time to first audio | Clean audio, rules path, p50 / p95 |
|---|---|---|
| This CPU (measured stages) | 2.08 / 11.04 s | 1.69 / 2.98 s |
| Pi 5 class (assumed) | 3.35 / 11.70 s | 2.97 / 4.63 s |
| Mini PC + GPU (assumed) | 1.22 / 4.28 s | 1.01 / 2.14 s |

| Design | Executed commands | "Done" true when heard |
|---|---|---|
| Reply after confirmation | 705 | 100% of 663 |
| Reply on ack (baseline) | 705 | 70.3% of 693 |

With a TV underneath at 0 dB, the wake word was detected 48.8% of the time and Whisper was correct on 14.0% of named commands. 18 of 95 cut-off commands still parsed and ran something other than what was said.

### What held

- Every "Done" came after the device's own report.
- No action came from the TV's own speech.
- The whole path ran from PyPI wheels without the internet.

### What broke

- A TV under the command.
- One fixed end-of-speech hangover cannot serve both a quick reply and a mid-command pause.
- Cuts that still parse.
- The LLM path, even as a stub, costs seconds on CPU tiers.

### Limits

- Synthetic speech, digitally mixed.
- Other hardware tiers are assumptions.
- The LLM is a stub.

### Reproduce

```
git checkout b05
python experiments/b05_pipeline/run.py
```

---

## 8. B06 Who is speaking

Pairs with Season 1 E06. Numbers from `experiments/b06_identity/results.json`.

### Question

What can a home safely do with knowing who is speaking? How often is the family refused, how often is someone else treated as family, how much do cheap replay checks help, and does any level-3 action ever run on a voice alone?

### Method

A voice match gives one of three tiers:

- *guest*: house defaults;
- *comfort*: preferences, where a wrong guess costs little;
- *named*: private data read aloud, and acting as the person for level 1-2.

Level 3 always needs a phone approval. A one-class liveness check is fitted on the household's own live speech. 534 requests went through the policy engine: family, guests, replays, and a teenager talking over a parent.

### Results

| Tier | Family refused | Named as another member | Guest accepted |
|---|---|---|---|
| comfort | 8.0% | 0% | 16.7% (3 of 18) |
| named | 25.3% | 0% | 0% (0 of 18) |

| Replay device, named tier | Accepted as the victim | With liveness |
|---|---|---|
| phone speaker | 1.1% | 0% |
| Bluetooth speaker | 48.3% | 44.8% |
| hi-fi speaker | 47.1% | 41.4% |

| Policy | Level 3 run | By voice alone |
|---|---|---|
| B06 policy | 55 | 0 |
| counterfactual: voice authorizes a recognised person | 85 | 85 (52 of them attacks) |

### What held

- Zero level-3 actions ran on a voice alone, in every run and seed.
- A spoken "yes" never counted as an approval.
- 20 of 22 comfort requests were personalized, and none for the wrong person.

### What broke

- Cheap liveness cues do not stop a decent loudspeaker.
- The overlap rule does not work.
- Reading private data aloud is exposed to replay: 8 of 22.
- A quarter of the family's first tries are refused at the named tier.

### Limits

- Every household voice is synthetic.
- Few guest windows.
- Rooms and replays are DSP proxies.
- Not a biometric system.

### Reproduce

```
git checkout b06
python experiments/b06_identity/run.py     # needs the series voices (ISLAB_VO)
```

---

## 9. B07 The agent and the approval gate

Pairs with Season 1 E07. Numbers from `experiments/b07_approval/results.json`.

### Question

Can an approval be stolen, reused, misdirected or stretched? And how do people stay in charge without drowning in prompts?

### Method

Each prompt is a ticket: request id, the exact action, a SHA-256 digest of it, a nonce and an expiry. The phone signs its reply with its own key. Right before execution, the action must still hash to the approved digest.

Standing rules, set in advance, have limits: days, a time window, uses per day, the longest window, and "only if a delivery is expected".

The tests:

- 10 attack types, 50 trials each;
- 30 days for each of 2 households under 5 designs;
- 300 prompt-injection scenarios per row.

People are modelled with an assumed fatigue curve.

### Results

| Attack (50 trials each) | B01-style engine | GatedEngine |
|---|---|---|
| Old reply reused for a new request | 50 | 0 |
| Forged reply | 50 | 0 |
| Reply after the garage window ended | 50 | 0 |
| Arguments changed after the prompt | 50 | 0 |
| Phone app sends yes with the other request's id | 50 | 0 |

| Design, 60 household-days | Prompts per day | Wrong level-3 done | Legit blocked |
|---|---|---|---|
| Ask for everything | 20.25 | 1 | 16.4% |
| Ask by level (B01) | 3.75 | 1 | 3.9% |
| Standing rules with limits | 1.02 | 1 | 3.2% |
| Standing rules, no limits | 0.67 | 6 | 3.2% |

Injected level-3 calls reaching a phone: 11-17 per 300 with the B01-style engine, 0 with GatedEngine.

### What held

- Approvals that are single-use, signed by the phone and bound to the digest of the exact action stopped every attack type.
- Nothing moved while a request waited.
- Untrusted text never reached a prompt.

### What broke

- The B01 engine executed whatever the arguments held when the yes arrived.
- It trusted any message on the reply topic.
- It traced the tool, not the arguments.
- Asking for everything made people hurried: 260 hurried answers and 16.4% of requests blocked.

### Limits

- No language model was run.
- People are a model.
- Keys come from the simulator; a real phone needs a hardware-backed key.

### Reproduce

```
git checkout b07
python experiments/b07_approval/run.py
```

---

## 10. B08 Cameras and the alarm path

Pairs with Season 1 E08. Numbers from `experiments/b08_security/results.json`.

### Question

Does a modelled second path (cellular) close the B01/B04 outage gap, and what does it cost? When a camera mistakes pets and shadows for people, what changes between a confidence threshold, an automatic verdict, and stating the evidence and asking a person?

### Method

- **Alarm hub:** a dual-path hub (internet first, cellular second) with per-path supervision.
- **Centre:** a monitoring centre that dedupes by incident.
- **Cellular link:** a modelled link; every value is an assumption.
- **Camera:** a detector model with assumed confusion rates.
- **Scale:** 300 alarms per condition and design, 10 days of cellular traffic, and 26 weeks per camera design.

### Results

| Condition | One path | Two paths |
|---|---|---|
| internet down 10 min | 13.0% | 100% |
| internet down all day | 0% | 100% (p95 0.9 s) |
| internet and cell down 10 min | 13.0% | 13.0% |

| Camera design | False "person" a week | False signals at the centre a week |
|---|---|---|
| threshold 0.7 | 1.46 | 0 |
| verdict 0.7 | 0.81 | 33.92 |
| ask 0.5 | 4.81 | 0.77 |

Cellular cost on a normal day was 10,587 bytes, or USD 0.01 at the assumed tariff. Incident-level dedupe opened 0 duplicate incidents after a hub reboot. Message-level dedupe opened one in all 200 smoke and leak runs.

### What held

- In the twin, a second path closed the outage gap.
- Dedupe belongs to the receiver, and works by incident.
- Asking a person kept fewer than two camera signals a week reaching the centre, each one decided by a person.

### What broke

- A flapping backup is noisy: 87.6 path messages a day with a degraded cell.
- A lossy primary flaps 119.9 times a day.
- Asking is slower and can go unanswered: the unexpected person reached the centre in 69.2% of tests.

### Limits

- The link, detector, scenes and people are models.
- A push to people away still needs the internet.
- Not a certified alarm system.

### Reproduce

```
git checkout b08
python experiments/b08_security/run.py
```

---

## 11. B09 What leaves the house

Pairs with Season 1 E09. Numbers from `experiments/b09_egress/results.json`.

### Question

What does a full-day ledger of every byte out look like? Can the daily answer be short and still never leave a category out? What does the gate catch when a device phones home? Does deleting shared data ask first, find every copy and tell the truth about backups?

### Method

- **Ledger gate:** a row for every attempt to send anything.
- **Source manifests:** each sender declares its purposes, its largest message and its messages per day.
- **Notice:** the household is told at a source's first undeclared attempt.
- **Answer:** a three-level answer built from the ledger only.
- **Deletion:** a scan of the whole hub disk, per-record backup keys, and a receipt that says what was not checked.
- **Tests:**
  - the E09 day;
  - 30 varied days;
  - 7 phone-home modes × 200 days;
  - 500 deletions with planted copies.

### Results

| Purpose, E09 day | Messages | Bytes | A person would notice |
|---|---|---|---|
| speech recognition | 12 | 164,980 | yes |
| encrypted backup | 1 | 48,000,000 | no |
| alarm check-ins | 287 | 27,839 | no |
| update checks | 4 | 4,800 | no |

| Answer | Days with every category mentioned |
|---|---|
| B09 short answer | 30 of 30 |
| B01 summary without routine traffic | 0 of 30 |

| Phone-home mode | B01 rules: got out | Ledger gate: got out |
|---|---|---|
| plug mimics an update check | 100% | 0% |
| update agent sends 50 KB "checks" | 100% | 0% |
| update agent, 2 extra checks inside its manifest | 100% | 100% |

| Hidden copy, 500 trials | B01 delete | B09 delete |
|---|---|---|
| exact copy, unregistered cache | 0% | 100% |
| re-encoded copy without lineage | 0% | 0%, listed as not checked |

### What held

- Every attempt has a ledger row.
- A short answer can name the background with its count, every day.
- Manifests stop a device that pretends to be another sender.
- No shared record was deleted without a yes.

### What broke

- The scripted E09 line left out 96% of messages.
- B01 rules checked the destination, not the sender.
- The B01 delete receipt overclaimed in 264 of 500 trials.
- A shared nightly backup key keeps deleted data readable until rotation.
- Manifests also block real update checks when a sender breaks them (800 of 800).

### Limits

- Byte sizes and promises are modelled or declared.
- Network default-deny is not simulated.
- Payloads are not inspected.

### Reproduce

```
git checkout b09
python experiments/b09_egress/run.py
```

---

## 12. B10 Fleet, cloud and the full test

Pairs with Season 1 E10, the season finale. Numbers from `experiments/b10_fleet/results.json`. The full episode paper is `docs/paper_b10.md`.

### Question

What changes when the Season 1 home is one of many? And does the whole design hold when every part runs together?

### Method

1. **Full test.** The B01 household day ran with every layer on at once, each through its public API (`islab/fleet/full.py`):
   - B02 as its Matter data-model round trip, discovery configs and the 15 s Last Will, modelled in the loop;
   - B03 reading quality;
   - the B04 valve state machine;
   - the B05 pipeline;
   - the B06 trust tier;
   - the B07 gate with signed phone replies;
   - B08 dual-path alarms and camera asks;
   - the B09 ledger.

   It ran with 6 seeds under none, light and heavy faults, with the B01-only house on the same seeds as a reference. The five principle suites ran unchanged, plus a route-aware P5 adapter (P5r) and one check per layer.
2. **Interaction bugs.** Every bug found between layers got one fix. Each fix was switched off once to show what it does.
3. **Bad update.** A manifest typo allows the alarm hub 200 messages a day instead of 2,000. It was run in the twin to see what a home's counts-only health report shows.
4. **Fleet.** 1,000 simulated homes over 14 days and 20 seeds (`islab/fleet/rollout.py`). The plan was canary 1%, then 5%, 20% and the rest, with a 24 h soak. Four designs were compared: all at once, rings with an operator halt, rings with an automatic halt at 2 failing homes, and the last with a 03:00 install window. All rates are assumptions except the bad update's health signature.
5. **Cloud.** A counts-only report and the raw bundle were each offered to a fleet plane through the B09 gate.

### Results

| Measure, 6 days per mix | none | light | heavy |
|---|---|---|---|
| P1, P2, P3, P4, ALARM, BUS pass | 100% | 100% | 100% |
| P5 unchanged / P5r route-aware | 0% / 100% | 0% / 100% | 0% / 100% |
| Every layer check (B02-B09) | 100% | 100% | 100% |
| Unapproved level-3, false "done", silent failures | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| Alarms within 60 s; p95 delay | 100%; 0.24 s | 100%; 0.22 s | 100%; 5.63 s |
| Home control in the outage, full / B01 only | 100% / 100% | 100% / 95.8% | 58.3% / 45.8% |
| Spoken "Done" true when heard | 42 of 42 | 42 of 42 | 26 of 26 |

Interaction bugs, each closed by one fix (seed 0, fix off):

| Fix | What broke between layers | With the fix off |
|---|---|---|
| F1 | B07 accepts only signed replies; the day's phones sent unsigned ones | 4 replies refused; 6 scenes fail |
| F2 | B09 manifests did not declare the day's senders | backup, update check and consented clip blocked |
| F3 | B05 republishes the transcript; the B01 hub handled it again | 31 actions instead of 19; P4 fails |
| F4 | B05 grammar lacks "good night", "keep the heating on" | both not understood |
| F5 | B01 P5 predates B08's second path, consented clips and B04's push | P5 fails 18 of 18 days |
| F6 | B01 leak rule and B04 controller both command the valve | two owners close the valve |
| F7 | people read B01 prompt wording; B07 words prompts differently | courier approval never answered |
| F8 | B06 `household.yaml` replaces Sam's deny list instead of adding to it | Sam's unlock goes to a parent instead of being refused |
| F9 | B08's consent check runs before the destination check | a phoning-home camera is blocked but nobody is told |
| F10 | B03 calls a thermostat holding its setpoint "stuck" | 4 false warnings a day |

The fleet (1,000 homes, 20 seeds, means):

| Update / design | Homes that installed it | Homes affected | Halt, hours after start |
|---|---|---|---|
| loud bad / all at once | 979.0 | 979.0 | 27.0 |
| loud bad / rings, operator halt | 43.8 | 43.8 | 27.2 |
| loud bad / rings, automatic halt | 7.5 | 7.5 | 3.8 |
| quiet bad (assumed 5%) / rings, automatic halt | 142.1 | 6.9 | 46.9 |
| quiet bad / automatic halt, 03:00 install | 176.6 (48.0 after the halt) | 8.8 | 51.0 |

- **Bad update in the twin.** It blocked the alarm path in 9 of 9 days, 2.87-3.23 h after midnight, and failed the ALARM suite in all 9. Every one of those days gave a failing health report; the 18 good-version days gave none.
- **Good update.** It reached 95% of homes in 77 h through the rings. 19.6 homes were still on the old version at day 14: the homes away the whole time.
- **Health report.** It was 265 bytes and was sent. The raw bundle was 4,094,716 bytes, with 14 requests in the household's own words and 49 messages to people verbatim. Each part was refused by the B09 manifest as over the declared size.

Cloud mapping, with dates only from the facts ledger:

| Stack | Status (ledger) | On the home hub | In the plane |
|---|---|---|---|
| AWS IoT Greengrass V2 | V1 support ended Oct 7, 2026 | the whole core | update rings, halt, counts-only health |
| Azure IoT Hub + DPS + Device Registry | IoT Central retires Sep 20, 2029 | the whole core | identity, provisioning, rings, halt, counts-only health |
| Self-hosted MQTT + Home Assistant fleet | no vendor dates | the whole core, local broker | operator-run update server and health collector |
| Google Cloud IoT Core | retired Aug 16, 2023 | - | not an option |

Product features of these stacks were not verified in this study.

### What held

- With every layer on, the safety and trust suites passed every day under every fault mix.
- The core stayed at home, and the cellular path carried the alarm line through the outage.
- Counts were enough for the cloud. Rings with an automatic halt kept the loud bad update to the canary.

### What broke

- Ten interaction bugs that no single episode's tests could see. Three of them changed what the home did: F3, F8 and F9.
- The unchanged P5 fails every day.
- A quiet bad update passed the canary in 20 of 20 rollouts.
- A 03:00 install window kept installing after a halt.
- Under heavy faults, an unconfirmed courier unlock that really happened was never relocked: the front door was still unlocked at 19:05 in 2 of 6 heavy days.

### Limits

- 6 seeds per mix.
- B02 is modelled in the loop.
- Every utterance was named-tier, and there were no recognition errors.
- No LLM was run.
- The fleet is a lightweight model with assumed rates.
- No real cloud was used.

### Reproduce

```
git checkout b10
python experiments/b10_fleet/run.py         # about 2-3 minutes on 2 cores
```

---

## 13. What is still open

Collected from every episode's limits and findings. Where a later episode closed an item in the twin, it says so.

**Alarms and life safety**

1. Push messages to people away from home still need the internet. B08's second path carries alarms to the centre, not texts to people. (B04, B08)
2. The cellular backup flaps when it is degraded: 87.6 path messages a day. A lossy primary flaps 119.9 times a day. (B08)
3. Incident dedupe can merge a genuinely new alarm in the same zone within 30 minutes if no restore arrives. (B08)
4. A Contact ID leak code is decoded as invalid by `pysiaalarm`. B04 and B08 use SIA-DCS, but the B01 day and its ALARM suite still recognise Contact ID codes only. (B01, B10)
5. At heavy loss, an alarm can wait for the next heartbeat when both the event and its state copy are lost (worst case 477 s in B01). (B01)
6. Nothing here is listed or certified, and the DC-09 standard text was not read. (B01, B08)

**Confirmation and devices**

7. An unlock that physically happened but was never confirmed is never relocked. The front door was still unlocked at 19:05 in 2 of 6 heavy-fault days. A relock is harmless and should follow any unconfirmed unlock. (B10, in B01 house code)
8. A device with a lagging clock stays stale for decisions under the B01 freshness rule. The study thermostat request failed on 6 of 6 heavy days in B01 and in B10. B03 estimates the skew, but letting it relax the B01 rule would make the unchanged P1 suite disagree. The two definitions of "fresh" need to be reconciled. (B01, B03, B10)
9. The flow meter is an extra device; without it, closed rests on the valve's own report. (B04)
10. Radios: no Wi-Fi, Thread or Zigbee radio was tested, and loss was i.i.d. (B02, B03)
11. A stable sensor at coarse resolution looks stuck. B10 had to exempt thermostats holding their setpoint. (B03, B10)

**Voice and identity**

12. A TV under the command, a cut that still parses, and one hangover for every pause. (B05)
13. Cheap liveness does not stop a loudspeaker, the overlap rule does not work, and reading private data aloud is exposed to replay. (B06)
14. A quarter of the family's first tries are refused at the named tier, and more in noise. (B06)
15. All speech was synthetic. Hardware tiers other than the measured CPU are assumptions. No LLM was run anywhere in the track. (B01, B05, B06, B07)

**People and approvals**

16. The phone approval is only as good as the person: 10% careless answers let 24 attacks through in B06. Fatigue is a model. (B06, B07)
17. A phone mix-up loses the intended action. Escalation does worse than fan-out when the first phone is offline. A fake courier can arrive first on a day a delivery is expected. (B07)
18. A real phone needs a hardware-backed key, enrolment and recovery. A stolen or coerced phone is out of scope. (B07)
19. The engine has no plain words for `open_garage` and `disarm_alarm`. This is a fix still to make in `islab/policy`. (B06, B07)
20. A camera ask reply and a B07 approval share `reply/<person>` with different id spaces. A B07 approval to share a clip is not a B08 consent. (B10)

**Egress and privacy**

21. A sender that stays inside its manifest is not caught. A manifest that holds a misbehaving update agent also stops its real checks. (B09)
22. The gate sees only traffic routed through it. A real home needs network default-deny, which is not simulated. Payloads are not inspected. (B09)
23. A key store lost with the hub makes every backup unreadable; key escrow is not modelled. (B09)
24. After a blocked share, B09 quarantines the policy engine itself as a source. (B10)

**Fleet and cloud**

25. The B01 P5 suite must learn routes, consents and new purposes as first-class facts. Today it needs the B10 adapter. (B10)
26. A quiet bad update passes a 10-home canary. Rings, soak and thresholds were not tuned. (B10)
27. Install must require a fresh go from the plane. A maintenance window installed after the halt. (B10)
28. Health counts are trusted. A home that lies in its counts, or a compromised plane, is not modelled. (B10)
29. Cloud product features beyond the ledger dates were not verified. (B10)

---

## 14. Appendix A: facts ledger

Checked on Nov 19, 2026, against primary sources wherever one could be opened. This is the same ledger as `docs/facts-ledger.md`. B10 added no rows: the AWS and Azure documentation pages it needed could not be opened from the build machine.

| # | Claim | Verdict | Correct value | Source |
|---|---|---|---|---|
| 1a | Matter 1.5 released Nov 20, 2025 | verified | Announced by CSA on November 20, 2025 | csa-iot.org newsroom |
| 1b | Matter 1.5 added cameras | verified | WebRTC live audio and video, two-way talk, detection and privacy zones, multi-stream, PTZ, local or cloud recording | same |
| 1c | Matter 1.5 added closures and tariff/energy features | verified | Modular closures cluster; electrical energy tariff device type; smart metering; EV charging state of charge and bi-directional charging | same |
| 1d | Matter 1.5.1 | verified | Released March 31, 2026; camera refinements | csa-iot.org newsroom |
| 1e | Ecosystems with Matter cameras | partly verified | SmartThings (Dec 9, 2025) and Home Assistant (2026.11, Nov 4, 2026). Apple Home not as of iOS 27. Google Home and Alexa not confirmed | home-assistant.io; secondary sources |
| 2a | Raspberry Pi AI HAT+ 2 | verified | Hailo-10H; 40 TOPS (INT4); 8 GB on-board RAM | raspberrypi.com |
| 2b | AI HAT+ 2 price | verified, explained | Launched at $130 (Jan 15, 2026); $200 list today, after memory-driven price rises | raspberrypi.com; cnx-software.com |
| 2c | LLMs on Hailo-10H | verified (Hailo figures) | About 1-2B parameter models at roughly 5-10 tokens/s | github.com/hailo-ai |
| 3 | NVIDIA Jetson price rise, July 2026 | verified | Orin Nano Super dev kit $249 → $399; AGX Thor dev kit $3,499 → $5,499 | developer.nvidia.com; cnx-software.com |
| 4a | Home Assistant 2026.8 | verified | Released Aug 5, 2026 | home-assistant.io |
| 4b | 2026.8 llama.cpp and LiteLLM integrations | verified | Both listed under new integrations | same |
| 4c | 2026.8 removed the Advanced mode toggle | corrected | It removed "advanced"/"expert" wording; the toggle's retirement is a separate deprecation (until Core 2027.6) | home-assistant.io; developers.home-assistant.io |
| 5a | Voice Preview Edition price | corrected | $69 USD / €59; $59 was the launch price | home-assistant.io |
| 5b | Connect ZBT-2 | verified | $49 USD / €45; Zigbee or Thread, not both at once | home-assistant.io |
| 6a | SIA DC-09-2026 | verified | Released March 3, 2026; autocommissioning, key rotation; backward-compatible | securityindustry.org |
| 6b | SIA DC-09 Java library | verified, caveat | PolyForm Noncommercial with a separate commercial licence; not OSI open source | securityindustry.org; github.com |
| 7a | ASAP to PSAP | verified | Developed by APCO with CSAA (now TMA); TMA runs the service | tma.us |
| 7b | How alarm data reaches 911 electronically | verified | Structured message over Nlets into the PSAP's CAD; 143 active ECCs, about 53M people (Apr 2024 page) | tma.us |
| 7c | RapidSOS | verified (high level) | A separate commercial path to 911 centres, not part of ASAP | rapidsos.com |
| 8a | AWS IoT Greengrass V1 | verified | Support ended Oct 7, 2026; migration target is Greengrass V2 | docs.aws.amazon.com |
| 8b | Azure IoT Central | verified (secondary mirror) | Retires Sep 20, 2029; Microsoft recommends IoT Hub, DPS and Azure Device Registry | Azure notice AZ569914; techcommunity.microsoft.com |
| 8c | Google Cloud IoT Core | verified | Retired Aug 16, 2023 | docs.cloud.google.com |
| 9a | NVIDIA Parakeet TDT 0.6B v3 | verified | CC-BY-4.0; 25 European languages | huggingface.co |
| 9b | Kokoro-82M | verified | Apache-2.0 | huggingface.co |
| 9c | Piper licence | corrected | rhasspy/piper (MIT) archived Oct 6, 2025; development moved to OHF-Voice/piper1-gpl (GPL-3.0) | github.com |
| 9d | SpeechBrain ECAPA | verified | Apache-2.0; EER 0.80% on VoxCeleb1-test (cleaned) | huggingface.co |
| 9e | Qwen3-4B-Instruct-2507 | verified | Apache-2.0 | huggingface.co |
| 9f | Gemma 4 | verified | Released April 2, 2026 under Apache 2.0 | opensource.googleblog.com |
| 9g | Qwen3.5-9B | verified | Apache-2.0 | huggingface.co |
| 10a | Frigate 0.16 | verified | Face recognition and licence plate recognition, Aug 16, 2025 | github.com |
| 10b | Current Frigate | filled in | 0.18.2 latest stable; 0.18.0 added UI configuration, GenAI providers, chat over cameras | github.com |
| 11a | MCP spec version | filled in | 2026-07-28, with per-request version negotiation | modelcontextprotocol.io |
| 11b | MCP governance | verified | Donated Dec 9, 2025 to the Agentic AI Foundation under the Linux Foundation | blog.modelcontextprotocol.io |
| 11c | Home Assistant MCP Server integration | verified | Since 2025.2, at `/api/mcp`; one-click card since 2026.10 | home-assistant.io |

Full source URLs for every row are in `docs/facts-ledger.md`.

---

## 15. Appendix B: prompts and the agent contract

**How the lab was built.** One coordinating AI session and AI sub-agents working in parallel built the lab, under human direction. The full task prompts are in `docs/prompts.md`. In short:

1. The coordinator wrote the simulator, the bus and the component contract by hand, so parallel agents could not drift apart.
2. For B01, five agents built the twin, the policy engine and agent, voice, the alarm path with privacy, and a fact check. An integration agent then assembled one home and wrote the five principle suites.
3. Every build agent got the same preamble:
   - read the contract and follow it exactly;
   - do not touch other agents' packages;
   - every experiment writes `results.json` with method, parameters, results and limitations;
   - run the tests until green;
   - end with a short report of files, public API, key numbers and open issues.
4. The fact-check agent opened primary sources and wrote the ledger in Appendix A. A search snippet did not count as a source.

**The agent contract for B05-B10.** Each later episode was built by one agent under a written contract:

- Read the README, the component contract, the simulator and the bus, and a finished episode paper as the model for shape.
- Work only in your own new package and files. Do not edit shared packages: wrap or subclass them, and report the need.
- Keep each experiment run under about 3 minutes on a shared 2-core machine. Use seeds; no busy loops.
- PyPI works; model hubs and most websites are blocked. If you could not run something, say so plainly and model it with labelled assumptions. Never invent a measurement.
- Every experiment writes `results.json` with `{experiment, date, method, params, results, limitations}`. Every number in a script or paper comes from it.
- Run the full test suite at the end; it must stay green. Do not commit: the coordinator reviews and commits.
- Write plainly, with no hype, and avoid a short list of marketing words. The disclaimer reads: "Research in simulation, provided as is. Use it at your own risk." Never call the work a product or a certified system.
- Deliverables: code with a small public API; the experiment with `run.py`, `results.json` and `notes.md`; tests; the episode paper; the episode README, a 14-paragraph narration script and a visuals list; one row in the episode index; and a short final report.

For B10 the contract was extended: build the full test and the fleet, write the cloud mapping table only from checked facts, and write this document.

---

## 16. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).

Questions about the research: open an issue in the repository. Research in simulation, provided as is. Use it at your own risk.
