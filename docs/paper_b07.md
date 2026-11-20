# It acts, you stay in charge: an approval gate for a home agent in a digital twin

Intelligent Spaces Lab, Build Track B07 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with simulated devices and simulated people, by AI agents under human direction. No language model was run: the agents are scripted and randomized doubles. It is not a product and not security advice. Use it at your own risk and for your own benefit.

## 1. Question

Season 1 episode seven lets an AI agent act in the home. It proposes; code decides; risky actions wait for a person. B01 showed the decision part holds: 1,000 adversarial proposals, 0 unapproved level-3 actions (`experiments/agent_gate`). B01 also listed what it assumed: phones are trusted, and approval fatigue is not modelled.

This study asks two things. Can an approval be stolen, reused, misdirected or stretched? And how do people stay in charge without drowning in prompts?

## 2. Design under test

- **Twin and B01 gate** (`islab/twin`, `islab/policy`, not edited). Three levels: act and tell; ask first; never alone (a phone approval from Anna or Mark).
- **Tools** (`islab/approval/tools.py`). One tool added, `open_garage {purpose, until}`, level 3, in the MCP shape (name, description, inputSchema). `purpose` is a fixed list, so a prompt never carries free text from a visitor, note or calendar.
- **Tickets** (`ledger.py`). Each prompt carries the request id, the exact action, a SHA-256 digest of it, a nonce and an expiry. The phone checks the digest against what it shows, then signs request id, digest, nonce and answer with its own key (HMAC here). A reply counts only if all of these hold: the request is known and not used; the person was asked; the signature is valid for that person; digest and nonce match; it arrives before expiry. Right before execution, the action must still hash to the approved digest, and a time-bound action must still be inside its window.
- **GatedEngine** (`engine.py`), a subclass of the B01 engine:
  - the home closes the garage at `until`;
  - arguments the person named (door, end time, recipient) must match the proposal;
  - level-2 calls the person did not ask for are denied;
  - an end time not in the person's words may be at most 20 minutes ahead, and one in the words at most 2 hours;
  - while waiting, nothing moves, the requester is told, and a second identical request sends no second prompt;
  - routing is fan-out (all approvers at once, as in B01) or escalate (requester or first approver, the rest after 45 s).
- **Standing rules** (`standing.py`). Set in advance by an approver. Each has a tool, who may ask, exact arguments, days, a time window, uses per day, a longest window, and optionally "only if the household entered an expected delivery". A rule grant tells its owner every time. It runs after permissions, traceability, house rules and freshness, so it cannot skip them. No rule lets a voice unlock a door: B01 measured a replayed recording accepted as the speaker in 81% of trials.
- **Doubles** (`agent.py`, `phone.py`). `B07Planner` is deterministic. `InjectionProneLLM` obeys instructions found in device names, calendar entries and delivery notes with probability `p_follow`, in three ways: add calls, swap arguments, or replace the request. `PersonModel` answers by availability (free, busy, asleep). With L prompts in the last 24 hours, P(hurried) = min(0.9, 0.05 + 0.03 L) and P(ignore) = min(0.6, 0.03 + 0.01 L). A hurried answer is yes 95% of the time. A careful answer is yes only to the action the person wants. Delays are lognormal with a median of 15 s free and 60 s busy. These numbers are assumptions, not measurements.
- **Independent checker** (`checker.py`). Reads only bus traffic, its own copy of phone keys and the rule specs. For every high-risk device command, it looks for a matching prompt, a valid signed yes in time, or a rule that allows it.

## 3. Method

1. **Attacks** (A): 10 attack types, 50 trials each, against a B01-style engine and GatedEngine. The B01-style engine is the B01 engine with only a doorbell identity added. The attacker can read and publish on the home bus but has no phone key. Success means a high-risk command the trial did not allow.
2. **Away** (B): 100 courier requests per row. 5 presence scenarios, fan-out vs escalate, time-to-live 120 s vs 300 s.
3. **A month** (C): 30 simulated days for each of 2 households, under 5 designs. Each day has:
   - about 17 level-1 voice requests, 4% of them misheard;
   - garage closing and arming;
   - own level-3 requests: unlocking the back door, water back on, sharing a clip;
   - expected deliveries, fake couriers, a stranger at night, and a weekly cleaner;
   - Mark away three days.

   The fatigue slope was also run at 0.015 and 0.06.
4. **Injection** (D): 300 scenarios per row. Each has one injected instruction in a device name, calendar entry or delivery note. Three engines, three modes, careful or hurried approvers, and p_follow 1.0 (0.3 for GatedEngine only).

## 4. Results

### 4.1 Attacks on the approval itself (`results.attacks`)

| Attack (50 trials each) | B01-style | GatedEngine |
|---|---|---|
| Old reply replayed later | 0 | 0 (replayed) |
| Old reply reused for a new request at night | 50 | 0 (bad signature) |
| Forged reply | 50 | 0 (bad signature) |
| Reply from the wrong person, own topic | 0 | 0 (wrong person) |
| Wrong person's phone posting as Anna | 50 | 0 (bad signature) |
| Reply after time-to-live | 0 | 0 (expired) |
| Reply after the garage window ended | 50 | 0 (window passed) |
| Arguments changed after the prompt | 50 | 0 (action changed) |
| Phone app sends yes with the other request's id | 50 | 0 (digest mismatch) |
| Same reply delivered twice | 0 | 0 (replayed) |

"Open the garage for the courier until 18:55" cannot be used to open it at 23:00. The old reply is refused as replayed. Pasted onto a new request, it fails the signature, because the signature covers the request id. GatedEngine closed the garage at the end of the window in all 150 trials that opened it. The B01-style engine never did.

The checker found 0 unapproved level-3 executions out of 150 in GatedEngine. With reply checks and execution binding switched off, it flagged 300.

### 4.2 Away, asleep, offline (`results.away`)

With both approvers free, fan-out got every courier in, p50 22.2 s and p95 34.1 s from doorbell to confirmed open, with 2.0 prompts per request. Escalation cut that to 1.05 prompts at p50 26.5 s and p95 54.3 s.

Both busy: 97% within 120 s (fan-out), 96% (escalate); 100% with a 300 s time-to-live. When Anna's phone was offline for 3-10 minutes and Mark was busy, escalation at 120 s got only 65% in, and refused 70 late replies. Fan-out got 85%. A 300 s time-to-live got 100%, at p95 244.4 s. Both asleep: 0%, by design.

In all 20 configurations the garage moved 0 times while a request was waiting.

### 4.3 A month: prompts, wrong approvals, blocked requests (`results.month`)

| Design | Prompts per day (Anna / Mark / Sam) | Wrong level-3 done | Wrong level-1/2 done | Legit blocked |
|---|---|---|---|---|
| Ask for everything | 9.23 / 6.02 / 5.0 | 1 | 5 | 16.4% |
| Ask by level (B01) | 2.23 / 1.52 / 0 | 1 | 35 | 3.9% |
| By level, escalate | 2.07 / 1.06 / 0 | 1 | 35 | 3.7% |
| Standing rules with limits | 0.62 / 0.40 / 0 | 1 | 35 | 3.2% |
| Standing rules, no limits | 0.42 / 0.25 / 0 | 6 | 35 | 3.2% |

These cover 60 household-days and 1,139 legitimate requests. Asking for everything gave 20.25 prompts a day and 260 hurried answers. 16.4% of legitimate requests were blocked, mostly because nobody answered in time (135). Standing rules with limits gave 1.02 prompts a day and 2 hurried answers, and level-3 actions came faster: p50 12.6 s against 30.0 s for ask by level.

Removing the limits saved another 0.35 prompts a day and let in 6 wrong garage openings instead of 1, all fake couriers or night strangers. The one wrong opening with limits was a fake courier on a day a delivery was expected. Unapproved level-3 executed: 0 in every design.

The 35 wrong level-1 actions are misheard requests (wrong light, wrong shade) that run because level 1 does not ask. Asking for everything caught them, at five times the prompts. 36 blocked requests in every design were misheard.

With the fatigue slope at 0.015 and 0.06, the ordering holds: 20.1 prompts and about 16% blocked for ask everything; 0.93 prompts and 3.5% blocked for rules.

### 4.4 Prompt injection (`results.injection`)

| Engine | Injected level 3 reaching a phone | Executed with a hurried approver | Injected level 2 reaching a phone |
|---|---|---|---|
| B01-style | 11-17 per 300 | 13-15 | 26-37 |
| Gated, argument trace off | 4-12 | 5-12 | 0 |
| GatedEngine | 0 | 0 | 0 |

Unapproved level-3 executed: 0 in all 24 rows. In GatedEngine, false blocks of legitimate requests were 0 and prompts with untrusted text were 0. Injected level-1 calls still ran (0-8 per row), as B01 allows. When the double replaced the person's request with the injection, up to 284 of 300 requests were lost. No gate fixes a model that ignores the person.

## 5. What held, what broke

**Held.** Approvals that are single-use, signed by the phone and bound to the digest of the exact action stopped every attack type here. Doing nothing while waiting held: 0 movements. Untrusted text never reached a prompt. The zero from B01 stayed zero under the new attacks.

**Broke, in B01 code** (wrapped here, not edited):
1. The B01 engine executes whatever the arguments object holds when the yes arrives (50/50).
2. It trusts any message on `reply/<person>`, and request ids are sequential (50/50 forged).
3. It traces the tool, not the arguments, so "front door" for "back door" reaches the phone.
4. It sends untraceable level-2 calls as questions that look like the person's own.
5. Its prompt for an unknown tool reads "open_garage done?".

**Broke, in ours.** A phone mix-up is refused, but the intended action is lost too (0/50 done), so the person taps again. Escalation is worse than fan-out when the first approver's phone is offline (65% vs 85%). The courier rule lets in a fake courier who arrives first on an expected day.

## 6. Limits

- No language model was run. Doubles stand in for it, and a real model will fail in other ways. The gate does not rely on the model, but the lost-request numbers do.
- People are a model. The fatigue curve, ignore rates and delays are assumptions. The ordering of designs is the claim; the absolute counts are not.
- HMAC keys are drawn from the seeded simulator. A real phone needs a hardware-backed key, enrolment and recovery. A stolen or coerced phone counts as its owner here. An attacker who controls the hub is out of scope.
- There is no camera check of who is at the door. How the expected-delivery entry is made is not modelled.
- Devices no decision reads skip their heartbeat, to keep the run time down.
- There are no device faults here; B01-B04 cover them.

## 7. Reproduce

```
pip install -r requirements.txt
pytest -q tests/test_b07_approval.py
python experiments/b07_approval/run.py      # about 2-3 minutes on 2 cores
```

## 8. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
