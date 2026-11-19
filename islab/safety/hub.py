"""The hub process for B04: controller + B01 alarm path + egress gate + notifier, and a reboot model.

A reboot kills every hub component at once (they stop receiving and sending), discards journal
records that were not yet synced, and after ``down_s`` starts fresh instances. Only the journal
survives. Devices, phones, the push relay, the WAN and the monitoring centre are outside the hub
and keep running.
"""
from __future__ import annotations

from islab.alarm import dc09
from islab.alarm.path import AlarmHub
from .controller import DESIGNS
from .journal import Journal
from .notify import Notifier, SafetyEgressGate

KEY = b"0123456789ABCDEF"
ACCOUNT = "1A2B3C"


class SafetyHub:
    def __init__(self, sim, bus, wan, centre, relay, internet, design="state_machine", journal=None,
                 people_home=None, persist_journal=True, controller_kw=None, alarm_kw=None):
        self.sim, self.bus, self.wan, self.centre, self.relay = sim, bus, wan, centre, relay
        self.internet = internet
        self.design, self.persist_journal = design, persist_journal
        self.controller_kw, self.alarm_kw = dict(controller_kw or {}), dict(alarm_kw or {})
        self.people_home = dict(people_home or {"anna": True, "mark": False})
        self.journal = journal if journal is not None else Journal(sim, autosync=(design != "state_machine"))
        self.journals = [self.journal]        # one per boot when the journal is not persisted
        self.boots = 0
        self.alive = False
        self.reboots = []
        self.gates = []
        self._start()

    def _start(self):
        self.boots += 1
        if self.boots > 1 and not self.persist_journal:
            self.journal = Journal(self.sim, autosync=self.journal.autosync)
            self.journals.append(self.journal)
        self.gate = SafetyEgressGate(self.sim, self.bus)
        self.gate.internet_up = bool(self.internet.up)
        self.gates.append(self.gate)
        self.alarm = AlarmHub(self.sim, self.bus, self.wan, self.centre, account=ACCOUNT, key=KEY,
                              fmt=dc09.SIA_DCS, egress=self.gate, **self.alarm_kw)
        self.notifier = Notifier(self.sim, self.bus, self.gate, self.relay, self.people_home)
        self.controller = DESIGNS[self.design](self.sim, self.bus, self.journal, self.alarm, self.notifier,
                                               **self.controller_kw)
        self.alive = True
        self.sim.trace("hub_boot", boot=self.boots, design=self.design)

    def set_internet(self, up: bool):
        if self.alive:
            self.gate.set_internet(up)

    def reboot(self, down_s: float):
        if not self.alive:
            return
        self.reboots.append((self.sim.now, down_s))
        self.sim.trace("hub_crash", boot=self.boots)
        self.alive = False
        self.controller.crash()
        self.alarm.crash()
        self.notifier.alive = False
        self.gate.queue = []                   # RAM queue is gone
        self.gate.internet_up = False          # the dead gate never sends again
        self.journal.crash()
        self.sim.after(down_s, self._start)

    @property
    def all_records(self):
        out = []
        for j in self.journals:
            out.extend(j.records)
        return out
