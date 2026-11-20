# Fleet, cloud and the full test: every layer at once, and one home among many, in a digital twin

Intelligent Spaces Lab, Build Track B10 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with simulated devices, simulated homes and no real cloud, by AI agents under human direction. It is not a product and not a certified system. Research in simulation, provided as is. Use it at your own risk.

## 1. Question

Season 1 episode ten asks what changes when the home is one of many. This study asks two things. First, does the whole design hold when every Build Track layer runs together in one home on one day? Second, what does a fleet need, so that a bad software update does not reach every home, and what may a cloud management plane see?

All numbers are from `experiments/b10_fleet/results.json`.

## 2. Design under test

- **Full house** (`islab/fleet/full.py`, new). It runs the B01 house and day script with every layer on, each through its public API:
  - B02 connect: the Matter data-model round trip, Home Assistant discovery configs, and Last Will availability after 15 s, as measured in B02. This is modelled in the loop; the real broker runs on wall time.
  - B03 reading quality. Decisions on stuck, disputed or dead readings are refused.
  - B04 leak-to-valve state machine, with its flow meter and journal.
  - B05 voice pipeline, mini PC tier, with measured stage times.
  - B06 trust tier for each utterance, and the household policy.
  - B07 approval gate. Phones sign their replies.
  - B08 dual-path alarm hub and centre, cellular link, and camera asks.
  - B09 egress ledger and manifests, composed with the B08 gate.
- **Checks** (`islab/fleet/checks.py`, new). The five principle suites run unchanged, with ALARM and BUS. P5r is a route-aware adapter for P5. There is one check per layer.
- **Fleet** (`islab/fleet/rollout.py`, new). 1,000 simulated homes, hourly for 14 days.
  - Homes check for updates every 6 h and send an hourly health report while online.
  - The plan has four rings: canary 1%, then 5%, 20% and the rest, each after a 24 h quiet soak.
  - Four designs: all at once with an operator halt 24 h after the first failing report; rings with the same operator halt; rings with an automatic halt at 2 failing homes; and the same, but installing in a 03:00 maintenance window. After a halt the plane rolls back.
- **Cloud plane** (`islab/fleet/cloud.py`, new):
  - a counts-only health report: 12 integers, no names, no words, no readings, no times of day;
  - a declared purpose `fleet_health`, class `health_counts`, at most 1,024 bytes and 24 a day, to one plane host. All other gate rules apply unchanged.

## 3. Method

1. **Full test.** The day ran with 6 seeds for each of three fault mixes: none, light (2% loss) and heavy (20% loss, a stuck light, a study thermostat clock 120 s behind). The day also has the B01 outage from 12:00 to 14:00. We added one B08 step: at 02:14 the front camera sees what looks like a person, and Anna shares the clip. The B01-only house ran on the same seeds as a reference. Seeds were cut from B01's 50 to keep the run near 3 minutes.
2. **Interaction bugs.** When the layers were first switched on together, some things broke between them. Each break got one fix. For each fix, one run (seed 0, no faults) repeats the day with that fix off.
3. **Bad update, calibrated on the twin.** The update has a typo in the alarm hub's manifest: 200 alarm messages a day instead of 2,000. It ran for 3 seeds per mix. A home's health report is failing when any must-be-zero count is not zero. A second bad update is assumed: it affects 5% of homes, 0-24 h after install.
4. **Fleet.** 20 seeds for each update and design. Every availability and plan value is an assumption, printed in `params`.
5. **Cloud.** The seed-0 health report and the raw bundle (trace, audit log, ledger, journal) were each offered to the plane through the B09 gate.

## 4. Results

### 4.1 The full test (`results.full_test.integrated`)

| Measure, 6 days per mix | none | light | heavy |
|---|---|---|---|
| P1, P2, P3, P4, ALARM, BUS pass | 100% | 100% | 100% |
| P5 unchanged / P5r route-aware | 0% / 100% | 0% / 100% | 0% / 100% |
| Every layer check (B02-B09) | 100% | 100% | 100% |
| Unapproved level-3 actions, false "done" messages, silent failures | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| Alarms at the centre within 60 s; delay p50 / p95 | 100%; 0.17 / 0.24 s | 100%; 0.17 / 0.22 s | 100%; 0.19 / 5.63 s |
| Home control confirmed in the outage, full / B01 only | 100% / 100% | 100% / 95.8% | 58.3% / 45.8% |
| Spoken "Done" true when heard | 42 of 42 | 42 of 42 | 26 of 26 |
| Leak: valve confirmed closed after detection | 5.0-7.0 s | 5.0-9.0 s | 7.0-16.0 s |
| Cellular messages during the outage, mean | 41.8 | 41.0 | 41.0 |
| Short "what did you send" answer names every category | 100% | 100% | 100% |
| Health report failing (good version) | 0 of 6 | 0 of 6 | 0 of 6 |

P5, unchanged, failed every day for three reasons: egress while the internet was down (B08's cellular alarm path), a camera clip out (the one Anna chose to share), and a purpose it does not know (B04's push). P5r takes those three out of P5's view and checks them on their own terms. It found no other egress problem.

Scenes that did not go as scripted were all under faults. Light: garage 5 of 6. Heavy: arm away 2 of 6, study warmer 0 of 6, outage home control 0 of 6, injection 5 of 6, garage 4 of 6, courier 2 of 6, good night 2 of 6. The B01-only house on the same seeds: light outage 5 of 6; heavy arm away 2, study warmer 0, outage 0, injection 5, garage 3, courier 5, good night 4 of 6. With six seeds these differences are within noise. In every miss the home told someone what had not happened (P4 100%).

On seed 0 with no faults, one day's egress was 1,394 messages and 51,137,042 bytes. 99.43% of the messages were background. B02 made 10,390 Matter round trips with 0 mismatches and sent 1 Last Will notice (the back lock at 22:38), which was told.

### 4.2 Interaction bugs (`results.full_test.ablation`)

| Fix | What broke between layers | With the fix off |
|---|---|---|
| F1 | B07 accepts only signed replies; the day's phones sent unsigned ones | 4 replies refused; 6 scenes fail, B07 check fails |
| F2 | B09 manifests did not declare the day's backup, update and camera-observer senders | backup, update check and the consented clip blocked; B08 check fails |
| F3 | B05 republishes the transcript on `voice/<sat>`; the B01 hub handled it again | 10 utterances handled twice; 31 actions instead of 19; P4 fails |
| F4 | B05 grammar does not know "good night" and "keep the heating on" | both not understood; window and good-night scenes fail |
| F5 | B01 P5 predates B08's second path, consented clips and B04's push purpose | P5 fails 18 of 18 days |
| F6 | the B01 leak rule and the B04 controller both command the valve | two owners send close; an extra leak message |
| F7 | scripted people read B01 prompt wording; B07 words prompts differently | courier and clip-share approvals never answered |
| F8 | B06 `household.yaml` replaces Sam's deny list instead of adding to it | Sam's unlock goes to a parent instead of being refused |
| F9 | B08's consent check runs before the destination check | a phoning-home camera is blocked, but B09 never tells anyone |
| F10 | B03 calls a thermostat holding its setpoint "stuck" | 4 false "I can't trust the thermostat" messages a day |

None of these showed up in any single episode's own tests. F3, F8 and F9 change what the home does: double actions, a weaker teen rule, and a phone-home nobody hears about. F1, F2, F4 and F7 stop things from working. F5 and F10 produce false failures.

### 4.3 A bad update in the twin (`results.fleet.calibration`)

With the manifest typo, the gate blocked the alarm hub in 9 of 9 days. The first block came 2.87-3.23 h after midnight, once 200 messages had gone. The ALARM suite failed in 9 of 9 days, and on average half of each day's alarms reached the centre. The 03:40 smoke alarm was among the blocked ones. Every one of these days produced a failing health report. The 18 good-version days produced none.

### 4.4 The fleet (`results.fleet.results`, 1,000 homes, 20 seeds, means)

| Update / design | Homes that installed it | Of those, after the halt | Homes affected | Halt, hours after start | Halt after first failing report |
|---|---|---|---|---|---|
| loud bad / all at once | 979.0 | 0 | 979.0 | 27.0 | 24.0 h |
| loud bad / rings, operator halt | 43.8 | 0 | 43.8 | 27.2 | 24.0 h |
| loud bad / rings, automatic halt | 7.5 (max 10) | 0 | 7.5 | 3.8 | 0.6 h |
| loud bad / automatic halt, 03:00 install | 7.9 | 1.8 | 7.9 | 4.0 | 0.0 h |
| quiet bad / all at once | 979.0 | 0 | 48.9 | 25.9 | 24.0 h |
| quiet bad / rings, operator halt | 318.6 | 0 | 15.9 | 58.9 | 24.0 h |
| quiet bad / rings, automatic halt | 142.1 | 0 | 6.9 | 46.9 | 12.1 h |
| quiet bad / automatic halt, 03:00 install | 176.6 | 48.0 | 8.8 | 51.0 | 13.2 h |

The good update reached 95% of homes in 5 h all at once, 77 h through the rings and 99 h with the 03:00 window, with no false halt in 60 rollouts. After 14 days, 19.6 homes were still on the old version: the homes that were away the whole time. After each halt, the rollback reached every home on the bad version within the 14 days, including intermittent ones.

### 4.5 What the plane may see (`results.cloud.seed0_none`)

| Offered to the plane | Bytes | Through the B09 gate |
|---|---|---|
| Counts-only health report (12 integers) | 265 | sent |
| Raw trace | 3,583,016 | blocked: over declared message size |
| Audit log | 42,907 | blocked: over declared message size |
| Egress ledger | 466,432 | blocked: over declared message size |
| Safety journal | 2,361 | blocked: over declared message size |
| Health report over 1,024 bytes | 1,025 | blocked: over declared message size |
| Health report with one extra field | 265 | blocked: health report carries extra fields |

The raw bundle holds 14 requests in the household's own words, 49 messages to people verbatim, and 58 trace records naming a person. The household was told about each over-size attempt. The counts were enough to halt the loud bad update within the first ring.

### 4.6 Cloud mapping (no real cloud; `results.cloud.mapping`)

| Stack | Status (facts ledger) | On the home hub | In the cloud plane |
|---|---|---|---|
| AWS IoT Greengrass V2 | V1 support ended Oct 7, 2026; V2 is the migration target (8a) | the whole core: policy and approval gate, action tracker, leak state machine, reading quality, voice, trust policy, alarm hub with both paths, egress gate and ledger | update packages, rings and halt rule; counts-only health reports |
| Azure IoT Hub + DPS + Azure Device Registry | IoT Central retires Sep 20, 2029; Microsoft recommends IoT Hub, DPS and Device Registry (8b) | the same core | device identity and provisioning, rings and halt rule, counts-only health reports |
| Self-hosted MQTT + Home Assistant fleet | no vendor dates; B02 measured a local broker and HA discovery | the same core, plus a local MQTT broker per home | an operator-run update server and health collector on the same `fleet_health` purpose |
| Google Cloud IoT Core | retired Aug 16, 2023 (8c) | - | not an option |

In every column, decisions, approvals, raw traces, audio and clips stay out of the plane. A consented clip goes to the monitoring centre, which is not the plane. The product features of these stacks (deployment, rollout and abort settings, provisioning details) were not verified in this study. The documentation pages could not be opened from this machine, so nothing beyond ledger rows 8a-8c is claimed.

## 5. What held

- With every layer on, the safety and trust suites passed every day under every fault mix. There were 0 unapproved level-3 actions, 0 false "done" messages, 0 silent failures and 100% of alarms at the centre within 60 s.
- The home core stayed at home. Home control in the outage did as well as or better than B01 alone. The cellular path carried the alarm line through the outage.
- Counts were enough. A 265-byte report caught a bad update that blocked the alarm path. The raw data the plane did not need was refused by the rules B09 already had.
- Rings with an automatic halt kept the loud bad update to the canary: 7.5 homes on average, at most 10, halted 3.8 h in.

## 6. What broke

- Ten interaction bugs, listed in 4.2, that no single episode's tests could see. Three of them change what the home does.
- B01's P5, written before B04 and B08, fails every full-test day. It is right about its own rule and wrong about the design that followed. The suite needs the route and the consent to be first-class facts.
- A quiet bad update, affecting 5% of homes, passed the 10-home canary in 20 of 20 rollouts. The automatic halt came in the second or third ring, at 46.9 h on average, after 142.1 homes had installed it (57 to 255).
- A 03:00 install window keeps installing after a halt. Homes that downloaded before the halt installed anyway: 48.0 homes for the quiet update. Install needs a fresh go from the plane, not just a downloaded package.
- Under heavy faults, a courier unlock that really happened but was never confirmed is never relocked. The relock runs only on a confirmed unlock. The front door was still unlocked at 19:05 in 2 of 6 heavy days. This is B01 house code.

## 7. Limits

- Everything ran in simulation, on 6 seeds per mix.
- B02 is a data-model and availability model, not MQTT in the loop.
- Voices are the bundled embeddings. Every utterance was named-tier, so B06's refusals are not exercised here.
- B05 reads the true text, so there are no recognition errors.
- No LLM was run.
- The fleet is a lightweight model. Availability, rings, soak, threshold, operator delay and the quiet update are assumptions. Only the loud update's health signature and the good version's false-failing share come from twin days.
- Health reports in the model arrive hourly and are trusted. A compromised home lying in its counts is not modelled.
- No real cloud was used, and product features beyond the ledger were not verified.

## 8. Reproduce

```
git checkout b10
pip install -r requirements.txt
pytest -q tests/test_b10_fleet.py
python experiments/b10_fleet/run.py         # about 2-3 minutes on 2 cores
```

## Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
