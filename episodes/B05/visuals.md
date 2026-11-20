# B05 visuals: one scene per script paragraph

| # | Key | Headline | On-screen numbers | Moving visual (runs from the first half second) |
|---|---|---|---|---|
| 1 | title | The voice pipeline | Build Track 05 | Seven stage boxes light up left to right along a line, a sound wave entering on the left and a speaker icon pulsing on the right. |
| 2 | twin | Talk to the home, locally | research in simulation · provided as is | Twin floor plan with two satellite icons (kitchen, living); a dotted line from each satellite to a hub box inside the house, a crossed-out cloud outside. |
| 3 | stages | Seven stages, each timestamped | 7 stages · 2-core CPU | Timeline ruler scrolling; event ticks drop onto it labelled wake, endpoint, STT, intent, policy, device, reply, each with a timestamp counter. |
| 4 | speed | First reply in 1.7 seconds | 1.69 s p50 · 66 ms tone | Stopwatch starts when a waveform ends; a tone blip at 66 ms, the reply waveform at 1.69 s, counter freezing. |
| 5 | tiers | Three hubs, labelled assumptions | Pi 5 2.97 s · mini PC 1.69 s · GPU 1.01 s | Three horizontal bars growing at different speeds, each tagged "assumption"; a small "measured" bar for this CPU beside them. |
| 6 | budget | Waiting and transcribing dominate | 412 ms · 396 ms · <1 ms · LLM 5.96 s | Stacked bar assembling segment by segment: hangover, STT, a hairline for grammar and policy, then a long LLM segment sliding in and dwarfing the rest. |
| 7 | confirm | Done only after the device | 663 · 100% true · ack 70.3% | Split screen: left, a light bulb icon turns on, then a "Done" bubble appears; right, "Done" appears first and the bulb stays dark in some frames. |
| 8 | broken | Broken devices, plain words | 76.9% vs 80.8% | A stuck lock icon jitters; on one side "Done" (red strike), on the other a speech bubble "never showed it happened". |
| 9 | tv | A TV under the command | 48.8% wake · 50.9% · 14.0% | Real footage idea: TV playing in a living room with a smart speaker in front. Pexels: "living room tv evening", "smart speaker table". Overlay: waveform of the voice drowning in a second waveform. |
| 10 | wake | The TV woke it twelve times | 12 of 12 · 0 actions · 8.7 min | TV icon emits "Hey Jarvis"; satellite ring lights up; the following words flow to the grammar box and drop into a bin marked "no intent"; a reply bubble "Sorry, I didn't catch that" points at the TV. |
| 11 | pause | A pause cuts the command | 300 ms · 26 → 62 of 95 · 18 | Waveform with a gap; a scissors line snaps at 300 ms; then a "keep listening" bracket stretches across the gap and the second half joins. |
| 12 | words | Misheard and ambiguous | unlock → "and lock" · 37.7% asked | Transcript text typing out "and lock the front door" with "un" fading away; then "turn it off" with two light icons and a question mark between them. |
| 13 | bugs | Our bugs, and a held request | 1 of 9 approvals | Code line "is the back door locked → lock_door" with a strike-through; a phone shows an approval card "share camera clip?" with a padlock. |
| 14 | close | Honest beats fast | intersog.com | The seven stage boxes from scene 1 replay; the final box only lights after a device icon shows a green check; Intersog logo and URL fade in. |
