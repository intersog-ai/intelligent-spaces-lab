# Egress: notes

Run: `python experiments/egress/run.py` (under 1 s). Output: `results.json`. Code: `islab/alarm/egress.py`.

## Rules the gate enforces

- The gate is the only publisher of `egress/<purpose>`. A watchdog logs any `egress/` envelope from another source.
- Purposes are a fixed set: alarm, backup_encrypted, fleet_update, speech_recognition_optional,
  llm_text_optional. Each destination is allowlisted for specific purposes.
- Payload classes:
  - `video`, ambient `audio`, `telemetry` and `unknown` never leave.
  - `camera_clip` and `image` can leave only with purpose=alarm, and only when the setting `alarm_clips_allowed` is on.
- **Interpretation to confirm:** speech recognition in the cloud has to send audio. So the class
  `speech_audio` is allowed only for `speech_recognition_optional`, and only when the household has
  opted in **and** the request is tagged `retention=not_stored`. Ambient or recorded audio stays blocked.
- `llm_text_optional` needs its own opt-in. `backup_encrypted` must be flagged encrypted.
- When the internet is down, the gate queues or drops by purpose:
  - alarm and backup are queued. A sender retry replaces the queued copy, so only the newest DC-09 timestamp is kept.
  - Update checks and the optional cloud purposes are dropped.
  - Alarm heartbeats are dropped, because a stale heartbeat would be NAKed for its timestamp.

## Key results (seed 2026)

Normal day, which is the E09 day:

| purpose | messages | bytes |
|---|---|---|
| speech recognition | 12 | 174,991 |
| encrypted backup | 1 | 48,000,000 |
| alarm (heartbeats only) | 287 | 27,839 |
| update checks | 4 | 4,800 |

- No camera items left the house. 25 clips were recorded locally.
- 169 attempts were blocked and logged:
  - 144 phone-home attempts from `plug.cheap` to `telemetry.unknown-vendor.example`. None left the house.
  - 25 camera clip uploads. 12 went to the vendor cloud (not allowlisted). 13 went to the allowlisted backup host but were blocked because a clip is not a backup payload.
- One direct `egress/` publication by the camera was logged as a violation.
- 200 of 200 home-control commands were confirmed, with a p50 of 0.32 s.

Generated answer, routine traffic excluded:
"Twelve voice requests for speech recognition, not stored, and last night's encrypted backup.
No camera footage left the house. 169 attempts to send elsewhere were blocked (camera.front, plug.cheap)."

**The scripted E09 line leaves out routine traffic.** That day also had 287 alarm-line check-ins
(about 28 KB) and 4 update checks. If the assistant is meant to answer exactly, it should say
"plus routine alarm-line check-ins and update checks" (`include_routine=True`). Otherwise the line
is accurate only for user-visible traffic.

Internet-down day:
- Nothing left the house.
- 200 of 200 home-control commands were confirmed, with the same latency as the normal day.
- 12 voice requests fell back to local recognition.
- The backup and the leak alarm were queued. 287 heartbeats and 4 update checks were dropped.
- The leak sounded the local siren 0.01 s after the event.
- The monitoring centre received the leak alarm only when the internet returned, 10 h later, and made one dispatch decision. There is no cellular or backup alarm path. This is the main gap the experiment exposes.
- The hub kept retrying all day: 1,720 transmissions into the gate queue. That is harmless because of
  replace-on-retry, but noisy. A future change would back off harder while the gate reports itself offline.

## Limitations

The gate only controls traffic that is routed through it. Real enforcement needs default-deny at the
network layer. Payload class is declared by the sender, and the gate does not inspect content.
Home control in this experiment uses a stand-in device, not islab/twin. The full list is in results.json.
