# Before / after: alarm recovery, command resend, local-rule refusals

Same 50 seeds x 3 fault mixes, same day script, same suites (islab/suites/principles.py incl. the new ALARM suite). Before = code snapshot before the Nov 19 fixes (results_before.json, section `recomputed_with_alarm_suite`); after = results.json.

Thresholds: ALARM passes a run only if every smoke/leak/intrusion that physically happened (outside internet outages) reaches the monitoring centre within 60 s and every smoke sounds the local siren within 1 s; per mix the pooled delivery target is >= 99 % within 60 s. P4 passes only if every failed/timed-out action and every refusal (including local-rule refusals) is told to someone within 60 s.

Fixes: (a) AlarmHub also raises from detector state (smoke/leak true, motion true while armed, lock locked->unlocked while armed with no hub unlock command), one alarm per episode; smoke and leak detectors are wired straight to the LocalSiren. (b) ActionTracker resends the same cmd_id when no ack arrives within 3 s (max 2 resends); twin devices now treat a repeated cmd_id as idempotent (re-ack, no second actuation). (c) Refused local rules notify the default person in plain words.

## Suite pass rates

### P1 pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 100.0% | 100.0% |

### P2 pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 100.0% | 100.0% |

### P3 pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 100.0% | 100.0% |

### P4 pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 90.0% | 100.0% |

### P5 pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 100.0% | 100.0% |

### ALARM pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 92.0% | 100.0% |
| heavy | 54.0% | 92.0% |

### BUS pass rate

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 100.0% | 100.0% |
| heavy | 100.0% | 100.0% |

## Alarm path (pooled over seeds)

### Alarms delivered to the centre within 60 s

Target >= 99 %.

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 98.0% | 100.0% |
| heavy | 81.9% | 98.0% |

### Alarm episodes counted (intrusions count only if the panel was physically armed)

| Mix | Before | After |
|---|---|---|
| none | 200 | 200 |
| light | 196 | 200 |
| heavy | 182 | 198 |

### Alarm delay p50 (s)

Delay statistics cover delivered alarms only: before, the alarms that never arrived are missing from them, which is why before looks faster at p95/max.

| Mix | Before | After |
|---|---|---|
| none | 0.156 | 0.156 |
| light | 0.158 | 0.158 |
| heavy | 0.177 | 0.19 |

### Alarm delay p95 (s)

| Mix | Before | After |
|---|---|---|
| none | 0.254 | 0.254 |
| light | 0.253 | 0.252 |
| heavy | 6.498 | 13.993 |

### Alarm delay max (s)

| Mix | Before | After |
|---|---|---|
| none | 0.259 | 0.259 |
| light | 6.332 | 6.332 |
| heavy | 13.839 | 476.883 |

### Smoke: local siren within 1 s

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 98.0% | 100.0% |
| heavy | 76.0% | 100.0% |

## Home control

### Home control confirmed, outside outage (mean over seeds)

| Mix | Before | After |
|---|---|---|
| none | 93.8% | 93.8% |
| light | 91.5% | 93.0% |
| heavy | 63.5% | 78.7% |

### Home control confirmed, internet down 12:00-14:00 (mean)

| Mix | Before | After |
|---|---|---|
| none | 100.0% | 100.0% |
| light | 98.0% | 99.5% |
| heavy | 55.0% | 55.0% |

### Time to confirm p50 (s, mean of per-day p50)

| Mix | Before | After |
|---|---|---|
| none | 0.7613 | 0.7613 |
| light | 0.7383 | 0.7542 |
| heavy | 1.9574 | 2.8253 |

### Time to confirm p95 (s, mean of per-day p95)

| Mix | Before | After |
|---|---|---|
| none | 12.1201 | 12.1201 |
| light | 13.4269 | 13.7537 |
| heavy | 18.9564 | 19.2478 |

### Local-rule refusals per day (mean)

Counted refusals; before, none of them was told (P4 failures); after, each is told.

| Mix | Before | After |
|---|---|---|
| none | 0 | 0 |
| light | 0 | 0 |
| heavy | 0.1 | 0.14 |

### P4 silent failures per day (mean)

| Mix | Before | After |
|---|---|---|
| none | 0 | 0 |
| light | 0 | 0 |
| heavy | 0.1 | 0 |

## Scenes as scripted (heavy mix)

| Scene | Before | After |
|---|---|---|
| arm_away | 26.0% | 54.0% |
| intrusion | 58.0% | 98.0% |
| smoke | 76.0% | 98.0% |
| warmer_anna | 78.0% | 92.0% |
| warmer_mark | 0.0% | 0.0% |
| sam_unlock | 100.0% | 100.0% |
| leak | 100.0% | 100.0% |
| internet_down | 0.0% | 0.0% |
| injection | 82.0% | 92.0% |
| garage | 64.0% | 70.0% |
| window | 40.0% | 96.0% |
| courier | 36.0% | 62.0% |
| clip_share | 100.0% | 100.0% |
| good_night | 14.0% | 34.0% |
| egress_summary | 100.0% | 100.0% |

## What changed, what is not solved

Recovering alarms from detector state lifted heavy-loss delivery within 60 s from 81.9% to 98.0% and light-loss delivery to 100.0%, and wiring the detectors to the siren made the local siren 100 % in every mix (recovered alarms form the slow tail: p95/max rose because they now arrive late instead of never). Command resends raised heavy-mix home-control confirmation from 63.5% to 78.7%, and local-rule refusals are now told, so P4 passes in every mix. Still not solved: heavy loss misses the 99 % alarm target, because when a detector's event and its state copy are both lost the hub only hears at the next sensor heartbeat (300 s), so the alarm arrives late (max 476.883 s); a retransmitting radio, a faster heartbeat in alarm state or a hard-wired detector would close that. There is no second (cellular) path to the centre, so during the internet outage alarms only queue; a lost state report after an ack (no resend then) still leaves actions timed out, and a lagging clock still makes a device permanently 'stale'.
