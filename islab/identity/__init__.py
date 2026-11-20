"""B06 "Who is speaking": what a home may safely do with knowing who is speaking.

Public API
  TrustPolicy, Evidence, Verdict, load_config     trust.py   identity tiers, personalization, proposals
  IdentityHome, IdentityToolRegistry               home.py    twin + policy engine with the trust policy
  LivenessCheck, liveness_features, ChallengePhrase,
  hifi_replay, REPLAY_DEVICES                      liveness.py  CPU replay checks and replay proxies
  load_segments, GUEST_VOICES                      voices.py  the series' synthetic voices as a household

Voice identity personalizes and can raise trust for level 1-2; it never authorizes level 3.
Research in simulation, provided as is. Use it at your own risk.
"""
from .trust import LEVEL3, TIERS, Evidence, TrustPolicy, Verdict, load_config
from .liveness import FEATURES, REPLAY_DEVICES, ChallengePhrase, LivenessCheck, hifi_replay
from .liveness import features as liveness_features
from .voices import GUEST_VOICES, load_segments

__all__ = ["TrustPolicy", "Evidence", "Verdict", "load_config", "TIERS", "LEVEL3", "LivenessCheck",
           "liveness_features", "FEATURES", "ChallengePhrase", "hifi_replay", "REPLAY_DEVICES",
           "load_segments", "GUEST_VOICES", "IdentityHome", "IdentityToolRegistry"]


def __getattr__(name):          # home.py imports the twin and suites; load it on first use
    if name in ("IdentityHome", "IdentityToolRegistry"):
        from . import home
        return getattr(home, name)
    raise AttributeError(name)
