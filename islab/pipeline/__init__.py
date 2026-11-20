"""B05: the staged local voice pipeline (wake word -> end of speech -> speech to text -> intent ->
policy -> device -> spoken reply) on the twin, plus adapters for real local audio components.

Public API
    VoicePipeline, Utterance            islab.pipeline.pipeline
    Grammar, LLMStub, DialogueContext,
    words_to_numbers                    islab.pipeline.intent
    build_tiers, StageModel, TIER_ORDER islab.pipeline.tiers
    islab.pipeline.real                 openWakeWord / silero-vad / webrtcvad / sherpa-onnx / Piper adapters
                                        (optional heavy imports; used by experiments/b05_pipeline)
Builds on islab.voice (B01) and islab.policy without editing them.
"""
from .intent import DialogueContext, Grammar, Interpretation, LLMStub, words_to_numbers, devices_of
from .tiers import StageModel, build_tiers, load_tiers, TIER_ORDER
from .pipeline import Utterance, VoicePipeline

__all__ = ["DialogueContext", "Grammar", "Interpretation", "LLMStub", "words_to_numbers", "devices_of",
           "StageModel", "build_tiers", "load_tiers", "TIER_ORDER", "Utterance", "VoicePipeline"]
