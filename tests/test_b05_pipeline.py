"""B05: staged voice pipeline (islab/pipeline) and experiments/b05_pipeline."""
import json, os, random
import pytest

from islab.core.sim import Sim
from islab.core.bus import Bus
from islab.twin.home import build_home
from islab.policy.state import StateStore
from islab.policy.engine import PolicyEngine
from islab.pipeline import (DialogueContext, Grammar, LLMStub, StageModel, Utterance, VoicePipeline, build_tiers,
                            words_to_numbers, TIER_ORDER)
from islab.pipeline.intent import explicit_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXP = os.path.join(ROOT, "experiments", "b05_pipeline")
MEASURED = {k: {"p50": v[0], "p95": v[1], "source": "measured: test"} for k, v in {
    "wake_detect": (40, 60), "vad_endpoint": (410, 460), "stt": (200, 300), "tts_first_audio": (100, 200),
    "intent_rules": (0.02, 0.04), "policy": (0.03, 0.09)}.items()}


# ---------------- grammar
def test_number_words():
    assert words_to_numbers("set the kitchen to twenty one degrees") == "set the kitchen to 21 degrees"
    assert words_to_numbers("kitchen light to fifty percent") == "kitchen light to 50 %"
    assert words_to_numbers("Set it to 50%.").startswith("set it to 50 %")


def test_grammar_named_and_room():
    g = Grammar()
    assert g.parse("Turn on the kitchen light.").calls == [{"name": "set_light", "arguments": {"room": "kitchen", "on": True}}]
    it = g.parse("turn off the light", satellite="sat.living")
    assert it.ok and it.resolved_by == "room" and it.calls[0]["arguments"]["room"] == "living"


def test_questions_are_not_commands():
    # B01's parse_intents reads this as lock_door; Whisper often drops the 'ed'
    g = Grammar()
    for t in ("is the back door locked", "is the back door lock."):
        it = g.parse(t)
        assert it.calls == [{"name": "get_state", "arguments": {"device_id": "lock.back"}}]


def test_cut_off_and_verbless_shade_ask():
    g = Grammar()
    for t in ("set the kitchen to", "turn off the...", "lock the", "set the study", "'cuss the living room shade'"):
        assert g.parse(t).status == "incomplete", t


def test_pronoun_needs_context():
    g, ctx = Grammar(), DialogueContext()
    it = g.parse("turn it off", "sat.kitchen", ctx, now=10)
    assert it.status == "ambiguous" and "Which one" in it.question
    ctx.remember("sat.kitchen", [{"name": "set_light", "arguments": {"room": "living", "on": True}}], now=10)
    it = g.parse("turn it off", "sat.kitchen", ctx, now=30)
    assert it.ok and it.resolved_by == "context" and it.calls[0]["arguments"] == {"room": "living", "on": False}
    assert g.parse("turn it off", "sat.kitchen", ctx, now=200).status == "ambiguous"     # context expired
    assert g.parse("turn it off", "sat.living", ctx, now=30).status == "ambiguous"       # other satellite
    assert g.parse("i'm closing it now", "sat.kitchen", ctx, now=30).status == "none"


def test_llm_stub_level3_not_explicit():
    it = LLMStub().parse("unlok the front dor")
    assert it.ok and it.calls[0]["name"] == "unlock_door"
    assert explicit_for(it.calls[0], it) is False       # the policy engine denies it as not requested


# ---------------- tiers
def test_tiers_scale_and_label():
    t = build_tiers(MEASURED)
    assert set(TIER_ORDER) <= set(t)
    assert t["this_cpu"]["stages"]["stt"]["p50"] == 200
    assert t["pi5_cpu"]["stages"]["stt"]["p50"] == pytest.approx(500)
    assert t["minipc_gpu"]["stages"]["stt"]["source"].startswith("assumption")
    for tier in t.values():
        for k, v in tier["stages"].items():
            assert v["source"].replace("B01 ", "").startswith(("measured", "assumption")), (k, v["source"])
    m = StageModel("pi5_cpu", t["pi5_cpu"])
    a = [m.draw("llm", random.Random(1)) for _ in range(3)]
    b = [m.draw("llm", random.Random(1)) for _ in range(3)]
    assert a == b and min(a) > 0


# ---------------- pipeline on the twin
def _rig(seed=3, reply_on="confirm", tier="minipc_cpu"):
    sim = Sim(seed=seed)
    bus = Bus(sim)
    home = build_home(sim, bus, seed=seed)
    eng = PolicyEngine(sim, bus, StateStore(sim, bus))
    t = build_tiers(MEASURED)
    pipe = VoicePipeline(sim, bus, eng, StageModel(tier, t[tier]), home=home, reply_on=reply_on)
    events = []
    bus.subscribe("pipeline/#", lambda e: events.append(e), name="test")
    voice = []
    bus.subscribe("voice/#", lambda e: voice.append(e), name="test")
    return sim, bus, home, eng, pipe, events, voice


def _u(text, hyp=None, person="anna", uid="u1"):
    return Utterance(uid, "sat.kitchen", person, 0.9, text, hyp or text, 1.5)


def test_stages_timestamped_and_done_after_confirmation():
    sim, bus, home, eng, pipe, events, voice = _rig()
    pipe.speak(_u("turn on the kitchen light"), at=10.0)
    sim.run(until=40)
    rec = pipe.records["u1"]
    st = [e for e in events if not e.topic.endswith("/say")]
    assert [e.payload["stage"] for e in st] == ["wake", "endpoint", "stt", "intent", "policy"]
    assert all(e.topic == f"pipeline/sat.kitchen/{e.payload['stage']}" and e.payload["source"] for e in st)
    ts = [e.payload["t"] for e in st]
    assert ts == sorted(ts) and ts[0] > 10.0
    assert voice and voice[0].payload["text"] == "turn on the kitchen light" and "audio_ref" in voice[0].payload
    assert rec["outcome"] == "confirmed"
    done = [l for l in rec["said"] if l["kind"] == "done"]
    assert len(done) == 1 and rec["done_checks"][0]["confirmed_before"] and rec["done_checks"][0]["true_at_audio"]
    assert done[0]["t_ready"] >= rec["t_done_basis"]
    assert home.devices["light.kitchen"].state["state"] == "on"


def test_stuck_device_never_said_done():
    sim, bus, home, eng, pipe, events, voice = _rig()
    home.faults.stuck("lock.back")
    home.devices["lock.back"].state["state"] = "unlocked"
    pipe.speak(_u("lock the back door"), at=10.0)
    sim.run(until=80)
    rec = pipe.records["u1"]
    kinds = [l["kind"] for l in rec["said"]]
    assert "done" not in kinds and "hold" in kinds and kinds[-1] == "failed"
    assert "never showed it happened" in rec["said"][-1]["text"]


def test_ack_baseline_claims_done_on_stuck_device():
    sim, bus, home, eng, pipe, events, voice = _rig(reply_on="ack")
    home.faults.stuck("lock.back")
    home.devices["lock.back"].state["state"] = "unlocked"
    pipe.speak(_u("lock the back door"), at=10.0)
    sim.run(until=80)
    rec = pipe.records["u1"]
    assert rec["done_checks"] and rec["done_checks"][0]["true_at_audio"] is False


def test_clarify_level3_and_permission_lines():
    sim, bus, home, eng, pipe, events, voice = _rig()
    pipe.speak(_u("turn it off", uid="a"), at=10.0)
    pipe.speak(_u("unlock the front door", uid="b"), at=30.0)
    pipe.speak(_u("unlock the front door", person="sam", uid="c"), at=50.0)
    sim.run(until=90)
    assert pipe.records["a"]["said"][0]["kind"] == "clarify"
    assert pipe.records["b"]["said"][0]["kind"] == "approval_sent"
    assert home.devices["lock.front"].state["state"] == "locked"
    assert pipe.records["c"]["said"][0]["kind"] == "denied" and "Sam" in pipe.records["c"]["said"][0]["text"]


def test_missed_wake_is_silent_and_deterministic():
    def run(seed):
        sim, bus, home, eng, pipe, events, voice = _rig(seed=seed)
        pipe.speak(Utterance("m", "sat.kitchen", "anna", 0.9, "x", "x", 1.0, wake_detected=False), at=5)
        pipe.speak(_u("close the garage door", uid="g"), at=10.0)
        sim.run(until=80)
        return pipe.records
    r1, r2 = run(7), run(7)
    assert r1["m"]["said"] == [] and r1["m"]["outcome"] == "missed_wake"
    assert r1["g"]["said"] == r2["g"]["said"]
    kinds = [l["kind"] for l in r1["g"]["said"]]
    assert kinds[0] == "question" and "done" in kinds          # level 2: asks, person says yes, confirmed


# ---------------- experiment outputs
@pytest.mark.skipif(not os.path.exists(os.path.join(EXP, "results.json")), reason="run experiments/b05_pipeline first")
def test_results_json_shape_and_principle2():
    d = json.load(open(os.path.join(EXP, "results.json")))
    for k in ("experiment", "date", "method", "params", "results", "limitations"):
        assert k in d
    r = d["results"]
    for tier in TIER_ORDER:
        t = r["tiers"][tier]
        assert t["ttfa_ms"]["p50"] <= t["ttfa_ms"]["p95"]
        assert t["done_claims_confirmed_before_pct"] == 100.0
        assert t["done_claims_true_at_audio_pct"] == 100.0
    assert r["baseline_ack"]["done_claims_true_at_audio_pct"] < 100.0
    m = json.load(open(os.path.join(EXP, "measured.json")))
    assert d["params"]["measured_stages_this_cpu"] == m["stages"]
    assert r["tiers"]["this_cpu"]["stage_ms"]["stt"]["p50"] == m["stages"]["stt"]["p50"]


MODELS = os.environ.get("ISLAB_MODELS", "models")


@pytest.mark.skipif(not os.path.isdir(os.path.join(MODELS, "vits-piper-en_US-amy-low")), reason="models not present")
def test_real_chain_wake_and_stt():
    real = pytest.importorskip("islab.pipeline.real")
    pytest.importorskip("openwakeword")
    import numpy as np
    tts = real.PiperVoice(MODELS, "en_US-amy-low", "en-us")
    x = np.concatenate([np.zeros(16000, np.float32), tts.synth("Hey Jarvis."), np.zeros(4800, np.float32),
                        tts.synth("Turn on the kitchen light."), np.zeros(16000, np.float32)])
    assert real.WakeWord("hey_jarvis").detect(x)
    text, _ = real.WhisperSTT(MODELS).transcribe(x[16000:])
    assert Grammar().parse(text).ok
