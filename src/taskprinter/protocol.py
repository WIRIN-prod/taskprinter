"""SC03h / iPrint "51 78" BLE printer protocol.

Reference: iPrint_SC03h_Protocol_Documentation.md (mini-print-studio).

Packet layout:  51 78 <cmd> 00 <len lo> <len hi> <payload...> <crc8(payload)> FF
CRC-8/ATM (poly 0x07, init 0x00) over the payload only.

Image rows are handed around as packed bytes: MSB = leftmost pixel, bit set = black,
the layout the protocol doc describes for A2. Real SC03h hardware, however, prints
that layout with every byte bit-reversed, so the default A2 encoding is "raw-lsb"
(bit 0 = leftmost pixel).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Sequence

SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
ALT_WRITE_SUFFIX = "ae3b"


class Cmd(IntEnum):
    RETRACT = 0xA0
    FEED = 0xA1
    DRAW_RAW = 0xA2
    DEVICE_STATE = 0xA3
    QUALITY = 0xA4
    DEVICE_INFO = 0xA8
    FLOW = 0xAE
    ENERGY = 0xAF
    SPEED = 0xBD
    DRAW_MODE = 0xBE
    DRAW_RLE = 0xBF
    # 0xA6 (lattice) is deliberately absent: the SC03h docs say never to send it.


def _crc_table() -> list[int]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return table


_CRC = _crc_table()
_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC[crc ^ byte]
    return crc


def packet(cmd: int, payload: bytes = b"") -> bytes:
    length = len(payload)
    if length > 0xFFFF:
        raise ValueError("payload too long")
    header = bytes((0x51, 0x78, cmd, 0x00, length & 0xFF, length >> 8))
    return header + payload + bytes((crc8(payload), 0xFF))


def u16(value: int) -> bytes:
    value = max(0, min(0xFFFF, int(value)))
    return bytes((value & 0xFF, value >> 8))


def encode_rle(row: bytes, width: int) -> bytes:
    """Run-length encode one row: each byte is (color << 7) | run (1..127).

    Used by some cat-printer clones via 0xBF; the SC03h itself documents only A2.
    """
    out = bytearray()
    current = (row[0] >> 7) & 1
    run = 0
    for x in range(width):
        bit = (row[x >> 3] >> (7 - (x & 7))) & 1
        if bit == current and run < 127:
            run += 1
        else:
            out.append((current << 7) | run)
            current, run = bit, 1
    out.append((current << 7) | run)
    return bytes(out)


ENCODINGS = ("raw", "raw-lsb", "rle")


def row_packet(row: bytes, width: int, encoding: str = "raw") -> bytes:
    if encoding == "rle":
        return packet(Cmd.DRAW_RLE, encode_rle(row, width))
    if encoding == "raw-lsb":  # SC03h hardware: bit 0 = leftmost pixel
        return packet(Cmd.DRAW_RAW, row.translate(_REVERSE))
    return packet(Cmd.DRAW_RAW, bytes(row))  # MSB-first, as the doc describes


FEED_MODES = ("bd", "a1")


@dataclass
class PrintSettings:
    width: int = 384
    energy: int = 17520  # 0x4470
    quality: int = 0x33
    speed: int = 0x23
    draw_mode: int = 0x00  # 1-bit image mode
    feed: int = 90  # a1: dots; bd: number of 0xBD packets
    feed_mode: str = "a1"
    encoding: str = "raw-lsb"
    retract: int = 0  # dots to pull back before printing (undo the last feed)
    backlash: int = 0  # extra pull-back, then printed as blank rows to take up gear slack


def _dot_packets(cmd: int, amount: int) -> bytes:
    """Paper motion in dots, split into packets of at most 100 dots."""
    out = bytearray()
    while amount > 0:
        step = min(100, amount)
        out += packet(cmd, u16(step))
        amount -= step
    return bytes(out)


def retract_packets(amount: int) -> bytes:
    """Pull paper back towards the head with `0xA0 <u16 dots>`."""
    return _dot_packets(Cmd.RETRACT, amount)


def feed_packets(amount: int, mode: str = "bd", speed: int = 0x23) -> bytes:
    """Paper feed to clear the tear bar.

    `0xA1 <u16 dots>` (<= 100 dots per packet) is what actually moves paper on the
    SC03h. The docs recommend repeated `0xBD 23` packets, but on real hardware those
    do not advance the paper; that mode is kept for other clones.
    """
    if mode == "a1":
        return _dot_packets(Cmd.FEED, amount)
    return packet(Cmd.SPEED, bytes((speed & 0xFF,))) * max(0, amount)


def build_job(rows: Sequence[bytes], settings: PrintSettings) -> bytes:
    """Build the full byte stream for one 1-bit print job."""
    job = bytearray()
    job += packet(Cmd.DEVICE_STATE, b"\x00")  # wake
    # Bring the last feed back under the head. Reversing the motor leaves some gear
    # slack, so pull back `backlash` dots further and spend them on blank rows: the
    # slack is taken up on blank paper and the content starts exactly where it should.
    slack = settings.backlash if settings.retract else 0
    job += retract_packets(settings.retract + slack)
    job += packet(Cmd.QUALITY, bytes((settings.quality & 0xFF,)))
    job += packet(Cmd.ENERGY, u16(settings.energy))
    job += packet(Cmd.DRAW_MODE, bytes((settings.draw_mode & 0xFF,)))
    job += packet(Cmd.SPEED, bytes((settings.speed & 0xFF,)))
    blank = bytes(settings.width // 8)
    for _ in range(slack):
        job += row_packet(blank, settings.width, settings.encoding)
    for row in rows:
        job += row_packet(row, settings.width, settings.encoding)
    job += feed_packets(settings.feed, settings.feed_mode, settings.speed)
    return bytes(job)


def iter_packets(data: bytes) -> Iterable[tuple[int, bytes]]:
    """Split a notification buffer into (cmd, payload) pairs, skipping junk."""
    i = 0
    while i + 8 <= len(data):
        if data[i] != 0x51 or data[i + 1] != 0x78:
            i += 1
            continue
        cmd = data[i + 2]
        length = data[i + 4] | (data[i + 5] << 8)
        end = i + 6 + length
        if end + 2 > len(data):
            break
        yield cmd, bytes(data[i + 6:end])
        i = end + 2


def decode_status(payload: bytes) -> dict:
    """Best-effort status heuristics from the docs (firmware dependent)."""
    status: dict = {}
    if not payload:
        return status
    status["ready"] = payload[0] == 0x00
    status["paper_out"] = bool(payload[0] & 0x04)
    if len(payload) > 1 and 1 <= payload[1] <= 100:
        status["battery"] = payload[1]
    return status


def is_flow_pause(cmd: int, payload: bytes) -> bool | None:
    """Some clones send 0xAE 10 (pause) / 0xAE 00 (resume); returns None otherwise."""
    if cmd != Cmd.FLOW or not payload:
        return None
    return payload[0] == 0x10
