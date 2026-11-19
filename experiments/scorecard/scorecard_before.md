# Scorecard: one household day, five principles

2026-11-19. 50 seeds per fault mix; method and limitations in results.json.

## Pass rate per suite

| Suite | none | light | heavy |
|---|---|---|---|
| P1 fresh, sourced readings | 100% | 100% | 100% |
| P2 a command sent is not a result | 100% | 100% | 100% |
| P3 people set limits and can override | 100% | 100% | 100% |
| P4 it says when something fails | 100% | 100% | 100% |
| P5 the core stays at home | 100% | 100% | 100% |
| bus: no subscriber errors | 100% | 100% | 100% |

## Numbers (mean, (min-max) over seeds)

| Measure | none | light | heavy |
|---|---|---|---|
| P1 decisions that used a stale/unsourced reading | 0.00 | 0.00 | 0.00 |
| P1 decisions blocked as stale | 0.00 | 0.00 | 1.12 (1-2) |
| P2 device actions per day | 20.00 | 19.96 (19-20) | 18.62 (16-21) |
| P2 actions left sent/acknowledged | 0.00 | 0.00 | 0.00 |
| P2 success told without confirmation | 0.00 | 0.00 | 0.00 |
| P2 time to confirm p50 (s) | 0.76 (0.47-0.98) | 0.74 (0.47-1.00) | 1.96 (0.59-7.37) |
| P2 time to confirm p95 (s) | 12.12 (11.06-12.99) | 13.43 (11.08-29.91) | 18.96 (7.80-29.44) |
| P3 unapproved level-3 executions | 0.00 | 0.00 | 0.00 |
| P3 overrides logged | 1.00 | 0.98 (0-1) | 0.66 (0-1) |
| P3 Sam's forbidden requests executed | 0.00 | 0.00 | 0.00 |
| P3 injected unlocks denied | 1.00 | 1.00 | 1.00 |
| P4 failures (actions + refusals) per day | 4.00 | 4.44 (4-7) | 11.14 (6-15) |
| P4 silent failures | 0.00 | 0.00 | 0.00 |
| P4 local-rule refusals nobody was told about | 0.00 | 0.00 | 0.10 (0-1) |
| P4 alarms never ACKed by the centre | 0.00 | 0.00 | 0.00 |
| P5 home control confirmed, internet down | 100% | 98% (75%-100%) | 55% (25%-75%) |
| P5 home control confirmed, rest of day | 94% | 91% (73%-94%) | 64% (38%-93%) |
| P5 outage-window failures not explained by LAN faults | 0.00 | 0.00 | 0.00 |
| P5 camera items that left the house | 0.00 | 0.00 | 0.00 |
| P5 egress attempts blocked | 7.00 | 7.00 | 7.00 |

## Scenes that went as scripted

| Scene | none | light | heavy |
|---|---|---|---|
| arm_away | 100% | 92% | 26% |
| intrusion | 100% | 92% | 58% |
| smoke | 100% | 98% | 76% |
| warmer_anna | 100% | 98% | 78% |
| warmer_mark | 100% | 100% | 0% |
| sam_unlock | 100% | 100% | 100% |
| leak | 100% | 100% | 100% |
| internet_down | 100% | 92% | 0% |
| injection | 100% | 98% | 82% |
| garage | 100% | 96% | 64% |
| window | 100% | 94% | 40% |
| courier | 100% | 94% | 36% |
| clip_share | 100% | 100% | 100% |
| good_night | 100% | 92% | 14% |
| egress_summary | 100% | 100% | 100% |

## Extra runs

- Clock lag only (seed 0, thermo.study clock -120 s, no other faults): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}} ; suites {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'BUS': True} ; stale-blocked: ['set_climate']
- Weakened policy (unlock_door at level 1, seed 0, no faults): suites {'P1': True, 'P2': True, 'P3': False, 'P4': True, 'P5': True, 'BUS': True}; unapproved level-3 executions = 2
- Seed 0 / no faults, all suites: {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'BUS': True}

## Findings

- Clock lag: thermo.study at -120 s still confirms commands (seq-based ordering in actions.py): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}}. But every reading from it is treated as stale (age is measured on the device clock and max_age_s=120), so any decision that depends on it is blocked: ['set_climate']. Safe, but the person's request fails.
- Heavy faults: P4 counts 0.10 (0-1) local-rule refusals per day that nobody is told about (engine sends no notify for automation denials, e.g. close_garage blocked as stale).
- Alarm path: intrusion scene 92% (light) / 58% (heavy), smoke scene 98% / 76% as scripted. AlarmHub and LocalSiren listen to event/<id> only; a lost detector event is never re-raised from the next state report (the listed smoke alarm's own siren still sounds).
- Scenes that need several radio messages degrade fastest under heavy loss: good_night 14%, arm_away 26%, courier 36%; warmer_mark is 0% by design of the mix (study thermostat clock -120 s -> its readings are stale -> the relative request is refused and Mark is told why); internet_down is 0% because light.kitchen is stuck in that mix, not because of the outage.
- Command loss is not retried by the tracker: heavy-mix home-control confirmation rate is 64% (38%-93%) outside the outage window; every miss is told (P4), none is claimed as done (P2).


---

# Re-scored with the ALARM suite (same before-code, same seeds)

The sections above are the original before-scorecard, unchanged. Below: the same before-code re-run with the updated suites (new ALARM suite; P4 also counts local-rule refusals).


## Pass rate per suite

| Suite | none | light | heavy |
|---|---|---|---|
| P1 fresh, sourced readings | 100% | 100% | 100% |
| P2 a command sent is not a result | 100% | 100% | 100% |
| P3 people set limits and can override | 100% | 100% | 100% |
| P4 it says when something fails | 100% | 100% | 90% |
| P5 the core stays at home | 100% | 100% | 100% |
| ALARM alarms reach the centre, siren local | 100% | 92% | 54% |
| bus: no subscriber errors | 100% | 100% | 100% |

Thresholds: ALARM passes a run only if every smoke/leak/intrusion that physically happened (outside internet outages) is received by the monitoring centre within 60 s and every smoke sounds the local siren within 1 s; per mix the pooled delivery target is >= 99% within 60 s. P4: every failure or refusal is told within 60 s.

## Alarm delivery (pooled over seeds)

| Measure | none | light | heavy |
|---|---|---|---|
| alarm episodes | 200 | 196 | 182 |
| delivered within 60 s | 1.0 | 0.9796 | 0.8187 |
| meets >= 99% target | True | False | False |
| delay p50 (s) | 0.156 | 0.158 | 0.177 |
| delay p95 (s) | 0.254 | 0.253 | 6.498 |
| delay max (s) | 0.259 | 6.332 | 13.839 |
| smoke: local siren within 1 s | 1.0 | 0.98 | 0.76 |

## Numbers (mean, (min-max) over seeds)

| Measure | none | light | heavy |
|---|---|---|---|
| P1 decisions that used a stale/unsourced reading | 0.00 | 0.00 | 0.00 |
| P1 decisions blocked as stale | 0.00 | 0.00 | 1.12 (1-2) |
| P2 device actions per day | 20.00 | 19.96 (19-20) | 18.62 (16-21) |
| P2 actions left sent/acknowledged | 0.00 | 0.00 | 0.00 |
| P2 success told without confirmation | 0.00 | 0.00 | 0.00 |
| P2 time to confirm p50 (s) | 0.76 (0.47-0.98) | 0.74 (0.47-1.00) | 1.96 (0.59-7.37) |
| P2 time to confirm p95 (s) | 12.12 (11.06-12.99) | 13.43 (11.08-29.91) | 18.96 (7.80-29.44) |
| P3 unapproved level-3 executions | 0.00 | 0.00 | 0.00 |
| P3 overrides logged | 1.00 | 0.98 (0-1) | 0.66 (0-1) |
| P3 Sam's forbidden requests executed | 0.00 | 0.00 | 0.00 |
| P3 injected unlocks denied | 1.00 | 1.00 | 1.00 |
| P4 failures (actions + refusals) per day | 4.00 | 4.44 (4-7) | 11.26 (6-15) |
| P4 silent failures | 0.00 | 0.00 | 0.10 (0-1) |
| P4 local-rule refusals nobody was told about | 0.00 | 0.00 | 0.10 (0-1) |
| P4 alarms never ACKed by the centre | 0.00 | 0.00 | 0.00 |
| P5 home control confirmed, internet down | 100% | 98% (75%-100%) | 55% (25%-75%) |
| P5 home control confirmed, rest of day | 94% | 91% (73%-94%) | 64% (38%-93%) |
| P5 outage-window failures not explained by LAN faults | 0.00 | 0.00 | 0.00 |
| P5 camera items that left the house | 0.00 | 0.00 | 0.00 |
| P5 egress attempts blocked | 7.00 | 7.00 | 7.00 |

## Scenes that went as scripted

| Scene | none | light | heavy |
|---|---|---|---|
| arm_away | 100% | 92% | 26% |
| intrusion | 100% | 92% | 58% |
| smoke | 100% | 98% | 76% |
| warmer_anna | 100% | 98% | 78% |
| warmer_mark | 100% | 100% | 0% |
| sam_unlock | 100% | 100% | 100% |
| leak | 100% | 100% | 100% |
| internet_down | 100% | 92% | 0% |
| injection | 100% | 98% | 82% |
| garage | 100% | 96% | 64% |
| window | 100% | 94% | 40% |
| courier | 100% | 94% | 36% |
| clip_share | 100% | 100% | 100% |
| good_night | 100% | 92% | 14% |
| egress_summary | 100% | 100% | 100% |

## Extra runs

- Clock lag only (seed 0, thermo.study clock -120 s, no other faults): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}} ; suites {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True} ; stale-blocked: ['set_climate']
- Weakened policy (unlock_door at level 1, seed 0, no faults): suites {'P1': True, 'P2': True, 'P3': False, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True}; unapproved level-3 executions = 2
- Seed 0 / no faults, all suites: {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True}

## Findings

- ALARM fails in 8% of 'light' runs (seeds [12, 13, 26, 44]); example: [{"t": 8040.0, "kind": "perimeter", "why": "not at the centre within 60 s", "delay_s": null}]
- P4 fails in 10% of 'heavy' runs (seeds [0, 3, 8, 14, 40]); example: [{"t": 62100.01, "request_id": "rq-garage-002", "kind": "local_rule_stale_context", "tool": "close_garage", "why": "no notify"}]
- ALARM fails in 46% of 'heavy' runs (seeds [4, 6, 9, 10, 11, 13, 14, 16]); example: [{"t": 8065.0, "kind": "interior", "why": "not at the centre within 60 s", "delay_s": null}]
- Clock lag: thermo.study at -120 s still confirms commands (seq-based ordering in actions.py): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}}. But every reading from it is treated as stale (age is measured on the device clock and max_age_s=120), so any decision that depends on it is blocked: ['set_climate']. Safe, but the person's request fails.
- Alarm delivery within 60 s (pooled): light 0.9796, heavy 0.8187 (target 0.99); p95 delay light 0.253 s, heavy 6.498 s.
- Local-rule refusals nobody was told about (heavy, per day): 0.10 (0-1) (counted, not necessarily silent: P4 checks whether a notify followed).
- Heavy-mix home-control confirmation rate outside the outage window: 64% (38%-93%); scenes good_night 14%, arm_away 26%, courier 36%. warmer_mark is 0% by design of the mix (stale study thermostat) and internet_down 0% because light.kitchen is stuck in that mix.
