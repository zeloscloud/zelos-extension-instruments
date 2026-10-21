"""Waveform blocks: the IEEE 488.2 definite-length block and the descriptors inside it.

A scope sends samples as `#<n><length><bytes>`. The descriptor that scales them is vendor
specific; `wavedesc` is the LeCroy-style block Siglent SDS scopes return from the preamble query.
"""

import math
import struct
from dataclasses import dataclass


class BlockError(ValueError):
    """A reply that is not the block it should be."""


def block_header(data: bytes) -> tuple[int, int] | None:
    """(payload start, payload length) of a definite-length block, or None until the header is
    complete. Raises BlockError when the bytes are not a block header."""
    if not data:
        return None
    if data[0:1] != b"#":
        raise BlockError(f"expected a #-block, got {data[:12]!r}")
    if len(data) < 2:
        return None
    if not data[1:2].isdigit() or data[1:2] == b"0":
        raise BlockError(f"expected a definite-length block, got {data[:12]!r}")
    digits = int(data[1:2])
    if len(data) < 2 + digits:
        return None
    length_text = data[2 : 2 + digits]
    if not length_text.isdigit():
        raise BlockError(f"expected a block length, got {data[:12]!r}")
    return 2 + digits, int(length_text)


def block_payload(data: bytes) -> bytes:
    """The bytes a complete block carries; raises BlockError when the block is short."""
    header = block_header(data)
    if header is None:
        raise BlockError(f"incomplete block header {data[:12]!r}")
    start, length = header
    if len(data) < start + length:
        raise BlockError(f"the block holds {len(data) - start} of {length} bytes")
    return bytes(data[start : start + length])


@dataclass(frozen=True, slots=True)
class WaveDesc:
    """What a LeCroy-style WAVEDESC descriptor says about the samples that follow it."""

    # 1 when each sample is a 16-bit word, 0 when a byte.
    comm_type: int
    # 1 when words are big-endian.
    comm_order: int
    # Samples in the whole acquisition, and bytes in the transferred array.
    count: int
    array_bytes: int
    # First point and decimation step of the transfer.
    first: int
    step: int
    # Vertical scale per division and offset, without the probe factor.
    vertical_gain: float
    vertical_offset: float
    code_per_div: float
    adc_bits: int
    # Seconds between samples, and from the trigger to the first point.
    horizontal_interval: float
    horizontal_offset: float
    probe: float

    @property
    def sample_format(self) -> str:
        order = ">" if self.comm_order else "<"
        return f"{order}h" if self.comm_type else "b"

    @property
    def bytes_per_sample(self) -> int:
        return 2 if self.comm_type else 1


_WAVEDESC_LENGTH = 346


def decode_wavedesc(payload: bytes) -> WaveDesc:
    """The descriptor fields the conversion needs, at the offsets the programming guide gives."""
    start = payload.find(b"WAVEDESC")
    if start < 0 or len(payload) - start < _WAVEDESC_LENGTH:
        raise BlockError(f"not a WAVEDESC descriptor ({len(payload)} bytes)")
    d = payload[start:]
    comm_order = struct.unpack_from("<h", d, 34)[0]
    order = ">" if comm_order else "<"

    def short(offset: int) -> int:
        return struct.unpack_from(f"{order}h", d, offset)[0]

    def long(offset: int) -> int:
        return struct.unpack_from(f"{order}i", d, offset)[0]

    def single(offset: int) -> float:
        return struct.unpack_from(f"{order}f", d, offset)[0]

    def double(offset: int) -> float:
        return struct.unpack_from(f"{order}d", d, offset)[0]

    desc = WaveDesc(
        comm_type=struct.unpack_from("<h", d, 32)[0],
        comm_order=comm_order,
        count=long(116),
        array_bytes=long(60),
        first=long(132),
        step=max(1, long(136)),
        vertical_gain=single(156),
        vertical_offset=single(160),
        code_per_div=single(164),
        adc_bits=short(172),
        horizontal_interval=single(176),
        horizontal_offset=double(180),
        probe=single(328),
    )
    if not desc.code_per_div or not math.isfinite(desc.code_per_div):
        raise BlockError("the descriptor has no code_per_div")
    if desc.probe <= 0 or not math.isfinite(desc.probe):
        raise BlockError(f"the descriptor has a probe factor of {desc.probe}")
    return desc


def samples(desc: WaveDesc, payload: bytes) -> list[float]:
    """The data block's samples in the channel's unit, probe factor applied."""
    size = desc.bytes_per_sample
    usable = len(payload) - len(payload) % size
    fmt = desc.sample_format
    codes = struct.unpack(
        f"{fmt[0] if len(fmt) > 1 else '<'}{usable // size}{fmt[-1]}", payload[:usable]
    )
    gain = desc.vertical_gain / desc.code_per_div * desc.probe
    offset = desc.vertical_offset * desc.probe
    return [code * gain - offset for code in codes]


def sample_times_s(desc: WaveDesc, count: int, timebase_s: float, grid: int) -> list[float]:
    """Seconds from the trigger for each of `count` transferred samples.

    The first sample sits at the left of the screen: the trigger delay minus half the sweep.
    Transferred samples are `step` acquisition samples apart.
    """
    first = desc.horizontal_offset - timebase_s * grid / 2
    pitch = desc.horizontal_interval * desc.step
    return [first + i * pitch for i in range(count)]
