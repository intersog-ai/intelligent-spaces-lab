"""Voice pipeline latency budget: Monte Carlo over islab/voice/latency_config.json + local measurements.

Run:  python experiments/voice_latency/run.py [--measure]
  --measure re-runs measure.py first (~1 min; needs the sherpa-onnx models in $ISLAB_MODELS).
Writes experiments/voice_latency/results.json."""
import datetime, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
from islab.voice.pipeline import budget_table, load_config, VoicePipelineModel  # noqa: E402

SEED, N = 2026, 20000

if "--measure" in sys.argv:
    subprocess.run([sys.executable, os.path.join(HERE, "measure.py")], check=True)

cfg = load_config()
table = budget_table(N, SEED, cfg)
measured = json.load(open(os.path.join(HERE, "measured.json"))) if os.path.exists(os.path.join(HERE, "measured.json")) else {}
summary = {t: {p: {"earcon_ms": r["earcon"], "end_of_speech_to_intent_ms": r["to_intent"], "end_of_speech_to_reply_audio_ms": r["reply"]}
               for p, r in v.items()} for t, v in table.items()}
n_assumed = sum(1 for t in cfg["tiers"] for k, v in {**cfg["common"], **cfg["tiers"][t]}.items()
                if isinstance(v, dict) and str(v.get("source", "")).startswith("assumption"))
out = {
    "experiment": "voice_latency",
    "date": datetime.date.today().isoformat(),
    "method": ("Per-stage lognormal (fit to p50/p95) Monte Carlo, N=%d, seed %d, stages summed serially "
               "(no overlap of STT with speech, no streaming LLM->TTS beyond first sentence). Stage values in "
               "islab/voice/latency_config.json, each tagged 'assumption' or 'measured'. Local components measured "
               "by measure.py on a 2-vCPU x86 box without GPU." % (N, SEED)),
    "params": {"n": N, "seed": SEED, "config": cfg},
    "results": {"budget": summary, "stage_sources": {t: VoicePipelineModel(t, cfg).sources() for t in cfg["tiers"]},
                "n_assumed_stage_values": n_assumed,
                "measured_locally": {k: {a: b for a, b in v.items() if a not in ("files", "sample_hyps")}
                                     for k, v in measured.items()}},
    "limitations": [
        "Every Pi 5 + Hailo-10H, Jetson Orin Nano Super and x86+GPU number is an ASSUMPTION written without web access; none was benchmarked or checked against vendor data. Treat the tier comparison as a template to fill with bench numbers.",
        "Stages are independent and serial; real pipelines overlap (streaming STT during speech, streaming LLM tokens into TTS), which lowers p50, and share CPU/accelerator under load, which raises p95.",
        "LLM path cost is dominated by the assumed tokens-before-first-speakable-sentence (35 p50) and tokens/s; a rules-first design avoids it for common commands.",
        "Local measurements: 2 vCPU cloud VM, not an ARM SBC. Wake-word delay is from 2 espeak-ng phrases only. VAD endpoint uses silencedetect boundaries as ground truth.",
        "No LLM was measured: weights are only on huggingface.co (blocked).",
    ],
}
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
for t, v in summary.items():
    for p, r in v.items():
        print(f"{t:24s} {p:5s} earcon p50/p95 {r['earcon_ms']['p50']:.0f}/{r['earcon_ms']['p95']:.0f}  "
              f"reply {r['end_of_speech_to_reply_audio_ms']['p50']:.0f}/{r['end_of_speech_to_reply_audio_ms']['p95']:.0f} ms")
