# Sent is not closed: a local leak-to-valve safety controller in a digital twin

Intelligent Spaces Lab, Build Track B04 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **No guarantee.** This is open research material. It was produced in a simulator, with simulated devices, by AI agents under human direction. It is not a product and not safety advice. **It is not a certified leak-protection system.** Use it at your own risk and for your own benefit. Real water shut-off equipment must be listed, installed and tested by qualified people.

## 1. Question

Season 1 episode five has a leak under the kitchen sink. A local rule closes the main water valve, with no AI in the loop. Every event carries a device identity, a timestamp and a unique event ID. A command sent is not a valve closed: closure needs fresh feedback. The explanation of what happened is built from the record, and only permitted data leaves the home.

This study asks: written as code on the hub, how much does that rule change the outcome compared with two common designs, when the valve, the network, the hub and the internet fail?

## 2. Design under test

- **Twin** (`islab/twin`, from B01): 15 devices including the main valve (4-8 s travel, acks before it moves) and the sink leak sensor, plus the fault injector.
- **Flow meter** (`islab/safety/devices.py`, new): a simulated meter on the main pipe. Flow = 6 L/min times the valve's physical opening while water escapes; reports on a change of 0.3 L/min and every 10 s.
- **Controller** (`islab/safety/controller.py`, new): an explicit state machine, idle → leak_detected → closing → closed_confirmed, or close_failed → escalated. No language model and no internet. It detects a leak from the sensor's event or from its state report. Each close attempt is an `ActionTracker` action from `islab.policy` with a fixed `cmd_id`; a command with no ack is resent with the same id, and the valve treats a repeated id as idempotent. Closed is confirmed only when a valve report received after the command shows position 0 and a flow reading at most 15 s old, received after the leak, shows 0.2 L/min or less. If that has not happened 30 s after the first command, the controller tells people in plain words to shut the water off by hand, sends a second leak alarm (zone 12) to the monitoring centre through the B01 alarm path, and keeps trying every 30 s.
- **Journal** (`islab/safety/journal.py`, new): JSON lines; each record has a record ID, time and boot number. The record announcing a command is synced before the command is sent. A crash loses unsynced records; a torn last line is ignored. A rebooted controller rebuilds its state from the journal and resumes.
- **Telling people** (`islab/safety/notify.py`, new): Anna at home gets messages over the home network; Mark is away and gets a push through the egress gate. The gate gains one purpose, `notify_push`, which may carry only the plain text, to one allowlisted relay.
- **Explanation** (`islab/safety/explain.py`, new): "what happened" built only from journal records, every sentence citing its records. A separate checker re-reads each sentence: its kind of fact, every clock time, percentage, flow figure, quoted message and count must match a cited record.
- **Baselines**: fire-and-forget reports "closed" when the command is sent. Ack-based reports "closed" on the valve's "accepted" ack, resends twice on no ack, and tells people to shut off by hand if no ack ever comes.

## 3. Method

1. For each of 8 conditions and 3 designs, 500 seeded runs (seed = trial index). A leak starts 60-120 s into the run; the run continues 360 s after it.
2. Conditions: normal; valve stuck (acks, never moves); valve partial (stops at 40%); command lost (each command copy dropped with probability 0.5); valve state reports lost for 15 s after the leak; hub reboot 0-10 s after the leak, down 20-40 s; internet down from 30 s before to 270 s after the leak; leak event lost while the state report still shows wet.
3. Ablation: the state machine with a journal that does not survive the reboot.
4. Ground truth is the valve's physical position read from the device object, never from messages. A "closed" claim is false if the valve is still open 30 s after the claim.
5. Alarms use SIA-DCS, after the B01 finding that a Contact ID leak code is decoded as invalid by `pysiaalarm`.

## 4. Results

### 4.1 Claims of "closed" (`experiments/b04_valve`)

| Condition | Fire-and-forget false "closed" | Ack-based false "closed" | State machine false "closed" |
|---|---|---|---|
| normal | 0% (but 100% sent while water flowed) | 0% (100% sent while water flowed) | 0% |
| valve stuck | 100% | 100% | 0% |
| valve partial | 100% | 100% | 0% |
| command lost | 51.6% | 0% | 0% |

With a stuck or partial valve, the last message both baselines had sent at 60 s said "closed" while the valve was open, in 100% of runs. The state machine's last message was never wrong.

### 4.2 Time to confirmed close, state machine

| Condition | p50 | p95 |
|---|---|---|
| normal | 6.53 s | 8.40 s |
| command lost | 7.81 s | 16.51 s |
| state reports lost 15 s | 16.91 s | 26.99 s |
| hub reboot, with journal | 34.05 s | 49.13 s |
| hub reboot, journal lost (ablation) | 204.63 s | 331.03 s |
| internet down | 6.60 s | 8.38 s |
| leak event lost | 6.60 s | 8.39 s |

The baselines never confirm; they claim. With a stuck or partial valve the state machine confirmed nothing, by design.

### 4.3 Escalation when closing fails

Stuck and partial valves: the state machine told people to shut off by hand in 100% of runs, at 30.01 s (p50 and p95), and its valve alarm reached the monitoring centre within 60 s of the escalation in 100%. Neither baseline escalated. Under command loss, the ack-based design escalated in 11.4% of runs at 9.01 s; the state machine escalated once in 500 runs and closed the valve in all 500.

### 4.4 People and alarms

Anyone told within 60 s: 100% in every condition and design, except two: with the leak event lost, both baselines told no one (0%); and in 0.2% of reboot runs the hub crashed before it saw the leak. Everyone told within 60 s with the internet down: 0% in every design, because the person away gets a push that waits for the internet. The leak alarm reached the centre within 60 s in 100% of runs except with the internet down (0%; it arrived when the internet returned, p50 270.15 s after the leak) and reboot runs (99.8%).

### 4.5 Explanations

64,768 statements were generated from the journals of 12,500 runs; 100% matched a cited record. Statements that the valve was closed were true against the twin in 100% of state machine runs and 0% of baseline runs: every baseline claim came before the valve moved. An explanation can only be as true as its record. Example, stuck valve:

> At 09:11 the sink sensor reported water (in a status report). [...] The valve was told to close at 09:11:30. [...] By 09:12:00 the valve had not confirmed closed (last readings: position 100%, flow 6.0 litres a minute). [...] At 09:12:00 people were told to turn off the water by hand.

### 4.6 What leaves the house

Across all runs only three payload classes left: alarm events, alarm heartbeats and plain-text pushes. The gate blocked nothing because nothing else tried; a push carrying any field beyond the person is blocked (test). Valve control needed no internet.

## 5. Findings in our own code

1. **Flow freshness after a reboot.** Requiring a flow reading received after the command made a rebooted hub wait for the meter's heartbeat and escalate a closed valve. Fixed with a freshness window (15 s) and a 10 s meter heartbeat.
2. **Subscriber order.** The confirming valve report was not yet cached when the action tracker confirmed. Fixed by registering the cache first.
3. **A rebooted alarm hub repeats itself.** The B01 alarm hub forgets the open leak episode on restart and raises it again on the next sensor report: a duplicate at the centre after 81.8% of reboots. The controller now tells the fresh alarm hub the episode is open; 9.2% remain, where the centre's ack was in flight at the crash. We prefer a duplicate to a lost alarm.
4. **A reboot before detection** loses the leak event. The state machine recovers at the sensor's next report (up to 300 s; worst case 229.82 s to confirmed close here); event-only designs never do.

## 6. Limits

Everything ran in simulation. The flow meter is an extra device most homes do not have; without it, confirmation rests on the valve's own report. Valve travel is modelled as a jump after 4-8 s, with no intermediate positions or seepage. A new close attempt stands in for reading the valve's state attribute. All hub components die together in a reboot; disk corruption beyond a torn last line is not modelled. One leak, one valve and one household per run; people's actions after being told are not modelled. The 30 s escalation and 10 s attempt timeout are design choices. With the internet down there is still no second path to the monitoring centre or to people away from home. This is not a certified leak-protection system.

## 7. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b04_safety.py
python experiments/b04_valve/run.py         # about 1-3 minutes on 2 cores
```

## 8. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
