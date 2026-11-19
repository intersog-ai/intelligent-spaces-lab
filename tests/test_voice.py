import json, os, random
import numpy as np
import pytest

from islab.core.sim import Sim
from islab.core.bus import Bus
from islab.voice import audio
from islab.voice.pipeline import VoicePipelineModel, budget_table, load_config
from islab.voice.speaker import SpeakerID, MFCCEncoder, THRESHOLDS
from islab.voice.events import VoiceSatellite, hub_identify, speaker_id_from_household, load_household

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VO = os.environ.get("ISLAB_VO", "audio")
ANNA1, ANNA2 = f"{VO}/v2/E06_Anna.mp3", f"{VO}/v2/E0709_Anna.mp3"
MARK1, MARK2 = f"{VO}/v2/E06_Mark.mp3", f"{VO}/v2/E0709_Mark.mp3"
have_audio = all(os.path.exists(p) for p in (ANNA1, ANNA2, MARK1, MARK2))


# ---------- speaker id ----------

@pytest.mark.skipif(not have_audio, reason="voice-over mp3s not present")
def test_enroll_identify_roundtrip_resemblyzer():
    pytest.importorskip("resemblyzer")
    from islab.voice.speaker import ResemblyzerEncoder
    sid = SpeakerID(ResemblyzerEncoder(), threshold=THRESHOLDS["studio_eer"])
    sid.enroll("anna", [ANNA1])
    sid.enroll("mark", [MARK1])
    # held-out files: the right person must score highest
    for path, who in ((ANNA2, "anna"), (MARK2, "mark")):
        s = sid.scores(path)
        assert max(s, key=s.get) == who, s
    person, score = sid.identify(ANNA2)
    assert person == "anna" and score >= THRESHOLDS["studio_eer"]
    # enrolled audio identifies itself with near-1 score
    person, score = sid.identify(MARK1)
    assert person == "mark" and score > 0.95
    # an unknown voice (narrator) is rejected at the strict threshold
    david = f"{VO}/v2/E06_David_same.mp3"
    if os.path.exists(david):
        assert sid.identify(david, threshold=THRESHOLDS["studio_strict"])[0] is None


@pytest.mark.skipif(not have_audio, reason="voice-over mp3s not present")
def test_roundtrip_mfcc_fallback_and_rejects_without_profiles():
    sid = SpeakerID(MFCCEncoder(), threshold=0.5)
    assert sid.identify(audio.load(ANNA1)) == (None, 0.0)
    sid.enroll("anna", [ANNA1]); sid.enroll("mark", [MARK1])
    person, score = sid.identify(audio.load(ANNA1))
    assert person == "anna" and score > 0.99


def test_thresholds_match_experiment_results():
    p = os.path.join(ROOT, "experiments", "speaker_id", "results.json")
    if not os.path.exists(p):
        pytest.skip("experiment not run")
    op = json.load(open(p))["results"]["operating_points"]
    for k, v in THRESHOLDS.items():
        assert abs(op[k] - v) < 1e-3, (k, op[k], v)


def test_degradations_are_seeded():
    x = np.sin(np.linspace(0, 2000, 32000)).astype(np.float32)
    a = audio.phone_replay(x, np.random.default_rng(1))
    b = audio.phone_replay(x, np.random.default_rng(1))
    assert np.array_equal(a, b) and len(a) == len(x)
    n = audio.kitchen_noise(16000, np.random.default_rng(2))
    y = audio.mix_snr(x[:16000], n, 10)
    snr = 10 * np.log10(np.mean(x[:16000] ** 2) / np.mean((y - x[:16000]) ** 2))
    assert abs(snr - 10) < 0.1


# ---------- latency model ----------

def test_latency_model_deterministic_with_seed():
    m = VoicePipelineModel("pi5_hailo10h")
    assert m.monte_carlo(2000, seed=7) == m.monte_carlo(2000, seed=7)
    assert m.monte_carlo(2000, seed=7) != m.monte_carlo(2000, seed=8)
    a = [VoicePipelineModel("x86_gpu12gb").sample(random.Random(3), "llm") for _ in range(2)]
    assert a[0] == a[1]
    t1, t2 = budget_table(1000, 5), budget_table(1000, 5)
    assert t1 == t2


def test_latency_model_sanity_and_sources():
    cfg = load_config()
    for tier in cfg["tiers"]:
        m = VoicePipelineModel(tier, cfg)
        r, l = m.monte_carlo(2000, 1, "rules"), m.monte_carlo(2000, 1, "llm")
        assert r["reply"]["p50"] < l["reply"]["p50"]          # LLM path is slower than rules
        assert r["reply"]["p50"] <= r["reply"]["p95"]
        # every stage carries a source label; non-dev tiers are assumptions
        assert all(src for src in m.sources().values())
        if tier != "devbox_cpu_measured":
            assert all(v["source"].startswith("assumption") for k, v in cfg["tiers"][tier].items() if isinstance(v, dict))


# ---------- bus adapter ----------

def test_voice_event_on_bus_and_hub_identify():
    sim = Sim(seed=11); bus = Bus(sim)
    got = []
    bus.subscribe("voice/+", lambda env: got.append(env), "test")
    sat = VoiceSatellite(sim, bus, "sat.kitchen", tier="jetson_orin_nano_super")
    sat.say("Anna", "set the kitchen to twenty one")
    sat.say(None, "what time is it")
    sim.run()
    assert [e.topic for e in got] == ["voice/sat.kitchen", "voice/sat.kitchen"]
    by_text = {e.payload["text"]: e for e in got}          # arrival order depends on sampled latency
    anna, anon = by_text["set the kitchen to twenty one"], by_text["what time is it"]
    assert all(set(e.payload) <= {"audio_ref", "text", "speaker_embedding"} for e in got)
    assert "speaker_embedding" not in anon.payload and len(anna.payload["speaker_embedding"]) == 256
    assert min(e.ts for e in got) > 0.3                     # VAD + STT latency applied in sim time
    sid = speaker_id_from_household(threshold="studio_strict")
    person, score = hub_identify(sid, anna.payload)
    assert person == "Anna" and score > 0.9
    assert hub_identify(sid, anon.payload) == (None, 0.0)
    # an impostor embedding is not accepted as a household member
    imp = load_household()["impostors"]["David"][0]
    assert sid.identify(emb=np.asarray(imp))[0] is None


def test_voice_event_timing_reproducible():
    def run(seed):
        sim = Sim(seed=seed); bus = Bus(sim); ts = []
        bus.subscribe("voice/#", lambda e: ts.append(e.ts))
        VoiceSatellite(sim, bus, "sat.hall").say("Mark", "lock the back door")
        sim.run(); return ts
    assert run(3) == run(3)
    assert run(3) != run(4)
