"""Interoperability check of islab.alarm.dc09 against pysiaalarm (the DC-09 receiver library
Home Assistant's SIA integration uses). Runs real pysiaalarm servers on 127.0.0.1 and sends
our frames over real sockets. This is a test harness, outside the twin: it uses wall-clock
timestamps because pysiaalarm checks message time against datetime.now()."""
from __future__ import annotations

import asyncio
import random
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from . import dc09

ACCOUNT = "AAA123"
ENC_ACCOUNT = "BBB456"
ENC256_ACCOUNT = "CCC789"
ENC192_ACCOUNT = "DDD012"
HEXKEY_ACCOUNT = "EEE345"
KEY16 = "0123456789ABCDEF"                     # configured in pysiaalarm as text
KEY24 = "0123456789ABCDEF01234567"
KEY32 = "0123456789ABCDEF0123456789ABCDEF"


def _now():
    return datetime.now(timezone.utc)


def cases():
    """(name, frame_bytes_factory, key_for_response, expect) -- factory called at send time."""
    rng = random.Random(7)

    def mk(mtype, acct, event, seq, enc=False, key=None, ts_shift=0.0, rcv="0", xdata=None):
        def f():
            m = dc09.DC09Message(mtype=mtype, account=acct, sequence=seq, receiver=rcv, line="1",
                                 event=event, encrypted=enc, xdata=xdata or [],
                                 timestamp=_now() + timedelta(seconds=ts_shift))
            return dc09.encode(m, key=key, rng=rng)
        return f

    k16, k24, k32 = KEY16.encode(), KEY24.encode(), KEY32.encode()
    C = []
    C.append(("adm_cid_smoke_1111", mk(dc09.ADM_CID, ACCOUNT, "1111 01 001", 1), None, "FA"))
    C.append(("adm_cid_smoke_restore_3111", mk(dc09.ADM_CID, ACCOUNT, "3111 01 001", 2), None, "FH"))
    C.append(("adm_cid_leak_1154", mk(dc09.ADM_CID, ACCOUNT, "1154 01 002", 3), None, "WA"))
    C.append(("adm_cid_burglary_1130", mk(dc09.ADM_CID, ACCOUNT, "1130 01 003", 4), None, "BA"))
    C.append(("adm_cid_test_1602", mk(dc09.ADM_CID, ACCOUNT, "1602 00 000", 5), None, "RP"))
    C.append(("sia_dcs_smoke_FA", mk(dc09.SIA_DCS, ACCOUNT, "Nri1/FA001", 6), None, "FA"))
    C.append(("sia_dcs_leak_WA", mk(dc09.SIA_DCS, ACCOUNT, "Nri1/WA002", 7), None, "WA"))
    C.append(("sia_dcs_burglary_BA", mk(dc09.SIA_DCS, ACCOUNT, "Nri1/BA003", 8), None, "BA"))
    C.append(("null_heartbeat", mk(dc09.NULL, ACCOUNT, "", 9), None, "RP"))
    C.append(("no_receiver_field", mk(dc09.ADM_CID, ACCOUNT, "1111 01 001", 10, rcv=None), None, "FA"))
    C.append(("sia_dcs_with_xdata", mk(dc09.SIA_DCS, ACCOUNT, "Nri1/FA001", 11, xdata=["Vsmoke hall"]), None, "FA"))
    C.append(("enc128_adm_cid_smoke", mk(dc09.ADM_CID, ENC_ACCOUNT, "1111 01 001", 12, True, k16), k16, "FA"))
    C.append(("enc128_sia_dcs_leak", mk(dc09.SIA_DCS, ENC_ACCOUNT, "Nri1/WA002", 13, True, k16), k16, "WA"))
    C.append(("enc128_null_heartbeat", mk(dc09.NULL, ENC_ACCOUNT, "", 14, True, k16), k16, "RP"))
    C.append(("enc192_adm_cid_burglary", mk(dc09.ADM_CID, ENC192_ACCOUNT, "1130 01 003", 15, True, k24), k24, "BA"))
    C.append(("enc256_sia_dcs_smoke", mk(dc09.SIA_DCS, ENC256_ACCOUNT, "Nri1/FA001", 16, True, k32), k32, "FA"))
    # same 32-char key, but our side treats it as hex -> 16 raw bytes (AES-128)
    C.append(("enc_key_as_hex_bytes_mismatch",
              mk(dc09.ADM_CID, HEXKEY_ACCOUNT, "1111 01 001", 17, True, bytes.fromhex(KEY32)),
              None, "NAK"))

    def bad_crc():
        b = bytearray(mk(dc09.ADM_CID, ACCOUNT, "1111 01 001", 18)())
        b[1:5] = b"0000" if b[1:5] != b"0000" else b"FFFF"
        return bytes(b)
    C.append(("corrupted_crc", bad_crc, None, "no ACK"))
    C.append(("unknown_account", mk(dc09.ADM_CID, "FFF999", "1111 01 001", 19), None, "NAK"))
    C.append(("stale_timestamp_-300s", mk(dc09.ADM_CID, ACCOUNT, "1111 01 001", 20, ts_shift=-300), None, "NAK"))
    C.append(("future_timestamp_+60s", mk(dc09.ADM_CID, ACCOUNT, "1111 01 001", 21, ts_shift=60), None, "NAK"))
    return C


def _accounts():
    from pysiaalarm import SIAAccount
    return [SIAAccount(ACCOUNT, None), SIAAccount(ENC_ACCOUNT, KEY16),
            SIAAccount(ENC192_ACCOUNT, KEY24), SIAAccount(ENC256_ACCOUNT, KEY32),
            SIAAccount(HEXKEY_ACCOUNT, KEY32)]


def _free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _row(name, expect, resp_bytes, events, key):
    resp = dc09.parse_response(resp_bytes, key) if resp_bytes is not None else {"kind": "TIMEOUT"}
    ev = events[0] if events else None
    code = getattr(ev, "code", None) if ev else None
    kind = resp.get("kind")
    if expect in ("NAK", "no ACK"):
        ok = kind != "ACK" and not events
    else:
        ok = kind == "ACK" and code == expect and not resp.get("header_nonstandard")
    return {"case": name, "expected": expect, "response": kind,
            "response_header_nonstandard": bool(resp.get("header_nonstandard")),
            "response_framed": resp.get("framed"), "response_crc_ok": resp.get("crc_ok"),
            "response_decrypted": resp.get("decrypted"), "callback_code": code,
            "callback_fired": bool(events), "pass": ok,
            "raw_response": resp_bytes.decode("ascii", "replace") if resp_bytes else None}


async def _run_aio(proto: str):
    from pysiaalarm.aio import SIAClient, CommunicationsProtocol
    got = []

    async def cb(ev):
        got.append(ev)
    port = _free_port(socket.SOCK_STREAM if proto == "TCP" else socket.SOCK_DGRAM)
    cp = CommunicationsProtocol.TCP if proto == "TCP" else CommunicationsProtocol.UDP
    client = SIAClient("127.0.0.1", port, _accounts(), cb, protocol=cp)
    await client.async_start()
    rows = []
    loop = asyncio.get_running_loop()
    try:
        for name, factory, key, expect in cases():
            got.clear()
            data = factory()
            resp = None
            if proto == "TCP":
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(data)
                await w.drain()
                try:
                    resp = await asyncio.wait_for(r.read(1024), 1.5)
                except asyncio.TimeoutError:
                    resp = None
                w.close()
            else:
                fut = loop.create_future()

                class P(asyncio.DatagramProtocol):
                    def datagram_received(self, d, a):
                        if not fut.done():
                            fut.set_result(d)
                tr, _ = await loop.create_datagram_endpoint(P, remote_addr=("127.0.0.1", port))
                tr.sendto(data)
                try:
                    resp = await asyncio.wait_for(fut, 1.5)
                except asyncio.TimeoutError:
                    resp = None
                tr.close()
            await asyncio.sleep(0.05)
            rows.append(_row(name, expect, resp, list(got), key))
        # two frames in one TCP write (a panel flushing a queue)
        if proto == "TCP":
            got.clear()
            f1 = cases()[0][1]()
            f2 = cases()[3][1]()
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f1 + f2)
            await w.drain()
            try:
                resp = await asyncio.wait_for(r.read(1024), 1.5)
            except asyncio.TimeoutError:
                resp = None
            await asyncio.sleep(0.1)
            w.close()
            rows.append({"case": "two_frames_one_tcp_write", "expected": "2 ACK",
                         "response": (resp or b"").decode("ascii", "replace"),
                         "callback_fired": len(got), "callback_codes": [e.code for e in got],
                         "pass": len(got) == 2})
    finally:
        await client.async_stop()
    return rows


def _run_sync_tcp():
    from pysiaalarm import SIAClient
    got = []
    port = _free_port()
    client = SIAClient("127.0.0.1", port, _accounts(), got.append)
    client.start()
    time.sleep(0.1)
    rows = []
    try:
        for name, factory, key, expect in cases():
            got.clear()
            s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            s.sendall(factory())
            try:
                resp = s.recv(1024)
            except socket.timeout:
                resp = None
            s.close()
            time.sleep(0.05)
            rows.append(_row(name, expect, resp, list(got), key))
    finally:
        client.stop()
    return rows


def run_matrix() -> dict:
    """Return {"pysiaalarm_version", "transports": {name: [rows]}} or {"error": ...}."""
    try:
        import pysiaalarm
    except ImportError as e:
        return {"error": f"pysiaalarm not importable: {e}"}
    out = {"pysiaalarm_version": pysiaalarm.__version__, "transports": {}}
    out["transports"]["aio_TCP"] = asyncio.run(_run_aio("TCP"))
    out["transports"]["aio_UDP"] = asyncio.run(_run_aio("UDP"))
    result = {}
    t = threading.Thread(target=lambda: result.update(rows=_run_sync_tcp()))
    t.start(); t.join(30)
    out["transports"]["sync_TCP"] = result.get("rows", [{"error": "sync run did not finish"}])
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run_matrix(), indent=1, default=str))
