"""B02: connect the twin over real, local protocols (no vendor cloud).

* :mod:`islab.connect.broker`        a real MQTT broker on localhost (amqtt, in-process; mosquitto fallback)
* :mod:`islab.connect.matter_model`  Matter cluster/attribute mapping of twin state (modelled, no radio)
* :mod:`islab.connect.ha_discovery`  Home Assistant MQTT discovery config payloads
* :mod:`islab.connect.adapter`       twin devices <-> MQTT (paho-mqtt): retained state, Last Will, QoS, loss
* :mod:`islab.connect.hub`           hub over MQTT: commands, confirmation, availability, the E03 routine

Time: the twin runs on virtual "home seconds". Over real MQTT we drive it in wall-clock time,
compressed by ``SPEED`` (home s = wall s x SPEED). See experiments/b02_connect/notes.md.
"""
SPEED = 10.0
