"""What a worker does for a request: sets with readback, reads, raw SCPI, clear, the state report
and the master-output guard."""

import base64
from collections.abc import Callable, Iterator

import pytest

from tests.conftest import (
    Dp832,
    RecordingEmitter,
    ScriptedLink,
    ask,
    start,
    start_open,
    wait_until,
)
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker


def test_set_voltage_verifies_by_readback_and_logs(running) -> None:
    worker, link, emitter, dp832 = running
    # A value the supply's three digits would round away is refused, never sent rounded.
    assert ask(worker, "set", channel=2, voltage=12.3456) == {
        "error": "voltage 12.3456 is finer than the instrument's 3 decimals; "
        "it would be sent as 12.346"
    }
    result = ask(worker, "set", channel=2, voltage=12.346)
    assert result == {"voltage": {"set": 12.346, "readback": 12.346}}
    assert dp832.volt[2] == 12.346
    assert ":SOUR2:VOLT 12.346" in link.commands
    assert ("info", "set ch2 voltage 12.346", "action") in emitter.notes
    own = [r for r in emitter.rows if r[1] == 2 and r[2] == {"voltage_set": 12.346}]
    assert own, "the setpoint lands in the trace at once"


def test_set_several_values_in_one_request(running) -> None:
    worker, link, emitter, dp832 = running
    result = ask(worker, "set", channel=1, voltage=6.0, current=0.25)
    assert result["voltage"]["readback"] == 6.0 and result["current"]["readback"] == 0.25
    assert dp832.volt[1] == 6.0 and dp832.curr[1] == 0.25


def test_set_refuses_a_value_outside_the_profile_range(running) -> None:
    worker, link, emitter, dp832 = running
    assert ask(worker, "set", channel=1, voltage=31.0) == {"error": "voltage must be from 0 to 30"}
    assert not any(c.startswith(":SOUR1:VOLT 31") for c in link.commands)


def test_set_refuses_a_value_above_the_configured_limit(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    worker, link = start(dp800, dp832, emitter, advanced={"limits": {"max_voltage": 15}})
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "set", channel=1, voltage=20.0) == {
            "error": "voltage must be at most 15, the configured limit"
        }
        assert ask(worker, "set", channel=1, voltage=14.0)["voltage"]["readback"] == 14.0
    finally:
        worker.stop()
        worker.join(2)


def test_channel_3_uses_its_own_range(running) -> None:
    worker, link, emitter, dp832 = running
    assert ask(worker, "set", channel=3, voltage=6.0) == {"error": "voltage must be from 0 to 5"}
    assert ask(worker, "set", channel=3, voltage=4.5)["voltage"]["readback"] == 4.5


def test_set_reports_the_instruments_own_error(running) -> None:
    worker, link, emitter, dp832 = running
    dp832.fail_next_set = '-221,"Settings conflict"'
    result = ask(worker, "set", channel=1, voltage=7.0)
    assert result == {"error": "the instrument reported -221,Settings conflict"}
    assert any("Settings conflict" in m for m in emitter.messages())


def test_set_reports_a_readback_that_disagrees(running) -> None:
    worker, link, emitter, dp832 = running
    original = dp832.__call__

    def clipping(command: str) -> str | None:
        if command == ":SOUR1:VOLT 9.000":
            dp832.volt[1] = 8.0
            dp832.calls.append(command)
            return None
        return original(command)

    link._handler = clipping
    result = ask(worker, "set", channel=1, voltage=9.0)
    assert result["error"].startswith("voltage reads back 8 after setting 9")


def test_output_switches_and_verifies(running) -> None:
    worker, link, emitter, dp832 = running
    assert ask(worker, "set", channel=2, output=True) == {"output": True}
    assert dp832.out[2] is True
    assert ask(worker, "set", channel=2, output=False) == {"output": False}
    assert ("info", "set ch2 output off", "action") in emitter.notes


def test_read_returns_fresh_measurements(running) -> None:
    worker, link, emitter, dp832 = running
    assert ask(worker, "read", channel=1) == {"voltage": 5.0, "current": 0.5, "power": 2.5}


def test_scpi_query_and_command(running) -> None:
    worker, link, emitter, dp832 = running
    assert ask(worker, "scpi", command="*IDN?") == {
        "reply": "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
    }
    assert ask(worker, "scpi", command="BOGUS") == {
        "error": "the instrument reported -113,Undefined header"
    }
    assert ("info", "> *IDN?", "tx") in emitter.notes


def test_state_report_has_identity_profile_channels_and_ranges(running) -> None:
    worker, link, emitter, dp832 = running
    state = ask(worker, "state")
    assert state["identity"] == "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
    assert state["profile"]["id"] == "rigol-dp800" and state["profile"]["badge"] == "manual"
    assert state["channels"]["ch3"]["ranges"]["voltage"]["max"] == 5.0
    assert state["channels"]["ch1"]["ranges"]["voltage"]["max"] == 30.0
    assert state["counters"]["target_hz"] == 50
    assert state["recent"][-1]["command"]


def test_a_multi_value_set_checks_everything_before_sending_anything(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link)
    try:
        result = ask(worker, "set", channel=1, voltage=12.0, current=50.0)
        assert result == {"error": "current must be from 0 to 3"}
        assert ":SOUR1:VOLT 12.000" not in link.commands
    finally:
        worker.stop()
        worker.join(2)


def test_a_set_that_fails_after_an_earlier_write_names_what_went_out(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link)
    try:
        dp832.fail_next_set = None
        original = dp832.__call__

        def refuse_current(command: str) -> str | None:
            if command.startswith(":SOUR1:CURR ") and "?" not in command:
                dp832.errors.append('-222,"Data out of range"')
                return None
            return original(command)

        link._handler = refuse_current
        result = ask(worker, "set", channel=1, voltage=12.0, current=2.0)
        assert result == {
            "error": "the instrument reported -222,Data out of range; already set: voltage"
        }
        assert dp832.volt[1] == 12.0
    finally:
        worker.stop()
        worker.join(2)


def test_clear_reports_how_many_errors_it_drained(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link)
    try:
        dp832.errors.extend(['-113,"Undefined header"', '-222,"Data out of range"'])
        assert ask(worker, "clear", channel=1) == {"ok": True, "errors_cleared": 2}
    finally:
        worker.stop()
        worker.join(2)


def test_a_stale_error_from_a_poll_does_not_fail_a_set(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link)
    try:
        dp832.errors.append('-113,"Undefined header"')
        assert ask(worker, "set", channel=1, voltage=5.0) == {
            "voltage": {"set": 5.0, "readback": 5.0}
        }
    finally:
        worker.stop()
        worker.join(2)


def test_every_error_a_write_raises_is_reported(dp800: Profile, dp832: Dp832) -> None:
    original = dp832.__call__

    def noisy(command: str) -> str | None:
        reply = original(command)
        if command.startswith(":SOUR1:VOLT ") and "?" not in command:
            dp832.errors.append('-222,"Data out of range"')
            dp832.errors.append('-221,"Settings conflict"')
        return reply

    link = ScriptedLink(noisy)
    worker, _ = start_open(dp800, link)
    try:
        result = ask(worker, "set", channel=1, voltage=5.0)
        assert result == {
            "error": "the instrument reported -222,Data out of range; -221,Settings conflict"
        }
        assert ask(worker, "state")["counters"]["instrument_errors"] == 2
    finally:
        worker.stop()
        worker.join(2)


def test_the_scpi_action_waits_its_own_timeout_and_returns_blocks(
    dp800: Profile, dp832: Dp832
) -> None:
    original = dp832.__call__

    def with_block(command: str) -> str | bytes | None:
        if command == "BLOCK?":
            return b"#3005hello\n"
        return original(command)

    link = ScriptedLink(with_block)
    worker, _ = start_open(dp800, link)
    try:
        assert ask(worker, "scpi", command="*IDN?", timeout_s=7.5)["reply"].startswith("RIGOL")
        assert 7.5 in link.timeouts_asked
        assert ask(worker, "scpi", command="BLOCK?") == {
            "block_bytes": 5,
            "base64": base64.b64encode(b"hello").decode(),
        }
        assert ask(worker, "scpi", command="*IDN?")["reply"].startswith("RIGOL")
    finally:
        worker.stop()
        worker.join(2)


@pytest.fixture
def nge103() -> FakeInstrument:
    profile = by_id(shipped(), "rohde-schwarz-nge100")
    assert profile is not None
    return FakeInstrument(profile, "NGE103B")


@pytest.fixture
def start_nge(
    nge103: FakeInstrument,
) -> Iterator[Callable[[], tuple[InstrumentWorker, ScriptedLink]]]:
    """Starts the worker once the test has set the fake up, so its channel selection is known."""
    workers: list[InstrumentWorker] = []

    def run() -> tuple[InstrumentWorker, ScriptedLink]:
        profile = nge103.profile
        worker, link = start(
            profile, nge103.handle, RecordingEmitter(), where="nge", profile=profile.id
        )
        workers.append(worker)
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        return worker, link

    yield run
    for worker in workers:
        worker.stop()
        worker.join(2)


def _arm(fake: FakeInstrument, *channels: int) -> None:
    for ch in channels:
        fake.handle(f"INST:NSEL {ch}")
        fake.handle("OUTP:SEL 1")


def _writes(link: ScriptedLink, before: int) -> list[str]:
    return [c for c in link.commands[before:] if "?" not in c and not c.startswith("INST:NSEL")]


def test_an_armed_channel_delivers_only_with_the_master_on(nge103: FakeInstrument) -> None:
    _arm(nge103, 1)
    assert nge103.handle("OUTP?") == "0"
    nge103.handle("OUTP:GEN 1")
    assert nge103.handle("OUTP?") == "1"


def test_switching_on_raises_the_master_after_the_channel(
    start_nge, nge103: FakeInstrument
) -> None:
    worker, link = start_nge()
    before = len(link.commands)
    assert ask(worker, "set", channel=2, output=True) == {"output": True}
    assert _writes(link, before) == ["OUTP:SEL 1", "OUTP:GEN 1"]
    assert nge103.handle("OUTP:GEN?") == "1"


def test_switching_on_is_refused_while_another_channel_is_armed(
    start_nge, nge103: FakeInstrument
) -> None:
    """Raising the master would switch on every armed channel, not only the one asked for."""
    _arm(nge103, 1, 3)
    worker, link = start_nge()
    before = len(link.commands)
    assert ask(worker, "set", channel=2, output=True) == {
        "error": "the master output is off, and raising it would also switch on ch1, ch3; "
        "switch those off first"
    }
    assert _writes(link, before) == []
    assert nge103.handle("OUTP:GEN?") == "0"


def test_with_the_master_on_only_the_channel_is_switched(start_nge, nge103: FakeInstrument) -> None:
    _arm(nge103, 1)
    nge103.handle("OUTP:GEN 1")
    worker, link = start_nge()
    before = len(link.commands)
    assert ask(worker, "set", channel=2, output=True) == {"output": True}
    assert _writes(link, before) == ["OUTP:SEL 1"]
    assert "OUTP:SEL?" not in link.commands[before:]


def test_switching_off_leaves_the_master_alone(start_nge, nge103: FakeInstrument) -> None:
    worker, link = start_nge()
    ask(worker, "set", channel=1, output=True)
    before = len(link.commands)
    assert ask(worker, "set", channel=1, output=False) == {"output": False}
    assert _writes(link, before) == ["OUTP:SEL 0"]
    assert nge103.handle("OUTP:GEN?") == "1"
