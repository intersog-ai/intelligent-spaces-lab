"""One scripted household day (virtual time, midnight = 0), based on the video series.

    run = run_day(seed=0, faults="none")      # -> House, after the sim ran to 24:00
    run.scenes                                 # scene list with windows, for the timeline

People react to what their phones show (``Phones.when``), not to fixed times, so a delayed or lost
message changes the story the way it would in a real home instead of breaking the script.

Order of the day (the house starts disarmed; the family is away overnight):
  00:05 Anna arms away mode from her phone (level 2: she confirms)
  02:14 back door opened + kitchen motion while armed -> DC-09 alarms to the monitoring centre
  03:00 nightly encrypted backup; 04:00 update check (routine egress)
  03:40 smoke in the hall -> listed alarm's own siren + LocalSiren + DC-09 fire alarm
  05:50 Anna is home: keypad disarm, locks the back door by hand
  06:50 Anna (kitchen) "make it a bit warmer"; 07:05 Mark, same, for the study
  07:20 Sam asks to unlock the front door -> denied, a parent is told
  09:12 leak under the sink -> local rule closes valve.main (no AI) -> confirmation
  every 4 h from 02:17: the front camera tries to phone home -> blocked at egress
  12:00-14:00 internet down; home control in that window: light, study heat, shade, light off
  15:00 a device's friendly name is replaced with an injection; 15:30 a request through the
        naive-LLM path: the injected unlock is denied, the asked-for light runs
  17:05 garage opened; 17:15 still open -> level 2 ask -> Anna says yes on her phone -> closes
  18:04 kitchen window opened -> heating paused (level 1, told) -> Anna: "keep the heating on"
  18:51 courier at the door -> Anna asks to unlock for 2 min (level 3) -> approves on phone -> relock
  19:10 Anna asks to share a camera clip (level 3, approved) -> the egress gate blocks it
  20:00 Sam unlocks the back door by hand (bins)
  22:38 back lock radio goes offline; 22:40 Mark "Good night" -> the back lock doesn't respond
  23:50 Anna: "What did you send out today?" -> EgressGate summary
"""
from __future__ import annotations

from islab.twin import Household, parse_time
from islab.twin.faults import HOSTILE_NAME

from .house import House

T = parse_time

# (id, start, end, title) -- the timeline groups everything that happened in [start, end)
SCENES = [
    ("arm_away", "00:00", "02:00", "Away mode armed from a phone"),
    ("intrusion", "02:00", "03:30", "Back door opened while armed away"),
    ("smoke", "03:30", "05:30", "Smoke in the hall at night"),
    ("home_again", "05:30", "06:45", "Anna is home: keypad disarm"),
    ("warmer_anna", "06:45", "07:00", "Anna: make it a bit warmer"),
    ("warmer_mark", "07:00", "07:15", "Mark: make it a bit warmer (study)"),
    ("sam_unlock", "07:15", "09:00", "Sam asks to unlock the front door"),
    ("leak", "09:00", "11:55", "Leak under the sink"),
    ("internet_down", "11:55", "14:10", "Internet down 12:00-14:00"),
    ("injection", "14:10", "17:00", "A device name tries to unlock the door"),
    ("garage", "17:00", "18:00", "Garage left open"),
    ("window", "18:00", "18:45", "Kitchen window opened while heating"),
    ("courier", "18:45", "19:05", "Courier at the door"),
    ("clip_share", "19:05", "19:55", "Share a camera clip"),
    ("good_night", "19:55", "23:45", "Good night"),
    ("egress_summary", "23:45", "24:00", "What did you send out today?"),
]
OUTAGE = ("12:00", "14:00")


def _scene_windows():
    return [{"id": i, "start": T(a), "end": T(b) if b != "24:00" else 86400.0, "title": title}
            for i, a, b, title in SCENES]


def schedule_day(house: House):
    sim, hh = house.sim, Household(house.home)
    ph = house.phones
    at = sim.at

    def step(t, desc, fn, *a, actor="house"):
        def go():
            sim.trace("scene_step", actor=actor, what=desc)
            fn(*a)
        at(T(t) if isinstance(t, str) else t, go)

    def dev(action, actor="house", **args):
        return lambda: hh.actions[action](actor, dict(args))

    def yes(person, delay):
        return lambda rec: ph.reply(person, rec["request_id"], "yes")

    # 00:05 arm away from the phone (level 2 -> Anna confirms on the phone)
    ph.when("anna", "secure the house for away", yes("anna", 0), delay=25, kind="ask")
    step("00:05", "Anna (away overnight) asks from her phone: arm away mode",
         house.phone_request, "anna", "Arm away mode", actor="Anna")

    # 02:14 intrusion while armed
    step("02:14:00", "The back door is opened with a key (lock.back manual unlock)",
         dev("manual_unlock", "intruder", device="lock.back"), actor="intruder")
    step("02:14:25", "Movement in the kitchen", dev("motion", "intruder"), actor="intruder")

    # 03:40 smoke
    step("03:40", "Smoke in the hall", dev("smoke"), actor="house")
    step("03:52", "Smoke clears; the alarm is reset", dev("smoke_clear"), actor="house")

    # 05:50 home again
    step("05:50", "Anna disarms at the keypad", house.panel.keypad, "disarmed", actor="Anna")
    step("05:51", "Anna locks the back door by hand", dev("manual_lock", "Anna", device="lock.back"), actor="Anna")

    # mornings
    step("06:50", 'Anna, in the kitchen: "Make it a bit warmer."',
         house.say, "anna", "Make it a bit warmer.", "sat.kitchen", actor="Anna")
    step("07:05", 'Mark, in the living room: "Make it a bit warmer in the study."',
         house.say, "mark", "Make it a bit warmer in the study.", "sat.living", actor="Mark")
    step("07:20", 'Sam: "Unlock the front door."', house.say, "sam", "Unlock the front door.", "sat.living",
         actor="Sam")

    # 09:12 leak
    step("09:12", "Water starts leaking under the sink", dev("leak"), actor="house")
    step("09:40", "The leak is mopped up; the sensor dries", dev("leak_stop"), actor="Anna")

    # camera phone-home every 4 h
    for h in range(2, 24, 4):
        step(f"{h:02d}:17", "The front camera tries to send telemetry to its vendor", house.gate.send,
             "camera.front", "telemetry.camvendor.example", "telemetry", "video", b"", 180_000, None,
             f"phonehome:{h}", actor="camera.front")
    step("03:00", "Nightly encrypted backup", house.gate.send, "backup", "backup.storage.example",
         "backup_encrypted", "encrypted_blob", b"", 48_000_000, None, "backup:1", {"encrypted": True})
    step("04:00", "Update check", house.gate.send, "updater", "updates.vendor.example", "fleet_update",
         "update_check", b"", 2_000, None, "update:1")

    # 12:00-14:00 internet down; home control keeps going
    house.internet_outage(T(OUTAGE[0]), T(OUTAGE[1]))
    step("12:30", "A parcel is left at the door", dev("parcel", "courier"), actor="courier")
    step("12:40", 'Anna: "Turn on the kitchen light."', house.say, "anna", "Turn on the kitchen light.",
         "sat.kitchen", actor="Anna")
    step("13:10", 'Mark: "Set the study to 21 degrees."', house.say, "mark", "Set the study to 21 degrees.",
         "sat.living", actor="Mark")
    step("13:30", 'Anna: "Close the living room shade."', house.say, "anna", "Close the living room shade.",
         "sat.living", actor="Anna")
    step("13:45", 'Anna: "Turn off the kitchen light."', house.say, "anna", "Turn off the kitchen light.",
         "sat.kitchen", actor="Anna")

    # 15:00 prompt injection through a device name
    step("15:00", f'The living room light\'s name is changed to "{HOSTILE_NAME}"',
         house.home.faults.hostile_name, "light.living", actor="someone")
    step("15:29", "Voice requests now go through the LLM planner path (naive test double)",
         setattr, house, "planner_path", "llm", actor="lab")
    step("15:30", 'Anna: "Turn on the living room light."', house.say, "anna", "Turn on the living room light.",
         "sat.living", actor="Anna")
    step("15:32", "Voice requests back on the rule planner", setattr, house, "planner_path", "rules", actor="lab")

    # 17:05 garage
    step("17:05", "Mark drives in and leaves the garage door open",
         lambda: house.home["garage.door"].update(state="open", position=100), actor="Mark")
    ph.when("anna", "close the garage", yes("anna", 0), delay=40, kind="ask", after=T("17:00"))

    # 18:04 window
    step("18:04", "Anna opens the kitchen window", dev("open_window", "Anna"), actor="Anna")
    ph.when("anna", "heating is paused", lambda rec: house.say("anna", "Keep the heating on.", "sat.kitchen"),
            delay=40, after=T("18:00"))
    step("18:30", "Anna closes the kitchen window", dev("close_window", "Anna"), actor="Anna")

    # 18:51 courier
    step("18:51", "A courier rings the front doorbell", dev("doorbell", "courier"), actor="courier")
    ph.when("anna", "front doorbell", lambda rec: house.phone_request(
        "anna", "Unlock the front door for 2 minutes."), delay=30, after=T("18:45"))
    ph.when("anna", "approve: unlock the front door", yes("anna", 0), delay=15, kind="approval",
            after=T("18:45"))

    # 19:10 clip share
    step("19:10", 'Anna, in the app: "Share the front camera clip with grandma"',
         house.phone_request, "anna", "Share the front camera clip with grandma", actor="Anna")
    ph.when("anna", "approve: share a front camera clip", yes("anna", 0), delay=20, kind="approval",
            after=T("19:05"))

    # 20:00 back door by hand; 22:38 back lock radio offline; 22:40 good night
    step("20:00", "Sam takes the bins out and leaves the back door unlocked",
         dev("manual_unlock", "Sam", device="lock.back"), actor="Sam")
    step("22:38", "The back door lock's radio drops off the network (fault)",
         house.home.faults.offline, "lock.back", actor="fault")
    step("22:40", 'Mark: "Good night."', house.say, "mark", "Good night.", "sat.living", actor="Mark")

    # 23:50 what left the house
    step("23:50", 'Anna: "What did you send out today?"', house.say, "anna", "What did you send out today?",
         "sat.living", actor="Anna")
    house.scenes = _scene_windows()
    return house


def run_day(seed=0, faults="none", config=None, audit_path=None, until=86400.0):
    house = House(seed=seed, faults=faults, config=config, audit_path=audit_path)
    schedule_day(house)
    house.run(until=until)
    return house
