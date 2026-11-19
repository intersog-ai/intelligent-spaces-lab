"""Write before_after.md from results_before.json (re-scored section) and results.json.

    python experiments/scorecard/compare.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MIXES = ["none", "light", "heavy"]


def load():
    b = json.load(open(os.path.join(HERE, "results_before.json")))["recomputed_with_alarm_suite"]["by_mix"]
    a = json.load(open(os.path.join(HERE, "results.json")))["results"]["by_mix"]
    return b, a


def pctf(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def num(x):
    return "-" if x is None else (f"{x:g}" if isinstance(x, (int, float)) else str(x))


def table(title, get, f, note=""):
    b, a = DATA
    L = [f"### {title}", ""] + ([note, ""] if note else []) + ["| Mix | Before | After |", "|---|---|---|"]
    for m in MIXES:
        L.append(f"| {m} | {f(get(b[m]))} | {f(get(a[m]))} |")
    return L + [""]


def mean(st):
    return None if st is None else st["mean"]


def main():
    global DATA
    DATA = load()
    L = ["# Before / after: alarm recovery, command resend, local-rule refusals", "",
         "Same 50 seeds x 3 fault mixes, same day script, same suites (islab/suites/principles.py incl. the new "
         "ALARM suite). Before = code snapshot before the Nov 19 fixes (results_before.json, section "
         "`recomputed_with_alarm_suite`); after = results.json.", "",
         "Thresholds: ALARM passes a run only if every smoke/leak/intrusion that physically happened (outside "
         "internet outages) reaches the monitoring centre within 60 s and every smoke sounds the local siren within "
         "1 s; per mix the pooled delivery target is >= 99 % within 60 s. P4 passes only if every failed/timed-out "
         "action and every refusal (including local-rule refusals) is told to someone within 60 s.", "",
         "Fixes: (a) AlarmHub also raises from detector state (smoke/leak true, motion true while armed, lock "
         "locked->unlocked while armed with no hub unlock command), one alarm per episode; smoke and leak "
         "detectors are wired straight to the LocalSiren. (b) ActionTracker resends the same cmd_id when no ack "
         "arrives within 3 s (max 2 resends); twin devices now treat a repeated cmd_id as idempotent (re-ack, no "
         "second actuation). (c) Refused local rules notify the default person in plain words.", "",
         "## Suite pass rates", ""]
    for su in ["P1", "P2", "P3", "P4", "P5", "ALARM", "BUS"]:
        L += table(f"{su} pass rate", lambda m, su=su: m["suites"][su]["pass_rate"], pctf)
    L += ["## Alarm path (pooled over seeds)", ""]
    L += table("Alarms delivered to the centre within 60 s", lambda m: m["alarm_pooled"]["delivery_rate_within_60s"],
               pctf, "Target >= 99 %.")
    L += table("Alarm episodes counted (intrusions count only if the panel was physically armed)",
               lambda m: m["alarm_pooled"]["episodes"], num)
    L += table("Alarm delay p50 (s)", lambda m: m["alarm_pooled"]["delay_p50_s"], num,
               "Delay statistics cover delivered alarms only: before, the alarms that never arrived are missing "
               "from them, which is why before looks faster at p95/max.")
    L += table("Alarm delay p95 (s)", lambda m: m["alarm_pooled"]["delay_p95_s"], num)
    L += table("Alarm delay max (s)", lambda m: m["alarm_pooled"]["delay_max_s"], num)
    L += table("Smoke: local siren within 1 s", lambda m: m["alarm_pooled"]["siren_within_1s_rate"], pctf)
    L += ["## Home control", ""]
    L += table("Home control confirmed, outside outage (mean over seeds)",
               lambda m: mean(m["suites"]["P5"]["numbers"]["home_control_pass_rate_outside_window"]), pctf)
    L += table("Home control confirmed, internet down 12:00-14:00 (mean)",
               lambda m: mean(m["suites"]["P5"]["numbers"]["home_control_pass_rate_in_window"]), pctf)
    L += table("Time to confirm p50 (s, mean of per-day p50)",
               lambda m: mean(m["suites"]["P2"]["numbers"]["time_to_confirm_p50_s"]), num)
    L += table("Time to confirm p95 (s, mean of per-day p95)",
               lambda m: mean(m["suites"]["P2"]["numbers"]["time_to_confirm_p95_s"]), num)
    L += table("Local-rule refusals per day (mean)",
               lambda m: mean(m["suites"]["P4"]["numbers"]["automation_denials_not_told"]), num,
               "Counted refusals; before, none of them was told (P4 failures); after, each is told.")
    L += table("P4 silent failures per day (mean)", lambda m: mean(m["suites"]["P4"]["numbers"]["silent_failures"]), num)
    L += ["## Scenes as scripted (heavy mix)", "", "| Scene | Before | After |", "|---|---|---|"]
    b, a = DATA
    for k in a["heavy"]["scenes"]:
        L.append(f"| {k} | {pctf(b['heavy']['scenes'][k])} | {pctf(a['heavy']['scenes'][k])} |")
    ah, bh = a["heavy"]["alarm_pooled"], b["heavy"]["alarm_pooled"]
    L += ["", "## What changed, what is not solved", "",
          f"Recovering alarms from detector state lifted heavy-loss delivery within 60 s from "
          f"{pctf(bh['delivery_rate_within_60s'])} to {pctf(ah['delivery_rate_within_60s'])} and light-loss delivery "
          f"to {pctf(a['light']['alarm_pooled']['delivery_rate_within_60s'])}, and wiring the detectors to the siren "
          f"made the local siren 100 % in every mix (recovered alarms form the slow "
          f"tail: p95/max rose because they now arrive late instead of never). Command resends raised heavy-mix home-control confirmation "
          f"from {pctf(mean(b['heavy']['suites']['P5']['numbers']['home_control_pass_rate_outside_window']))} to "
          f"{pctf(mean(a['heavy']['suites']['P5']['numbers']['home_control_pass_rate_outside_window']))}, and "
          f"local-rule refusals are now told, so P4 passes in every mix. Still not solved: heavy loss misses the "
          f"99 % alarm target, because when a detector's event and its state copy are both lost the hub only hears "
          f"at the next sensor heartbeat (300 s), so the alarm arrives late (max {num(ah['delay_max_s'])} s); a "
          f"retransmitting radio, a faster heartbeat in alarm state or a hard-wired detector would close that. "
          f"There is no second (cellular) path to the centre, so during the internet outage alarms only queue; a lost "
          f"state report after an ack (no resend then) still leaves actions timed out, and a lagging clock still "
          f"makes a device permanently 'stale'.", ""]
    with open(os.path.join(HERE, "before_after.md"), "w") as f:
        f.write("\n".join(L))
    print("\n".join(L[-3:]))


if __name__ == "__main__":
    main()
