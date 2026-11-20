"""Cameras: evidence, not verdicts.

ObservingCamera   the B01 twin camera (islab.twin.devices.Camera) with one more label, "pet".
                  Clips stay on the hub: the event carries only label, confidence and a clip_ref.
DetectorModel     what a camera detector reports for what was really there (person, parcel,
                  vehicle, pet, shadow). All rates are ASSUMPTIONS for the twin (DETECTOR_ASSUMPTIONS);
                  no detector was run and no footage exists.
CameraObserver    turns detections into messages, in one of three designs:
                    "threshold"  tells the owner "Person detected" for every person label >= T
                    "verdict"    while armed, a person label >= T raises an intrusion alarm to the
                                 centre and tells the owner "Intruder detected" (the design E08 rejects)
                    "ask"        while armed, a person label >= floor is stated as an observation with
                                 its confidence, its alternatives and where the clip is, and the owner
                                 is asked whether to share this one clip with the monitoring centre.
                                 Yes: an alarm verified by that person, and the clip, with a single-use
                                 consent. No: nothing leaves. No answer: the rule the owner set in
                                 advance (default: ask the next person, then keep it at home).
The observer never uses a verdict word (VERDICT_WORDS) in the "ask" design.
"""
from __future__ import annotations

from islab.core.bus import Envelope
from islab.twin.devices import Camera

SCENE_CLASSES = ("person", "parcel", "vehicle", "pet", "shadow")
VERDICT_WORDS = ("intruder", "burglar", "stranger", "break-in", "threat", "suspicious", "criminal")

# truth -> list of (reported label or None, probability, (confidence mean, sd)). ASSUMED.
DETECTOR_ASSUMPTIONS = {
    "person":  [("person", 0.95, (0.85, 0.08)), (None, 0.05, None)],
    "parcel":  [("parcel", 0.85, (0.80, 0.10)), (None, 0.15, None)],
    "vehicle": [("vehicle", 0.90, (0.85, 0.08)), ("person", 0.02, (0.55, 0.10)), (None, 0.08, None)],
    "pet":     [("pet", 0.80, (0.80, 0.10)), ("person", 0.12, (0.62, 0.12)), (None, 0.08, None)],
    "shadow":  [(None, 0.85, None), ("person", 0.07, (0.50, 0.12)), ("pet", 0.05, (0.50, 0.10)),
                ("vehicle", 0.03, (0.50, 0.10))],
}
CLIP_BYTES = 3_000_000      # a 10 s doorway clip, assumed


class ObservingCamera(Camera):
    detection_types = ("person", "parcel", "vehicle", "pet")

    def initial_state(self):
        st = super().initial_state()
        self.local_clips = {}          # clip_ref -> bytes; footage stays here
        return st

    def detect(self, type_, confidence=None):
        clip = super().detect(type_, confidence)
        self.local_clips[clip] = CLIP_BYTES
        return clip


class DetectorModel:
    def __init__(self, rng, table=None):
        self.rng, self.table = rng, table or DETECTOR_ASSUMPTIONS

    def report(self, truth):
        """-> (label, confidence) or None when nothing is reported."""
        u, acc = self.rng.random(), 0.0
        for label, p, conf in self.table[truth]:
            acc += p
            if u < acc:
                if label is None:
                    return None
                mu, sd = conf
                c = min(0.99, max(0.30, self.rng.gauss(mu, sd)))
                return label, round(c, 2)
        return None


def _clock(t):
    t = int(t) % 86400
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}"


ALTERNATIVES = {"person": "a pet, a shadow or a reflection", "pet": "a person bending down or a shadow",
                "vehicle": "headlights or a shadow", "parcel": "a bag or a shadow"}


def observation_text(camera_name, label, conf, t, clip_ref, armed):
    """The owner is told what was observed, with its evidence, and asked. No verdict."""
    where = camera_name
    return (f"The {where} saw something that looks like a {label} at {_clock(t)} "
            f"(confidence {conf:.2f}). It could also be {ALTERNATIVES.get(label, 'something else')}. "
            f"The clip is kept at home ({clip_ref}); you can watch it in the home app. "
            f"The alarm is {armed}. Share this clip with the monitoring centre? Yes / No.")


class CameraObserver:
    NAME = "camera_observer"

    def __init__(self, sim, bus, design="ask", threshold=0.7, floor=0.5, persons=("anna", "mark"),
                 alarm_hub=None, gate=None, centre=None, camera_name="front door camera",
                 ask_timeout_s=180.0, no_answer_rule="ask_next_then_keep_home", armed=lambda t: "off"):
        assert design in ("threshold", "verdict", "ask")
        self.sim, self.bus, self.design = sim, bus, design
        self.threshold, self.floor, self.persons = threshold, floor, list(persons)
        self.hub, self.gate, self.centre = alarm_hub, gate, centre
        self.camera_name, self.ask_timeout_s = camera_name, ask_timeout_s
        self.no_answer_rule, self.armed = no_answer_rule, armed
        self.messages = []          # (t, person, kind, text, clip_ref)
        self.asks = {}              # request_id -> state
        self.signals_to_centre = [] # (t, clip_ref, how)
        self.clips_shared = []      # (t, clip_ref, consent_id)
        self._n = 0
        bus.subscribe("event/+", self._on_event, name=self.NAME)
        for p in self.persons:
            bus.subscribe(f"reply/{p}", self._on_reply, name=self.NAME)

    def _send(self, person, kind, text, clip_ref=None, request_id=None):
        payload = {"text": text, "kind": kind}
        if request_id:
            payload["request_id"] = request_id
        self.messages.append((self.sim.now, person, kind, text, clip_ref))
        self.bus.publish(Envelope(topic=f"notify/{person}", payload=payload, source=self.NAME,
                                  ts=self.sim.now))

    def _on_event(self, env):
        if not env.topic.split("/", 1)[1].startswith("camera."):
            return
        p = env.payload or {}
        label, conf, clip = p.get("type"), p.get("confidence"), p.get("clip_ref")
        if label not in ("person", "parcel", "vehicle", "pet") or conf is None:
            return
        armed = self.armed(self.sim.now)
        if self.design == "threshold":
            if label == "person" and conf >= self.threshold:
                self._send(self.persons[0], "tell", "Person detected at the front door.", clip)
        elif self.design == "verdict":
            if label == "person" and conf >= self.threshold and armed != "off":
                self._send(self.persons[0], "tell", "Intruder detected at the front door. "
                           "The monitoring centre has been alerted.", clip)
                self.signals_to_centre.append((self.sim.now, clip, "automatic"))
                if self.hub is not None:
                    self.hub.raise_alarm("intrusion", 8, t_event=self.sim.now, source="camera.front")
        else:
            if label == "person" and conf >= self.floor and armed != "off":
                self._ask(0, label, conf, clip, armed, self.sim.now)

    def _ask(self, i, label, conf, clip, armed, t_obs):
        self._n += 1
        rid = f"cam{self._n:05d}"
        text = observation_text(self.camera_name, label, conf, t_obs, clip, armed)
        self.asks[rid] = {"t": self.sim.now, "t_obs": t_obs, "clip_ref": clip, "label": label,
                          "conf": conf, "person": self.persons[i], "i": i, "armed": armed,
                          "answer": None, "t_answer": None, "outcome": None}
        self._send(self.persons[i], "ask", text, clip, rid)
        self.sim.after(self.ask_timeout_s, self._timeout, rid)

    def _timeout(self, rid):
        a = self.asks[rid]
        if a["answer"] is not None:
            return
        a["outcome"] = "no_answer"
        nxt = a["i"] + 1
        if self.no_answer_rule == "ask_next_then_keep_home" and nxt < len(self.persons):
            self._ask(nxt, a["label"], a["conf"], a["clip_ref"], a["armed"], a["t_obs"])
        else:
            a["outcome"] = "no_answer_kept_home"
            self.sim.trace("camera_kept_home", clip_ref=a["clip_ref"])

    def _on_reply(self, env):
        p = env.payload or {}
        a = self.asks.get(p.get("request_id"))
        if a is None or a["answer"] is not None or a["outcome"] is not None:
            return
        person = env.topic.split("/", 1)[1]
        if person != a["person"]:
            return
        a["answer"], a["t_answer"] = p.get("answer"), self.sim.now
        if a["answer"] != "yes":
            a["outcome"] = "person_said_no"
            return
        a["outcome"] = "person_shared"
        self.signals_to_centre.append((self.sim.now, a["clip_ref"], f"shared_by_{person}"))
        if self.hub is not None:
            self.hub.raise_alarm("intrusion", 8, t_event=self.sim.now, source="camera.front")
        if self.gate is not None:
            cid = self.gate.record_consent(person, a["clip_ref"], getattr(self.hub, "centre_host",
                                                                         "receiver.monitoring.example"))
            ok = self.gate.send(source=self.NAME, dest=getattr(self.hub, "centre_host",
                                                               "receiver.monitoring.example"),
                                purpose="alarm", payload_class="camera_clip", size=CLIP_BYTES,
                                msg_id=f"clip:{a['clip_ref']}",
                                meta={"consent_id": cid, "clip_ref": a["clip_ref"]},
                                transport=(lambda d, c=a["clip_ref"]: self.centre.attach_clip(c, CLIP_BYTES))
                                if self.centre is not None else None)
            if ok:
                self.clips_shared.append((self.sim.now, a["clip_ref"], cid))


class PersonModel:
    """A household member answering asks on the phone. ASSUMED behaviour, labelled in results.
    Answers with probability p_answer (lower at night), after a delay; judges the clip correctly
    with probability p_correct. 'yes' = share with the centre, which is right only for a real
    unexpected person (truth_of(clip) == 'intruder')."""

    def __init__(self, sim, bus, name, truth_of, p_answer_day=0.85, p_answer_night=0.5,
                 delay_median_s=45.0, p_correct=0.97, rng=None):
        self.sim, self.bus, self.name, self.truth_of = sim, bus, name, truth_of
        self.rng = rng or sim.rng
        self.p_day, self.p_night = p_answer_day, p_answer_night
        self.delay_median_s, self.p_correct = delay_median_s, p_correct
        self.received = []
        bus.subscribe(f"notify/{name}", self._on, name=f"phone_{name}")

    def _on(self, env):
        p = env.payload or {}
        self.received.append((self.sim.now, p.get("kind"), p.get("text")))
        if p.get("kind") != "ask":
            return
        rng = self.rng
        hour = (self.sim.now % 86400) / 3600
        night = hour >= 23 or hour < 6.5
        if rng.random() > (self.p_night if night else self.p_day):
            return
        delay = self.delay_median_s * (2.0 ** rng.gauss(0, 1.0))
        truth = self.truth_of(p.get("text", ""))
        right = "yes" if truth == "intruder" else "no"
        ans = right if rng.random() < self.p_correct else ("no" if right == "yes" else "yes")
        self.sim.after(delay, self.bus.publish, Envelope(
            topic=f"reply/{self.name}", payload={"request_id": p["request_id"], "answer": ans},
            source=f"phone_{self.name}", ts=self.sim.now + delay))
