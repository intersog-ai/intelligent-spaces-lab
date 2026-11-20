"""The staged local voice pipeline on the twin.

    wake word -> earcon -> end of speech -> speech to text -> intent (rules, then LLM stub)
              -> policy engine -> device -> spoken reply

    pipe = VoicePipeline(sim, bus, engine, StageModel("pi5_cpu", tiers["pi5_cpu"]), home=home)
    pipe.speak(Utterance("u1", "sat.kitchen", "anna", 0.9, "close the garage door",
                         hyp="close the garage door", speech_s=1.4), at=10.0)
    sim.run(until=60)
    pipe.records["u1"]          # stage times, spoken lines, outcome, ground-truth checks

Stage durations are drawn per utterance from the tier's StageModel with ``sim.rng`` (measured on this
CPU where marked, assumed elsewhere). The device stage is the twin itself: B01 devices, the B01
ActionTracker and PolicyEngine, unchanged.

Every stage publishes ``pipeline/<satellite>/<stage>`` on the bus, payload
``{"uid", "stage", "t", "ms", "source"}`` (t = virtual time the stage finished). The transcript is also
published on the contract topic ``voice/<satellite>`` as ``{"audio_ref", "text"}``. Spoken output is
``pipeline/<satellite>/say`` with ``{"uid", "kind", "text", "t_audio"}``.

Principle 2: with ``reply_on="confirm"`` (default) the pipeline says "Done" only after the action
tracker has confirmed the target state from a device report received after the command. If the device
has not confirmed within ``hold_s`` it says a holding line that claims nothing ("Sent. Waiting for the
garage door to confirm."). ``reply_on="ack"`` is the baseline that says "Done" on the device's ack.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from islab.core.bus import Envelope
from islab.policy.engine import Proposal, _intent, _summary
from islab.policy.tools import plain

from .intent import DialogueContext, Grammar, LLMStub, devices_of, explicit_for


def _name(device_id):
    return {"temp.kitchen": "kitchen temperature sensor"}.get(device_id, plain(device_id))

WAKE_GAP_S = 0.30          # person pauses after the wake word before the command (assumption)
ANSWER_DELAY_S = 1.0       # person says "yes" this long after a question finishes (assumption)
ANSWER_SPEECH_S = 0.5      # length of a spoken "yes" (assumption)


@dataclass
class Utterance:
    uid: str
    satellite: str
    person: str | None            # who is speaking (None = TV / unknown)
    confidence: float | None      # voice identity confidence handed to the policy engine
    text: str                     # what was actually said (ground truth)
    hyp: str                      # what speech to text produced (measured on audio, or = text)
    speech_s: float               # command audio from start to the point where the endpointer closed
    wake_detected: bool = True
    source: str = "person"        # person | tv
    meta: dict = field(default_factory=dict)


class VoicePipeline:
    def __init__(self, sim, bus, engine, model, home=None, grammar=None, llm=None, ctx=None,
                 reply_on="confirm", hold_s=1.5, use_llm=True, answer_level2=True, answer_fn=None):
        if reply_on not in ("confirm", "ack"):
            raise ValueError("reply_on must be 'confirm' or 'ack'")
        self.sim, self.bus, self.engine, self.model, self.home = sim, bus, engine, model, home
        self.grammar = grammar or Grammar(engine.store)
        self.llm = llm or LLMStub()
        self.use_llm = use_llm
        self.ctx = ctx or DialogueContext()
        self.reply_on, self.hold_s = reply_on, hold_s
        self.answer_level2 = answer_level2
        # what the simulated person answers to a spoken level-2 question: callable(utt, decision) -> "yes" | "no"
        self.answer_fn = answer_fn or (lambda utt, d: "yes")
        self.records = {}
        self._by_rid = {}                 # request_id -> uid
        self._acked = set()
        engine.listeners.append(self._on_final)
        if reply_on == "ack":
            bus.subscribe("ack/#", self._on_ack, name="pipeline.ack_baseline")

    # ------------------------------------------------------------------ helpers
    def _stage(self, rec, stage, ms, source=None):
        rec["stages"][stage] = {"t": round(self.sim.now, 4), "ms": round(ms, 2)}
        self.bus.publish(Envelope(f"pipeline/{rec['satellite']}/{stage}",
                                  {"uid": rec["uid"], "stage": stage, "t": round(self.sim.now, 4), "ms": round(ms, 2),
                                   "source": source or self.model.source(stage)},
                                  source="pipeline", ts=self.sim.now))

    def _say(self, rec, kind, text):
        """TTS + downlink + audio out; records when the first audio is heard."""
        rng = self.sim.rng
        ms = self.model.draw("tts_first_audio", rng) + self.model.draw("net_hub_to_sat", rng) + \
            self.model.draw("audio_out", rng)
        t_audio = self.sim.now + ms / 1000.0
        line = {"kind": kind, "text": text, "t_ready": round(self.sim.now, 4), "t_audio": round(t_audio, 4)}
        rec["said"].append(line)
        self.sim.at(t_audio, self._audio_out, rec, line)
        return line

    def _audio_out(self, rec, line):
        if line["kind"] == "done" and rec.get("_check_done"):
            rec["_check_done"](line)
        self.bus.publish(Envelope(f"pipeline/{rec['satellite']}/say",
                                  {"uid": rec["uid"], "kind": line["kind"], "text": line["text"], "t_audio": line["t_audio"]},
                                  source="pipeline", ts=self.sim.now))

    # ------------------------------------------------------------------ entry
    def speak(self, utt: Utterance, at: float):
        """The wake word ends at virtual time ``at``; the command follows WAKE_GAP_S later."""
        rec = {"uid": utt.uid, "satellite": utt.satellite, "person": utt.person, "source": utt.source,
               "text": utt.text, "hyp": utt.hyp, "meta": dict(utt.meta), "t_wake_end": at,
               "t_speech_end": at + WAKE_GAP_S + utt.speech_s, "stages": {}, "said": [], "requests": [],
               "interp": None, "outcome": None, "done_checks": []}
        self.records[utt.uid] = rec
        self.sim.at(at, self._wake, utt, rec)
        return rec

    def _wake(self, utt, rec):
        ms = self.model.draw("wake_detect", self.sim.rng)
        if not utt.wake_detected:
            rec["outcome"] = "missed_wake"
            self.sim.trace("pipeline_missed_wake", uid=utt.uid)
            return
        self.sim.after(ms / 1000.0, self._woke, utt, rec, ms)

    def _woke(self, utt, rec, ms):
        self._stage(rec, "wake", ms)
        e = self.model.draw("earcon_start", self.sim.rng)
        rec["t_earcon"] = round(self.sim.now + e / 1000.0, 4)
        self.sim.at(rec["t_speech_end"], self._speech_end, utt, rec)

    def _speech_end(self, utt, rec):
        ms = utt.meta.get("endpoint_ms")             # measured on this utterance's audio, if known
        ms = self.model.draw("vad_endpoint", self.sim.rng) if ms is None else float(ms)
        self.sim.after(ms / 1000.0, self._endpoint, utt, rec, ms)

    def _endpoint(self, utt, rec, ms):
        self._stage(rec, "endpoint", ms, source=("measured: this utterance's audio" if "endpoint_ms" in utt.meta else None))
        up = self.model.draw("net_sat_to_hub", self.sim.rng)
        stt = self.model.draw("stt", self.sim.rng)
        self.sim.after((up + stt) / 1000.0, self._transcript, utt, rec, up + stt)

    def _transcript(self, utt, rec, ms):
        self._stage(rec, "stt", ms)
        self.bus.publish(Envelope(f"voice/{utt.satellite}", {"audio_ref": f"{utt.satellite}/{utt.uid}.wav",
                                                              "text": utt.hyp}, source=utt.satellite, ts=self.sim.now))
        it = self.grammar.parse(utt.hyp, satellite=utt.satellite, ctx=self.ctx, now=self.sim.now)
        ms = self.model.draw("intent_rules", self.sim.rng)
        if it.status == "none" and self.use_llm:
            it2 = self.llm.parse(utt.hyp)
            ms += self.model.draw("llm", self.sim.rng)
            if it2.ok:
                it = it2
            rec["llm_used"] = True
        self.sim.after(ms / 1000.0, self._intent, utt, rec, it, ms)

    def _intent(self, utt, rec, it, ms):
        self._stage(rec, "intent", ms, source=(self.model.source("llm") if rec.get("llm_used") else None))
        rec["interp"] = {"status": it.status, "calls": it.calls, "question": it.question, "resolved_by": it.resolved_by}
        if it.status in ("ambiguous", "incomplete"):
            rec["outcome"] = "clarify"
            self._say(rec, "clarify", it.question)
            return
        if not it.ok:
            rec["outcome"] = "not_understood"
            self._say(rec, "not_understood", "Sorry, I didn't catch that.")
            return
        ms = self.model.draw("policy", self.sim.rng)
        self.sim.after(ms / 1000.0, self._policy, utt, rec, it, ms)

    def _policy(self, utt, rec, it, ms):
        self._stage(rec, "policy", ms)
        lines, executing = [], []
        for c in it.calls:
            deps = []
            if it.resolved_by == "relative":
                deps = [(devices_of(c)[0], "setpoint")]
            p = Proposal(tool=c["name"], args=c.get("arguments") or {}, requester=utt.person,
                         confidence=utt.confidence, channel="voice", explicit=explicit_for(c, it),
                         depends_on=deps, text=utt.hyp, origin=it.resolved_by)
            d = self.engine.propose(p)
            self._by_rid[d.request_id] = utt.uid
            rec["requests"].append({"request_id": d.request_id, "tool": d.tool, "args": d.args, "status": d.status,
                                    "reason": d.reason, "final": d.final})
            if d.status == "executing":
                executing.append(d)
            elif d.status == "asked":
                lines.append(("question", f"Should I {_intent(d.tool, d.args)}? Say yes to go ahead."))
                if self.answer_level2 and utt.person:
                    self.sim.after(0.0, self._schedule_yes, rec, d, utt)
            elif d.status == "awaiting_approval":
                who = " or ".join(n.capitalize() for n in self.engine.approvers(d.tool))
                lines.append(("approval_sent", f"Only {who} can {_intent(d.tool, d.args)}. I've sent a request."))
            elif d.status == "denied":
                lines.append(("denied", self._denied_text(d)))
            elif d.status == "answered":
                lines.append(("answer", self._answer_text(d)))
            else:
                lines.append(("rejected", "Sorry, I can't do that."))
        if any(d.status in ("executing", "asked", "awaiting_approval") for d in
               [self.engine.decisions[r["request_id"]] for r in rec["requests"]]):
            self.ctx.remember(utt.satellite, it.calls, self.sim.now)
        for kind, text in lines:
            self._say(rec, kind, text)
        if executing:
            self._watch(rec, executing)
        elif not lines:
            rec["outcome"] = "nothing"
        else:
            rec["outcome"] = lines[0][0]

    # ------------------------------------------------------------------ device stage
    def _watch(self, rec, decisions):
        rec["outcome"] = "pending"
        rec["_pending"] = {d.request_id for d in decisions}
        rec["t_cmd_sent"] = round(self.sim.now, 4)
        if self.reply_on == "confirm" and self.hold_s is not None:
            self.sim.after(self.hold_s, self._hold, rec, decisions)
        rec["_check_done"] = lambda line, rec=rec: self._check_claim(rec, line)

    def _hold(self, rec, decisions):
        if rec["outcome"] != "pending" or any(l["kind"] in ("done", "failed") for l in rec["said"]):
            return
        devs = [c.device_id for d in decisions for c in self.engine.tracker.actions[d.request_id].commands]
        names = " and the ".join(plain(x) for x in dict.fromkeys(devs))
        self._say(rec, "hold", f"Sent. Waiting for the {names} to confirm.")

    def _schedule_yes(self, rec, d, utt):
        q = rec["said"][-1] if rec["said"] else None
        t_q = q["t_audio"] if q else self.sim.now
        rng = self.sim.rng
        lag = (self.model.draw("vad_endpoint", rng) + self.model.draw("net_sat_to_hub", rng) +
               self.model.draw("stt", rng) + self.model.draw("intent_rules", rng)) / 1000.0
        t_yes = t_q + 2.0 + ANSWER_DELAY_S + ANSWER_SPEECH_S + lag   # question ~2 s long (assumption)
        rec["_pending"] = {d.request_id}
        self.sim.at(t_yes, self._yes, rec, d, utt)

    def _yes(self, rec, d, utt):
        rec["outcome"] = "pending"
        rec["t_cmd_sent"] = round(self.sim.now, 4)
        rec["_check_done"] = lambda line, rec=rec: self._check_claim(rec, line)
        ans = self.answer_fn(utt, d)
        rec["voice_answer"] = ans
        ok = self.engine.voice_answer(d.request_id, utt.person, utt.confidence, ans)
        rec["voice_yes"] = ok and ans == "yes"
        if ans != "yes":
            rec["outcome"] = "declined"
            self._say(rec, "declined", "OK, I won't.")
            return
        if ok and self.reply_on == "confirm" and self.hold_s is not None:
            self.sim.after(self.hold_s, self._hold, rec, [d])

    def _on_ack(self, env):
        cmd_id = (env.payload or {}).get("cmd_id", "")
        rid = cmd_id.rsplit(".c", 1)[0]
        uid = self._by_rid.get(rid)
        if uid is None or rid in self._acked:
            return
        rec = self.records[uid]
        self._acked.add(rid)
        d = self.engine.decisions.get(rid)
        if env.payload.get("status") == "accepted":
            rec["t_done_basis"] = round(self.sim.now, 4)
            self._say(rec, "done", f"Done. {_summary(d.tool, d.args)}.")
        else:
            self._say(rec, "failed", f"The {plain(env.topic.split('/', 1)[1])} refused the command.")

    def _on_final(self, d):
        uid = self._by_rid.get(d.request_id)
        if uid is None:
            return
        rec = self.records[uid]
        for r in rec["requests"]:
            if r["request_id"] == d.request_id:
                r["final"] = d.final
        if d.final not in ("confirmed", "failed", "timed_out"):
            if d.final in ("declined", "no_answer", "expired") and d.request_id in rec.get("_pending", ()):
                rec["outcome"] = d.final
            return
        rec.setdefault("finals", {})[d.request_id] = d.final
        rec["t_outcome"] = round(self.sim.now, 4)
        pend = rec.get("_pending", set())
        if not pend or not all(r in rec["finals"] for r in pend):
            return
        finals = [rec["finals"][r] for r in pend]
        rec["outcome"] = "confirmed" if all(f == "confirmed" for f in finals) else "failed"
        if self.reply_on == "ack":
            if d.final in ("failed", "timed_out") and d.request_id not in self._acked:
                self._say(rec, "failed", (d.detail or {}).get("message") or "That didn't work.")
            return
        if rec["outcome"] == "confirmed":
            rec["t_done_basis"] = round(self.sim.now, 4)
            text = " ".join(f"{_summary(self.engine.decisions[r].tool, self.engine.decisions[r].args)}." for r in pend)
            self._say(rec, "done", f"Done. {text}")
        else:
            msgs = [((self.engine.decisions[r].detail or {}).get("message") or "That didn't work.") for r in pend
                    if rec["finals"][r] != "confirmed"]
            self._say(rec, "failed", " ".join(msgs))

    # ------------------------------------------------------------------ ground truth
    def _check_claim(self, rec, line):
        """At the moment 'Done' is heard: was the action confirmed by device state first, and is the
        twin's physical device actually in the target state?"""
        chk = {"t_audio": line["t_audio"], "confirmed_before": False, "true_at_audio": None}
        tr = self.engine.tracker
        cmds = [c for r in rec.get("_pending", ()) if r in tr.actions for c in tr.actions[r].commands]
        chk["confirmed_before"] = bool(cmds) and all(c.status == "confirmed" for c in cmds)
        if self.home is not None and cmds:
            chk["true_at_audio"] = all(all(self.home.devices[c.device_id].state.get(k) == v for k, v in c.expect.items())
                                       for c in cmds)
        rec["done_checks"].append(chk)

    @staticmethod
    def _denied_text(d):
        if d.reason == "not_permitted":
            return f"I can't {_intent(d.tool, d.args)} for {str(d.actor).capitalize()}: that isn't allowed in the house settings."
        if d.reason == "stale_context":
            names = ", ".join(plain(s["device_id"]) for s in (d.detail or []))
            return f"I didn't {_intent(d.tool, d.args)} because my reading of the {names} is not fresh."
        if d.reason == "not_requested_by_person":
            return "I didn't do that: it isn't what I heard you ask for."
        if str(d.reason).startswith("rule:"):
            return f"I didn't {_intent(d.tool, d.args)}: {d.detail}"
        return f"I didn't {_intent(d.tool, d.args)}."

    @staticmethod
    def _answer_text(d):
        res = d.result or {}
        if not res.get("known", True) and "attrs" not in res:
            return f"I don't have a reading from the {_name(res.get('device_id'))}."
        attrs = res.get("attrs", {})
        dev = res.get("device_id")
        for k in ("temperature", "current_temp", "state"):
            if k in attrs:
                a = attrs[k]
                return (f"The {_name(dev)} reads {a['value']}, {int(round(a['age_s']))} seconds ago"
                        + ("." if res.get("freshness") == "fresh" else ", and that reading is old."))
        return f"I have readings from the {_name(dev)}."
