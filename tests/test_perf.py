"""Cadence and memory over time: ten fakes at 10 Hz, p99 cadence inside one interval, flat RSS.

`SOAK_MINUTES` sets the run time; the default is a few seconds so `-m perf` stays quick, and the
nightly Soak workflow runs an hour.
"""

import os
import time

import psutil
import pytest

from tests.conftest import (
    RecordingEmitter,
    ScriptedLink,
    instrument_config,
    opener_for,
    wait_until,
)
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker

SOAK_S = float(os.environ.get("SOAK_MINUTES", "0.1")) * 60
INSTRUMENTS = 10
RATE_HZ = 10
INTERVAL_NS = 1_000_000_000 // RATE_HZ


@pytest.mark.perf
def test_ten_instruments_hold_cadence_and_memory() -> None:
    profile: Profile | None = by_id(shipped(), "rigol-dp800")
    assert profile is not None
    emitter = RecordingEmitter()
    workers = []
    for i in range(INSTRUMENTS):
        fake = FakeInstrument(profile, model="DP832")
        cfg = instrument_config(name=f"psu{i}", profile=profile.id, rate_hz=RATE_HZ)
        worker = InstrumentWorker(
            cfg, [profile], emitter, open=opener_for(ScriptedLink(fake.handle))
        )
        worker.start()
        workers.append(worker)
    process = psutil.Process()
    try:
        assert wait_until(lambda: all(w.state == "open" for w in workers), timeout_s=30)
        time.sleep(2)
        rss_start = process.memory_info().rss
        emitter.rows.clear()
        time.sleep(SOAK_S)
        rss_end = process.memory_info().rss
    finally:
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(5)
    for i in range(INSTRUMENTS):
        times = [
            r[3] for r in emitter.rows if r[0] == f"psu{i}" and r[1] == 1 and "voltage" in r[2]
        ]
        assert len(times) >= SOAK_S * RATE_HZ * 0.9, f"psu{i} delivered {len(times)} readings"
        gaps = sorted(b - a for a, b in zip(times, times[1:], strict=False))
        p99 = gaps[int(len(gaps) * 0.99) - 1]
        assert p99 < 2 * INTERVAL_NS, f"psu{i} p99 gap {p99 / 1e6:.1f} ms"
    rows = len(emitter.rows)
    emitter.rows.clear()
    growth_mb = (rss_end - rss_start) / 1e6
    assert growth_mb < 20 + rows * 0.001, f"RSS grew {growth_mb:.1f} MB over {SOAK_S:.0f} s"
