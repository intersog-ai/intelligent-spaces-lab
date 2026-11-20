# Who is speaking: what a home may do with a voice match, in a digital twin

Intelligent Spaces Lab, Build Track B06 · [Intersog](https://www.intersog.com) · November 19, 2026 · Apache-2.0

> **Provided as is.** This is open research material. It was produced in a simulator, with synthetic voices and simulated devices, by AI agents under human direction. It is not a product and not security advice. **It is not a biometric system and not anti-spoofing.** Use it at your own risk and for your own benefit.

## 1. Question

Season 1 episode six recognises who is speaking and answers with that person's preferences. It also says: "Only Anna or Mark can unlock the front door. I've sent Anna a request." B01 measured the voice match itself: equal error rate 1.2% on clean audio, 11.9% through a simulated room, and a Bluetooth replay of a person's own recording accepted as that person in 81% of trials at the room threshold (`experiments/speaker_id/results.json`).

This study asks: what can a home safely do with knowing who is speaking? Specifically, how often does the family get refused, how often does someone else get treated as a family member, how much do cheap replay checks help, and does any level-3 action (unlock, disarm, open garage) ever run on a voice alone?

## 2. Design under test

- **Trust policy** (`islab/identity/trust.py`, `household.yaml`, new). A voice match gives one of three tiers. *Guest*: not recognised; house defaults. *Comfort*: music, temperature preference, greeting; a wrong guess costs little. *Named*: private data read out loud (calendar, messages) and acting as the named person in the B01 policy engine for level 1 and 2. A failed liveness check or two voices at once caps the tier at comfort. Level 3 is never decided here: the policy only names the requester, and `islab.policy.PolicyEngine` requires a single-use phone approval. When the home is not sure who asks for private data, it does not read it aloud; it offers to send it to the person's phone.
- **Per-person permissions.** Anna and Mark are adults and approvers. Sam, a teenager, may ask to unlock the door or open the garage (a parent approves on the phone) and may not disarm the alarm. A guest may ask for a door (an adult approves) and may not disarm.
- **Twin** (`islab/identity/home.py`, new): the B01 twin and policy engine, the alarm panel from `islab/suites/house.py`, and two level-3 tools the base registry lacks, `open_garage` and `disarm_alarm`, added by subclassing. A probe wraps the engine's execute step and records every execution with its approval, independent of the engine's own bookkeeping.
- **Liveness** (`islab/identity/liveness.py`, new). Six channel cues per 2 s window: low-band and high-band energy, spectral flatness, roll-off, clipping, envelope range. One-class: fitted only on the household's own live speech heard through the satellite, threshold set to reject 5% of it. No attack audio is used to fit it. A challenge phrase (random word plus number, 3,600 combinations) is modelled on transcripts.

## 3. Method

1. **Voices.** The S1 character voices have 1.4 to 6.2 s of speech each, too little to enroll. The household is therefore played by the three series voices with enough audio: anna = Amanda (54 s), mark = David (224 s), sam = Ollie (65 s). Guests, never enrolled: Courtney, Chris and Eric (the S1 Anna, Mark and Sam voices), the AnnaAssist voice, and three LibriSpeech clips of real people bundled with sherpa-onnx. LibriSpeech (Panayotov, Chen, Povey and Khudanpur, 2015) is published under CC BY 4.0; the clips are used as distributed in the sherpa-onnx test files and are not included in this repository.
2. **Channel.** Every window passes through the B01 satellite channel (synthetic room, RT60 0.4 s, 30 dB pink noise). Embeddings: Resemblyzer GE2E, 2 s windows, 1 s hop, as in B01.
3. **Splits.** Enrollment = first 10 s. The rest is split in time: calibration (first half) chooses thresholds, the liveness fit and the overlap rule; evaluation (second half) gives every reported rate. Evaluation: 87 member windows (anna 12, mark 60, sam 15), 18 guest windows.
4. **Thresholds.** Minimise expected cost (1 − π)(refused + c × wrong member) + π × c × guest accepted, π = 0.2, c = 1 for comfort and 10 for named; the named search also allowed a top-1/top-2 margin.
5. **Attacks.** Replay of each member's evaluation windows through a phone speaker and a Bluetooth speaker (B01 proxies) and a new near-flat hi-fi speaker proxy, then the room. Two people: two members' windows summed at −6, 0 and +6 dB, then the room.
6. **Engine.** 534 requests from all of the above go through the policy engine in the twin: family requests (clean and in 20 dB kitchen noise), guests, replays and a teenager talking over a parent. Phone replies are scripted: legitimate requests get a yes, others a no. Two variants: 10% careless yes answers to illegitimate requests, and a counterfactual where level 3 acts on the voice of a recognised, permitted person.
7. Seed 606; seeds 607-609 repeat the pipeline with other rooms, noise and replay draws.

## 4. Results

### 4.1 Thresholds and errors (evaluation set)

| Tier | Threshold | Family refused | Named as another member | Guest accepted |
|---|---|---|---|---|
| comfort, cost 1 | 0.7575 | 8.0% | 0% | 16.7% (3 of 18) |
| named, cost 10 | 0.8175 | 25.3% | 0% | 0% (0 of 18) |
| B01 room EER | 0.7752 | 11.5% | 0% | 16.7% |
| B01 room strict | 0.8241 | 31.0% | 0% | 0% |

The chosen margin was 0. No family member was named as another at either tier. At the comfort tier, one of two Courtney windows was taken for Anna, Eric's one window for Mark, and one of 12 LibriSpeech windows for Anna. Across seeds 606-609: named threshold 0.77 to 0.8175, family refused 11.5% to 25.3%, guest accepted 0% to 11.1%.

### 4.2 When the family is refused

| Named tier | Recognised |
|---|---|
| first 2 s try | 75.9% |
| within two tries | 96.6% |
| one 4 s utterance | 100% |

29 requests. Refusals at the named tier rise with conditions: 32.2% in 20 dB kitchen noise, 67.8% at 10 dB, 57.5% in a different room (RT60 0.8 s, 20 dB). In the engine run, 87 clean family requests: 11 calendar requests gave 6 read out, 2 offered to the phone, 3 refused by a liveness false reject. 22 comfort requests: 20 personalized, 2 house defaults, 0 for the wrong person. 54 actions: 21 handled as a guest's; 4 refused because the voice was not recognised (disarm), 1 by permission (Sam, disarm). 32 level-3 requests: 27 ran after a phone yes. What the home says when it is not sure:

> I'm not sure who's asking, so I won't read a calendar out loud. Tell me your name and I'll send it to your phone.
>
> I can't disarm the alarm for a voice I don't recognise. Anna or Mark can do it in the app or at the keypad.
>
> Only Anna or Mark can unlock the front door. I've sent them a request.

### 4.3 Replay and liveness (named tier, 87 replays per device)

| Device | Accepted as the victim | With liveness | Liveness passed |
|---|---|---|---|
| phone speaker | 1.1% | 0% | 6.9% |
| Bluetooth speaker | 48.3% | 44.8% | 93.1% |
| hi-fi speaker | 47.1% | 41.4% | 88.5% |

At the comfort tier, Bluetooth replay was accepted 81.6% and hi-fi 88.5%. At the B01 room EER threshold, Bluetooth replay was accepted 79.3% (B01: 81%) and 73.6% with liveness. Seeds 606-609: Bluetooth replay at the named tier 48.3% to 72.4% without liveness, 42.5% to 64.4% with it.

The liveness check rejected 8.0% of the family's own live windows (target 5%; 8.0% to 14.9% across seeds), 65.2% of the real LibriSpeech clips and 50% of the AnnaAssist windows. It learns how this household sounds through this microphone. It catches the band-limited, clipped phone replay and little else. The challenge phrase, modelled: 0 of 1,000 random phrases per member appear in any recorded line. That stops a recording; a voice clone says any phrase.

### 4.4 Two people talking

Mixed speech mostly fell below every threshold. With Sam talking over a parent, the named tier named the parent in 8 of 81 mixes. The overlap rule (second-best score at least 0.8061) flagged 6.0% of mixes and 8.0% of single-speaker windows: it does not detect overlap, and removed only 2 of those 8.

### 4.5 Level 3 through the policy engine (534 requests)

| Policy | Level 3 run | By voice alone | Attacks that ran |
|---|---|---|---|
| B06 policy, approvers say no to attacks | 55 | 0 | 0 |
| same, 10% careless phone yes | 79 | 0 | 24 (all approved on a phone) |
| counterfactual: voice authorizes a recognised person | 85 | 85 | 52 |

In the counterfactual, Bluetooth replay opened 26 of 65 level-3 requests, hi-fi replay 23 of 65, a teenager over a parent 3 of 54, phone replay 0 of 66. In the B06 policy, 23 spoken "yes" answers to pending level-3 approvals were all refused. Sam's two unlock and two garage requests went to a parent's phone and ran after a yes; his disarm request was refused.

### 4.6 Private data

No calendar went to the wrong family member. Replays did get calendars read out: Bluetooth 8 of 22, hi-fi 7 of 22, a teenager over a parent 4 of 27.

## 5. What held, what broke

**Held.** Zero level-3 actions ran on a voice alone, in every run and seed. A spoken yes never counted. Personalization worked: 20 of 22 comfort requests were personalized and none for the wrong person. A family member who is refused gets a next step in plain words, and a second try or a longer sentence fixes most refusals.

**Broke.** Cheap liveness cues do not stop a decent loudspeaker: they cut named-tier Bluetooth replay from 48.3% to 44.8%. They also refuse real voices from other microphones. The overlap rule does not work. Reading private data aloud on a voice match is exposed to replay (8 of 22). The phone approval is the control that matters, and it is only as good as the person: 10% careless answers let 24 attacks through. A quarter of the family's first tries are refused at the named tier, more in noise or another room.

## 6. Findings in our own code

1. Two torch threads were 30 times slower than one on the shared 2-core machine (1.05 s against 0.036 s per embedding). The run uses one thread.
2. The first counterfactual also let unrecognised voices open doors, through the guest allow-list. It now measures only "voice authorizes a recognised person".
3. `islab.policy.engine._intent` has no words for `open_garage` and `disarm_alarm`; the phone approval text would read "open_garage done". The spoken replies in B06 use local words; the engine text is an open fix in `islab/policy`.
4. The liveness threshold set on one half of the recordings did not hold on the other half.

## 7. Limits

Every household voice is ElevenLabs TTS: same timbre, same microphone, same recording chain every time. Real people vary with colds, mood, distance and age, and teenagers' voices change over months; real refusals will be higher. The household is played by narrator and assistant voices, not the S1 characters. There are 3 enrolled voices and 18 guest evaluation windows, so guest acceptance rests on a handful of trials; windows overlap and are correlated, and no confidence intervals are reported. Calibration and evaluation come from the same recordings. Rooms, noise and replay devices are DSP proxies; the liveness check is fitted and tested in the same synthetic room. The challenge phrase was not tested with a speech recogniser, so its own false rejects are not measured. Phone approvals are scripted. Overlap is two clean windows summed. This is not a biometric system.

## 8. Reproduce

```
pip install -r requirements.txt -r requirements-voice.txt
pytest -q tests/test_b06_identity.py
ISLAB_VO=/path/to/voice-over python experiments/b06_identity/run.py    # about 2-3 minutes on 2 cores
```

The Season 1 voices are not distributed; without them the policy and liveness tests still run.

## 9. Contact

This research is published by Intersog. If you build devices, platforms or spaces like these, talk to our engineers: [www.intersog.com](https://www.intersog.com).
