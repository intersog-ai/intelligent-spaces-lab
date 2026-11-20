# B06 · Who is speaking

*Intelligent Spaces · Build Track · episode 6 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode six knows who is speaking and answers with that person's preferences. B01 showed a voice match is not proof: a replayed recording passed as the person 81% of the time. Here we ask what a home can safely do with knowing who is speaking. We built a trust policy where voice personalizes and can raise trust, but never unlocks, disarms or opens the garage on its own. A household of three synthetic voices, a teenager and guests, replay attacks with simple liveness checks, two people talking at once, and 534 requests through the twin's policy engine.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is. This is not a biometric system.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | Voice never opens the house on its own | 0 level-3 actions by voice alone out of 534 requests; 55 ran, all after a phone yes; 23 spoken "yes" answers refused | [`experiments/b06_identity`](../../experiments/b06_identity) |
| 2 | If voice were allowed to decide, replays would open doors | Counterfactual: 85 level-3 actions by voice alone, 52 of them attacks; Bluetooth replay 26 of 65 | [`results.json`](../../experiments/b06_identity/results.json) |
| 3 | Two tiers, two thresholds | Comfort 0.7575: family refused 8.0%, guests accepted 16.7%. Named 0.8175: family refused 25.3%, guests 0 of 18, no member mistaken for another | [`islab/identity/household.yaml`](../../islab/identity/household.yaml) |
| 4 | A refused family member gets a next step | Named tier: 75.9% recognised first try, 96.6% within two tries, 100% with a 4 s sentence; private data goes to the phone instead of out loud | [`islab/identity/trust.py`](../../islab/identity/trust.py) |
| 5 | Noise and a new room cost the family | Named-tier refusals: 32.2% in 20 dB kitchen noise, 57.5% in a different room | [`results.json`](../../experiments/b06_identity/results.json) |
| 6 | Cheap liveness stops a phone speaker, not a good speaker | Named-tier replay accepted: phone 1.1% to 0%, Bluetooth 48.3% to 44.8%, hi-fi 47.1% to 41.4%; liveness refused 8.0% of the family and 65.2% of real recorded voices | [`islab/identity/liveness.py`](../../islab/identity/liveness.py) |
| 7 | A challenge phrase beats a recording, not a clone | Modelled: 0 of 1,000 phrases per person found in any recorded line | [`experiments/b06_identity/notes.md`](../../experiments/b06_identity/notes.md) |
| 8 | Private data read aloud is exposed to replay | Calendar read out to a Bluetooth replay 8 of 22 times; a teenager talking over a parent got it 4 of 27 | [`results.json`](../../experiments/b06_identity/results.json) |
| 9 | Our overlap rule does not detect overlap | Flagged 6.0% of two-voice mixes and 8.0% of single voices | [`experiments/b06_identity/notes.md`](../../experiments/b06_identity/notes.md) |
| 10 | The phone approval is the real control | With 10% careless approvals, 24 attack actions ran, all approved on a phone | [`results.json`](../../experiments/b06_identity/results.json) |

## Proving core: what reproduces this episode

```
git checkout v1.0
pip install -r requirements.txt -r requirements-voice.txt
pytest -q tests/test_b06_identity.py         # trust policy, liveness, identity home, results invariants
ISLAB_VO=/path/to/voice-over python experiments/b06_identity/run.py   # about 2-3 minutes
```

## Documents in this episode

- [`docs/paper_b06.md`](../../docs/paper_b06.md): method, results, limits.
- [`experiments/b06_identity/notes.md`](../../experiments/b06_identity/notes.md): results tables and what we fixed along the way.
- [`experiments/b06_identity/results.json`](../../experiments/b06_identity/results.json): every number in this episode.
- [`islab/identity/`](../../islab/identity/): trust policy, household settings, liveness checks, identity home.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): scene plan.
- Downloadable: the complete research file and pack, release [`v1.0`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/v1.0) (`intelligent-spaces-research.pdf`, `IS_series_pack.zip`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
