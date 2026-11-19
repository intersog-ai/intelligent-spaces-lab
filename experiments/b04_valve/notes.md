# B04 valve experiment: notes

`python experiments/b04_valve/run.py` (12,500 runs, about 1 minute on 2 cores; up to 3 minutes when the machine is shared). Numbers below are from `results.json`, 500 seeded runs per cell, virtual time, measured from the moment water starts.

## What is compared

| Design | Says "closed" when | If it does not close |
|---|---|---|
| fire_and_forget | the close command is sent | nothing |
| ack_based | the valve acks "accepted" | resends on no ack; after 9 s with no ack, tells people to shut off by hand |
| state_machine (`islab/safety`) | a fresh valve report shows position 0 AND the flow meter reads ~0 | after 30 s: tells people in plain words to shut off by hand, sends a second leak alarm (zone 12), keeps trying |

## Results

| Condition | fire_and_forget | ack_based | state_machine |
|---|---|---|---|
| normal | claim at 0.01 s, every claim made while water still flowed | claim at 0.03 s, every claim made while water still flowed | confirmed p50 6.53 s, p95 8.40 s |
| valve stuck | false "closed" 100%, never escalated | false "closed" 100%, never escalated | 0 false claims; escalated 100% at 30.01 s |
| valve partial (40%) | false "closed" 100% | false "closed" 100% | 0 false claims; escalated 100% at 30.01 s |
| command lost (p=0.5 per copy) | false "closed" 51.6% | 0 false; 11.4% no ack, told at 9.01 s | confirmed 100%, p50 7.81 s, p95 16.51 s; 1 run escalated |
| valve state reports lost 15 s | claim right by luck | claim right by luck | confirmed p50 16.91 s, p95 26.99 s, no escalation |
| hub reboot mid-action | (acted before the crash) | (acted before the crash) | confirmed p50 34.05 s, p95 49.13 s |
| hub reboot, journal lost (ablation) | | | confirmed p50 204.63 s, p95 331.03 s |
| internet down | valve closed; leak alarm reached the centre only when the internet returned (p50 270.15 s) | same | confirmed p50 6.60 s, p95 8.38 s; same alarm delay |
| leak event lost, state shows wet | valve never closed, nobody told | valve never closed, nobody told | confirmed p50 6.60 s, p95 8.39 s |

People told within 60 s (anyone): 100% for all designs in all conditions except leak event lost (baselines 0%) and the hub reboot runs where the crash came before detection (0.2%). Everyone told within 60 s: 0% with the internet down in every design, because Mark is away and his push waits in the egress queue. Misled at 60 s (the last message said "closed" while the valve was open): fire_and_forget and ack_based 100% with a stuck or partial valve; fire_and_forget 51.6% with command loss; state_machine 0% everywhere.

Explanations: 64,768 statements generated from the journals of all 12,500 runs; 100% matched a cited record (times, percentages, flow figures, quoted messages, counts). Statements that the valve was closed were true against the twin in 100% of state_machine runs and 0% of the baseline runs where a claim was checked: every baseline claim came before the valve moved. A faithful explanation of a false record is still false.

## Things we found along the way

1. **Flow freshness after a reboot.** The first version required a flow reading received after the close command. The meter reports on change, so after the valve closed nothing new arrived and a rebooted hub waited for a heartbeat (30 s then) and escalated a closed valve. Fixed: a zero reading counts if it arrived after the leak and is at most 15 s old; the meter heartbeat is 10 s.
2. **Reading order.** The controller cached valve reports in a subscriber registered after the action tracker, so the confirming report was not yet cached when the tracker confirmed. Fixed by subscribing first.
3. **A rebooted B01 alarm hub raises the same leak twice.** It forgets the open episode and raises again on the next leak sensor report: 81.8% of reboot runs put a duplicate leak alarm at the centre (baselines). The controller tells the fresh alarm hub the episode is open: 9.2% remain, all where the centre's ack was in flight at the crash. Those are at-least-once duplicates; the alternative is a lost alarm.
4. **A reboot before detection** (0.2% of reboot runs): every design missed the leak until the sensor's next report, up to 300 s later. The state machine then closed the valve (max 229.82 s); the baselines, which only listen for the leak event, never did.

## Limits

See `limitations` in `results.json`. Most important: simulated devices; the flow meter is an extra device most homes lack; a new close attempt stands in for reading the valve attribute; not a certified leak-protection system.
