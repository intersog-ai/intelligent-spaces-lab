# Readings you can trust: a reading-quality layer for a home assistant, tested in a digital twin

Intelligent Spaces Lab, Build Track B03 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **No guarantee.** This is open research material. It was produced in a simulator, with simulated devices and scripted faults, by AI agents under human direction. It is not a product, not a certified alarm system and not safety advice. Use it at your own risk and for your own benefit. Life-safety equipment must be listed and installed by qualified people.

## 1. Question

Principle 1 of *Intelligent Spaces* Season 1 says: fresh, sourced readings. Every reading has a device ID, a timestamp and an event ID; stale and duplicate data must not drive decisions. B01 implemented this as a state store that dedupes event IDs and checks age by device timestamp. This study asks: which sensor faults does that miss, what does a fuller reading-quality layer catch, and what does the extra caution cost the people in the house?

## 2. Design under test

`islab/readings/quality.py` turns every `state/` and `event/` message into a reading with five provenance fields (device, device sequence number, device timestamp, hub receive time, event ID) and checks it before it may drive a decision:

- **Sourced**: device ID, event ID and timestamp present; the topic's device equals the source.
- **Duplicate**: event ID seen, or the same device sequence number seen.
- **Out of order**: a lower sequence number than the newest from that device. Late state is rejected; late events are kept.
- **Clock skew**: per device, the rolling median of 15 (hub receive time minus device time); device time is corrected by it. A timestamp that stops advancing is a replay. Offsets above 60 s are reported.
- **Plausible**: range and rate-of-change limits per sensor kind (temperature: -30 to 60 °C, at most 0.05 °C/s). A faster jump is held, not dropped: if the next report agrees, the jump was real; if not, it was a spike.
- **Stuck**: identical values for 20 minutes (at least 15 reports).
- **Stale**: older than 2.5 heartbeats by receive time or corrected device time; a battery report of 0 makes the device dead at once.
- **Latched alarm events**: a leak or smoke event is cleared only when the device itself goes wet then dry, or a person confirms. A sensor that sent a leak event and then only "dry" heartbeats is disputed.
- **Disagreement**: two kitchen thermometers more than 1.5 °C apart for 10 minutes.

The layer answers one question per reading, *may this drive a decision now?*, and publishes new problems as issues so the hub can tell people (principle 4). It never acts.

## 3. Method

1. The B01 twin (`islab.twin.build_home`) ran for 7 simulated days per seed, 10 seeds. Link faults came from `FaultInjector`; sensor faults were scheduled on the devices.
2. Faults: 10% of state and event messages duplicated all week; 0 to 30 s jitter on the kitchen window contact (about 14 short airings a day, so messages arrive out of order); the kitchen thermometer's clock drifting 2 s per hour behind; one +15 °C spike (day 0); the thermometer stuck for 12 hours (day 1); a leak sensor that sends one leak event and then only dry heartbeats while the floor is wet for 2 hours (day 2, the person reads the phone at 07:10); a dead leak-sensor battery (day 5). Legitimate events: a real 20-minute leak (day 1), a real 5 °C step for 30 minutes (day 3), cooking every evening.
3. Three hub views read the same bus at once: **naive** (latest arrival wins), **timestamp** (the B01 `islab.policy.StateStore`), **quality** (the B03 layer). The same decision code ran on each: kitchen heating every 60 s, an overheat alert on every temperature report, leak shut-off and valve reopen after 5 dry minutes, night light on motion, doorbell notice, a 22:00 house check. Decisions were recorded, not executed.
4. An oracle, installed as the first link hook and first subscriber, labelled every message at publish and delivery. The views never read it. A decision is on bad data if a reading it used came from a lying sensor and differed from the truth by more than 1 °C (or had the wrong leak state), was superseded by a newer honest reading already delivered, was older than the device's maximum age, or the decision was triggered by a duplicate copy of an event.

## 4. Results

Per simulated week, mean of 10 seeds (range in brackets). Source: `experiments/b03_readings/results.json`.

| Measure | naive | timestamp | quality |
|---|---|---|---|
| Decisions on bad data | 637.3 (607-660) | 605.2 (584-616) | 2 (2-2) |
| ...stuck sensor / reorder / duplicate | 602.8 / 16.2 / 13.9 | 602.8 / 0 / 0 | 0 / 0 / 0 |
| ...spike / dead battery / one-shot leak | 1.4 / 2 / 1 | 1.4 / 0 / 1 | 0 / 0 / 0 |
| ...real change held one report | 0 | 0 | 2 |
| False alarms | 4.7 | 2 | 0 |
| Water running with the valve open during a leak | 112 min | 112 min | 0 |
| Valve closed while dry after a leak | 5.4 min | 5.4 min | 133.6 min (108-153) |
| Dead battery noticed | never | 782 s | 0 s (reading unusable after 26 s) |
| Stuck sensor noticed | never | never | 19.2 min |
| One-shot leak disputed | never | never | 143 s (39-257) |
| Good readings rejected at ingest | 0 | 0 | 4 |
| Decision ticks with a good primary reading discarded | 0 | 6,585 | 0 |
| Refused decision ticks | 0 | 4.8 | 247.8 |
| Refusal episodes told within 60 s | n/a | 48 of 48 | 30 of 30 |

Naive false alarms were duplicate doorbell notices (3.2), the spike's overheat alert (1) and duplicate leak alarms (0.5). Timestamp false alarms were the spike (1) and calling the drifting sensor stale (1). The layer's skew estimate at the end of the week was 335.7 s against a true 336 s; it reported the offset at hour 30.1.

## 5. Findings

1. **The stuck sensor did the most damage.** Fresh timestamps, plausible values, no duplicates: nothing in a timestamp check fires. Both simpler hubs made about 603 heating decisions per week on it.
2. **Timestamps fix the transport, not the sensor.** The B01 store removed all duplicate and reordered decisions, and none of the faults where the sensor itself is wrong.
3. **Alarm events need a latch.** A heartbeat that never said "wet" is not evidence the leak is over. Without the latch, water ran 112 minutes with the valve open.
4. **Read the last gasp.** A battery-0 report is the earliest stale signal; waiting for missed heartbeats took 13 minutes.
5. **Estimate clock skew instead of trusting device time.** A 2 s per hour drift made a device-timestamp check throw away a good sensor after two to three days.
6. **Caution costs time and must be told.** The valve stayed closed 134 minutes after the floor was dry, until a person answered; a real fast change reached decisions one report late. Every refusal was told.

## 6. Limits

Everything ran in simulation, with one of each fault per week at scripted times. Decisions were not executed, so heating did not change the room. The thermometers report with 0.01 °C resolution and 0.05 °C noise, which makes a stuck sensor easy to see; a coarse sensor in a stable room can repeat one value for hours and needs a longer window or a second source. The rate limit holds any fast rise for one report, including a real fire's, so fire detection must stay with listed smoke alarms. Duplicates and reordering were independent per message; real radios fail in bursts. The person's reply time was scripted; a person who never answers keeps the valve closed. Thresholds were fixed before the run and not tuned; other thresholds give other numbers. The ground-truth oracle defines "bad data" with a 1 °C tolerance, which is our choice.

## 7. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b03_readings.py        # 19 tests
python experiments/b03_readings/run.py      # 10 seeds x 7 simulated days, about 2 minutes on 2 cores
```

## 8. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
