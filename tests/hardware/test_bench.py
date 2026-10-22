"""Real instruments on the bench: identify, poll, set and read back, protection, reconnect.

`BENCH` is a JSON list of `{"profile": "<id>", "address": "<host[:port] | COMn | VISA>"}`. The
tests skip without it. Every test leaves the instrument as it found it, except the output, which
the bench job switches off at the end.
"""

import json
import os
from typing import Any

import pytest

from tests.conftest import instrument_config, wait_until
from tests.test_worker import RecordingEmitter
from zelos_extension_instruments.link import SerialAddress, TcpAddress
from zelos_extension_instruments.profile import by_id, shipped
from zelos_extension_instruments.record import parse_address
from zelos_extension_instruments.worker import InstrumentWorker

pytestmark = pytest.mark.hardware

BENCH: list[dict[str, Any]] = json.loads(os.environ.get("BENCH", "[]"))


def _config(entry: dict[str, Any]) -> Any:
    profile = by_id(shipped(), entry["profile"])
    assert profile is not None, f"no profile {entry['profile']}"
    address = parse_address(entry["address"], profile)
    if isinstance(address, TcpAddress):
        return instrument_config(host=address.host, port=address.port, profile=profile.id)
    if isinstance(address, SerialAddress):
        return instrument_config(
            connection="serial", port=address.port, baud=address.baud, profile=profile.id
        )
    return instrument_config(connection="visa", resource=address.resource, profile=profile.id)


@pytest.fixture(params=BENCH, ids=[e["profile"] for e in BENCH])
def unit(request: Any):
    emitter = RecordingEmitter()
    cfg = _config(request.param)
    worker = InstrumentWorker(cfg, shipped(), emitter)
    worker.start()
    assert wait_until(lambda: worker.state in ("open", "failed"), timeout_s=20), worker.health
    assert worker.state == "open", worker.health
    yield worker, emitter
    worker.stop()
    worker.join(5)


def test_identity_matches_the_profile(unit) -> None:
    worker, _ = unit
    assert worker.idn is not None and worker.profile is not None
    assert worker.profile.generic or worker.profile.matches(worker.idn)


def test_every_signal_is_read_within_two_seconds(unit) -> None:
    worker, emitter = unit
    fields = set(emitter.fields(worker.cfg.name)) - {"value"}
    assert wait_until(
        lambda: fields <= {k for r in emitter.rows for k in r[2]} or not fields, timeout_s=2
    ), f"not read: {fields - {k for r in emitter.rows for k in r[2]}}"
    state = worker.request("state").result(5)
    assert state["counters"]["timeouts"] == 0, state["counters"]


def test_setpoints_round_trip_at_their_current_value(unit) -> None:
    worker, _ = unit
    assert worker.profile is not None
    state = worker.request("state").result(5)
    for ch, channel in state["channels"].items():
        number = int(ch[2:])
        for name in ("voltage", "current", "power", "resistance", "frequency"):
            current = channel["values"].get(f"{name}_set")
            if current is None or name not in worker.profile.setters:
                continue
            result = worker.request("set", channel=number, **{name: current}).result(10)
            assert "error" not in result, result
            assert abs(result[name]["readback"] - current) <= 10 ** -channel["ranges"].get(
                name, {}
            ).get("digits", 3)


def test_the_error_queue_is_empty_after_a_poll_cycle(unit) -> None:
    worker, emitter = unit
    assert wait_until(lambda: len(emitter.statuses) >= 2, timeout_s=5)
    state = worker.request("state").result(5)
    assert state["counters"]["instrument_errors"] == 0, state["counters"]["last_error"]
