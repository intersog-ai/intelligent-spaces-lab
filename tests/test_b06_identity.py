"""B06 "Who is speaking": trust policy, liveness checks, identity home, and results.json invariants."""
import json
import os

import numpy as np
import pytest

from islab.identity import (LEVEL3, ChallengePhrase, Evidence, LivenessCheck, TrustPolicy, hifi_replay,
                            liveness_features, load_config)
from islab.identity.home import IdentityHome
from islab.voice import audio

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RES = os.path.join(ROOT, "experiments", "b06_identity", "results.json")


@pytest.fixture
def pol():
    return TrustPolicy()


def ev(pol, anna=0.5, mark=0.5, sam=0.5, live=True, overlap=None):
    return pol.evidence({"anna": anna, "mark": mark, "sam": sam}, live=live, overlap=overlap)


# ---- trust policy ---------------------------------------------------------------------------------
def test_tiers_and_caps(pol):
    hi = pol.tiers["named"]["threshold"] + 0.05
    mid = (pol.tiers["named"]["threshold"] + pol.tiers["comfort"]["threshold"]) / 2
    assert pol.tier(ev(pol, anna=hi)) == "named"
    assert pol.tier(ev(pol, anna=mid)) == "comfort"
    assert pol.tier(ev(pol, anna=0.3)) == "guest"
    assert pol.tier(ev(pol, anna=hi, live=False)) == "comfort"        # failed liveness caps the tier
    assert pol.tier(ev(pol, anna=hi, overlap=True)) == "comfort"      # two voices cap the tier
    # overlap inferred from the second-best score
    sec = pol.cfg["overlap"]["second_score"]
    assert pol.evidence({"anna": 0.95, "mark": sec + 0.01, "sam": 0.1}).overlap is True
    assert pol.evidence({"anna": 0.95, "mark": sec - 0.05, "sam": 0.1}).overlap is False


def test_voice_never_authorizes_level3(pol):
    for tool in LEVEL3:
        assert pol.authorizes_level3(Evidence("anna", 0.999, 0.9, True), tool) is False


def test_proposal_names_requester_only_at_named_tier(pol):
    hi = pol.tiers["named"]["threshold"] + 0.05
    p = pol.proposal(ev(pol, mark=hi), "unlock_door", {"door": "front"})
    assert p.requester == "mark" and p.channel == "voice" and p.confidence == pytest.approx(hi)
    p = pol.proposal(ev(pol, mark=hi, live=False), "unlock_door", {"door": "front"})
    assert p.requester is None and p.confidence is None


def test_private_data_never_read_out_unless_named(pol):
    hi = pol.tiers["named"]["threshold"] + 0.05
    mid = (pol.tiers["named"]["threshold"] + pol.tiers["comfort"]["threshold"]) / 2
    assert pol.personal(ev(pol, anna=hi), "calendar").kind == "serve"
    v = pol.personal(ev(pol, anna=mid), "calendar")
    assert v.kind == "to_phone" and v.person is None and "phone" in v.say
    assert pol.personal(ev(pol, anna=hi, overlap=True), "calendar").kind == "refuse"
    assert pol.personal(ev(pol, anna=hi, live=False), "calendar").kind == "refuse"
    # comfort scopes personalize at the lower tier, house default below it
    assert pol.personal(ev(pol, anna=mid), "music").person == "anna"
    assert pol.personal(ev(pol, anna=0.3), "temperature").kind == "generic"


def test_engine_config(pol):
    cfg = pol.engine_config()
    for t in ("unlock_door", "open_garage", "disarm_alarm"):
        assert cfg["levels"][t] == 3
    assert "disarm_alarm" in cfg["permissions"]["sam"]["deny"]
    assert "disarm_alarm" not in cfg["permissions"]["guest"]["allow"]
    assert cfg["voice"]["min_confidence"] == pol.tiers["named"]["threshold"]


# ---- the home ----------------------------------------------------------------------------------
def test_level3_needs_phone_not_voice(pol):
    h = IdentityHome(seed=3, policy=pol)
    e = ev(pol, mark=0.99)
    for tool, args in (("unlock_door", {"door": "front"}), ("open_garage", {}), ("disarm_alarm", {})):
        o = h.request(e, tool, args)
        assert o.status == "awaiting_approval" and o.actor == "mark"
        assert h.voice_approval(e, o.request_id) is False          # a spoken yes never counts
    h.sim.run(until=h.sim.now + 200)                                 # nobody answers on the phone
    assert h.level3_executions() == []
    o = h.request(e, "disarm_alarm", {})
    h.phone_reply("mark", o.request_id, "yes", after_s=10)
    h.sim.run(until=h.sim.now + 60)
    assert len(h.level3_executions()) == 1 and h.level3_by_voice_alone() == 0
    assert h.engine.decisions[o.request_id].final == "confirmed"
    assert h.store.get("alarm.panel")["mode"].value == "disarmed"


def test_teen_and_guest(pol):
    h = IdentityHome(seed=4, policy=pol)
    sam = ev(pol, sam=0.99)
    o = h.request(sam, "disarm_alarm", {})
    assert o.status == "denied" and "Sam" in o.say
    o = h.request(sam, "unlock_door", {"door": "front"})
    assert o.status == "awaiting_approval" and "phone" in o.say
    guest = ev(pol, anna=0.4, mark=0.4, sam=0.4)
    o = h.request(guest, "disarm_alarm", {})
    assert o.status == "denied" and o.actor == "guest" and "don't recognise" in o.say
    o = h.request(guest, "unlock_door", {"door": "front"})
    assert o.status == "awaiting_approval" and o.say.startswith("Only Anna or Mark")
    assert set(h.engine.pending[o.request_id].asked) == {"anna", "mark"}


def test_weakened_policy_is_counted(pol):
    weak = pol.engine_config()
    weak["levels"]["unlock_door"] = 1
    h = IdentityHome(seed=5, policy=pol, engine_config=weak)
    h.request(ev(pol, anna=0.99), "unlock_door", {"door": "front"})
    h.sim.run(until=h.sim.now + 30)
    assert h.level3_by_voice_alone() == 1                          # the probe sees it


# ---- liveness ----------------------------------------------------------------------------------
def _speechlike(rng, n=32000):
    x = audio.pink_noise(n, rng) * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * np.arange(n) / audio.SR) ** 2)
    return audio.satellite_channel(x.astype(np.float32) * 0.1, rng)


def test_liveness_one_class_flags_band_limited_replay():
    rng = np.random.default_rng(0)
    live = [_speechlike(rng) for _ in range(40)]
    lv = LivenessCheck(max_live_reject=0.05).fit(live)
    assert np.mean([lv.is_live(w) for w in live]) >= 0.9
    phone = [audio.satellite_channel(audio.phone_replay(_speechlike(rng), rng), rng) for _ in range(10)]
    assert np.mean([lv.is_live(w) for w in phone]) <= 0.2
    assert liveness_features(hifi_replay(live[0], rng)).shape == (6,)


def test_challenge_phrase():
    cp = ChallengePhrase(np.random.default_rng(1))
    ph = cp.issue()
    assert len(ph.split()) == 3 and ChallengePhrase.combinations == 3600
    assert ChallengePhrase.verify(f"Okay, {ph.upper()}!", ph)
    assert not ChallengePhrase.verify("Intersog presents. Intelligent Spaces.", ph)


# ---- results.json --------------------------------------------------------------------------------
@pytest.mark.skipif(not os.path.exists(RES), reason="run experiments/b06_identity/run.py first")
def test_results_contract_and_yaml_match():
    doc = json.load(open(RES))
    assert set(doc) >= {"experiment", "date", "method", "params", "results", "limitations"}
    r = doc["results"]
    cfg = load_config()
    for tier in ("comfort", "named"):
        assert cfg["tiers"][tier]["threshold"] == r["calibration"]["chosen"][tier]["threshold"]
        assert cfg["tiers"][tier]["margin"] == r["calibration"]["chosen"][tier]["margin"]
    assert cfg["overlap"]["second_score"] == r["calibration"]["overlap_second_score_threshold"]
    assert r["engine"]["policy"]["level3_executed_by_voice_alone"] == 0
    assert r["engine"]["careless_approver_10pct"]["level3_executed_by_voice_alone"] == 0
    assert r["engine"]["policy"]["spoken_yes_to_level3_approval"]["accepted"] == 0
    assert all(v["level3_executed"] == 0 for v in r["engine"]["policy"]["attacks"].values())
    assert r["engine"]["weakened_voice_alone_opens"]["level3_executed_by_voice_alone"] > 0
    assert r["seed_spread"]["max"]["level3_by_voice_alone"] == 0
