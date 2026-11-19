# b03_readings: readings you can trust

**Run:** `python experiments/b03_readings/run.py` (10 seeds x 7 virtual days, 2 processes, about 2 minutes wall time on 2 cores).
**What:** one simulated week of the B01 twin with reading faults injected. Three hub views read the same bus at the same time and the same decision code runs on each:

- **naive**: latest arrival wins.
- **timestamp**: the B01 `StateStore`. Dedupe by event id, order and freshness by device timestamp.
- **quality**: the B03 layer `islab/readings/quality.py`. Per reading: device, seq, device ts, hub receive time, event id. Checks: sourced, duplicate (event id and seq), out of order (seq), clock skew (rolling median, corrected), range and rate of change (hold, then let the next report decide), stuck, stale (receive time and corrected ts), latched alarm events, two-source disagreement.

Decisions: kitchen heating every 60 s, overheat alert on every temperature report, leak shut-off and valve reopen, night light on motion, doorbell notice, 22:00 house check. They are recorded, not executed.

## Results (per simulated week, mean of 10 seeds)

| Measure | naive | timestamp | quality |
|---|---|---|---|
| Decisions made | 10,201 | 10,185 | 10,187 |
| Decisions on bad data | 637.3 (607-660) | 605.2 (584-616) | 2 (2-2) |
| ...stuck sensor | 602.8 | 602.8 | 0 |
| ...reordered message | 16.2 | 0 | 0 |
| ...duplicate copy | 13.9 | 0 | 0 |
| ...spike | 1.4 | 1.4 | 0 |
| ...dead battery (stale) | 2 | 0 | 0 |
| ...one-shot leak | 1 | 1 | 0 |
| ...real change held one report | 0 | 0 | 2 |
| False alarms | 4.7 | 2 | 0 |
| Water running with the valve open during a leak | 112 min | 112 min | 0 min |
| Valve closed while dry after a leak | 5.4 min | 5.4 min | 133.6 min (108-153) |
| Dead battery: first notice | never | 782 s (755-809) | 0 s (last gasp) |
| Dead battery: reading unusable | never | 782 s | 26 s (next decision) |
| Stuck sensor: detected | never | never | 19.2 min (18.0-19.9) |
| One-shot leak: disputed | never | never | 143 s (39-257) |
| Good readings rejected at ingest | 0 | 0 | 4 |
| Decision ticks with a good primary reading thrown away | 0 | 6,585 (110 h) | 0 |
| Decision ticks refused | 0 | 4.8 | 247.8 |
| Refusal episodes told within 60 s | n/a | 48 of 48 | 30 of 30 |

Clock drift: the layer estimated temp.kitchen's offset at 335.7 s against a true 336 s at the end of the week and told the person at hour 30.1 (60 s threshold). The timestamp view called the same sensor stale between hour 45 and 67 (median 59) and from then on used the thermostat instead: a false alarm and about 110 hours of good readings discarded per week.

## What it shows

1. **The stuck sensor is the expensive fault.** It looks perfect: fresh timestamps, plausible value, no duplicates. Both baselines made about 603 heating decisions per week on it. The layer saw 20 minutes of identical values, switched to the second thermometer and told the person.
2. **Timestamps fix transport faults only.** The timestamp view removed every duplicate and reordered decision (30 per week for naive) but nothing a sensor lies about: stuck, spike, one-shot leak.
3. **A leak event must not be cleared by a heartbeat that never said wet.** Naive and timestamp reopened the valve about 5 minutes after the first dry heartbeat; water ran for 112 minutes on average. The layer held the valve closed, disputed the reading within 143 s on average, and told the person.
4. **The last gasp is the earliest stale signal.** A dying sensor reported battery 0; the layer flagged it at once. Waiting for missed heartbeats took 782 s; naive never noticed and reported "all clear" twice on a dead sensor.
5. **Device timestamps alone punish a drifting clock.** 2 s per hour is enough for a timestamp check to throw away a good sensor after two to three days. Estimating skew from receive times keeps it.
6. **The cost is real and visible.** The valve stayed closed for 134 minutes after the floor was dry, waiting for a person (07:10 reply). Heating was decided a minute late twice when a real 5 degree step was held for one report. Every refusal was told.

## Limitations

Simulation only; one of each fault per week at scripted times (+-30 min). Open loop: nothing is executed, heating does not change the room. 0.01 degC resolution with 0.05 degC noise makes stuck detection easy; a coarse sensor in a stable room can repeat a value for hours. The rate limit would delay a real fire's temperature rise by one report: fire detection belongs to smoke alarms. Duplicates and reordering are i.i.d., real radios fail in bursts. The person's reply time is scripted. Thresholds were set before the run; others give other numbers. `results.json` has every seed.
