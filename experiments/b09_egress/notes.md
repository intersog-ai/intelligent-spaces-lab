# B09 What leaves the house: notes

Run: `python experiments/b09_egress/run.py` (about 30 s on 2 cores). Output: `results.json`.
Code: `islab/privacy2/` (builds on `islab/alarm/egress.py`, `islab/alarm/privacy.py` and the B04 push purpose in `islab/safety/notify.py`; none of them edited).

## A. The E09 day, every byte out (seed 2026)

| purpose | messages | bytes | a person would notice? | promised retention (declared) |
|---|---|---|---|---|
| speech recognition | 12 | 164,980 | yes | not stored |
| encrypted backup | 1 | 48,000,000 | no | encrypted, rotated after 30 days |
| alarm (check-ins only) | 287 | 27,839 | no | per monitoring contract |
| update checks | 4 | 4,800 | no | request logs 90 days, no content |
| **total** | **304** | **48,197,619** | | |

- Background: 292 of 304 messages (96.05%) and 99.66% of bytes.
- Blocked: 169 attempts (plug.cheap 144, camera.front 25). Two notices to the household, at the first attempt of each. One direct `egress/` publication logged.
- Ledger: 473 rows (304 sent, 169 blocked).

Short answer (48 words):
"Twelve voice requests for speech recognition, not stored. In the background: two hundred ninety-two messages, the nightly encrypted backup, alarm-line check-ins, and update checks. No camera footage left. One hundred sixty-nine attempts to send elsewhere were blocked, from two devices. One try to go around the gate, logged."

Graded for category coverage on the same day:
- Short answer: all 6 categories.
- Scripted E09 line: misses alarm check-ins, update checks, blocked attempts, the bypass try.
- B01 `summary_text(include_routine=False)`: misses alarm check-ins, update checks, the bypass try.

## B. 30 varied days (seeds 2026-2055)

Per day: speech 1-24 messages (mean 10.7), alarm 237-288, update checks 3-4, backup 1 (48 MB), pushes 0-5 on 16 days, assistant text 0-10 on 12 days.
Background share: messages mean 95.18% (90.97-98.65%), bytes mean 99.69%.
Short answer covered every category on 30 of 30 days; the B01 summary on 0 of 30 (missed check-ins and update checks 30 times, the bypass try 30, pushes 16, dropped 6, waiting 1). Longest short answer: 73 words, on a day with an outage.

## C. Phone home, 200 days per mode

| mode | B01 rules: got out | ledger gate: got out | household told (ledger gate) | legit update checks blocked |
|---|---|---|---|---|
| undeclared destination | 0% | 0% | 100%, at the first attempt | 0 |
| allowlisted host, telemetry class | 0% | 0% | 100% | 0 |
| plug mimics an update check | 100% (60 KB/day) | 0% | 100% | 0 |
| update agent, 50 KB "checks" | 100% | 0% | 100% | 800 of 800 |
| update agent, 20 extra checks a day | 100% | 25% | 100%, about 6 h in | 600 of 800 |
| update agent, 2 extra checks inside its manifest | 100% | 100% | 0% | 0 |
| publishes `egress/` itself | 0% (logged) | 0% (logged) | 100% | 0 |

B01 rules log blocked attempts but tell nobody (0% told in every mode).

## D. Internet down all day

- Nothing left. 200 of 200 home-control commands confirmed, p50 0.32 s.
- Voice: 48 handled locally; the 12 that needed the cloud got "I did not understand".
- Leak: local siren 0.01 s; monitoring centre ack 36,000.3 s later (when the internet returned).
- 2 messages waiting at the end of the day (backup, leak alarm); 303 dropped (287 check-ins, 4 update checks, 12 speech).
- A deletion with the internet down: receipt in 0.02 s (modelled scan), backup copy unreadable.
- Cloud speech opted out, internet up: 0 speech bytes; the same 12 requests got "I did not understand".

## E. Deletion

500 trials, 20 clips, one deleted; 7 kinds of hidden copies planted every time.

| hidden copy | B01 delete | B09 delete |
|---|---|---|
| renamed copy, registered cache | 100% | 100% |
| share-link record | 100% | 100% |
| re-encoded copy with lineage | 0% | 100% |
| re-encoded copy without lineage | 0% | 0% (receipt: not checked) |
| exact copy, unregistered cache | 0% | 100% |
| inside a bundle file | 0% | 100% (cut out, rest kept) |
| old thumbnail, unregistered cache | 0% | 100% |

- B01 receipt said `complete: true` in 264 of 500 trials, all with copies left (the trials where the clip was not yet in a backup).
- B09 receipt never says complete. Copies left of a kind it says it checks: 0 of 500. Collateral damage: 0 in both.
- Time to receipt, one owner: 0.064 s modelled scan (12.5 ms p50 Python wall time here).

Shared clip (Anna asks, Mark away), 500 trials per key scheme:
- 397 deleted, 43 declined, 60 no answer (receipt at 24 h). Deleted without a yes: 0.
- Time to receipt when deleted: p50 872.6 s, p95 4,995.8 s. This is the modelled reply delay.
- 500 asks left the house as pushes, one per request.
- 183 deleted clips had a copy in last night's backup. Per-record keys: 0 restorable after the delete. One key per night: 183 restorable until rotation (day 30). Both receipts say which.

## What we fixed along the way

- The first short answer counted held send attempts as messages waiting (1,730, mostly alarm retries that replace each other). It now counts the queue: 2.
- `LedgerGate` with manifests off still sent notices; that made the B01 column look better than B01. Notices are now only in the ledger gate.

## Limits

Modelled sizes, visibility labels, retention promises, recogniser confidence, the co-owner's answers and the scan rate. AES-GCM is real. The full list is in `results.json`.
