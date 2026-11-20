# What leaves the house: an egress ledger, an honest daily answer and deletion with receipts in a digital twin

Intelligent Spaces Lab, Build Track B09 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with simulated devices, by AI agents under human direction. It is not a product and not legal or privacy compliance advice. Research in simulation, provided as is. Use it at your own risk.

## 1. Question

Season 1 episode nine, "Private by design", keeps the core at home and sends things out only for a purpose. Asked "what did you send out today?", the assistant answers: twelve speech requests, not stored, and last night's encrypted backup. Shared data asks before it is deleted, and deletion comes with a receipt.

B01 found that answer true only for traffic a person would notice: the same day also carried 287 alarm-line check-ins and 4 update checks. This study asks:

1. What does a full-day ledger of every byte out look like, and how much of it would a person never notice?
2. Can the answer be short and still never leave a category out?
3. What does the gate catch when a device phones home, and what still works with the internet down?
4. When data belongs to two people, does deletion ask first, find every copy, and say the truth about backups?

## 2. Design under test

- **Gate** (`islab/alarm/egress.py`, B01, unchanged): fixed purposes, destination allowlist, payload classes, opt-ins, queue or drop when offline. B04 added one purpose, a plain-text push (`islab/safety/notify.py`).
- **Ledger gate** (`islab/privacy2/ledger.py`, new): keeps every rule and adds:
  - a ledger row for every attempt: time, source, destination, purpose, payload class, bytes, outcome (sent, blocked, queued, dropped), whether a person would notice it, and the destination's declared retention promise;
  - source manifests: each sender declares its purposes, its largest message and its messages per day;
  - a notice to the household at a source's first undeclared attempt, after which the source is held at home.
- **Answer** (`islab/privacy2/answer.py`, new): three levels from the ledger only.
  - Short, spoken: things a person did or was told about are named with a count. Background traffic may be grouped, but it is always said, with its count.
  - Phone: every purpose with messages, bytes, destination and promise; what was blocked, held or dropped.
  - Raw: the ledger rows.
  A checker grades any short answer for the categories that occurred that day.
- **Devices** (`islab/privacy2/devices.py`, new): a device that phones home in seven ways, and a voice router that uses cloud speech only when the local recogniser is unsure, the household opted in and the internet is up.
- **Deletion** (`islab/privacy2/deletion.py`, new), extending the B01 store:
  - Owners. A shared record asks every other owner first; "no" or no answer in 24 h keeps it.
  - Scan of the whole hub disk, registered or not, by id, content hash, containment in a larger file and recorded lineage.
  - Backups encrypted per record with AES-GCM, the key kept at home. Deleting the record destroys its key.
  - A receipt that lists what was removed, what was scanned and what was not checked. It never says "complete".

## 3. Method

1. **A.** The E09 day with the plan and seed of `experiments/egress`: 60 voice requests, of which the local recogniser hands 12 to the cloud; a 48 MB nightly backup; update checks every 6 h; alarm check-ins every 300 s; 25 camera upload attempts; a plug phoning home every 10 minutes; one direct `egress/` publication; 200 home-control commands.
2. **B.** 30 varied days (seeds 2026-2055): random cloud speech count, pushes, assistant questions, leaks and 2-6 h outages.
3. **C.** Seven phone-home modes, 200 seeded days each, B01 rules against the ledger gate.
4. **D.** The E09 day with the internet down 00:00-24:00, and with cloud speech opted out.
5. **E.** 500 deletions with 7 kinds of hidden copies planted, B01 delete against B09 delete. Then 500 shared deletions per backup key scheme: Anna asks, Mark is away. Mark's answer is modelled: 80% yes, 10% no, 10% never, lognormal delay with a median of 15 minutes.

## 4. Results

### 4.1 Every byte out (`experiments/b09_egress`, part A)

| Purpose | Messages | Bytes | A person would notice | Promised retention |
|---|---|---|---|---|
| speech recognition | 12 | 164,980 | yes | not stored |
| encrypted backup | 1 | 48,000,000 | no | encrypted, rotated after 30 days |
| alarm check-ins | 287 | 27,839 | no | per monitoring contract |
| update checks | 4 | 4,800 | no | request logs 90 days |
| total | 304 | 48,197,619 | | |

A person would never notice 292 of 304 messages (96.05%) and 99.66% of bytes. Over 30 varied days, the background share was 95.18% of messages on average (90.97% to 98.65%) and 99.69% of bytes. The 48 MB backup carries the byte share.

### 4.2 The answer

Short answer for the E09 day, 48 words:

> Twelve voice requests for speech recognition, not stored. In the background: two hundred ninety-two messages, the nightly encrypted backup, alarm-line check-ins, and update checks. No camera footage left. One hundred sixty-nine attempts to send elsewhere were blocked, from two devices. One try to go around the gate, logged.

| Answer | Days with every category mentioned |
|---|---|
| B09 short answer | 30 of 30 (and 300 of 300 random days in the tests) |
| B01 summary without routine traffic | 0 of 30 |
| Scripted E09 line, on the E09 day | missed 4 of 6 categories |

The B01 summary missed check-ins and update checks on all 30 days, the bypass try on 30, pushes on 16, dropped messages on 6 and waiting messages on 1. The longest short answer was 73 words, on a day with an outage. The phone level lists each purpose; the raw level had 473 rows that day.

### 4.3 Phone home (part C, 200 days per mode)

| Mode | B01 rules: got out | Ledger gate: got out | Household told | Legit update checks blocked |
|---|---|---|---|---|
| undeclared destination | 0% | 0% | 100%, first attempt | 0 |
| allowlisted host, telemetry | 0% | 0% | 100% | 0 |
| plug mimics an update check | 100% | 0% | 100% | 0 |
| update agent sends 50 KB "checks" | 100% | 0% | 100% | 800 of 800 |
| update agent, 20 extra checks a day | 100% | 25% | 100%, about 6 h in | 600 of 800 |
| update agent, 2 extra checks inside its manifest | 100% | 100% | 0% | 0 |
| publishes `egress/` itself | 0%, logged | 0%, logged | 100% | 0 |

Under B01 rules, blocked attempts are logged but nobody is told: 0% in every mode.

### 4.4 Internet down (part D)

| Function | Internet down all day |
|---|---|
| home control | 200 of 200 confirmed, p50 0.32 s |
| voice, local | 48 handled |
| voice that needed the cloud | 12 answered "I did not understand" |
| leak: local siren | 0.01 s |
| leak: monitoring centre | ack 36,000.3 s later, when the internet returned |
| backup, leak alarm | 2 waiting at the end of the day |
| check-ins, update checks, cloud speech | 303 dropped |
| deletion | receipt in 0.02 s; backup copy unreadable |

With cloud speech opted out, 0 speech bytes left and the same 12 requests got "I did not understand".

### 4.5 Deletion (part E)

| Hidden copy, 500 trials | B01 delete | B09 delete |
|---|---|---|
| renamed copy, registered cache | 100% removed | 100% |
| share-link record | 100% | 100% |
| re-encoded copy with lineage | 0% | 100% |
| re-encoded copy without lineage | 0% | 0%, listed as not checked |
| exact copy, unregistered cache | 0% | 100% |
| inside a bundle file | 0% | 100%, rest of the bundle kept |
| old thumbnail, unregistered cache | 0% | 100% |

- The B01 receipt said "complete" in 264 of 500 trials, each with five copies left. These are the trials where the clip had not yet gone into a backup.
- The B09 receipt left no copy of a kind it says it checks (0 of 500). Neither design damaged another record.
- Time to receipt for one owner: 0.064 s of modelled scan (12.5 ms p50 of Python time here).

| Shared clip, 500 trials | Per-record backup keys | One key per night |
|---|---|---|
| deleted / declined / no answer | 397 / 43 / 60 | 397 / 43 / 60 |
| deleted without a yes | 0 | 0 |
| time to receipt, deleted, p50 / p95 | 872.6 s / 4,995.8 s | same |
| deleted with a copy in last night's backup | 183 | 183 |
| that copy readable after the delete | 0 | 183, until rotation on day 30 |

Time to a shared receipt is the co-owner's modelled reply time. Each ask left the house as one push (500 in 500 trials).

## 5. What held

- Every attempt to send anything has a ledger row, and the bytes add up to what left.
- A short answer can name what a person did and still say the background, with its count, every day.
- Manifests stop a device that pretends to be another sender, and the household is told at the first attempt.
- Home control, local voice, the siren and deletion work with no internet.
- No shared record was deleted without a yes. A per-record key makes a backup copy unreadable at once.

## 6. What broke

1. **The scripted E09 line and the B01 summary leave out background traffic.** That is 96% of messages on the E09 day, and the gap appeared every day.
2. **B01 rules check destination, purpose and class, not the sender.** A plug labelling its traffic "update check" to the allowlisted update host got out 100% of the time, 60 KB a day on average.
3. **The B01 delete receipt overclaims.** It said "complete" with five copies left in 264 of 500 trials. It scans registered locations only, and has no containment or lineage checks.
4. **A shared nightly backup key keeps deleted data readable** until rotation. The B01 receipt says a remote copy is pending, but it does not say the copy is readable.
5. **Manifests have costs.** Holding an update agent that breaks its size limit also stops its real update checks (800 of 800). A daily count limit catches extra checks only once the budget is spent (about 6 h in), and the extras use up the budget meant for real checks (600 of 800 blocked). A sender that stays inside its manifest is not caught at all.
6. **Our own bug.** The first short answer counted held send attempts as messages waiting (1,730, mostly alarm retries that replace each other). Fixed: it counts the queue (2).

## 7. Limits

- Everything ran in simulation.
- Byte sizes, the visibility label per purpose and the providers' retention promises are modelled or declared. The gate cannot check what a provider keeps.
- Which voice requests need the cloud is an input; no recogniser was run.
- The gate sees only traffic routed through it. A real home needs network default-deny (VLAN, firewall), which is not simulated.
- Payload content is not inspected.
- The co-owner's answers and delays are modelled.
- Scan time is modelled at 200 MB/s plus 5 ms per location.
- A key store lost with the hub makes every backup unreadable; key escrow is not modelled.
- Copies off the hub disk (phones, exports already shared) are outside any scan, and the receipt says so.

## 8. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b09_egress.py
python experiments/b09_egress/run.py        # about 30 seconds on 2 cores
```

## Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
