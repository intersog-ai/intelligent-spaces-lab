# B10 notes: fleet, cloud and the full test

Run: `python experiments/b10_fleet/run.py` (6 seeds per fault mix, 20 fleet seeds, 1,000 homes). It took 178 s on 2 cores that were shared with other jobs (`results.runtime_s`); about 100 s when the cores are idle. Seeds were cut from the 50 the B01 scorecard used to stay near 3 minutes.

## What is real and what is modelled

- **Real code, simulated world.** Every layer is the code from B01-B09, called through its public API (`islab/fleet/full.py`). Nothing in another package was edited.
- **B02 is modelled in the loop.** The B02 MQTT broker runs on wall-clock time; the day runs on virtual time. B02 comes in as three things: the Matter data-model round trip (`islab.connect.matter_model`), Home Assistant discovery configs (`islab.connect.ha_discovery`), and Last Will availability 15.0 s after a power loss. The 15.0 s was measured in B02 (`experiments/b02_connect/results.json`).
- **B05** uses the mini PC tier. Its stage times come from `experiments/b05_pipeline/measured.json`. The utterance text is used as the transcript (no audio in the day).
- **B06** uses the bundled household embeddings and the `household.yaml` thresholds. On seed 0 all 10 utterances were named-tier. The bundled scores against the speaker's own profile are 0.916 to 1.0.
- **The fleet is a model** (`islab/fleet/rollout.py`). Availability, ring sizes, soak time, halt threshold, operator delay and the quiet bad update are assumptions, printed in `params`. Two inputs come from twin days. One is the loud bad update's effect on a home's health report. The other is the good version's false-failing share.
- **No cloud was used.** Two documentation pages, for AWS IoT Greengrass V2 and Azure DPS, could not be opened from this machine (the permission request timed out). The mapping table therefore uses only ledger rows 8a-8c.

## Interaction bugs (one fix each; `results.full_test.ablation` shows each fix switched off)

| Fix | Bug found when the layers ran together | Seen with the fix off (seed 0, no faults) |
|---|---|---|
| F1 | B07 accepts only signed replies; the B01 day's phones sent unsigned ones | 4 replies refused as `bad_signature`; arm away, intrusion, garage, courier, clip share and camera ask scenes fail |
| F2 | B09 manifests did not declare the day's senders | backup, update check and the consented clip blocked; backup, updater, camera observer quarantined |
| F3 | B05 republishes the transcript on `voice/<sat>` and the B01 hub handled it again | 10 utterances handled twice, 16 extra decisions, 31 actions instead of 19; P4 fails (a second, unidentified "keep the heating on" was refused silently) |
| F4 | the B05 grammar does not know "good night" or "keep the heating on" | both not understood; window and good-night scenes fail |
| F5 | B01's P5 counts B08's cellular alarm path, a consented clip and the B04 push purpose as forbidden egress | P5 fails in 18 of 18 full-test days; the route-aware adapter P5r passes 18 of 18 |
| F6 | the B01 house leak rule and the B04 controller both command the valve | two sources close the valve; 4 leak messages instead of 3 |
| F7 | the scripted people answer B01's prompt wording; B07 words the same action differently | courier and clip-share approvals never answered |
| F8 | B06 `household.yaml` replaces Sam's deny list instead of adding to it | Sam's "unlock the front door" goes to a parent for approval instead of being refused |
| F9 | B08's clip-consent check runs before the destination check, so B09 never sees a phoning-home camera as undeclared | no notice to the household; the camera is never held |
| F10 | B03 calls a thermostat that holds its setpoint "stuck" | 4 false "I can't trust the thermostat" messages a day |

F5 is an adapter, not a runtime switch: every run computes both P5 (unchanged) and P5r.

## Other observations

- With the B07 gate on, a camera ask reply on `reply/anna` is also seen by the ledger and refused as `unknown` (1 a day). This is harmless, but two subsystems share one reply topic with different id spaces.
- Anna's phone approval to share a clip with grandma (B07) is not a B08 consent. The share is blocked either way: grandma is not allowlisted. The two consent records are not joined.
- After the blocked share, B09 quarantines `policy.engine` as a source and the notice blames the engine. No later egress from the engine was needed that day.
- Under heavy faults, in 2 of 6 days the courier unlock physically happened but its state report was lost, so it timed out. The relock in `islab/suites/house.py` runs only on a confirmed unlock, so the front door was still unlocked at 19:05. This is B01 House behaviour and it is open.
- The B01 request-id counter (`PolicyEngine._ids`) is process-global. `run.py` resets it before each run so traces repeat.
- `FastBus` caches topic matches. The trace is identical to `Bus` (test).
