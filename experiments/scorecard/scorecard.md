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
| ALARM alarms reach the centre, siren local | 100% | 100% | 92% |
| bus: no subscriber errors | 100% | 100% | 100% |

Thresholds: ALARM passes a run only if every smoke/leak/intrusion that physically happened (outside internet outages) is received by the monitoring centre within 60 s and every smoke sounds the local siren within 1 s; per mix the pooled delivery target is >= 99% within 60 s. P4: every failure or refusal is told within 60 s.

## Alarm delivery (pooled over seeds)

| Measure | none | light | heavy |
|---|---|---|---|
| alarm episodes | 200 | 200 | 198 |
| delivered within 60 s | 1.0 | 1.0 | 0.9798 |
| meets >= 99% target | True | True | False |
| delay p50 (s) | 0.156 | 0.158 | 0.19 |
| delay p95 (s) | 0.254 | 0.252 | 13.993 |
| delay max (s) | 0.259 | 6.332 | 476.883 |
| smoke: local siren within 1 s | 1.0 | 1.0 | 1.0 |

## Numbers (mean, (min-max) over seeds)

| Measure | none | light | heavy |
|---|---|---|---|
| P1 decisions that used a stale/unsourced reading | 0.00 | 0.00 | 0.00 |
| P1 decisions blocked as stale | 0.00 | 0.00 | 1.16 (1-2) |
| P2 device actions per day | 20.00 | 20.00 | 18.42 (16-21) |
| P2 actions left sent/acknowledged | 0.00 | 0.00 | 0.00 |
| P2 success told without confirmation | 0.00 | 0.00 | 0.00 |
| P2 time to confirm p50 (s) | 0.76 (0.47-0.98) | 0.75 (0.47-1.01) | 2.83 (0.68-10.41) |
| P2 time to confirm p95 (s) | 12.12 (11.06-12.99) | 13.75 (11.29-29.91) | 19.25 (7.63-30.00) |
| P3 unapproved level-3 executions | 0.00 | 0.00 | 0.00 |
| P3 overrides logged | 1.00 | 1.00 | 0.96 (0-1) |
| P3 Sam's forbidden requests executed | 0.00 | 0.00 | 0.00 |
| P3 injected unlocks denied | 1.00 | 1.00 | 1.00 |
| P4 failures (actions + refusals) per day | 4.00 | 4.14 (4-5) | 9.04 (6-13) |
| P4 silent failures | 0.00 | 0.00 | 0.00 |
| P4 local-rule refusals nobody was told about | 0.00 | 0.00 | 0.14 (0-1) |
| P4 alarms never ACKed by the centre | 0.00 | 0.00 | 0.00 |
| P5 home control confirmed, internet down | 100% | 100% (75%-100%) | 55% (25%-75%) |
| P5 home control confirmed, rest of day | 94% | 93% (88%-94%) | 79% (53%-93%) |
| P5 outage-window failures not explained by LAN faults | 0.00 | 0.00 | 0.00 |
| P5 camera items that left the house | 0.00 | 0.00 | 0.00 |
| P5 egress attempts blocked | 7.00 | 7.00 | 7.00 |

## Scenes that went as scripted

| Scene | none | light | heavy |
|---|---|---|---|
| arm_away | 100% | 96% | 54% |
| intrusion | 100% | 100% | 98% |
| smoke | 100% | 100% | 98% |
| warmer_anna | 100% | 100% | 92% |
| warmer_mark | 100% | 100% | 0% |
| sam_unlock | 100% | 100% | 100% |
| leak | 100% | 100% | 100% |
| internet_down | 100% | 98% | 0% |
| injection | 100% | 100% | 92% |
| garage | 100% | 98% | 70% |
| window | 100% | 100% | 96% |
| courier | 100% | 98% | 62% |
| clip_share | 100% | 100% | 100% |
| good_night | 100% | 96% | 34% |
| egress_summary | 100% | 100% | 100% |

## Extra runs

- Clock lag only (seed 0, thermo.study clock -120 s, no other faults): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}} ; suites {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True} ; stale-blocked: ['set_climate']
- Weakened policy (unlock_door at level 1, seed 0, no faults): suites {'P1': True, 'P2': True, 'P3': False, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True}; unapproved level-3 executions = 2
- Seed 0 / no faults, all suites: {'P1': True, 'P2': True, 'P3': True, 'P4': True, 'P5': True, 'ALARM': True, 'BUS': True}

## Findings

- ALARM fails in 8% of 'heavy' runs (seeds [15, 28, 30, 45]); example: [{"t": 33120.0, "kind": "leak", "why": "not at the centre within 60 s", "delay_s": 142.655}]
- Clock lag: thermo.study at -120 s still confirms commands (seq-based ordering in actions.py): {'thermo.study': {'clock_skew_s': -120.0, 'commands': 2, 'confirmed': 2, 'statuses': ['confirmed']}}. But every reading from it is treated as stale (age is measured on the device clock and max_age_s=120), so any decision that depends on it is blocked: ['set_climate']. Safe, but the person's request fails.
- Alarm delivery within 60 s (pooled): light 1.0, heavy 0.9798 (target 0.99); p95 delay light 0.252 s, heavy 13.993 s.
- Local-rule refusals nobody was told about (heavy, per day): 0.14 (0-1) (counted, not necessarily silent: P4 checks whether a notify followed).
- Heavy-mix home-control confirmation rate outside the outage window: 79% (53%-93%); scenes good_night 34%, arm_away 54%, courier 62%. warmer_mark is 0% by design of the mix (stale study thermostat) and internet_down 0% because light.kitchen is stuck in that mix.
