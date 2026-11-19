"""Egress experiment: a simulated 24 h household day through the EgressGate, then the same
day with the internet down. Writes results.json next to this file.

    python experiments/egress/run.py
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from islab.core.sim import Sim                        # noqa: E402
from islab.core.bus import Bus, Envelope              # noqa: E402
from islab.alarm.egress import EgressGate              # noqa: E402
from islab.alarm.path import AlarmHub, LocalSiren, MonitoringCentre, WanLink   # noqa: E402
from islab.alarm.privacy import RecordingStore         # noqa: E402

DAY = 86400.0
SEED = 2026
KEY = b"0123456789ABCDEF"
SETTINGS = {"cloud_speech_opt_in": True, "cloud_llm_opt_in": False, "alarm_clips_allowed": False}
PLAN = {
    "voice_requests_cloud_asr": 12,          # requests the local recogniser handed to the cloud
    "speech_bytes_range": (9_000, 18_000),   # 3-6 s of Opus at ~24 kbit/s
    "backup_bytes": 48_000_000,              # nightly encrypted backup at 03:00
    "update_checks_every_s": 6 * 3600,
    "update_check_bytes": 1_200,
    "alarm_heartbeat_s": 300.0,
    "camera_motion_clips": 25,
    "phone_home_every_s": 600,               # fake plug firmware
    "home_control_commands": 200,
    "leak_event_at_s": 14 * 3600,
}
CTRL_DEVICES = ["light.kitchen", "light.living", "shade.living", "thermo.kitchen", "lock.front"]


class StandInDevice:
    """Minimal local device so home control can be measured without islab/twin."""

    def __init__(self, sim, bus, dev):
        self.sim, self.bus, self.dev = sim, bus, dev
        bus.subscribe(f"cmd/{dev}", self.on_cmd, name=dev)

    def on_cmd(self, env):
        p = env.payload
        self.bus.publish(Envelope(topic=f"ack/{self.dev}", source=self.dev, ts=self.sim.now,
                                  payload={"cmd_id": p["cmd_id"], "status": "accepted"}))
        self.sim.after(0.3, lambda: self.bus.publish(Envelope(
            topic=f"state/{self.dev}", source=self.dev, ts=self.sim.now,
            payload={"value": p["args"].get("value"), "cmd_id": p["cmd_id"]})))


def run_day(internet_down: bool, leak: bool):
    sim = Sim(seed=SEED)
    rng = sim.rng
    bus = Bus(sim)
    gate = EgressGate(sim, bus, settings=SETTINGS)
    outages = [(0, DAY)] if internet_down else []
    link = WanLink(sim, outages=outages)
    centre = MonitoringCentre(sim, link, {"1A2B3C": KEY}, supervision_s=1800)
    hub = AlarmHub(sim, bus, link, centre, account="1A2B3C", key=KEY, egress=gate,
                   heartbeat_s=PLAN["alarm_heartbeat_s"], link_fault_after=900)
    siren = LocalSiren(sim, bus)
    store = RecordingStore(sim, retention_days=7)
    if internet_down:
        gate.set_internet(False)
        sim.at(DAY, gate.set_internet, True)

    log = {"voice_cloud": 0, "voice_local_fallback": 0, "camera_clips_recorded": 0}

    # voice requests that the local recogniser escalated to cloud ASR
    for _ in range(PLAN["voice_requests_cloud_asr"]):
        t = 7 * 3600 + rng.random() * 15 * 3600
        size = rng.randint(*PLAN["speech_bytes_range"])

        def voice(size=size):
            ok = gate.send("voice.pipeline", "asr.provider.example", "speech_recognition_optional",
                           "speech_audio", size=size, meta={"retention": "not_stored"})
            log["voice_cloud" if ok else "voice_local_fallback"] += 1
        sim.at(t, voice)

    # nightly backup (encrypted before it reaches the gate)
    sim.at(3 * 3600, lambda: gate.send("backup.agent", "backup.storage.example", "backup_encrypted",
                                       "encrypted_blob", size=PLAN["backup_bytes"],
                                       meta={"encrypted": True}, msg_id="backup:day0"))
    # fleet update checks
    t = 1800.0
    while t < DAY:
        sim.at(t, lambda: gate.send("fleet.agent", "updates.vendor.example", "fleet_update",
                                    "update_check", size=PLAN["update_check_bytes"]))
        t += PLAN["update_checks_every_s"]
    # camera: motion clips are recorded locally; firmware also tries to upload them
    for i in range(PLAN["camera_motion_clips"]):
        t = rng.random() * DAY

        def clip(i=i):
            data = bytes([i]) * 2_000_000
            store.add(f"clip{i:03d}", data)
            log["camera_clips_recorded"] += 1
            # alternate: vendor cloud (not allowlisted) / the household backup host (allowlisted)
            dest = "cloud.camvendor.example" if i % 2 else "backup.storage.example"
            gate.send("camera.front", dest, "backup_encrypted", "camera_clip",
                      data=data, meta={"encrypted": True})
        sim.at(t, clip)
    # a camera that tries to skip the gate and publish egress/ itself
    sim.at(12 * 3600, lambda: bus.publish(Envelope(topic="egress/backup_encrypted",
                                                   payload={"dest": "cloud.camvendor.example"},
                                                   source="camera.front", ts=sim.now)))
    # the device that phones home
    t = 60.0
    while t < DAY:
        sim.at(t, lambda: gate.send("plug.cheap", "telemetry.unknown-vendor.example",
                                    "fleet_update", "telemetry", size=420))
        t += PLAN["phone_home_every_s"]
    # a leak in the afternoon
    if leak:
        sim.at(PLAN["leak_event_at_s"], lambda: bus.publish(Envelope(
            topic="event/leak.sink", payload={"type": "leak"}, source="leak.sink", ts=sim.now)))

    # home control: local commands, confirmed by state
    for d in CTRL_DEVICES:
        StandInDevice(sim, bus, d)
    pending, confirmed = {}, []

    def on_state(env):
        cid = env.payload.get("cmd_id")
        if cid in pending:
            confirmed.append(sim.now - pending.pop(cid))
    bus.subscribe("state/+", on_state, name="controller")
    for i in range(PLAN["home_control_commands"]):
        t = rng.random() * DAY
        dev = CTRL_DEVICES[i % len(CTRL_DEVICES)]

        def cmd(i=i, dev=dev):
            cid = f"c{i}"
            pending[cid] = sim.now
            bus.publish(Envelope(topic=f"cmd/{dev}", source="controller", ts=sim.now,
                                 payload={"action": "set", "args": {"value": i}, "cmd_id": cid}))
        sim.at(t, cmd)

    sim.run(until=DAY - 1e-6)
    report_day = gate.report(0)
    summary = gate.summary_text(0, include_routine=False)
    summary_full = gate.summary_text(0, include_routine=True)
    sim.run(until=DAY + 3600)       # internet back: queue flush
    leak = hub.alarms[0] if hub.alarms else None
    blocked_ph = [r for r in gate.blocked if r["source"] == "plug.cheap"]
    cs = sorted(confirmed)
    return {
        "bytes_out_by_purpose": {p: v["bytes"] for p, v in report_day["by_purpose"].items()},
        "messages_out_by_purpose": {p: v["messages"] for p, v in report_day["by_purpose"].items()},
        "total_bytes_out": report_day["total_bytes"],
        "camera_items_out": report_day["camera_items_out"],
        "blocked": report_day["blocked"],
        "blocked_by_reason": report_day["blocked_by_reason"],
        "blocked_by_source": report_day["blocked_by_source"],
        "blocked_destinations": report_day["blocked_destinations"],
        "phone_home": {"attempts": len(blocked_ph), "blocked": len(blocked_ph),
                       "left_house": sum(1 for r in gate.sent if r["source"] == "plug.cheap"),
                       "first_log_entry": {k: blocked_ph[0][k] for k in
                                           ("t", "source", "dest", "purpose", "payload_class", "reason")}
                       if blocked_ph else None},
        "egress_topic_violations_logged": [{k: v for k, v in x.items()} for x in gate.violations],
        "dropped_offline": report_day["dropped_offline"],
        "dropped_offline_by_purpose": _count(gate.dropped_offline, "purpose"),
        "queued_at_end_of_day": report_day["queued_now"],
        "voice": log,
        "camera_clips_recorded_locally": log["camera_clips_recorded"],
        "home_control": {"commands": PLAN["home_control_commands"], "confirmed": len(cs),
                         "confirm_latency_s_p50": round(cs[len(cs) // 2], 3) if cs else None,
                         "confirm_latency_s_max": round(cs[-1], 3) if cs else None},
        "leak_alarm": None if not leak else {"local_siren_s_after_event": round(siren.activations[0][0] - PLAN["leak_event_at_s"], 3)
                       if siren.activations else None,
                       "centre_ack_s_after_event": round(leak["t_ack"] - leak["t_event"], 1)
                       if leak and leak["t_ack"] else None,
                       "transmissions": leak["attempts"] if leak else 0,
                       "centre_dispatch_decisions": len(centre.dispatches)},
        "what_left_the_house_today": summary,
        "what_left_the_house_today_incl_routine": summary_full,
    }


def _count(rows, key):
    out = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


def main():
    normal = run_day(False, leak=False)      # the E09 day
    down = run_day(True, leak=True)
    out = {
        "experiment": "egress",
        "date": date.today().isoformat(),
        "method": ("Discrete-event twin, seed 2026. One 24 h household day through EgressGate "
                   "(the only publisher of egress/<purpose>): 12 cloud-ASR voice requests "
                   "(household opted in, provider must not store), nightly 48 MB encrypted backup, "
                   "update checks every 6 h, AlarmHub supervision heartbeats every 300 s (plus, on the internet-down day only, a "
                   "14:00 leak alarm (AES-128 ADM-CID over a simulated WAN to a simulated centre)), "
                   "25 camera motion clips recorded locally while the camera firmware tries to "
                   "upload each one, a fake plug that phones home every 10 min, one attempt to "
                   "publish egress/ directly, and 200 local home-control commands confirmed by "
                   "state. The same day is rerun with the internet down 00:00-24:00 (gate offline "
                   "and WAN outage), then 1 h of internet back."),
        "params": {"seed": SEED, "settings": SETTINGS, "plan": PLAN},
        "results": {"normal_day": normal, "internet_down_day": down},
        "limitations": [
            "Byte sizes are modelled (Opus voice, 48 MB backup), not measured from real devices.",
            "Home control uses a minimal stand-in device on the bus, not islab/twin; the "
            "'unaffected' result shows no hidden dependency in our components, it does not "
            "test real device firmware that may itself require the cloud.",
            "The gate only sees traffic that is routed through it. A device with its own "
            "route to the internet (own Wi-Fi uplink, cellular) bypasses it; in a real home "
            "this needs network enforcement (VLAN + firewall default-deny), which is not "
            "simulated.",
            "Payload class is declared by the sender; the gate does not inspect content "
            "(an encrypted blob could contain anything). Encryption of backups is trusted "
            "from the sender's flag.",
            "Internet-down day has no alternate alarm path (no cellular backup), so the "
            "centre only receives the leak alarm after the internet returns.",
        ],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps(out["results"], indent=1, default=str))


if __name__ == "__main__":
    main()
