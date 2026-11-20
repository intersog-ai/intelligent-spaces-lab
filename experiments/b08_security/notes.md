# B08 security experiment: notes

`python experiments/b08_security/run.py` (3,720 alarm runs, 40 simulated days, 208 simulated camera weeks; about 2 minutes on 2 idle cores, 3-4 minutes when the machine is shared). Every number below is from `results.json`. Virtual time only. Nothing here touched a real network, a real modem or real footage.

## What is compared

| Part | Designs |
|---|---|
| A. Alarm path | `one_path`: the B01 `AlarmHub`, home internet only. `two_paths`: `DualPathAlarmHub`, home internet plus a modelled cellular link. Both report to `DualPathCentre`. |
| B. Cellular per day | `two_paths` for 24 h with one smoke, one leak and one intrusion alarm per day. |
| C. Camera week | `threshold_T`: "Person detected" to the owner for every person label at confidence T or more. `verdict_0.7`: while armed, a person label at 0.7 or more raises an intrusion alarm to the centre and says "Intruder detected". `ask_F`: while armed, a person label at F or more is stated as an observation with its confidence, alternatives and clip location, and the owner is asked whether to share that clip with the centre. |

## A. Alarms at the centre within 60 s

300 alarms per cell (100 smoke, 100 leak, 100 intrusion); 60 per cell for the all-day outage.

| Condition | one path | two paths | two paths, latency p50 / p95 / max |
|---|---|---|---|
| normal | 100% | 100% | 0.17 / 0.25 / 0.26 s |
| 20% loss on the internet path | 100% (p95 6.59 s) | 100% | 0.19 / 5.78 / 13.1 s |
| internet down 10 min | 13.0% (p50 293.82 s) | 100% | 0.68 / 5.79 / 7.36 s |
| internet down all day | 0% (p50 40,847 s, about 11.3 h) | 100% | 0.59 / 0.9 / 1.51 s |
| internet down 10 min, cell degraded | 13.0% | 99.3% | 3.68 / 23.93 / 102.5 s |
| internet and cell down 10 min | 13.0% | 13.0% | 294.81 / 578.58 / 600.88 s |
| hub reboot after the alarm | 100% | 100% | 0.17 / 0.25 / 0.26 s |

With one path, the 13.0% inside a 10 minute outage are the alarms raised in its last minute. With the internet down, the two-path hub sent the first copy over cellular in 300 of 300 runs.

## A. Duplicates at the centre

| Condition | copies received (one / two paths) | runs with copies (two paths) | extra incidents, incident dedupe | extra incidents, message dedupe only |
|---|---|---|---|---|
| 20% loss | 82 / 49 | 15.7% | 0 | 0 |
| internet down 10 min | 4 / 3 | 1.0% | 0 | 0 |
| cell degraded | 4 / 158 | 35.3% | 0 | 0 |
| hub reboot | 0 / 0 | 0% | 0 | 200 (every smoke and leak run, both designs) |

Copies (same sequence number) are suppressed by the B01 message-level dedupe. The reboot case is the B04 bug: a restarted hub raises the same leak or smoke with a new sequence number, so the message-level dedupe opened a second incident in 200 of 200 smoke and leak runs. The incident-level dedupe (same account, event code and zone, no restore in between, within 30 min) attached all 200 to the open incident. Intrusion runs did not re-raise, because the lock does not resend a manual unlock.

Centre actions (modelled workflow): smoke `centre_calls_911_with_address_sensor_time`, intrusion `centre_verifies_with_keyholder_then_calls_911`, leak `centre_calls_keyholder`. The only egress destination in every run was `receiver.monitoring.example`; no destination containing "911".

## B. Cellular traffic per day (10 days per condition, both directions, with assumed overhead)

| Day | messages billed | bytes billed | assumed cost | primary failovers | household messages about paths |
|---|---|---|---|---|---|
| normal | 48.9 | 10,587 | USD 0.01 | 0 | 0 |
| 20% loss on the internet path | 335.0 | 72,599 | USD 0.07 | 119.9 | 0.6 |
| internet down all day | 985.2 | 213,481 | USD 0.21 | 1 | 1 |
| internet down all day, cell degraded | 1,269.7 | 275,384 | USD 0.28 | 1 | 87.6 |

All 30 alarms acknowledged in every day condition. No camera clip went over cellular (the route refuses clips).

## C. Camera week (26 weeks per design; the same scenes for every design)

Per week the generator made 264.58 scenes; the detector model labelled 85.38 as a person, of which 13.85 were not a person (pets, shadows, vehicles).

| Design | owner messages a week | false "person" | to the centre | false to the centre | unexpected person: owner told in 60 s / reached centre |
|---|---|---|---|---|---|
| threshold 0.5 | 79.96 | 8.42 | 0 | 0 | 92.3% / 0% |
| threshold 0.7 | 70.81 | 1.46 | 0 | 0 | 88.5% / 0% |
| threshold 0.9 | 19.42 | 0.08 | 0 | 0 | 19.2% / 0% |
| verdict 0.7 | 34.81 | 0.81 | 34.81 | 33.92 | 88.5% / 88.5% |
| ask 0.5 | 39.62 first asks | 4.81 | 1.46 | 0.77 | 92.3% / 69.2% |
| ask 0.7 | 34.81 first asks | 0.81 | 1.42 | 0.65 | 88.5% / 76.9% |

Threshold 0.7 missed 6.19 real persons a week; 0.9 missed 56.19. In the ask designs, false signals to the centre are the person's own wrong answers (assumed 3% error). Clips left the house 1.46 a week (ask 0.5), every one with a single-use consent for that clip; 0 without. Ask messages with a verdict word: 0. Verdict design: 905 messages with a verdict word over 26 weeks.

What the person was asked (first ask of the first week):

> The front door camera saw something that looks like a person at 00:27 (confidence 0.58). It could also be a pet, a shadow or a reflection. The clip is kept at home (clip/camera.front/0001); you can watch it in the home app. The alarm is set to night. Share this clip with the monitoring centre? Yes / No.

Unexpected person, ask 0.5 (26 tests): 18 shared by a person, 5 nobody answered and it stayed at home, 1 person said no, 2 not reported as a person by the detector. Time to the centre when shared: p50 40.71 s, p95 340.09 s.

## Fixed along the way

1. The observer subscribed to `event/camera.+`; the bus wildcard matches a whole segment only, so it heard nothing. Now `event/+` filtered by device.
2. "The internet path is back" was sent after an episode where the household had only been told both paths were down. Fixed with a separate flag.
3. A shared clip reached the centre before its alarm and was attached to no incident. The centre now holds a clip for up to 300 s until its burglary incident opens (test `test_yes_shares_that_one_clip_and_raises_a_verified_alarm`). This fix changed no number in results.json (re-run, identical).

## Still open

1. With the internet down and the cell degraded, the hub flips between "both down" and "reachable" about 50 times a day and told the household 87.6 times a day, even with a 300 s debounce on "reached again". The centre raised "all paths lost" 63 times over those 10 days.
2. Under 20% loss the primary failed supervision 119.9 times a day. Alarms were fine and the household was told 0.6 times a day, but the hub's view of its own path is noisy.
3. Incident dedupe will merge a genuinely new alarm in the same zone into an open incident for 30 min if no restore arrives.
