"""B08 "Cameras and the alarm path".

cellular.py   a modelled LTE link: latency, radio setup, loss, outages, bytes and cost (assumptions
              in CELL_ASSUMPTIONS; no real network)
dualpath.py   egress gate with a cellular route, a dual-path alarm hub (built on islab.alarm's
              AlarmHub), and a monitoring centre with per-path supervision and incident dedupe
camera.py     camera observations as evidence: the owner is told what was seen and asked; no
              footage leaves except a clip a person chooses to share with the centre
scenario.py   seeded runs used by experiments/b08_security

The home reports what it observed; a person or the monitoring centre decides. The home never
calls 911. Research in simulation, provided as is. Not a certified alarm system.
"""
from .cellular import CELL_ASSUMPTIONS, CellularLink
from .dualpath import (CENTRE_ACTIONS, DualPathAlarmHub, DualPathCentre, DualPathEgressGate,
                       parse_event)
from .camera import (DETECTOR_ASSUMPTIONS, VERDICT_WORDS, CameraObserver, DetectorModel,
                     ObservingCamera, PersonModel, observation_text)

__all__ = ["CELL_ASSUMPTIONS", "CellularLink", "CENTRE_ACTIONS", "DualPathAlarmHub",
           "DualPathCentre", "DualPathEgressGate", "parse_event", "DETECTOR_ASSUMPTIONS",
           "VERDICT_WORDS", "CameraObserver", "DetectorModel", "ObservingCamera", "PersonModel",
           "observation_text"]
