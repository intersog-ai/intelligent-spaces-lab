"""B10: fleet, cloud and the full test.

full.py     FullHouse: the Season 1 household day with every Build Track layer (B02-B09) on at once
checks.py   the five principle suites over it, a route-aware P5 adapter, one check per layer
rollout.py  a fleet of simulated homes with a staged update rollout (canary, rings, automatic halt)
cloud.py    what a cloud management plane may see under the B09 egress rules; the cloud mapping table

Research in simulation, provided as is. Use it at your own risk.
"""
from .full import ALL_FIXES, FIXES, FullGate, FullEngine, FullHouse, run_full_day, schedule_full_day
from .checks import SCENES, SUITE_ORDER, full_suites, layer_checks, p5_route_aware

__all__ = ["ALL_FIXES", "FIXES", "FullGate", "FullEngine", "FullHouse", "run_full_day", "schedule_full_day",
           "SCENES", "SUITE_ORDER", "full_suites", "layer_checks", "p5_route_aware"]
