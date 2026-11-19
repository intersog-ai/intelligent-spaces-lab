# Intelligent Spaces Lab

A digital twin of a smart home for testing one claim: an AI home assistant can be trusted only if it is engineered that way. The language model proposes, code decides, devices confirm.

This repo accompanies the *Intelligent Spaces* Build Track by [Intersog](https://www.intersog.com). Each episode has a folder in [`episodes/`](episodes/) with its description, findings, evidence and documents. Episode [B01](episodes/B01/) builds the twin and breaks it.

> **Research material, no guarantee.** Everything here runs in simulation, with synthetic voices and simulated devices. It is not a product, not a certified alarm system and not safety advice. Use it at your own risk and for your own benefit. Life-safety equipment must be listed and installed by qualified people.

## What is inside

| Folder | What it does |
|---|---|
| `islab/core` | Deterministic discrete-event simulator and an MQTT-like bus. `CONTRACT.md` fixes topics and the meaning of "confirmed". |
| `islab/twin` | 15 devices, a household (Anna, Mark, Sam), a fault injector: loss, delay, stuck and partial actuators, stale sensors, clock skew, internet outages, hostile device names. |
| `islab/policy` | State store with freshness; MCP-shaped tools; policy engine (act and tell, ask first, never alone); action tracker that confirms by device state; agent with a rule planner, a scripted adversary and an OpenAI-compatible client (llama.cpp, Ollama, vLLM); audit log. Edit `policy.yaml`. |
| `islab/voice` | Speaker identification (GE2E via Resemblyzer); voice pipeline latency model. |
| `islab/alarm` | SIA DC-09 encoder and decoder; alarm path to a simulated monitoring centre; local siren independent of the hub; egress gate with purpose tags; delete with receipts. |
| `islab/suites` | The five principles as tests, plus ALARM and BUS checks, over one household day. |
| `experiments/` | Seeded experiments, each with `run.py`, `results.json`, `notes.md`. |
| `episodes/` | One folder per episode: description, findings with links to evidence, the commands that reproduce it, narration. |
| `docs/` | `paper.md` (method, results, limits), `prompts.md` (the AI agent prompts that built this), `facts-ledger.md` (facts checked against primary sources), `architecture.svg`. |

## The five principles, as tests

1. Fresh, sourced readings: no decision rests on a stale or unsourced reading.
2. A command sent is not a result: every action ends confirmed, failed or timed out.
3. People set limits and can override: zero unapproved level-3 actions; overrides logged.
4. It says when something fails: every failure or refusal is told within 60 s.
5. The core stays at home: home control works with the internet down; nothing leaves without a purpose tag.

## Run it

```
pip install -r requirements.txt
pytest -q                                   # 119 tests; voice tests skip without audio
python experiments/scorecard/run.py         # the household day, 50 seeds x 3 fault mixes
python experiments/agent_gate/run.py        # 1,000 adversarial proposals through the gate
python experiments/twin_faults/run.py       # why an ack is not a result
python experiments/alarm_path/run.py        # alarm delivery and pysiaalarm interoperability
```

Voice experiments need `pip install -r requirements-voice.txt` and your own audio in `./audio` (set `ISLAB_VO`). The Season 1 voices are not distributed.

## Connect a real model

`islab.policy.agent.OpenAICompatibleLLM(base_url, model)` talks to any OpenAI-compatible chat endpoint with tool calling. Every tool call it proposes still goes through the policy engine. Below about 8B parameters, constrain tool calls with a grammar.

## Key results (B01)

- Stuck, partial, stale and clock-skewed devices acknowledged 100% of commands and confirmed none.
- 1,000 adversarial scenarios: 0 unapproved level-3 actions executed; a deliberately weakened policy was caught 141 times.
- A replayed recording was accepted as the speaker in 81% of trials at the room threshold.
- A leak sent as Contact ID was acknowledged but decoded as invalid by `pysiaalarm`.
- Under heavy faults, alarm delivery within 60 s went from 81.9% to 98.0% after the fixes in `experiments/scorecard/before_after.md`.

Full numbers and limits: `docs/paper.md`.

## Talk to us

If you build devices, platforms or spaces like these, Intersog's engineers do this work: [www.intersog.com](https://www.intersog.com). Questions about the research: open an issue.

## License

Apache-2.0. See `LICENSE`. Third-party packages keep their own licenses.

## Contributing

Issues and pull requests welcome. Every new claim needs a seeded experiment with `results.json`.
