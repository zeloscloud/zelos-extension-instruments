"""What the fake's system functions need beside the profile: pictures for a screenshot, and a scope
or data-acquisition unit's settings as JSON for a saved state."""

import struct
import zlib
from typing import TYPE_CHECKING, Any

from .fake_scope import FakeScope

if TYPE_CHECKING:
    from .fake_daq import FakeDaq


def block(payload: bytes) -> bytes:
    """An IEEE 488.2 definite-length block."""
    length = str(len(payload))
    return f"#{len(length)}{length}".encode() + payload


def png() -> bytes:
    """A 2 by 2 PNG, the smallest picture a screenshot can be."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x20\x80\xc0" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def bmp() -> bytes:
    """A 1 by 1 bitmap of 24 bits a pixel, with its file size in the header."""
    pixel = b"\xc0\x80\x20\x00"
    info = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, len(pixel), 2835, 2835, 0, 0)
    offset = 14 + len(info)
    return b"BM" + struct.pack("<IHHI", offset + len(pixel), 0, 0, offset) + info + pixel


def scope_state(scope: FakeScope) -> dict[str, Any]:
    return {
        "per_channel": {str(ch): v for ch, v in scope.per_channel.items()},
        "shared": scope.shared,
        "slots": {str(n): list(v) for n, v in scope.slots.items()},
    }


def restore_scope(scope: FakeScope, data: dict[str, Any]) -> None:
    scope.per_channel = {int(ch): v for ch, v in data["per_channel"].items()}
    scope.shared = data["shared"]
    scope.slots = {int(n): (bool(v[0]), str(v[1]), str(v[2])) for n, v in data["slots"].items()}


def daq_state(unit: "FakeDaq") -> dict[str, Any]:
    return {
        "functions": {str(ch): v for ch, v in unit.functions.items()},
        "thermocouples": {str(ch): v for ch, v in unit.thermocouples.items()},
        "nplc": {str(ch): v for ch, v in unit.nplc.items()},
        "scan_list": list(unit.scan_list),
        "closed": sorted(unit.closed),
    }


def restore_daq(unit: "FakeDaq", data: dict[str, Any]) -> None:
    unit.functions = {int(ch): v for ch, v in data["functions"].items()}
    unit.thermocouples = {int(ch): v for ch, v in data["thermocouples"].items()}
    unit.nplc = {int(ch): v for ch, v in data["nplc"].items()}
    unit.scan_list = tuple(data["scan_list"])
    unit.closed = set(data["closed"])
