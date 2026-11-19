# twin_faults: why "ack" must never be reported as "done"

**Run:** `python experiments/twin_faults/run.py` (200 seeded trials per fault, under 1 s wall time).
**Client:** sends one command (`valve.main close`; `lock.front unlock` for battery_dead) and waits
30 virtual seconds for a `state/<device>` message with the target attributes and device `ts` later
than the command. That is the contract's definition of CONFIRMED.

| fault | confirmed % | ack, not confirmed % | no ack % | ttc p50 / p95 (s) |
|---|---|---|---|---|
| none | 100.0 | 0.0 | 0.0 | 5.8 / 7.9 |
| link_drop (30% on cmd/ack/state) | 53.5 | 14.0 | 32.5 | 6.2 / 22.4 |
| link_delay (+5 s) | 100.0 | 0.0 | 0.0 | 15.8 / 17.9 |
| link_duplicate (50%) | 100.0 | 0.0 | 0.0 | 5.5 / 7.7 |
| link_reorder (0-10 s jitter) | 100.0 | 0.0 | 0.0 | 15.7 / 24.0 |
| stuck | 0.0 | **100.0** | 0.0 | - |
| partial (40%) | 0.0 | **100.0** | 0.0 | - |
| offline | 0.0 | 0.0 | 100.0 | - |
| stale | 0.0 | **100.0** | 0.0 | - |
| clock_skew_behind (-120 s) | 0.0 | **100.0** | 0.0 | - |
| clock_skew_ahead (+120 s) | 100.0 | 0.0 | 0.0 | 5.8 / 7.9 |
| battery_dead | 0.0 | 0.0 | 100.0 | - |
| internet_down | 100.0 | 0.0 | 0.0 | 5.8 / 7.9 |
| hostile_name | 100.0 | 0.0 | 0.0 | 5.8 / 7.9 |

## What it shows

1. **Four device faults (stuck, partial, stale, clock lag) each produce a clean "accepted" ack and no
   outcome 100% of the time.** A hub that says "done" on ack would tell the user the water is off in
   every one of those trials. The valve never moved, or stopped at 40%, or nobody can tell.
2. **Under 30% link loss, 14% of commands are acked but never confirmed, and 15% are confirmed
   *without* an ack** (the ack was the message that got lost). So ack is neither sufficient nor
   necessary. Only the state is evidence.
3. **Silence is not the same as failure.** Offline and battery-dead devices give no ack at all. The
   honest report is "I could not reach the valve", not "the valve failed".
4. **The timestamp rule has a sharp edge.** If the device clock *lags* the hub, a valve that really
   did close is never CONFIRMED, because its `ts` predates the command. A clock that runs *ahead* is
   harmless. Policy should either check skew against the device's heartbeats or also correlate on
   message arrival order. We record this here; we do not change the contract.
5. **Duplicates are frequent but harmless here.** 73% of link_duplicate trials saw two acks for one
   command, because the duplicated command was executed twice. "close" is idempotent; "toggle" would
   not be.
6. **Latency faults stretch time-to-confirm toward the 30 s timeout** (p95 24 s under jitter). A
   timeout tuned to the clean baseline of about 8 s would misclassify most of these as failures.
7. internet_down and hostile_name leave the local transport untouched. That is the point of
   local-first control. Their real effects belong to the egress and agent suites.

## Limitations

Synthetic delays (uniform), i.i.d. link loss (real radios lose packets in bursts), one command per
trial with no retries, a single device type per fault. These numbers are a baseline for comparing
policies. They are not field reliability figures.
