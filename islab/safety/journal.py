"""Local append-only journal (JSON lines) with fsync-style durability, modelled in virtual time.

Write-ahead rule used by the safety controller: a record that announces an action (``cmd_intent``)
is made durable (``sync``) BEFORE the action happens. After a hub reboot the controller rebuilds
its state from the durable records only.

Durability model
  * ``append`` puts a record in an OS-buffer-like pending list and returns it.
  * ``sync`` moves pending records to the durable list (like ``write`` + ``fsync``). If a file path
    is given, the lines are written and ``os.fsync`` is called.
  * ``crash`` discards pending records (they were never synced), exactly what a power cut does to
    an un-fsynced write.
  * ``load`` (reading a file after a crash) ignores a torn last line that is not valid JSON.

Every record gets a unique record id (``rid``), the virtual time ``t`` and a ``boot`` number, so an
explanation can cite the record it came from.
"""
from __future__ import annotations

import json
import os


class Journal:
    def __init__(self, sim, path=None, autosync=False):
        self.sim = sim
        self.path = path
        self.autosync = autosync
        self.durable: list[dict] = []
        self.pending: list[dict] = []
        self.syncs = 0
        self.lost_on_crash = 0
        self._n = 0
        self.boot = 0
        if path and os.path.exists(path):
            self.durable = self.load(path)
            self._n = len(self.durable)
            self.boot = max([r.get("boot", 0) for r in self.durable] or [0])

    # ---- writing --------------------------------------------------------------------
    def append(self, kind: str, **fields) -> dict:
        self._n += 1
        rec = {"rid": f"r{self._n:05d}", "t": round(self.sim.now, 4), "boot": self.boot,
               "kind": kind, **fields}
        self.pending.append(rec)
        if self.autosync:
            self.sync()
        return rec

    def sync(self) -> None:
        if not self.pending:
            return
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                for r in self.pending:
                    f.write(json.dumps(r, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        self.durable.extend(self.pending)
        self.pending = []
        self.syncs += 1

    # ---- failure model --------------------------------------------------------------
    def crash(self) -> None:
        """Power cut: everything not yet synced is gone."""
        self.lost_on_crash += len(self.pending)
        self.pending = []
        # record ids restart after the last durable one, as a rebooted process would compute them
        self._n = len(self.durable)

    def new_boot(self) -> int:
        self.boot += 1
        return self.boot

    # ---- reading --------------------------------------------------------------------
    @property
    def records(self) -> list[dict]:
        """What a reader sees now: durable plus pending (pending is lost on a crash)."""
        return self.durable + self.pending

    def of(self, kind=None) -> list[dict]:
        return [r for r in self.records if kind is None or r["kind"] == kind]

    @staticmethod
    def load(path) -> list[dict]:
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    break          # torn last write: everything after it is untrusted
        return out
