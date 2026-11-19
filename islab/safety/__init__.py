"""B04 "Sent is not closed": a local leak -> valve safety controller with no LLM and no internet.

controller.py  state machine + fire-and-forget and ack-based baselines
journal.py     JSON-lines journal with fsync-style durability (survives a hub reboot)
devices.py     simulated flow meter on the main pipe; ground truth of the valve position
notify.py      plain-words telling (LAN at home, push through the egress gate when away)
explain.py     "what happened" built only from journal records, plus an automatic checker
hub.py         the hub process and its reboot model
scenario.py    one seeded leak in the twin, with the B04 fault conditions, and its metrics

Research in simulation. Not a certified leak-protection system.
"""
