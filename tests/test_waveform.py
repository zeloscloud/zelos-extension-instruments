"""Waveform blocks and descriptors, decoded from a scope's recorded reply."""

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
