# Alarm path: notes

Run: `python experiments/alarm_path/run.py` (about 15 s; `--no-interop` skips the socket tests).
Output: `results.json`. Code: `islab/alarm/dc09.py`, `islab/alarm/path.py`, `islab/alarm/interop.py`.

## Where the DC-09 details came from (the standard was NOT read)

ANSI/SIA DC-09 is paywalled. Nothing here was checked against its text. Conformance or
certification is not claimed.

Taken from **pysiaalarm 3.2.2 source code** (`event.py`, `utils/regexes.py`, `const.py`,
`data/adm_mapping.py`), installed from PyPI on 2026-11-19:
- CRC algorithm: CRC-16 with reflected poly 0xA001 and init 0, which is CRC-16/ARC. Our
  `crc16` gives the catalogue check value 0xBB3D for "123456789" and matches pysiaalarm's
  `_crc_calc` on 200 random strings (unit test).
- CRC and length are computed over the span from the first `"` up to the CR. The length is 4 hex digits.
- Header layout: `"ID"` + 4-digit sequence + `R`(1-6 hex) + `L`(1-6 hex) + `#`account (3-16 hex) + `[`.
- ADM-CID content is `#acct|QEEE GG ZZZ]`. SIA-DCS content is `#acct|Nri<area>/<code><zone>]`. NULL content is `]`.
- Timestamp is `_HH:MM:SS,MM-DD-YYYY` in UTC. pysiaalarm accepts it in the window now-40 s to now+20 s.
- Encryption is AES-CBC with an all-zero IV and uppercase hex output. The encrypted part is everything after `[`.
  The plaintext starts with pad characters ended by `|`.
- ACK is `"ACK"seq Rrcv Lline #acct []`. The encrypted ACK is `"*ACK"…[hex]`. NAK is `"NAK"0000…A0[]_timestamp`.
- The CID to SIA code mapping that pysiaalarm applies (`ADM_MAPPING`).

Taken from **public descriptions** of DC-09, Contact ID and SIA DC-03. I used my general
knowledge here and did not re-check these against a source in this session:
- LF/CR framing, and `*` before the ID marking encryption.
- The NULL message serves as the link test / supervision message.
- Retransmissions repeat the same sequence number.
- CID qualifiers: 1 = new event, 3 = restore, 6 = previous.
- CID event codes: 111 smoke, 130/131/132 burglary, 154 water leakage, 602 periodic test.
- SIA codes: FA, WA, BA, RP.

**Our own choices**, which a receiver does not mandate:
- Pad characters are random alphanumerics from `sim.rng`. Pad length brings `pad|data` up to a multiple of 16.
- Each retransmission gets a fresh timestamp but keeps the same sequence number.
- ACK timeout is 5 s. Backoff is min(cap, 2^(n-1)) s with jitter between ×0.5 and ×1.5. The cap is 16 s.
- Heartbeats are NULL messages every 60 s and are not retried.

## pysiaalarm interoperability (real sockets, 127.0.0.1)

We tested three receivers: the aio TCP and aio UDP servers (the ones Home Assistant uses) and the sync TCP server.
Result: **19/22 pass on aio TCP, 19/21 on aio UDP and 19/21 on sync TCP.** The extra case on aio TCP is the two-frames test.

Passed on all three transports:
- These messages got an ACK and the code shown:
  - ADM-CID 1111 → FA, 3111 → FH, 1130 → BA, 1602 → RP
  - SIA-DCS FA, WA and BA, plus FA with xdata `[Vsmoke hall]`
  - NULL → RP (pysiaalarm maps an empty NULL to RP)
- Encrypted messages got `*ACK`:
  - ADM-CID smoke and SIA-DCS leak with AES-128 (16-char key)
  - NULL with AES-128
  - ADM-CID burglary with AES-192
  - SIA-DCS smoke with AES-256
- Our parser decrypted pysiaalarm's encrypted ACKs (`]_timestamp`), and its plain ACK CRCs check out with our CRC.
- These cases were correctly refused:
  - Wrong key interpretation got a framed NAK.
  - Unknown account got a bare, unframed `"NAK"`.
  - Timestamps 300 s old or 60 s in the future got a framed NAK.
  - A corrupted CRC got only `\n\r`: no ACK and no NAK. Our hub treats anything that is not a matching ACK as a failure and retries.

Mismatches found (exact):
1. **CID 154 (water leakage) is not in pysiaalarm's ADM_MAPPING.** `1154 01 002` is ACKed, but
   the event code comes out as `YN`, which is pysiaalarm's "invalid data" fallback, instead of WA.
   Home Assistant would show a leak as an unknown event. SIA-DCS `WA` works. If the twin's centre is
   HA/pysiaalarm, send leaks as SIA-DCS. `AlarmHub(fmt=dc09.SIA_DCS)` does this.
2. **A frame without the optional R field gets a malformed ACK.** pysiaalarm writes the literal
   text `None` where the receiver field should be: `"ACK"0010NoneL1#AAA123[]`. A strict sender rejects
   that ACK. Our hub always sends an R field.
3. **Two frames in one TCP write (aio TCP server): both events are lost.** It reads up to
   1000 bytes as one line and answers one NAK. Senders must wait for the ACK before sending the next
   frame. Our hub sends one frame per transmission; the WAN model has no TCP streams.
4. Minor quirks:
   - pysiaalarm's own NAK has the header order `L0R0`, which its own receive regex would not accept. Our
     `parse_response` falls back to a loose parse.
   - The key semantics differ. pysiaalarm uses the **ASCII bytes of the key string** as the AES key, so a
     32-character "hex" key becomes AES-256, not 16 raw bytes. Our API takes raw key bytes. To talk to
     pysiaalarm, pass `key_string.encode()`. We could not check what real panels do.
   - pysiaalarm pads its encrypted ACKs with `'0'` and no `|`. With a zero IV, every ACK under one key
     therefore has the **same first cipher block** (visible in the matrix as `73C47CB1…`). Our encoder
     uses random pad, so its first block varies.

## Key numbers (500 seeded trials per condition, AES-128 ADM-CID through the egress gate)

| condition | p50 s | p95 s | ≤60 s | transmissions/alarm | duplicates suppressed |
|---|---|---|---|---|---|
| normal | 0.15 | 0.25 | 100 % | 1.00 | 0 |
| 2 % loss | 0.16 | 0.26 | 100 % | 1.04 | 7 |
| 20 % loss | 0.18 | 6.7 | 100 % | 1.60 | 119 |
| event inside a 60 s outage (random point) | 36.7 | 68.1 | 82.4 % | 4.84 | 0 |
| event at the start of a 60 s outage | 70.9 | 83.7 | 0 % | 6.72 | 0 |
| inside a 60 s outage, backoff cap 4 s | 33.1 | 61.7 | 92.8 % | 5.36 | 0 |

- All alarms were delivered in every condition.
- Every trial produced exactly one dispatch decision at the centre. Retransmissions caused by lost ACKs were recognised as duplicates and did not re-dispatch.
- The local siren sounded 0.01 s after the event (one bus hop) in every smoke and leak trial, whatever the WAN did.
- After an outage, the delay is set by the backoff cap plus the 5 s ACK timeout. A tighter cap
  buys about 10 points of "within 60 s" for about 11 % more transmissions.

## Findings for other parts of the lab

- `islab/core/bus.py` `_deliver` calls subscribers in a loop with no try/except. A subscriber that
  raises stops delivery to every later subscriber, and also stops `sim.run`. The hub catches its own
  exceptions, but life safety should not depend on every other component doing the same. The siren
  also offers `hardwire()` for a path that does not use the bus. This needs a decision by the core owner.
- The hub notifies the owner ("received by the monitoring centre. They handle dispatch."). The hub has
  no method that could dispatch, and a unit test checks that.
