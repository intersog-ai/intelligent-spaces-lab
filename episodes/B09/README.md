# B09 · What leaves the house

*Intelligent Spaces · Build Track · episode 9 of 10 · by [Intersog](https://www.intersog.com)*

Season 1 episode nine keeps the core at home and sends things out only for a purpose. Asked "what did you send out today?", the assistant answers: twelve speech requests, not stored, and last night's encrypted backup. We built a ledger of every byte that leaves the twin, an answer at three levels that is graded for what it leaves out, a device that phones home seven ways, and deletion of shared data that asks first and returns a receipt. Then we planted hidden copies and took a backup before the delete.

Video: [CHECK: YouTube URL] · synthetic narration · research in simulation, provided as is.

## Findings

| # | Finding | Number | Evidence |
|---|---|---|---|
| 1 | Most of what leaves, nobody would notice | E09 day: 292 of 304 messages (96.05%) and 99.66% of bytes were background; 30 days: 95.18% of messages on average | [`experiments/b09_egress`](../../experiments/b09_egress) |
| 2 | The scripted answer leaves categories out | E09 line missed 4 of 6 categories; the B01 summary covered every category on 0 of 30 days; the B09 short answer on 30 of 30 | [`results.json`](../../experiments/b09_egress/results.json) |
| 3 | Short can still be complete | 48 words for the E09 day, background grouped with its count; 300 of 300 random days pass the coverage test | [`islab/privacy2/answer.py`](../../islab/privacy2/answer.py) |
| 4 | Check the sender, not only the label | A plug posing as an update check got out 100% under B01 rules, 0% with manifests; household told at the first attempt | [`islab/privacy2/ledger.py`](../../islab/privacy2/ledger.py) |
| 5 | Manifests have limits and costs | A declared sender leaking inside its manifest: 100% got out, 0% detected; holding an oversize update agent blocked 800 of 800 real checks | [`experiments/b09_egress/notes.md`](../../experiments/b09_egress/notes.md) |
| 6 | Internet down: the home keeps working | 200 of 200 commands confirmed; 48 local voice requests handled, 12 cloud ones answered "I did not understand"; the leak alarm reached the centre 10 h later | [`results.json`](../../experiments/b09_egress/results.json) |
| 7 | Shared data asks first | 500 shared deletions: 397 deleted, 43 declined, 60 no answer; 0 deleted without a yes | [`islab/privacy2/deletion.py`](../../islab/privacy2/deletion.py) |
| 8 | Hidden copies | Of 7 kinds of hidden copies, B01 delete removed 2, B09 removed 6; the one left (re-encoded without lineage) is named on the receipt as not checked | [`experiments/b09_egress/notes.md`](../../experiments/b09_egress/notes.md) |
| 9 | Our own bug: the B01 receipt overclaims | It said "complete" in 264 of 500 trials, each with five copies left | [`results.json`](../../experiments/b09_egress/results.json) |
| 10 | A backup taken before the delete | Per-record key destroyed: 0 of 183 backup copies readable after the delete; one key per night: 183 of 183 readable until rotation | [`islab/privacy2/deletion.py`](../../islab/privacy2/deletion.py) |

## Proving core: what reproduces this episode

```
git checkout b09
pip install -r requirements.txt
pytest -q tests/test_b09_egress.py           # ledger, answer coverage, manifests, consent, hidden copies, backups
python experiments/b09_egress/run.py        # 5 parts, about 30 seconds
```

## Documents in this episode

- [`docs/paper_b09.md`](../../docs/paper_b09.md): method, results, limits.
- [`experiments/b09_egress/notes.md`](../../experiments/b09_egress/notes.md): results tables and what we fixed along the way.
- [`experiments/b09_egress/results.json`](../../experiments/b09_egress/results.json): every number in this episode.
- [`islab/privacy2/`](../../islab/privacy2/): the ledger gate, the three-level answer, the phone-home device and voice router, shared-data deletion.
- [`script.txt`](script.txt): the narration, word for word.
- [`visuals.md`](visuals.md): one moving visual per paragraph.
- Downloadable pack: [`IS_B09_pack.zip`](https://github.com/intersog-ai/intelligent-spaces-lab/releases/tag/b09) (release asset, same content as tag `b09`).

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work. [www.intersog.com](https://www.intersog.com)

Questions about the research: open an issue in this repository.
