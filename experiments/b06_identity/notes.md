# b06_identity: notes (2026-11-19)

`ISLAB_VO=/path/to/voice-over python experiments/b06_identity/run.py` takes about 2-3 minutes on 2 shared cores (one full run at seed 606, about 50 s, plus three shorter seeds for the spread). It writes `results.json`. Every number below is from that file, seed 606 unless marked.

**Household.** anna, mark and sam (teen) are played by the Amanda, David and Ollie voices, the only series voices with enough audio (54 s, 224 s, 65 s). Each is enrolled on its first 10 s of speech, heard through the B01 satellite channel. Guests are never enrolled: Courtney, Chris, Eric (the S1 Anna, Mark and Sam voices, 1.4-6.2 s each), the AnnaAssist voice, and three LibriSpeech clips (real people) bundled with the sherpa-onnx test files. Evaluation: 87 member windows (anna 12, mark 60, sam 15) and 18 guest windows. These are small numbers.

**Thresholds (cost model, chosen on calibration, reported on evaluation).**

| Tier | Used for | Threshold | Family refused | Wrong family member | Guest accepted |
|---|---|---|---|---|---|
| comfort (false accept cost 1) | music, temperature | 0.7575 | 8.0% | 0% | 16.7% (3 of 18) |
| named (false accept cost 10) | calendar aloud, acting as the person | 0.8175 | 25.3% | 0% | 0% (0 of 18) |
| B01 room_strict, for comparison | | 0.8241 | 31.0% | 0% | 0% |

The search also allowed a top-1/top-2 margin; it chose 0. Across seeds 606-609 the named threshold ranged 0.77-0.8175, named family refusals 11.5-25.3%, named guest acceptance 0-11.1%.

**Retries.** At the named tier 75.9% of family requests were recognised on the first 2 s window, 96.6% within two tries, 100% with one 4 s utterance (29 requests). Kitchen noise at 20 dB raised named refusals to 32.2%; a different room (RT60 0.8 s, 20 dB) to 57.5%.

**Replay, named tier, 87 replays per device.**

| Device | Accepted, no liveness | Accepted, with liveness | Liveness passed |
|---|---|---|---|
| phone speaker | 1.1% | 0% | 6.9% |
| Bluetooth speaker | 48.3% | 44.8% | 93.1% |
| hi-fi speaker | 47.1% | 41.4% | 88.5% |

At the B01 room EER threshold, Bluetooth replay was accepted 79.3% (B01 reported 81%), 73.6% with liveness. The one-class liveness check rejected 8.0% of the family's own live windows (target was 5%), 65.2% of the real LibriSpeech clips and 50% of the AnnaAssist windows: it learns "this household through this microphone", not "a live person". The "max" scoring variant (reported only, not tuned on attacks) did no better. The challenge phrase is modelled: 0 of 1,000 random phrases appear in any recorded line of any member (3,600 combinations). A voice clone says any phrase.

**Two people talking.** The overlap rule (second-best score at least 0.8061) flagged 6.0% of mixes and 8.0% of single-speaker windows: it does not detect overlap. Mixed audio mostly fell below every threshold. With sam talking over a parent, the named tier named the parent in 8 of 81 mixes (6 with the rule).

**Through the policy engine (534 requests).** 55 level-3 actions ran, all after a phone yes; 0 by voice alone. 23 spoken "yes" answers to level-3 approvals were all refused. No attack opened anything when approvers said no. With 10% careless phone approvals, 24 attack level-3 actions ran (all through the phone). With voice allowed to authorize level 3 for a recognised person, 85 level-3 actions ran by voice alone, 52 of them attacks (Bluetooth replay 26 of 65, hi-fi 23 of 65, teen-over-parent 3 of 54, phone 0 of 66).

**Private data.** No family calendar went to the wrong family member, but replays had the calendar read out: Bluetooth 8 of 22, hi-fi 7 of 22; teen-over-parent 4 of 27. Reading private data aloud on a voice match is exposed to replay.

**Family refused.** Of 11 calendar requests, 6 were read out, 2 offered to the phone, 3 refused (liveness false reject). Of 54 action requests, 21 were treated as a guest's; 4 were refused because the voice was not recognised (disarm), 1 by permission (Sam, disarm). Of 32 level-3 requests, 27 ran after a phone yes.

## Found along the way

1. **Torch threads on a shared machine.** Two torch threads made each embedding 1.05 s instead of 0.036 s under load from parallel jobs. `run.py` sets one thread. `experiments/speaker_id/run.py` would benefit too (not edited).
2. **First counterfactual was wrong.** Lowering level 3 to level 1 also let unrecognised voices open doors, because the guest allow-list lets a guest *ask* for a door. The counterfactual now removes door tools from the guest list, so it measures exactly "voice authorizes a recognised person".
3. **Engine words for new tools.** `islab.policy.engine._intent` has no words for `open_garage` and `disarm_alarm`, so the phone approval text would read "Approve: open_garage done?". The home's spoken replies use local words; the engine text needs a fix in `islab/policy` (not edited).
4. **Liveness threshold does not transfer even between halves of the same recordings** (5% target, 8.0% measured; 8.0-14.9% across seeds 606-609).
