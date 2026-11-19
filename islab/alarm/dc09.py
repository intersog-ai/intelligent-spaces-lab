"""SIA DC-09 alarm transmission: encoder, decoder, ACK/NAK frames.

Carries Contact ID ("ADM-CID"), SIA DC-03 ("SIA-DCS") and link-test ("NULL") events,
plain or AES-CBC encrypted ('*' prefix on the token).

Frame layout (as implemented here; see experiments/alarm_path/notes.md for the sources,
the DC-09 standard text itself is paywalled and was NOT consulted):

    <LF> CRC(4 hex) 0LLL(length, 4 hex) "ID" seq(4 dec) [Rrcvr] Lline #acct [data] <CR>

    plain data     : [#acct|<event>]<xdata>*_HH:MM:SS,MM-DD-YYYY
    encrypted data : [<HEX(AES-CBC(pad|#acct|<event>]<xdata>*_timestamp))>
    ADM-CID event  : QEEE GG ZZZ   (qualifier 1|3|6, event code, group/partition, zone/user)
    SIA-DCS event  : Nri<area>/<code><zone>
    NULL           : []_timestamp  (supervision / link test)

CRC is CRC-16/ARC (poly 0x8005 reflected = 0xA001, init 0) over the bytes between the
length field and the CR. Length is the byte count of that same span, 4 hex digits.
No wall clock is read here: the caller passes a datetime (the twin derives it from sim.now).
"""
from __future__ import annotations

import re
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ADM_CID = "ADM-CID"
SIA_DCS = "SIA-DCS"
NULL = "NULL"
MESSAGE_TYPES = (ADM_CID, SIA_DCS, NULL)

IV_ZERO = bytes(16)
TS_FMT = "%H:%M:%S,%m-%d-%Y"
# Fixed epoch so twin timestamps are deterministic: sim time 0 == this instant (UTC).
SIM_EPOCH = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)

# Twin event kind -> (Contact ID event code, SIA DC-03 alarm code, SIA restore code)
EVENT_CODES = {
    "smoke":     ("111", "FA", "FH"),
    "fire":      ("110", "FA", "FH"),
    "leak":      ("154", "WA", "WH"),   # CID 154 "water leakage"; see notes: pysiaalarm lacks it
    "intrusion": ("130", "BA", "BH"),
    "perimeter": ("131", "BA", "BH"),
    "interior":  ("132", "BA", "BH"),
    "panic":     ("120", "PA", "PH"),
    "test":      ("602", "RP", "RP"),   # periodic test report
}


class DC09Error(ValueError):
    pass


def crc16(data: bytes) -> int:
    """CRC-16/ARC (reflected poly 0xA001, init 0x0000)."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def sim_timestamp(now_s: float) -> datetime:
    return SIM_EPOCH + timedelta(seconds=now_s)


def fmt_ts(ts: datetime) -> str:
    return "_" + ts.astimezone(timezone.utc).strftime(TS_FMT)


def _aes(key: bytes):
    if len(key) not in (16, 24, 32):
        raise DC09Error("AES key must be 16, 24 or 32 bytes")
    return Cipher(algorithms.AES(key), modes.CBC(IV_ZERO))


def encrypt_block(plain: str, key: bytes, rng: random.Random | None = None) -> str:
    """Prepend pad + '|' so the total is a multiple of 16, AES-CBC (IV=0), return upper hex.
    Pad characters are printable ASCII excluding '|', '[' and ']'."""
    rng = rng or random.Random(0)
    body = "|" + plain
    padlen = (-len(body)) % 16
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    pad = "".join(rng.choice(alphabet) for _ in range(padlen))
    data = (pad + body).encode("ascii")
    enc = _aes(key).encryptor()
    return (enc.update(data) + enc.finalize()).hex().upper()


def decrypt_block(hexdata: str, key: bytes, strict: bool = True) -> str:
    try:
        raw = bytes.fromhex(hexdata)
    except ValueError as e:
        raise DC09Error("encrypted block is not hex") from e
    if len(raw) % 16:
        raise DC09Error("encrypted block not a multiple of 16 bytes")
    dec = _aes(key).decryptor()
    text = (dec.update(raw) + dec.finalize()).decode("ascii", "replace")
    if "|" not in text:
        # pysiaalarm pads its encrypted ACKs with '0' and no '|': accept that when lenient
        if not strict and "]" in text:
            return text[text.index("]"):]
        raise DC09Error("decrypted block has no pad separator (wrong key?)")
    return text.split("|", 1)[1]


@dataclass
class DC09Message:
    mtype: str                      # ADM-CID | SIA-DCS | NULL
    account: str                    # 3..16 hex chars
    sequence: int = 1               # 1..9999; retransmissions reuse it
    receiver: str | None = "0"      # hex, None = omit R field
    line: str = "0"                 # hex
    event: str = ""                 # "1111 01 001" or "Nri1/FA001" ('' for NULL)
    xdata: list[str] = field(default_factory=list)   # e.g. ["X123.45W67.89"] -> [X...]
    timestamp: datetime | None = None
    encrypted: bool = False

    def validate(self):
        if self.mtype not in MESSAGE_TYPES:
            raise DC09Error(f"unknown message type {self.mtype}")
        if not re.fullmatch(r"[0-9A-Fa-f]{3,16}", self.account):
            raise DC09Error("account must be 3-16 hex chars")
        if not 0 <= self.sequence <= 9999:
            raise DC09Error("sequence must be 0..9999")
        for f in (self.line, self.receiver or "0"):
            if not re.fullmatch(r"[0-9A-Fa-f]{1,6}", f):
                raise DC09Error("receiver/line must be 1-6 hex chars")
        if self.mtype == ADM_CID and not re.fullmatch(r"[136]\d{3} \d{2} \d{3}", self.event):
            raise DC09Error("ADM-CID event must look like 'QEEE GG ZZZ'")
        if self.encrypted and self.timestamp is None:
            raise DC09Error("encrypted messages must carry a timestamp")


def adm_cid_event(kind: str, zone: int, partition: int = 1, restore: bool = False) -> str:
    cid = EVENT_CODES[kind][0]
    return f"{3 if restore else 1}{cid} {partition:02d} {zone:03d}"


def sia_dcs_event(kind: str, zone: int, area: int = 1, restore: bool = False) -> str:
    code = EVENT_CODES[kind][2 if restore else 1]
    return f"Nri{area}/{code}{zone:03d}"


def _header(msg: DC09Message, token: str) -> str:
    h = f'"{"*" if msg.encrypted else ""}{token}"{msg.sequence:04d}'
    if msg.receiver is not None:
        h += f"R{msg.receiver.upper()}"
    return h + f"L{msg.line.upper()}#{msg.account.upper()}["


def frame(body: str) -> bytes:
    b = body.encode("ascii")
    if len(b) > 0xFFF:
        raise DC09Error("message too long")
    return b"\n" + f"{crc16(b):04X}0{len(b):03X}".encode() + b + b"\r"


def encode(msg: DC09Message, key: bytes | None = None, rng: random.Random | None = None) -> bytes:
    msg.validate()
    acct = msg.account.upper()
    inner = (f"#{acct}|{msg.event}]" if msg.mtype != NULL else "]")
    inner += "".join(f"[{x}]" for x in msg.xdata)
    if msg.timestamp is not None:
        inner += fmt_ts(msg.timestamp)
    if msg.encrypted:
        if key is None:
            raise DC09Error("key required for encrypted message")
        data = encrypt_block(inner, key, rng)
    else:
        data = inner
    return frame(_header(msg, msg.mtype) + data)


_FRAME = re.compile(
    r'(?P<crc>[0-9A-F]{4})(?P<len>[0-9A-F]{4})(?P<body>"(?P<enc>\*)?(?P<id>[A-Z\-]+)"'
    r'(?P<seq>\d{4})(?:R(?P<rcv>[0-9A-F]{1,6}))?L(?P<line>[0-9A-F]{1,6})'
    r'(?:#(?P<acct>[0-9A-F]{3,16}))?(?:A0)?\[(?P<rest>.*))$', re.S)

_INNER = re.compile(r'(?:#(?P<acct>[0-9A-F]{3,16})\|)?(?P<event>[^\]\[]*)\]'
                    r'(?P<xdata>(?:\[[^\]]*\])*)(?:_(?P<ts>\d\d:\d\d:\d\d,\d\d-\d\d-\d{4}))?$')


@dataclass
class Decoded:
    token: str
    encrypted: bool
    sequence: int
    receiver: str | None
    line: str
    account: str | None
    event: str
    xdata: list[str]
    timestamp: datetime | None
    crc_ok: bool
    length_ok: bool


def decode(raw: bytes, key: bytes | None = None) -> Decoded:
    """Parse one frame (with or without LF/CR). Raises DC09Error on format problems."""
    s = raw.decode("ascii", "replace").strip("\n\r")
    m = _FRAME.match(s)
    if not m:
        raise DC09Error("not a DC-09 frame")
    body = m["body"].encode("ascii")
    crc_ok = int(m["crc"], 16) == crc16(body)
    length_ok = int(m["len"], 16) == len(body)
    rest = m["rest"]
    enc = bool(m["enc"])
    if enc:
        if key is None:
            raise DC09Error("encrypted frame but no key")
        rest = decrypt_block(rest, key)
    im = _INNER.match(rest)
    if not im:
        raise DC09Error(f"cannot parse data block {rest!r}")
    ts = None
    if im["ts"]:
        ts = datetime.strptime(im["ts"], TS_FMT).replace(tzinfo=timezone.utc)
    xd = re.findall(r"\[([^\]]*)\]", im["xdata"] or "")
    return Decoded(token=m["id"], encrypted=enc, sequence=int(m["seq"]), receiver=m["rcv"],
                   line=m["line"], account=m["acct"] or im["acct"], event=im["event"],
                   xdata=xd, timestamp=ts, crc_ok=crc_ok, length_ok=length_ok)


def build_ack(d: Decoded, key: bytes | None = None, now: datetime | None = None,
              rng: random.Random | None = None) -> bytes:
    """Receiver-side ACK echoing sequence, receiver, line and account."""
    rcv = f"R{d.receiver}" if d.receiver is not None else ""
    head = f'"{"*" if d.encrypted else ""}ACK"{d.sequence:04d}{rcv}L{d.line}#{d.account}['
    if d.encrypted:
        if key is None or now is None:
            raise DC09Error("encrypted ACK needs key and timestamp")
        return frame(head + encrypt_block("]" + fmt_ts(now), key, rng))
    return frame(head + "]")


def build_nak(now: datetime) -> bytes:
    return frame('"NAK"0000R0L0A0[]' + fmt_ts(now))


def parse_response(raw: bytes, key: bytes | None = None) -> dict:
    """Parse an ACK/NAK/DUH/RSP frame from a receiver. Tolerant of pysiaalarm quirks
    (it may answer a bare '"NAK"' without framing, or '\\n\\r' for a CRC failure)."""
    s = raw.decode("ascii", "replace").strip("\n\r")
    if not s:
        return {"kind": "EMPTY"}
    if s.startswith('"') and s.endswith('"') and s.count('"') == 2:
        return {"kind": s.strip('"').lstrip("*"), "framed": False}
    m = _FRAME.match(s)
    if not m:
        # Framed but with a non-standard header (pysiaalarm's NAKEvent writes "L0R0", and an
        # ACK to a frame without R field contains the literal text "None").
        loose = re.match(r'(?P<crc>[0-9A-F]{4})(?P<len>[0-9A-F]{4})(?P<body>"\*?(?P<id>[A-Z]+)".*)$', s)
        if loose:
            body = loose["body"].encode("ascii")
            return {"kind": loose["id"], "framed": True, "header_nonstandard": True,
                    "crc_ok": int(loose["crc"], 16) == crc16(body),
                    "length_ok": int(loose["len"], 16) == len(body), "raw": s}
        return {"kind": "UNPARSEABLE", "raw": s}
    body = m["body"].encode("ascii")
    out = {"kind": m["id"], "framed": True, "encrypted": bool(m["enc"]),
           "sequence": int(m["seq"]), "account": m["acct"], "receiver": m["rcv"],
           "line": m["line"], "crc_ok": int(m["crc"], 16) == crc16(body),
           "length_ok": int(m["len"], 16) == len(body)}
    if m["enc"] and key is not None:
        try:
            out["decrypted"] = decrypt_block(m["rest"], key, strict=False)
        except DC09Error as e:
            out["decrypt_error"] = str(e)
    return out
