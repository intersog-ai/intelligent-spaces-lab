# Contract between components (read before writing code)

- Time: `islab.core.sim.Sim` virtual seconds; never use wall-clock time. `sim.rng` is the only randomness (seeded).
- Messages: `islab.core.bus.Envelope(topic, payload, source, ts, event_id, seq)` on `islab.core.bus.Bus`.
- Topics:
  - `state/<device_id>`            device -> hub. payload: {"attr": value, ...}. ts = device clock.
  - `cmd/<device_id>`              hub -> device. payload: {"action": str, "args": {...}, "cmd_id": str}
  - `ack/<device_id>`              device -> hub. payload: {"cmd_id": str, "status": "accepted"|"rejected", "reason"?}
                                   accepted means the device received it, NOT that it happened.
  - `event/<device_id>`            device -> hub, discrete events (leak, smoke, motion, doorbell). payload: {"type": str, ...}
  - `voice/<satellite_id>`         satellite -> hub. payload: {"audio_ref": str, "text"?: str, "speaker_embedding"?: [...]}
  - `egress/<purpose>`             hub -> outside world. Only the egress gate may publish here.
  - `notify/<person>`              hub -> phone. payload: {"text": str, "kind": "tell"|"ask"|"approval", "request_id"?}
  - `reply/<person>`               phone -> hub. payload: {"request_id": str, "answer": "yes"|"no"}
- An action is CONFIRMED only when a `state/<device>` RECEIVED by the hub after the command was sent shows the target value. Use hub receive time for ordering against commands (device clocks can lag); use device ts only for freshness/out-of-order checks between readings of the same device.
- Voice satellites: sat.living, sat.kitchen.
- Device ids: valve.main, lock.front, lock.back, shade.living, thermo.kitchen, thermo.study, window.kitchen,
  garage.door, leak.sink, smoke.hall, motion.kitchen, camera.front, light.kitchen, light.living, temp.kitchen.
- Package layout: islab/twin (devices, faults, household), islab/policy (policy engine, tools, agent),
  islab/voice (speaker id, pipeline model), islab/alarm (DC-09, egress, privacy), islab/suites (five principle tests),
  experiments/<name>/ (scripts + results.json + notes.md). Tests in tests/ with pytest.
- Every experiment writes results.json with: {"experiment", "date", "method", "params", "results", "limitations"}.
