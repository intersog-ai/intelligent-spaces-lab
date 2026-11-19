# speaker_id — notes (2026-11-19)

`python experiments/speaker_id/run.py` takes about 45 s on 2 vCPU. It writes `results.json` and `islab/voice/data/household_embeddings.json`.

**Model:** Resemblyzer 0.1.4 GE2E d-vector (256-d). `pip install resemblyzer` worked, and its weights ship in the wheel. No fallback was needed. `MFCCEncoder` is kept in `islab/voice/speaker.py` only so the API still works without torch.

**Data reality:** the voice-over contains 6.2 s of speech for Anna, 3.6 s for Mark and 1.4 s for Sam. Enrolling them with 10 s or 30 s is impossible. That gave two protocols:
- **P1 (the real household):** 2-fold, enroll on one file and test on the other. That yields 5 target trials and 334 non-target trials. The numbers are anecdotal.
- **P2 (surrogate household):** David, Amanda and Ollie are enrolled with 10 s and with 30 s. The thresholds come from P2. A "room" variant passes enrollment, tests and impostors through a simulated room and satellite mic (RT60 0.4 s plus 30 dB pink noise).

**Results (10 s enrollment, 2 s windows):**
- Studio audio: EER 1.2 %. At the strict threshold of 0.80, FAR is 0.8 % and FRR is 2.3%. Enrolling with 30 s did not help (EER 1.3 %).
- Room channel: EER 11.9 % at a threshold of 0.775. At the strict threshold of 0.824, FAR is 0.8 % and FRR is 34 %.
- P1 household at the studio strict threshold: FAR 0 of 334 and FRR 3 of 5. Mark's scores against his own profile (0.68) are lower than Anna's scores against Amanda's assistant voice (mean 0.76 in P2).
- Replay in the room. A Bluetooth-speaker replay of the victim's own recording was accepted as the victim in 81 % of cases at the room EER threshold and 38 % at the strict threshold, and the victim was the top-1 match 98 % of the time. A phone-speaker replay (300-3400 Hz, clipped) was accepted 6 % / 0 % of the time. A voice match is therefore not proof of identity.
- Kitchen-like noise, FRR at the room EER threshold: 12 % clean, 14 % at 20 dB, 45 % at 10 dB, 85 % at 5 dB. With thresholds calibrated on studio audio, FRR is already 52 % at 20 dB. Thresholds have to be calibrated on the channel the hub actually hears.

The limitations are listed in results.json. The most important: every voice is ElevenLabs TTS, there are only 7 voices, trials are correlated, and replay and noise are DSP proxies.

**Next step, if credits allow:** generate 60 s or more per household voice with ElevenLabs, ideally with prosody variation. Better still, record real colleagues with consent. Then rerun P1 with true 10 s and 30 s enrollment.
