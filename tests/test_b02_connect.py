"""B02: twin over a real local MQTT broker. Broker tests skip without amqtt/mosquitto + paho-mqtt."""
import json
import re
import threading
import time

import pytest

from islab.connect import ha_discovery as ha
from islab.connect import matter_model as mm
from islab.twin import devices as d

pytest.importorskip("paho.mqtt.client")
from islab.connect import broker as brk  # noqa: E402

needs_broker = pytest.mark.skipif(brk.available() is None, reason="no MQTT broker package")

LAYOUT = {
    "light.kitchen":  (d.Light, "Kitchen light", "light"),
    "light.living":   (d.Light, "Living room light", "light"),
    "thermo.kitchen": (d.Thermostat, "Kitchen thermostat", "thermostat"),
    "thermo.study":   (d.Thermostat, "Study thermostat", "thermostat"),
    "shade.living":   (d.Shade, "Living room shade", "shade"),
    "shade.bedroom":  (d.Shade, "Bedroom shade", "shade"),
    "window.kitchen": (d.WindowContact, "Kitchen window", "contact"),
}
NAMES = {k: v[1] for k, v in LAYOUT.items()}


# ------------------------------------------------------------------ no broker needed
def test_matter_mapping_roundtrip_and_inversions():
    assert mm.to_matter("shade", {"state": "open", "position": 100})[
        "WindowCovering.CurrentPositionLiftPercent100ths"] == 0          # 0 = fully OPEN in Matter
    assert mm.to_matter("shade", {"state": "closed", "position": 0})[
        "WindowCovering.CurrentPositionLiftPercent100ths"] == 10000
    assert mm.to_matter("contact", {"state": "closed"})["BooleanState.StateValue"] is True
    for kind, st in [("light", {"state": "on", "brightness": 100}),
                     ("light", {"state": "off", "brightness": 0}),
                     ("thermostat", {"mode": "heat", "setpoint": 16.0, "current_temp": 19.5}),
                     ("shade", {"state": "partial", "position": 40}),
                     ("contact", {"state": "open"})]:
        back = mm.from_matter(kind, mm.to_matter(kind, st))
        assert all(back[k] == st[k] for k in back), (kind, st, back)


def test_matter_ids_match_sdk_package_when_installed():
    C = pytest.importorskip("chip.clusters.Objects")
    assert C.WindowCovering.id == 0x0102 and C.BooleanState.id == 0x0045
    assert "verified" in mm.SOURCE


def test_ha_discovery_topics_and_templates():
    jinja2 = pytest.importorskip("jinja2")
    state = {"light": {"state": "off", "brightness": 0},
             "shade": {"state": "closed", "position": 0},
             "thermostat": {"mode": "heat", "setpoint": 16.0, "current_temp": 19.4},
             "contact": {"state": "open"}}
    for dev, (_, name, kind) in LAYOUT.items():
        topic = ha.config_topic("h1", dev, kind)
        assert re.fullmatch(r"homeassistant/(light|cover|climate|binary_sensor)/[A-Za-z0-9_-]+/config",
                            topic), topic
        cfg = ha.config("h1", dev, kind, name)
        assert cfg["availability_topic"].endswith("/availability") and cfg["unique_id"]
        json.loads(ha.dumps(cfg))
        for key, tpl in cfg.items():
            if key.endswith("template"):
                out = jinja2.Template(tpl).render(value_json=state[kind])
                assert out not in ("", "None"), (dev, key)
        if kind == "shade":
            assert jinja2.Template(cfg["position_template"]).render(
                value_json=state["shade"]) == str(cfg["position_closed"])


def test_ha_command_translation():
    assert ha.ha_command("cover/set", b"CLOSE") == ("close", {})
    assert ha.ha_command("position/set", "40") == ("set_position", {"position": 40.0})
    assert ha.ha_command("temperature/set", b"16.0") == ("set_setpoint", {"setpoint": 16.0})
    assert ha.ha_command("light/set", b"off") == ("off", {})
    assert ha.ha_command("cover/set", b"WIGGLE") is None


def test_plain_report_never_says_done_without_state():
    from islab.connect.hub import Step, plain_report
    s1 = Step("light.kitchen", "off", {}, {"state": "off"}, "Kitchen light off", outcome="confirmed", at=3)
    s2 = Step("shade.bedroom", "close", {}, {"position": 0}, "Bedroom shade closed", outcome="offline", at=15)
    s3 = Step("shade.living", "close", {}, {"position": 0}, "Living room shade closed",
              outcome="not_confirmed", at=30)
    txt = plain_report([s1, s2, s3], 0, {"state": "open", "ts": 20}, "Kitchen window", 30, NAMES)
    assert "1 of 3 steps confirmed" in txt and "may still be open" in txt
    assert "no reading that it finished" in txt and "someone needs to" in txt
    assert "Living room shade closed" not in txt.split("Confirmed by the devices:")[1].split(".")[0]


# ------------------------------------------------------------------ real broker
@pytest.fixture(scope="module")
def broker():
    if brk.available() is None:
        pytest.skip("no MQTT broker package")
    with brk.LocalBroker() as b:
        yield b


@needs_broker
def test_leaving_routine_over_real_mqtt_with_dropped_shade(broker):
    from islab.connect.adapter import Clock, DeviceSide
    from islab.connect.hub import Hub, leaving_plan, plain_report
    clock = Clock()
    hub = Hub(broker.host, broker.port, "tr1", clock, qos=1)
    ds = DeviceSide(broker.host, broker.port, "tr1", LAYOUT, clock, seed=3, qos=1)
    for light in ("light.kitchen", "light.living"):
        ds.devices[light].state.update(state="on", brightness=100)
    ds.start()
    time.sleep(0.2)
    ds.drop("shade.bedroom", "power")
    steps = leaving_plan(NAMES)
    t0 = clock.home()
    hub.run(steps, timeout=30)
    out = {s.device_id: s.outcome for s in steps}
    assert out["shade.bedroom"] == "offline"                    # Last Will, not silence
    assert all(v == "confirmed" for k, v in out.items() if k != "shade.bedroom")
    assert ds.devices["shade.living"].state["position"] == 0
    assert ds.devices["shade.bedroom"].state["position"] == 100  # it really is still open
    txt = plain_report(steps, t0, hub.latest("window.kitchen"), "Kitchen window", clock.home(), NAMES)
    assert "5 of 6" in txt and "Bedroom shade" in txt
    # discovery configs are retained and visible; HA-style command works end to end
    assert len(hub.discovery) == 7
    hub.client.publish(f"{ha.base('tr1', 'shade.living')}/cover/set", "OPEN", qos=1)
    end = time.monotonic() + 3
    while time.monotonic() < end and (hub.latest("shade.living") or {}).get("position") != 100:
        time.sleep(0.02)
    assert hub.latest("shade.living")["position"] == 100
    assert hub.latest("shade.living")["matter"]["WindowCovering.CurrentPositionLiftPercent100ths"] == 0
    # a restarted hub learns the shade is offline from the retained Last Will
    hub2 = Hub(broker.host, broker.port, "tr1", clock, qos=1, client_id="tr1-hub2")
    time.sleep(0.3)
    assert hub2.avail["shade.bedroom"][0] == "offline" and hub2.avail["shade.bedroom"][2] is True
    hub2.close()
    ds.stop()
    hub.close()


@needs_broker
def test_qos1_survives_blip_only_with_persistent_session(broker):
    from islab.connect.adapter import new_client
    got = {}
    for qos in (0, 1):
        topic, cid = f"tblip/{qos}", f"tblip-dev-{qos}"
        ready = threading.Event()
        dev = new_client(cid, clean_session=False)
        dev.on_subscribe = lambda *a, **k: ready.set()
        dev.connect(broker.host, broker.port, 5)
        dev.loop_start()
        dev.subscribe(topic, qos)
        assert ready.wait(5)
        dev.loop_stop()
        dev.socket().close()
        time.sleep(0.3)
        hub = new_client(f"tblip-hub-{qos}")
        hub.connect(broker.host, broker.port, 5)
        hub.loop_start()
        time.sleep(0.05)
        hub.publish(topic, "close", qos=qos).wait_for_publish(2)
        ev = threading.Event()
        dev2 = new_client(cid, clean_session=False)
        dev2.on_message = lambda c, u, m: ev.set()
        dev2.connect(broker.host, broker.port, 5)
        dev2.loop_start()
        got[qos] = ev.wait(1.0)
        for c in (dev2, hub):
            c.disconnect()
            c.loop_stop()
    assert got == {0: False, 1: True}


@needs_broker
def test_adapter_ignores_topics_it_did_not_subscribe_to(broker):
    from islab.connect.adapter import Clock, DeviceSide

    class Msg:
        topic = "islab/tr3/light.living/cmd"
        payload = json.dumps({"action": "off", "args": {}, "cmd_id": "x"}).encode()
        retain = True

    ds = DeviceSide(broker.host, broker.port, "tr3", {"light.kitchen": LAYOUT["light.kitchen"]},
                    Clock(), seed=1, discovery=False)
    ds.start()
    ds._on_message(None, "light.kitchen", Msg())         # another device's topic
    assert ds.stats["unsolicited"] >= 1 and ds.inbox.empty()
    ds.stop()
