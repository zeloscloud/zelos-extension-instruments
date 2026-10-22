"""Waveform blocks: the IEEE 488.2 definite-length block and the descriptors inside it.

A scope sends samples as `#<n><length><bytes>`. The descriptor that scales them is vendor
specific; `wavedesc` is the LeCroy-style block Siglent SDS scopes return from the preamble query,
and `linear` is six numbers the profile says where to find.
"""

import math
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from .profile_scope import ByteOrder, Encoding


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


def samples(desc: WaveDesc, payload: bytes) -> npt.NDArray[np.float64]:
    """The data block's samples in the channel's unit, probe factor applied."""
    size = desc.bytes_per_sample
    codes = np.frombuffer(payload, dtype=np.dtype(desc.sample_format), count=len(payload) // size)
    gain = desc.vertical_gain / desc.code_per_div * desc.probe
    offset = desc.vertical_offset * desc.probe
    return codes * gain - offset


def sample_offsets_ns(
    desc: WaveDesc, count: int, timebase_s: float, grid: int
) -> npt.NDArray[np.int64]:
    """Nanoseconds from the trigger for each of `count` transferred samples.

    The first sample sits at the left of the screen: the trigger delay minus half the sweep.
    Transferred samples are `step` acquisition samples apart.
    """
    first = desc.horizontal_offset - timebase_s * grid / 2
    pitch = desc.horizontal_interval * desc.step
    return np.rint((first + np.arange(count) * pitch) * 1e9).astype(np.int64)


@dataclass(frozen=True, slots=True)
class LinearScale:
    """The six numbers that place linear codes: V = (code - y_ref) * y_mult + y_zero and
    t = (i - x_ref) * x_inc + x_zero, in seconds from the trigger."""

    x_inc: float
    x_zero: float
    x_ref: float
    y_mult: float
    y_zero: float
    y_ref: float


def linear_codes(payload: bytes, encoding: Encoding, byte_order: ByteOrder) -> npt.NDArray[Any]:
    """The integer codes of a data block; a trailing partial sample is dropped."""
    order = ">" if byte_order == "big" else "<"
    dtype = np.dtype(f"{order}{encoding}")
    return np.frombuffer(payload, dtype=dtype, count=len(payload) // dtype.itemsize)


def linear_samples(scale: LinearScale, codes: npt.NDArray[Any]) -> npt.NDArray[np.float64]:
    """The codes in the channel's unit."""
    return (codes.astype(np.float64) - scale.y_ref) * scale.y_mult + scale.y_zero


def linear_offsets_ns(scale: LinearScale, count: int) -> npt.NDArray[np.int64]:
    """Nanoseconds from the trigger for each of `count` samples."""
    times = (np.arange(count) - scale.x_ref) * scale.x_inc + scale.x_zero
    return np.rint(times * 1e9).astype(np.int64)
