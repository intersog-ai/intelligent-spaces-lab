"""Latency-budget model of the voice pipeline (Monte Carlo over per-stage lognormals).

Timeline of one interaction
  user says wake word ──wake_detect──► earcon_start ──► earcon heard          ("earcon" metric, from end of wake word)
  user finishes command ─vad_endpoint─► net_sat_to_hub ─► stt ─► intent ─► tts_first_audio ─► net_hub_to_sat ─► audio_out
                                                                                           ("reply" metric, from end of command)
  intent = intent_rules  (path "rules")
         | llm_ttft + llm_output_tokens / llm_tok_per_s   (path "llm"; tokens before the first speakable sentence)

All numbers live in latency_config.json with a per-stage 'source' ('assumption' or 'measured: ...').
Randomness: a random.Random (e.g. sim.rng) or an int seed; same seed -> same result.
"""
from __future__ import annotations
import json, math, os, random
from dataclasses import dataclass

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "latency_config.json")
REPLY_STAGES = ["vad_endpoint", "net_sat_to_hub", "stt", "intent", "tts_first_audio", "net_hub_to_sat", "audio_out"]
EARCON_STAGES = ["wake_detect", "earcon_start"]
_Z95 = 1.6448536269514722


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def _draw(spec: dict, rng: random.Random) -> float:
    p50, p95 = float(spec["p50"]), float(spec["p95"])
    if p50 == p95:
        return p50
    if p95 < p50:
        raise ValueError(f"p95 < p50 in {spec}")
    return rng.lognormvariate(math.log(p50), (math.log(p95) - math.log(p50)) / _Z95)


@dataclass
class VoicePipelineModel:
    tier: str
    config: dict = None

    def __post_init__(self):
        if self.config is None:
            self.config = load_config()
        if self.tier not in self.config["tiers"]:
            raise KeyError(f"unknown tier {self.tier}; have {list(self.config['tiers'])}")
        self.stages = {**self.config["common"], **self.config["tiers"][self.tier]}

    def sample(self, rng: random.Random, path: str = "rules") -> dict:
        """One interaction: per-stage ms + totals {'earcon', 'reply', 'to_intent'}."""
        s = self.stages
        d = {k: _draw(s[k], rng) for k in ("wake_detect", "earcon_start", "vad_endpoint", "net_sat_to_hub", "stt",
                                           "tts_first_audio", "net_hub_to_sat", "audio_out")}
        if path == "rules":
            d["intent"] = _draw(s["intent_rules"], rng)
        elif path == "llm":
            toks = _draw(s["llm_output_tokens"], rng)
            tps = s["llm_tok_per_s"]  # throughput: its p95 is the *slow* tail -> model ms/token instead
            ms_per_tok = _draw({"p50": 1000.0 / tps["p50"], "p95": 1000.0 / tps["p95"]}, rng)
            d["intent"] = _draw(s["llm_ttft"], rng) + toks * ms_per_tok
        else:
            raise ValueError("path must be 'rules' or 'llm'")
        d["earcon"] = sum(d[k] for k in EARCON_STAGES)
        d["to_intent"] = sum(d[k] for k in REPLY_STAGES[:4])        # end of speech -> intent decided (command can be sent)
        d["reply"] = sum(d[k] for k in REPLY_STAGES)
        return d

    def monte_carlo(self, n: int = 20000, seed: int | random.Random = 0, path: str = "rules") -> dict:
        rng = seed if isinstance(seed, random.Random) else random.Random(seed)
        samples = [self.sample(rng, path) for _ in range(n)]
        out = {}
        for k in samples[0]:
            xs = sorted(x[k] for x in samples)
            out[k] = {"p50": round(xs[int(0.50 * (n - 1))], 1), "p95": round(xs[int(0.95 * (n - 1))], 1)}
        return out

    def sources(self) -> dict:
        return {k: v.get("source", "?") for k, v in self.stages.items() if isinstance(v, dict)}


def budget_table(n: int = 20000, seed: int = 0, config: dict | None = None) -> dict:
    """p50/p95 of earcon, to_intent and reply for every tier x path."""
    cfg = config or load_config()
    res = {}
    for tier in cfg["tiers"]:
        m = VoicePipelineModel(tier, cfg)
        res[tier] = {path: m.monte_carlo(n, seed, path) for path in ("rules", "llm")}
    return res
