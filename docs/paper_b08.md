# Cameras and the alarm path: evidence, not verdicts, and a second way out, in a digital twin

Intelligent Spaces Lab, Build Track B08 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with simulated devices and a modelled cellular link, by AI agents under human direction. No real network, modem, camera, detector or footage was used. It is not a product and not safety advice. **It is not a certified alarm system** and it was not tested against EN 50136, UL or NFPA requirements. Use it at your own risk and for your own benefit. Life-safety equipment must be listed, installed and monitored by qualified people.

## 1. Question

Season 1 episode eight, "Safe at home", keeps two things apart: what the home observed, and what it might mean. The home offers an observation with its evidence, never a verdict about a stranger. It does not call for help on its own judgment. Smoke goes to a professional monitoring centre, and the centre calls 911.

B01 and B04 left one gap open. With the home internet down, an alarm reached the centre only when the internet came back: 270 s later in B04, 10 hours later in a full-day outage in B01.

This study asks two questions. First, does a modelled second path (cellular) close that gap, and what does it cost in duplicates, traffic and noise? Second, when a front camera mistakes pets and shadows for people, what changes between a confidence threshold, an automatic verdict to the centre, and stating the evidence and asking a person?

## 2. Design under test

- **Alarm path** (`islab/alarm`, from B01, unchanged): SIA DC-09 frames, AES encrypted, sent by the `AlarmHub` through the egress gate to a simulated monitoring centre, with retries and 60 s heartbeats. SIA-DCS format, after the B01 finding on the Contact ID leak code.
- **Cellular link** (`islab/security/cellular.py`, new): a model, not a network. One-way latency 0.05-0.20 s; a radio setup of 0.1-0.8 s after 10 s idle; 2% of messages late by 1-6 s; 1% lost; 120 bytes of overhead per message in each direction; USD 1.00 per MB. Degraded: 30% loss, latency four times, 10% late. **Every one of these values is an assumption**, printed in `results.json`.
- **Egress with a cellular route** (`DualPathEgressGate`, new, subclass of the B01 gate): the cellular route works without the home internet and carries only alarm events and alarm heartbeats. A camera clip leaves only with a person's consent for that one clip, to that one destination, once, and never over cellular. A global "clips allowed" setting is not consent.
- **Dual-path hub** (`DualPathAlarmHub`, new, subclass of the B01 hub): primary heartbeats every 60 s over the internet; cellular heartbeats every hour, every 180 s while the primary has failed. The primary fails supervision after 130 s without an acknowledgement; the cellular path after three unanswered 20 s probes. Every alarm goes first on the preferred path; if it is not acknowledged within 5 s, the second attempt goes at once on the other path. The household is told when the internet path has been down for 300 s, when both paths are down ("If there is a fire, leave and call 911 yourself."), and when the centre can be reached again.
- **Receiver** (`DualPathCentre`, new, subclass of the B01 centre): supervision per path, and a second dedupe layer. Copies with the same sequence number are suppressed as before. A new alarm with the same account, event code and zone as an open incident, with no restore in between and within 30 minutes, is attached to that incident. The centre's actions are a modelled workflow: smoke, call 911 with the address, sensor and time; intrusion, verify with the keyholder, then call 911; leak, call the keyholder. The hub has no way to call 911.
- **Camera** (`islab/security/camera.py`, new): the B01 twin camera with one more label, "pet". Clips stay on the hub; events carry a label, a confidence and a clip reference. An assumed detector table turns what was really there (person, parcel, vehicle, pet, shadow) into labels: for example a pet is labelled "person" 12% of the time with confidence around 0.62, a shadow 7% of the time around 0.50.
- **Three camera designs**: *threshold T* says "Person detected at the front door." for every person label at T or above. *Verdict* raises an intrusion alarm to the centre while armed and says "Intruder detected". *Ask* states the observation with its confidence, what else it could be and where the clip is kept, and asks whether to share the clip with the centre. Yes: an alarm verified by that person, plus that clip. No: nothing leaves. No answer within 180 s: the rule the owner set in advance, here ask the next person, then keep it at home.

## 3. Method

1. Part A: 300 seeded alarms per condition and design (100 smoke, 100 leak, 100 intrusion), 60 for the all-day outage. Seed = trial index; both designs see the same event times. Each run warms up for 600 s.
2. Conditions: normal; 20% loss each way on the internet path; internet down 10 min; internet down all day; internet down 10 min with a degraded cell; internet and cellular both down for the same 10 min; a hub reboot 0.5-4 s after the alarm, down 20-40 s, with the detector's next state report 0-10 s after restart. With the internet down, the WAN drops everything and the egress gate queues alarms and drops heartbeats.
3. Part B: 10 days per condition with two paths and one alarm of each kind per day; we count what the cellular link billed in both directions.
4. Part C: 26 seeded weeks per design. The assumed household's front door sees per day about 6 residents, 3 visitors, 1.5 couriers with a parcel, 6 vehicles, 6 pets and 15 shadows or headlights, plus one unexpected person per week while armed (armed at night 23:00-06:30, away 08:30-17:30 on weekdays). The assumed people answer 85% of asks by day and 50% at night, after about 45 s, and judge the clip right 97% of the time. Ground truth comes from the scene generator, never from messages.
5. An alarm "reached the centre" at its first decoded receipt there.

## 4. Results

### 4.1 Alarms at the centre within 60 s (`experiments/b08_security`)

| Condition | One path | Two paths |
|---|---|---|
| normal | 100% | 100% |
| 20% loss on the internet path | 100% | 100% |
| internet down 10 min | 13.0% | 100% (p95 5.79 s) |
| internet down all day | 0% (p50 40,847 s) | 100% (p95 0.9 s) |
| internet down 10 min, cell degraded | 13.0% | 99.3% (p95 23.93 s, max 102.5 s) |
| internet and cell down 10 min | 13.0% | 13.0% |
| hub reboot | 100% | 100% |

The 13.0% with one path are alarms raised in the last minute of the outage. With both paths down, the second path cannot help; the household was told, and the alarm arrived when a path returned.

### 4.2 Duplicates at the centre

| Condition | Copies received, one path | Copies received, two paths | Extra incidents with incident dedupe | Extra incidents, message dedupe only |
|---|---|---|---|---|
| 20% loss | 82 | 49 | 0 | 0 |
| internet down 10 min | 4 | 3 | 0 | 0 |
| cell degraded | 4 | 158 | 0 | 0 |
| hub reboot | 0 | 0 | 0 | 200 |

The second path adds copies when the cell is slow (158 copies, in 35.3% of runs): the hub retries before the first copy is acknowledged. All of them had the same sequence number and were suppressed. The reboot case is the B04 duplicate: the restarted hub raised the same smoke or leak again with a new sequence number. Message-level dedupe opened a second incident in all 200 smoke and leak runs; incident-level dedupe opened none.

### 4.3 Cellular traffic per day

| Day | Messages billed | Bytes billed | Assumed cost | Primary failovers | Household told about paths |
|---|---|---|---|---|---|
| normal | 48.9 | 10,587 | USD 0.01 | 0 | 0 |
| 20% loss on the internet path | 335.0 | 72,599 | USD 0.07 | 119.9 | 0.6 |
| internet down all day | 985.2 | 213,481 | USD 0.21 | 1 | 1 |
| internet down all day, cell degraded | 1,269.7 | 275,384 | USD 0.28 | 1 | 87.6 |

Almost all of it is supervision: on an all-day outage, 490.2 heartbeats and 5.2 alarm transmissions went up the cellular link per day. 30 of 30 alarms were acknowledged in every day condition.

### 4.4 Camera: false "person" alerts per week, and what the person was asked

| Design | Owner messages a week | False "person" | Sent to the centre | False at the centre | Unexpected person reached the centre |
|---|---|---|---|---|---|
| threshold 0.5 | 79.96 | 8.42 | 0 | 0 | 0% |
| threshold 0.7 | 70.81 | 1.46 | 0 | 0 | 0% |
| threshold 0.9 | 19.42 | 0.08 | 0 | 0 | 0% |
| verdict 0.7 | 34.81 | 0.81 | 34.81 | 33.92 | 88.5% |
| ask 0.5 | 39.62 asks | 4.81 | 1.46 | 0.77 | 69.2% |
| ask 0.7 | 34.81 asks | 0.81 | 1.42 | 0.65 | 76.9% |

A higher threshold trades false alerts for missed people: at 0.9, 56.19 real persons a week were not reported, and the owner heard about the unexpected person within 60 s in 19.2% of tests. The verdict design sent 33.92 false intrusion signals a week to the centre, mostly residents and couriers seen while armed; the centre's incident dedupe folded them into 25.23 incidents. Asking cut what reached the centre to 1.46 a week, each with a person's decision and that one clip. The cost is time and coverage: the unexpected person reached the centre in 69.2% of tests (p50 40.71 s, p95 340.09 s); in 5 of 26 nobody answered and the clip stayed at home.

What the person was asked:

> The front door camera saw something that looks like a person at 00:27 (confidence 0.58). It could also be a pet, a shadow or a reflection. The clip is kept at home (clip/camera.front/0001); you can watch it in the home app. The alarm is set to night. Share this clip with the monitoring centre? Yes / No.

### 4.5 What leaves the house

Alarm runs: the only destination was the monitoring centre's receiver. Camera weeks: clips left only in the ask designs, 1.46 a week at floor 0.5, all with a single-use consent for that clip; 0 without consent. No clip went over cellular. Verdict words in ask messages: 0.

## 5. What held

1. **A second path closes the B01/B04 gap in the twin.** Internet down for 10 minutes or a whole day: 100% of alarms at the centre within 60 s, against 13.0% and 0% with one path.
2. **Dedupe belongs to the receiver, and by incident.** Copies from two paths were always suppressed by sequence number; the reboot re-raise needed the incident layer.
3. **Supervision is cheap on a normal day** and the main cost on a bad one: 10,587 bytes a day normally, 213,481 on an all-day outage, at the assumed tariff USD 0.01 and USD 0.21.
4. **Asking keeps the decision with people.** Fewer than two camera signals a week reached the centre, each one decided by a person, against 34.81 automatic verdicts.

## 6. What broke

1. **A flapping backup is noisy.** With the internet down and a degraded cell, the cellular path failed supervision 50.2 times a day, the household got 87.6 path messages a day despite a 300 s debounce, and the centre raised "all paths lost" 63 times in 10 days. Open.
2. **A lossy primary flaps.** At 20% loss the primary failed supervision 119.9 times a day. Alarms were unaffected and the household was told 0.6 times a day, but the hub's view of its own path is not stable. Open.
3. **Asking is slower and can go unanswered.** The unexpected person reached the centre in 69.2% of tests at floor 0.5. The owner can set a different rule in advance; we tested only "ask the next person, then keep it at home". A real break-in would also trip door and window sensors, which raise their own alarm; the camera test does not model them.
4. **Our own bugs**: the camera observer's wildcard never matched; a "path is back" message followed a "both down" message; a shared clip reached the centre before its alarm and attached to nothing. All three fixed, with tests; re-running after the last fix gave identical results.

## 7. Limits

Everything ran in simulation. The cellular link, the detector, the scene rates and the people are models with assumed values; the false "person" counts follow from those assumptions and are not measurements of any detector. The household's clip choice, the centre's workflow and the operator's time to act are modelled, not observed. Alarms in part A are injected detector events, not the full 15-device home. Push messages to people away still need the internet; a cellular SMS or call path to people is not modelled. Incident dedupe can merge a genuinely new alarm in the same zone within 30 minutes if no restore arrives. For orientation only, an alarm-signalling provider lists catastrophic failure reporting for dual-path categories from 25 hours down to 3 minutes ([CSL](https://www.csl-group.com/?p=5389)); we did not test against any category. This is not a certified alarm system.

## 8. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b08_security.py
python experiments/b08_security/run.py      # about 2 minutes on 2 idle cores, 3-4 when shared
```

## 9. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
