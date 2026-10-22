"""Waveform blocks and descriptors, decoded from a scope's recorded reply."""

import dataclasses
import json
import struct
from pathlib import Path

import pytest

from zelos_extension_instruments import waveform

FIXTURES = Path(__file__).parent / "fixtures"


def test_the_recorded_sds824x_descriptor_places_the_trigger_at_zero() -> None:
    """A preamble and a whole data block recorded from the SDS824X HD: edge trigger on channel
    1, rising, at 0.4 V, delay 1 ms, 1000 samples 2500 acquisitions apart. The formulas must put
    the first sample at the left edge of the screen and the trigger at t = 0, where the recorded
    signal must cross the trigger level on its way up."""
    meta = json.loads((FIXTURES / "sds824x_waveform.json").read_text(encoding="utf-8"))
    preamble = waveform.block_payload((FIXTURES / "sds824x_preamble.bin").read_bytes())
    desc = waveform.decode_wavedesc(preamble)
    timebase = float(meta["timebase"])
    delay = float(meta["delay"])
    # The descriptor describes the whole acquisition; only the data block says how many
    # samples the transfer carried.
    assert desc.count == 2_500_000 and desc.array_bytes == 5_000_000
    assert desc.bytes_per_sample == 2 and desc.step == 2500
    assert desc.horizontal_interval == pytest.approx(1 / float(meta["sample_rate"]))
    assert desc.horizontal_offset == pytest.approx(delay)
    assert desc.probe == pytest.approx(float(meta["probe"]))
    assert desc.vertical_gain * desc.probe == pytest.approx(float(meta["scale"]))
    raw = (FIXTURES / "sds824x_data.bin").read_bytes()
    head = waveform.block_header(raw)
    # The guide's "0A 0A" trailer follows the block: two bytes the block reader drops.
    assert head is not None and head[1] == 2000 and meta["data_len"] == head[0] + head[1] + 2
    payload = waveform.block_payload(raw)
    values = waveform.samples(desc, payload)
    codes = struct.unpack(f"<{len(payload) // 2}h", payload)
    assert len(values) == len(codes) == 1000
    times = (waveform.sample_offsets_ns(desc, len(values), timebase, 10) / 1e9).tolist()
    pitch = desc.horizontal_interval * desc.step
    assert pitch == pytest.approx(5e-6)
    assert times[0] == pytest.approx(delay - timebase * 5)
    assert times[-1] - times[0] == pytest.approx(pitch * 999)
    assert pitch * 1000 == pytest.approx(timebase * 10, rel=1e-6)
    # A 12-bit code left-aligned in 16 bits: 7680 codes per division at 0.5 V/div with a 10x
    # probe, so every sample sits within the screen's eight divisions.
    assert all(abs(v) <= 4.5 for v in values)
    assert values[0] == pytest.approx(
        codes[0] * desc.vertical_gain / desc.code_per_div * desc.probe
        - desc.vertical_offset * desc.probe
    )
    # The sign of the delay: the sample at t = 0 is the trigger, where the rising edge crosses
    # the trigger level. A delay added instead of subtracted would put t = 0 two edges away.
    level = float(meta["trigger_level"])
    at_trigger = round(-times[0] / pitch)
    assert abs(times[at_trigger]) < pitch / 10
    assert values[at_trigger - 1] < level <= values[at_trigger + 1]
    assert values[at_trigger] == pytest.approx(level, abs=0.05)
    crossings = [i for i in range(1, len(values)) if values[i - 1] < level <= values[i]]
    assert at_trigger in crossings


def recorded_descriptor() -> bytearray:
    return bytearray(waveform.block_payload((FIXTURES / "sds824x_preamble.bin").read_bytes()))


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"WAVEDESC", "expected a #-block"),
        (b"#", None),
        (b"#x", "expected a definite-length block"),
        (b"#0", "expected a definite-length block"),
        (b"#3", None),
        (b"#30", None),
        (b"#2ab", "expected a block length"),
    ],
)
def test_a_block_header_is_incomplete_or_refused(data: bytes, complaint: str | None) -> None:
    if complaint is None:
        assert waveform.block_header(data) is None
    else:
        with pytest.raises(waveform.BlockError, match=complaint):
            waveform.block_header(data)


def test_an_empty_reply_has_no_header_yet() -> None:
    assert waveform.block_header(b"") is None


def test_a_block_payload_needs_a_whole_header_and_all_its_bytes() -> None:
    assert waveform.block_payload(b"#15hello") == b"hello"
    with pytest.raises(waveform.BlockError, match="incomplete block header"):
        waveform.block_payload(b"#3")
    with pytest.raises(waveform.BlockError, match="the block holds 3 of 5 bytes"):
        waveform.block_payload(b"#15hel")


def test_a_descriptor_is_found_wherever_it_starts_in_the_payload() -> None:
    desc = recorded_descriptor()
    assert waveform.decode_wavedesc(b"junk" + bytes(desc)) == waveform.decode_wavedesc(bytes(desc))


@pytest.mark.parametrize("payload", [b"not a descriptor" * 30, b"WAVEDESC" + bytes(100)])
def test_bytes_that_are_not_a_descriptor_are_refused(payload: bytes) -> None:
    with pytest.raises(waveform.BlockError, match="not a WAVEDESC descriptor"):
        waveform.decode_wavedesc(payload)


def test_a_descriptor_cut_short_is_refused() -> None:
    with pytest.raises(waveform.BlockError, match="not a WAVEDESC descriptor"):
        waveform.decode_wavedesc(bytes(recorded_descriptor()[:-1]))


@pytest.mark.parametrize("code_per_div", [0.0, float("nan"), float("inf")])
def test_a_descriptor_without_a_usable_code_scale_is_refused(code_per_div: float) -> None:
    desc = recorded_descriptor()
    struct.pack_into("<f", desc, 164, code_per_div)
    with pytest.raises(waveform.BlockError, match="no code_per_div"):
        waveform.decode_wavedesc(bytes(desc))


@pytest.mark.parametrize("probe", [0.0, -10.0, float("nan"), float("inf")])
def test_a_descriptor_with_a_bad_probe_factor_is_refused(probe: float) -> None:
    desc = recorded_descriptor()
    struct.pack_into("<f", desc, 328, probe)
    with pytest.raises(waveform.BlockError, match="probe factor"):
        waveform.decode_wavedesc(bytes(desc))


def test_big_endian_words_are_decoded_with_the_descriptors_byte_order() -> None:
    desc = recorded_descriptor()
    little = waveform.decode_wavedesc(bytes(desc))
    struct.pack_into("<h", desc, 34, 1)
    for offset, fmt in ((60, "i"), (116, "i"), (132, "i"), (136, "i"), (156, "f"), (160, "f")):
        value = struct.unpack_from(f"<{fmt}", desc, offset)[0]
        struct.pack_into(f">{fmt}", desc, offset, value)
    struct.pack_into(">f", desc, 164, little.code_per_div)
    struct.pack_into(">h", desc, 172, little.adc_bits)
    struct.pack_into(">f", desc, 176, little.horizontal_interval)
    struct.pack_into(">d", desc, 180, little.horizontal_offset)
    struct.pack_into(">f", desc, 328, little.probe)
    big = waveform.decode_wavedesc(bytes(desc))
    assert big == dataclasses.replace(little, comm_order=1)
    assert big.sample_format == ">h"


def test_a_byte_wide_descriptor_reads_one_byte_per_sample() -> None:
    desc = recorded_descriptor()
    struct.pack_into("<h", desc, 32, 0)
    decoded = waveform.decode_wavedesc(bytes(desc))
    assert decoded.sample_format == "b" and decoded.bytes_per_sample == 1
    values = waveform.samples(decoded, bytes([0, 64, 192]))
    gain = decoded.vertical_gain / decoded.code_per_div * decoded.probe
    offset = decoded.vertical_offset * decoded.probe
    assert values.tolist() == pytest.approx([-offset, 64 * gain - offset, -64 * gain - offset])
