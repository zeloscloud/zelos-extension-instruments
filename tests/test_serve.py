"""What a worker does for a request: sets with readback, reads, raw SCPI, clear, the state report
and the master-output guard."""

import base64
import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.conftest import (
    Dp832,
    RecordingEmitter,
    ScriptedLink,
    ask,
    instrument_config,
    minimal,
    opener_for,
    profile_from,
    start,
    start_open,
    wait_until,
)
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker, RequestKind


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


def test_a_switch_whose_readback_disagrees_is_reported(running) -> None:
    worker, link, emitter, dp832 = running
    original = dp832.__call__

    def stuck(command: str) -> str | None:
        if command.startswith(":OUTP CH"):
            dp832.calls.append(command)
            return None
        return original(command)

    link._handler = stuck
    assert ask(worker, "set", channel=2, output=True) == {"error": "output reads back off"}
    assert ask(worker, "set", channel=1, output=False) == {"error": "output reads back on"}
    assert dp832.out == {1: True, 2: False, 3: False}


def unread_profile() -> Profile:
    """A supply whose profile reads no output state and no setpoint back."""
    raw = minimal()
    raw["setters"] = {"output": {"command": "OUTP {bit}"}, "voltage": {"command": "VOLT {value}"}}
    raw["ranges"] = {"voltage": {"max": 30}}
    return profile_from(raw)


def acme(command: str) -> str | None:
    return {"*IDN?": "ACME,P1,SN1,1.0", "MEAS:VOLT? 1": "5.0"}.get(command)


def test_a_switch_the_profile_cannot_read_back_returns_the_state_asked_for() -> None:
    worker, link = start(unread_profile(), acme, RecordingEmitter())
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "set", channel=1, output=True) == {"output": True}
        assert ask(worker, "set", channel=1, output=False) == {"output": False}
        assert [c for c in link.commands if c.startswith("OUTP")] == ["OUTP 1", "OUTP 0"]
    finally:
        worker.stop()
        worker.join(2)


def test_a_value_the_profile_cannot_read_back_is_returned_unverified() -> None:
    worker, link = start(unread_profile(), acme, RecordingEmitter())
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "set", channel=1, voltage=5.0) == {
            "voltage": {
                "set": 5.0,
                "readback": None,
                "unverified": "the profile has no readback",
            }
        }
        assert "VOLT 5.000" in link.commands
    finally:
        worker.stop()
        worker.join(2)


def bench(
    profile: Profile, handler: Callable[[str], str | bytes | None], channels: list[int]
) -> tuple[InstrumentWorker, ScriptedLink, RecordingEmitter]:
    """A worker holding `profile` on a scripted link, with nothing sent yet."""
    link = ScriptedLink(handler)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="unit", profile=profile.id)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.profile = profile
    worker._link = link
    worker._install(profile, None, channels)
    return worker, link, emitter


def served(worker: InstrumentWorker, kind: RequestKind, **args: Any) -> dict[str, Any]:
    """A request answered on this thread, as the worker's own thread answers it."""
    future = worker.request(kind, **args)
    worker._serve_requests()
    return future.result(timeout=0)


class Echo:
    """Answers a query with what the command of the same header last set, or a fixed reply."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = {"SYST:ERR?": '0,"No error"', **replies}
        self.writes: list[str] = []

    def __call__(self, command: str) -> str | None:
        if "?" in command:
            return self.replies.get(command, "0")
        self.writes.append(command)
        head, _, arg = command.partition(" ")
        self.replies[f"{head}?"] = arg
        return None


def test_a_switch_with_its_own_off_command_sends_it_for_off() -> None:
    raw = minimal()
    raw["errors"] = {"query": "SYST:ERR?"}
    raw["setters"] = {
        "remote_sense": {"command": "VOLT:SENS EXT", "off": "VOLT:SENS INT", "verify": "SENS?"}
    }
    unit = Echo({})

    def sensing(command: str) -> str | None:
        if command == "SENS?":
            return "1" if unit.writes[-1].endswith("EXT") else "0"
        return unit(command)

    worker, _, emitter = bench(profile_from(raw), sensing, [1])
    assert served(worker, "set", channel=1, remote_sense=True) == {"remote_sense": True}
    assert served(worker, "set", channel=1, remote_sense=False) == {"remote_sense": False}
    assert unit.writes == ["VOLT:SENS EXT", "VOLT:SENS INT"]
    assert [r[2] for r in emitter.rows] == [{"remote_sense": True}, {"remote_sense": False}]


def meter(**range_setter: Any) -> Profile:
    return profile_from(
        {
            "schema": 1,
            "id": "acme-dmm",
            "name": "Acme meter",
            "category": "dmm",
            "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-22"},
            "match": {"manufacturer": "(?i)^acme", "model": "^M1$"},
            "errors": {"query": "SYST:ERR?"},
            "signals": {"value": {"query": "READ?", "parse": "float"}},
            "dmm": {
                "function_query": "FUNC?",
                "function_map": {"VOLT": "dc_voltage", "VOLT:AC": "ac_voltage"},
                "functions": {"dc_voltage": "V", "ac_voltage": "V"},
            },
            "setters": {
                "function": {
                    "command": "FUNC {function}",
                    "map": {"dc_voltage": "VOLT", "ac_voltage": "VOLT:AC"},
                },
                "range": {
                    "command": "{function}:RANG {value}",
                    "verify": "{function}:RANG?",
                    **range_setter,
                },
                "nplc": {"command": "{function}:NPLC {value}"},
            },
        }
    )


class Meter:
    """A meter that picks the smallest range covering the value asked for, up to 1000 V."""

    RANGES = (0.1, 1.0, 10.0, 100.0, 1000.0)

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.ranges: dict[str, float] = {}

    def __call__(self, command: str) -> str | None:
        head, _, arg = command.partition(" ")
        if command == "SYST:ERR?":
            return '0,"No error"'
        if head.endswith(":RANG?"):
            return f"{self.ranges.get(head.removesuffix(':RANG?'), 1000.0):g}"
        self.writes.append(command)
        if head.endswith(":RANG"):
            asked = float(arg)
            self.ranges[head.removesuffix(":RANG")] = next(
                (r for r in self.RANGES if r >= asked), self.RANGES[-1]
            )
        return None


def test_a_meter_range_passes_when_the_meter_picks_a_range_that_covers_it() -> None:
    unit = Meter()
    worker, _, _ = bench(meter(), unit, [1])
    worker._function = "dc_voltage"
    assert served(worker, "set", channel=1, range=5.0) == {"range": {"set": 5.0, "readback": 10.0}}
    assert unit.writes == ["VOLT:RANG 5"]
    assert served(worker, "set", channel=1, range=2000.0) == {
        "error": "range reads back 1000 after setting 2000; "
        "the instrument may have clipped it or refused it"
    }


def test_a_meter_range_names_the_function_set_in_the_same_request() -> None:
    unit = Meter()
    worker, _, _ = bench(meter(), unit, [1])
    worker._function = "dc_voltage"
    result = served(worker, "set", channel=1, range=0.5, function="ac_voltage")
    assert result == {"function": "ac_voltage", "range": {"set": 0.5, "readback": 1.0}}
    assert unit.writes == ["FUNC VOLT:AC", "VOLT:AC:RANG 0.5"]


def test_a_meter_range_takes_its_own_word_for_the_function() -> None:
    unit = Meter()
    worker, _, _ = bench(meter(map={"dc_voltage": "VOLT:DC"}), unit, [1])
    worker._function = "dc_voltage"
    assert served(worker, "set", channel=1, range=50.0)["range"]["readback"] == 100.0
    assert unit.writes == ["VOLT:DC:RANG 50"]


def test_a_meter_setting_for_a_function_without_a_word_is_refused() -> None:
    unit = Meter()
    worker, _, _ = bench(meter(), unit, [1])
    assert served(worker, "set", channel=1, range=5.0) == {
        "error": "this profile has no word for the meter's function other"
    }
    assert unit.writes == []


def test_a_meter_integration_time_still_needs_a_range_or_a_limit() -> None:
    worker, _, _ = bench(meter(), Meter(), [1])
    worker._function = "dc_voltage"
    assert served(worker, "set", channel=1, nplc=1.0) == {
        "error": "no range is known for nplc on this profile; set a limit for it in the config"
    }


def triggered_scope() -> Profile:
    """The shipped scope, with a trigger level set on the trigger source's own channel."""
    scope = by_id(shipped(), "siglent-sds")
    assert scope is not None
    level = dataclasses.replace(
        scope.setters["trigger_level"],
        command="C{src}:TRLV {value}",
        commands=("C{src}:TRLV {value}",),
        verify="C{src}:TRLV?",
    )
    return dataclasses.replace(scope, setters={**scope.setters, "trigger_level": level})


def scope_bench() -> tuple[InstrumentWorker, Echo, RecordingEmitter]:
    unit = Echo({":SYSTem:ERRor?": '0,"No error"'})
    worker, _, emitter = bench(triggered_scope(), unit, [1, 2, 3, 4])
    return worker, unit, emitter


def test_a_trigger_command_names_the_trigger_source_channel() -> None:
    worker, unit, emitter = scope_bench()
    worker._acquisition["trigger_source"] = "ch2"
    assert served(worker, "set", channel=1, trigger_level=0.5) == {
        "trigger_level": {"set": 0.5, "readback": 0.5}
    }
    assert unit.writes == ["C2:TRLV 0.5"]
    assert worker._acquisition["trigger_level"] == 0.5
    assert emitter.acquisitions[-1][1] == {"trigger_level": 0.5}


def test_a_trigger_source_set_in_the_same_request_is_the_one_named() -> None:
    worker, unit, _ = scope_bench()
    worker._acquisition["trigger_source"] = "ch1"
    result = served(worker, "set", channel=1, trigger_level=0.25, trigger_source="ch3")
    assert result == {"trigger_source": "ch3", "trigger_level": {"set": 0.25, "readback": 0.25}}
    assert unit.writes == [":TRIGger:EDGE:SOURce C3", "C3:TRLV 0.25"]
    assert worker._acquisition["trigger_source"] == "ch3"


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("line", "the trigger source is not a channel"),
        (None, "the trigger source is not known yet"),
    ],
)
def test_a_trigger_command_is_refused_when_the_source_is_no_channel(
    source: str | None, error: str
) -> None:
    worker, unit, _ = scope_bench()
    if source is not None:
        worker._acquisition["trigger_source"] = source
    assert served(worker, "set", channel=1, trigger_level=0.5) == {"error": error}
    assert unit.writes == []


def generator(trigger: dict[str, Any]) -> Profile:
    return profile_from(
        {
            "schema": 1,
            "id": "acme-gen",
            "name": "Acme generator",
            "category": "fgen",
            "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-22"},
            "match": {"manufacturer": "(?i)^acme", "model": "^G1$", "channels": 2},
            "errors": {"query": "SYST:ERR?"},
            "ranges": {"modulation_depth": {"max": 120, "digits": 1}},
            "state": {
                "modulation_type": {
                    "query": "SOUR{ch}:MOD:TYPE?",
                    "parse": "map",
                    "map": {"AM": "am", "FM": "fm"},
                },
                "burst": {
                    "query": "C{ch}:BTWV?",
                    "parse": "keyed",
                    "key": "STATE",
                    "map": {"ON": "on", "OFF": "off"},
                },
            },
            "setters": {
                "modulation_type": {
                    "command": "SOUR{ch}:MOD:TYPE {value}",
                    "map": {"am": "AM", "fm": "FM"},
                },
                "modulation_depth": {
                    "command": "SOUR{ch}:{modulation}:DEPT {value}",
                    "verify": "SOUR{ch}:{modulation}:DEPT?",
                },
                "burst": {"command": "C{ch}:BTWV STATE,{on}"},
                "trigger": trigger,
            },
        }
    )


def test_a_modulation_setting_names_the_channels_modulation_type() -> None:
    unit = Echo({})
    worker, _, _ = bench(generator({"command": "*TRG", "channel": False}), unit, [1, 2])
    worker._channel[1].values["modulation_type"] = "am"
    assert served(worker, "set", channel=1, modulation_depth=50.0) == {
        "modulation_depth": {"set": 50.0, "readback": 50.0}
    }
    result = served(worker, "set", channel=2, modulation_depth=30.0, modulation_type="fm")
    assert result["modulation_type"] == "fm"
    assert unit.writes == ["SOUR1:AM:DEPT 50.0", "SOUR2:MOD:TYPE FM", "SOUR2:FM:DEPT 30.0"]


def test_a_modulation_type_set_without_a_readback_names_the_next_modulation_setting() -> None:
    unit = Echo({})
    profile = generator({"command": "*TRG", "channel": False})
    unread = tuple(r for r in profile.state if "modulation_type" not in r.fields)
    worker, _, _ = bench(dataclasses.replace(profile, state=unread), unit, [1, 2])
    assert served(worker, "set", channel=1, modulation_type="fm") == {"modulation_type": "fm"}
    assert served(worker, "set", channel=1, modulation_depth=40.0) == {
        "modulation_depth": {"set": 40.0, "readback": 40.0}
    }
    assert unit.writes == ["SOUR1:MOD:TYPE FM", "SOUR1:FM:DEPT 40.0"]


def test_a_modulation_setting_is_refused_while_the_type_is_unknown() -> None:
    unit = Echo({})
    worker, _, _ = bench(generator({"command": "*TRG", "channel": False}), unit, [1, 2])
    assert served(worker, "set", channel=1, modulation_depth=50.0) == {
        "error": "the modulation type of ch1 is not known"
    }
    assert unit.writes == []


# A generator whose type setter switches that type's modulation on.
STAT_PER_TYPE = profile_from(
    {
        "schema": 1,
        "id": "acme-stat-gen",
        "name": "Acme generator with a switch per type",
        "category": "fgen",
        "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-22"},
        "match": {"manufacturer": "(?i)^acme", "model": "^G2$", "channels": 2},
        "errors": {"query": "SYST:ERR?"},
        "ranges": {"modulation_frequency": {"min": 1, "max": 1e6, "digits": 3}},
        "setters": {
            "modulation": {
                "command": "SOUR{ch}:{modulation}:STAT {bit}",
                "verify": "SOUR{ch}:{modulation}:STAT?",
            },
            "modulation_type": {
                "command": "SOUR{ch}:{value}:STAT ON",
                "map": {"am": "AM", "fm": "FM"},
            },
            "modulation_frequency": {
                "command": "SOUR{ch}:{modulation}:INT:FREQ {value}",
                "verify": "SOUR{ch}:{modulation}:INT:FREQ?",
            },
        },
    }
)


def test_modulation_off_goes_first_and_the_type_asked_for_is_only_remembered() -> None:
    unit = Echo({})
    worker, _, _ = bench(STAT_PER_TYPE, unit, [1, 2])
    worker._channel[1].values["modulation_type"] = "am"
    result = served(
        worker,
        "set",
        channel=1,
        modulation_type="fm",
        modulation_frequency=100.0,
        modulation=False,
    )
    assert result == {
        "modulation": False,
        "modulation_frequency": {"set": 100.0, "readback": 100.0},
        "modulation_type": "fm",
    }
    assert unit.writes == ["SOUR1:FM:STAT 0", "SOUR1:FM:INT:FREQ 100.000"]
    assert worker._channel[1].values["modulation_type"] == "fm"
    assert served(worker, "set", channel=1, modulation=True) == {"modulation": True}
    assert unit.writes[-1] == "SOUR1:FM:STAT 1"


def test_modulation_off_without_a_type_names_the_remembered_type() -> None:
    unit = Echo({})
    worker, _, _ = bench(STAT_PER_TYPE, unit, [1, 2])
    worker._channel[2].values["modulation_type"] = "am"
    result = served(worker, "set", channel=2, modulation_frequency=50.0, modulation=False)
    assert result["modulation"] is False
    assert unit.writes == ["SOUR2:AM:STAT 0", "SOUR2:AM:INT:FREQ 50.000"]


def test_modulation_on_sends_the_type_first_as_before() -> None:
    unit = Echo({})
    worker, _, _ = bench(STAT_PER_TYPE, unit, [1, 2])
    result = served(worker, "set", channel=1, modulation=True, modulation_type="fm")
    assert result == {"modulation_type": "fm", "modulation": True}
    assert unit.writes == ["SOUR1:FM:STAT ON", "SOUR1:FM:STAT 1"]


def test_a_keyed_reply_without_the_field_is_no_readback() -> None:
    """A unit drops the keys of a mode that is off, so the set has nothing to check."""
    unit = Echo({})

    def without_state(command: str) -> str | None:
        if command == "C1:BTWV?":
            return "C1:BTWV CARR,WVTP,SINE"
        return unit(command)

    worker, _, _ = bench(generator({"command": "*TRG", "channel": False}), without_state, [1, 2])
    assert served(worker, "set", channel=1, burst=True) == {"burst": True}
    assert unit.writes == ["C1:BTWV STATE,ON"]


def test_a_per_channel_command_goes_to_the_channel_asked_for() -> None:
    unit = Echo({})
    worker, _, _ = bench(generator({"command": ["TRIG{ch}:SOUR BUS", "TRIG{ch}"]}), unit, [1, 2])
    assert served(worker, "command", name="trigger", channel=2) == {
        "ok": True,
        "command": "trigger",
    }
    assert served(worker, "command", name="trigger") == {"ok": True, "command": "trigger"}
    assert unit.writes == ["TRIG2:SOUR BUS", "TRIG2", "TRIG1:SOUR BUS", "TRIG1"]


def test_an_instrument_wide_command_names_no_channel() -> None:
    unit = Echo({})
    worker, _, _ = bench(generator({"command": "*TRG", "channel": False}), unit, [1, 2])
    assert served(worker, "command", name="trigger", channel=2)["ok"] is True
    assert unit.writes == ["*TRG"]


def test_every_error_query_is_asked_after_a_write() -> None:
    """A unit with two error registers: the second reports what the write did."""
    raw = minimal()
    raw["errors"] = {"query": ["CMR?", "EXR?"]}
    raw["setters"] = {"voltage": {"command": "VOLT {value}"}}
    raw["ranges"] = {"voltage": {"max": 30}}
    pending: list[str] = []

    def unit(command: str) -> str | None:
        if command == "CMR?":
            return "0"
        if command == "EXR?":
            return pending.pop(0) if pending else "0"
        if command.startswith("VOLT "):
            pending.append('-222,"Data out of range"')
        return None

    worker, link, _ = bench(profile_from(raw), unit, [1])
    assert served(worker, "set", channel=1, voltage=5.0) == {
        "error": "the instrument reported -222,Data out of range"
    }
    assert link.commands[-4:] == ["VOLT 5.000", "CMR?", "EXR?", "EXR?"]


def test_the_state_report_lists_the_system_functions_the_profile_gives() -> None:
    raw = minimal()
    raw["system"] = {
        "reset": False,
        "beep": "SYST:BEEP",
        "lock": "SYST:KLOC ON",
        "unlock": "SYST:KLOC OFF",
        "screenshot": ["HCOP:DATA?"],
        "setup_query": "SYST:SET?",
    }
    worker, _, _ = bench(profile_from(raw), Echo({}), [1])
    assert served(worker, "state")["profile"]["system"] == [
        "beep",
        "lock",
        "screenshot",
        "self_test",
        "setup",
    ]
