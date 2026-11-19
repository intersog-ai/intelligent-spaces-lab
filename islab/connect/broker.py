"""A real MQTT 3.1.1 broker on 127.0.0.1, started in-process.

First choice: ``amqtt`` (pure Python, PyPI) on its own asyncio loop in a daemon thread.
Fallback: a ``mosquitto`` binary on PATH. If neither exists, :func:`available` is False and
tests skip.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import threading
import time


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def available() -> str | None:
    """Which broker implementation can run here: 'amqtt', 'mosquitto' or None. Needs paho too."""
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        return None
    try:
        import amqtt.broker  # noqa: F401
        return "amqtt"
    except ImportError:
        pass
    return "mosquitto" if shutil.which("mosquitto") else None


class LocalBroker:
    """Context manager: ``with LocalBroker() as b: b.host, b.port``."""

    def __init__(self, port: int | None = None, impl: str | None = None,
                 allow_anonymous: bool = False):
        self.host = "127.0.0.1"
        self.port = port or free_port()
        self.impl = impl or available()
        if self.impl is None:
            raise RuntimeError("no MQTT broker available: pip install amqtt paho-mqtt")
        self._loop = None
        self._thread = None
        self._broker = None
        self._proc = None
        self.version = ""
        # amqtt marks EVERY session anonymous when anonymous access is allowed, and anonymous
        # sessions never get QoS 1/2 messages queued while offline. So we require a username
        # (any; no password check) and islab clients always send one (adapter.new_client).
        self.allow_anonymous = allow_anonymous

    def start(self) -> "LocalBroker":
        if self.impl == "amqtt":
            self._start_amqtt()
        else:
            self._start_mosquitto()
        return self

    def _start_amqtt(self) -> None:
        import amqtt
        from amqtt.broker import Broker
        logging.getLogger("amqtt").setLevel(logging.CRITICAL)   # it logs every disconnect loudly
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)  # PUBACK timeouts to dead clients
        self.version = f"amqtt {getattr(amqtt, '__version__', '?')}"
        cfg = {"listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}"}},
               "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin":
                           {"allow_anonymous": self.allow_anonymous}}}
        ready = threading.Event()
        errors: list[BaseException] = []
        self._loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            try:
                self._broker = Broker(cfg, loop=self._loop)
                self._loop.run_until_complete(self._broker.start())
            except BaseException as exc:          # surface start errors to the caller
                errors.append(exc)
                ready.set()
                return
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="amqtt-broker", daemon=True)
        self._thread.start()
        if not ready.wait(15) or errors:
            raise RuntimeError(f"amqtt broker did not start: {errors}")
        self._wait_port()

    def _start_mosquitto(self) -> None:
        self.version = "mosquitto"
        self._proc = subprocess.Popen(["mosquitto", "-p", str(self.port)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_port()

    def _wait_port(self, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                socket.create_connection((self.host, self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("broker port never opened")

    def stop(self) -> None:
        if self._broker is not None and self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._broker.shutdown(), self._loop)
            try:
                fut.result(10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(5)

    def __enter__(self) -> "LocalBroker":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
