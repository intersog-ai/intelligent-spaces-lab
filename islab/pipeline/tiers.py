"""Hub tiers for the staged pipeline: per-stage latency (ms) as lognormals fitted to {p50, p95}.

    from islab.pipeline.tiers import build_tiers, StageModel
    tiers = build_tiers(measured)          # measured = {"stt": {"p50", "p95", "source"}, ...} from this CPU
    m = StageModel("pi5_cpu", tiers["pi5_cpu"])
    ms = m.draw("stt", sim.rng)

Built on B01 (islab/voice/latency_config.json) without editing it: stages shared by every tier
(earcon_start, network hops, audio_out, LLM output tokens) come from B01's 'common' block.
Every stage keeps a 'source' string: 'measured: ...' or 'assumption: ...'.
"""
from __future__ import annotations
import json, math, os, random

from islab.voice.pipeline import load_config as load_b01_config

TIERS_PATH = os.path.join(os.path.dirname(__file__), "tiers.json")
B01_COMMON = ("earcon_start", "net_sat_to_hub", "net_hub_to_sat", "audio_out", "llm_output_tokens")
TIER_ORDER = ("this_cpu", "pi5_cpu", "minipc_cpu", "minipc_gpu")
_Z95 = 1.6448536269514722


def load_tiers(path: str = TIERS_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def build_tiers(measured: dict, spec: dict | None = None, b01: dict | None = None) -> dict:
    """Compose the final stage table for 'this_cpu' and every hardware tier.

    measured: stage -> {"p50", "p95", "source"} measured on this machine (must cover spec['measured_stages']).
    Returns {tier: {"label": str, "stages": {stage: {"p50", "p95", "source"}}}}."""
    spec = spec or load_tiers()
    b01 = b01 or load_b01_config()
    missing = [s for s in spec["measured_stages"] if s not in measured]
    if missing:
        raise KeyError(f"measured stages missing: {missing}")
    common = {k: dict(b01["common"][k], source="B01 " + b01["common"][k]["source"]) for k in B01_COMMON}
    out = {}
    this = {k: dict(measured[k]) for k in spec["measured_stages"]}
    this.update({k: v for k, v in spec["this_cpu"].items() if isinstance(v, dict)})
    out["this_cpu"] = {"label": spec["this_cpu"]["label"], "stages": {**common, **this}}
    for name, t in spec["tiers"].items():
        st = dict(common)
        for k, v in t.items():
            if not isinstance(v, dict):
                continue
            if "scale_measured" in v:
                f = float(v["scale_measured"])
                st[k] = {"p50": round(measured[k]["p50"] * f, 2), "p95": round(measured[k]["p95"] * f, 2),
                         "source": v["source"] + f" (measured {measured[k]['p50']}/{measured[k]['p95']} ms x{f})"}
            else:
                st[k] = dict(v)
        out[name] = {"label": t["label"], "stages": st}
    return out


def draw(spec: dict, rng: random.Random) -> float:
    p50, p95 = float(spec["p50"]), float(spec["p95"])
    if p50 <= 0:
        return 0.0
    if p95 <= p50:
        return p50
    return rng.lognormvariate(math.log(p50), (math.log(p95) - math.log(p50)) / _Z95)


class StageModel:
    """Per-stage draws for one tier. All randomness from the caller's rng (e.g. sim.rng)."""

    def __init__(self, tier: str, table: dict):
        self.tier = tier
        self.label = table.get("label", tier)
        self.stages = table["stages"]

    def draw(self, stage: str, rng: random.Random) -> float:
        if stage == "llm":
            s = self.stages
            toks = draw(s["llm_output_tokens"], rng)
            tps = s["llm_tok_per_s"]          # throughput: p95 is the slow tail -> draw ms per token
            ms_tok = draw({"p50": 1000.0 / tps["p50"], "p95": 1000.0 / tps["p95"]}, rng)
            return draw(s["llm_ttft"], rng) + toks * ms_tok
        return draw(self.stages[stage], rng)

    ALIAS = {"wake": "wake_detect", "endpoint": "vad_endpoint", "intent": "intent_rules"}

    def source(self, stage: str) -> str:
        stage = self.ALIAS.get(stage, stage)
        if stage == "llm":
            return self.stages["llm_ttft"]["source"]
        return self.stages[stage].get("source", "?")
