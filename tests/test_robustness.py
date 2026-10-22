"""The behaviors the bench instruments and the established libraries taught: a late reply never
lands under the next field, stale errors never fail a request, the panel stays locked for meters
that need it, and the SCPI action waits as long as it was told."""

import base64
import dataclasses
import time
from typing import Any

import pytest

from tests.conftest import ScriptedLink, instrument_config, opener_for, settings_for, wait_until
from tests.test_worker import Dp832, RecordingEmitter, ask
from zelos_extension_instruments import engine
from zelos_extension_instruments.engine import RangeError, parse_float
from zelos_extension_instruments.link import LinkTimeout
from zelos_extension_instruments.profile import Profile, Range, load_file, shipped
from zelos_extension_instruments.worker import InstrumentWorker


@pytest.fixture
def dp832() -> Dp832:
    return Dp832()


def _worker(
    dp800: Profile, link: ScriptedLink, **config: Any
) -> tuple[InstrumentWorker, RecordingEmitter]:
    emitter = RecordingEmitter()
    cfg = instrument_config(rate_hz=50, **config)
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
    return worker, emitter


def test_the_link_is_flushed_and_the_error_queue_drained_before_the_first_poll(
    dp800: Profile, dp832: Dp832
) -> None:
    """Bytes a banner or another client left, and errors another session queued, are not ours."""
    dp832.errors.append('-113,"Undefined header"')
    dp832.errors.append('-222,"Data out of range"')
    link = ScriptedLink(dp832)
    worker, emitter = _worker(dp800, link)
    try:
        assert link.cleared >= 1
        assert not dp832.errors
        state = ask(worker, "state")
        assert state["counters"]["instrument_errors"] == 2
        assert not any("instrument error" in m for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(2)


def test_a_stale_error_from_a_poll_does_not_fail_a_set(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link)
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
    worker, _ = _worker(dp800, link)
    try:
        result = ask(worker, "set", channel=1, voltage=5.0)
        assert result == {
            "error": "the instrument reported -222,Data out of range; -221,Settings conflict"
        }
        assert ask(worker, "state")["counters"]["instrument_errors"] == 2
    finally:
        worker.stop()
        worker.join(2)


def test_a_late_reply_is_not_recorded_under_the_next_query(dp800: Profile, dp832: Dp832) -> None:
    """One silent query, then its reply arrives with the next one: the worker resynchronizes
    instead of shifting every later reading by one field."""
    original = dp832.__call__
    late: list[str] = []
    silent = {"armed": True}

    def slow(command: str) -> str | None:
        reply = original(command)
        if silent["armed"] and command == ":MEAS:ALL? CH1":
            silent["armed"] = False
            late.append(reply or "")
            raise LinkTimeout("no reply")
        if late and reply is not None:
            held, reply = late.pop(), reply
            link.push(held)
        return reply

    link = ScriptedLink(slow)
    worker, emitter = _worker(dp800, link)
    try:
        assert wait_until(
            lambda: any("replies were out of step" in m for m in emitter.messages()), timeout_s=5
        )
        time.sleep(0.3)
        # Channel 2 is off: its voltage is 0, never channel 1's 5 V shifted down by one reply.
        rows = [r for r in emitter.rows if r[1] == 2 and "voltage" in r[2]]
        assert rows and all(abs(r[2]["voltage"]) < 1e-6 for r in rows), rows[-3:]
        assert ask(worker, "state")["counters"]["timeouts"] == 1
    finally:
        worker.stop()
        worker.join(2)


def test_resync_accepts_only_the_identity(dp800: Profile, dp832: Dp832) -> None:
    """Decoys that arrive after the clear, ahead of the identity, are read past."""
    original = dp832.__call__
    decoys = {"armed": False}

    def with_decoys(command: str) -> str | None:
        if command == "*IDN?" and decoys["armed"]:
            decoys["armed"] = False
            link.push("5.000,1.000,5.000")
            link.push("another,comma,reply")
        return original(command)

    link = ScriptedLink(with_decoys)
    worker, _ = _worker(dp800, link)
    try:
        decoys["armed"] = True
        worker._resync("test")
        assert worker._recent[-1][2].startswith("RIGOL TECHNOLOGIES,DP832A")
        assert worker._recent[-1][1] == "*IDN?"
    finally:
        worker.stop()
        worker.join(2)


def test_keep_panel_is_skipped_for_a_meter_that_needs_remote() -> None:
    profile = load_file(next(p.path for p in shipped() if p.id == "agilent-34401a" and p.path))
    assert profile.link.needs_remote and profile.link.locks_panel and profile.link.local
    commands: list[str] = []

    def meter(command: str) -> str | None:
        commands.append(command)
        if command == "*IDN?":
            return "HEWLETT-PACKARD,34401A,0,11-5-2"
        if "ERR" in command:
            return '0,"No error"'
        if "READ" in command:
            return "+1.00000000E+00"
        if "FUNC" in command:
            return '"VOLT"'
        return "0" if command.endswith("?") else None

    link = ScriptedLink(meter)
    emitter = RecordingEmitter()
    cfg = instrument_config(rate_hz=50, profile="agilent-34401a")
    worker = InstrumentWorker(cfg, [profile], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        time.sleep(0.3)
        assert profile.link.remote in commands
        assert profile.link.local not in commands
    finally:
        worker.stop()
        worker.join(2)


def test_stop_hands_the_panel_back(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link, advanced={"keep_panel": False})
    worker.stop()
    worker.join(2)
    assert "SYST:LOCAL" in link.commands[-4:]


def test_the_scpi_action_waits_its_own_timeout_and_returns_blocks(
    dp800: Profile, dp832: Dp832
) -> None:
    original = dp832.__call__

    def with_block(command: str) -> str | bytes | None:
        if command == "BLOCK?":
            return b"#3005hello\n"
        return original(command)

    link = ScriptedLink(with_block)
    worker, _ = _worker(dp800, link)
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


def test_a_value_finer_than_the_instruments_digits_is_refused() -> None:
    three = Range(min=0.0, max=30.0, digits=3, step=None, min_query=None, max_query=None)
    engine.check_resolution(5.001, three, "voltage")
    with pytest.raises(RangeError, match="finer than the instrument's 3 decimals"):
        engine.check_resolution(0.00012, three, "current")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10M", 10e6),
        ("10k", 10e3),
        ("1.00GSa/s", 1e9),
        ("5mV", 0.005),
        ("2.5e6 pts", 2.5e6),
        ("-0.25 V", -0.25),
        ("3.58E+00V", 3.58),
    ],
)
def test_parse_float_reads_metric_prefixes_and_units(text: str, expected: float) -> None:
    assert parse_float(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["C1:VDIV 5.00E-01V", "C1:PAVA PKPK,3.58E+00V", "INR 8193"])
def test_parse_float_refuses_a_header(text: str) -> None:
    with pytest.raises(engine.ParseError):
        parse_float(text)


def test_a_missed_reply_sends_the_remote_command_again(dp800: Profile, dp832: Dp832) -> None:
    """A 34401A that idled out of remote mode answered nothing until SYST:REM was resent."""
    original = dp832.__call__
    silent = {"armed": True}

    def drops_remote(command: str) -> str | None:
        if silent["armed"] and command == ":MEAS:ALL? CH1":
            silent["armed"] = False
            raise LinkTimeout("no reply")
        return original(command)

    link = ScriptedLink(drops_remote)
    worker, emitter = _worker(dp800, link)
    try:
        assert wait_until(
            lambda: any("replies were out of step" in m for m in emitter.messages()), timeout_s=5
        )
        after = link.commands[link.commands.index(":MEAS:ALL? CH1") + 1 :]
        assert after[after.index("SYST:REMOTE") + 1] == "*IDN?"
    finally:
        worker.stop()
        worker.join(2)


@pytest.mark.parametrize("first", ["silent", "Invalid Character"])
def test_identity_is_asked_again_after_a_silent_or_garbled_first_reply(
    first: str, dp800: Profile, dp832: Dp832
) -> None:
    """An HMP ignores the first *IDN? after a connect; stale bytes made a GPD answer Invalid
    Character; sigrok asks again."""
    original = dp832.__call__
    asked = {"n": 0}

    def wakes_late(command: str) -> str | None:
        if command == "*IDN?":
            asked["n"] += 1
            if asked["n"] == 1:
                if first == "silent":
                    raise LinkTimeout("no reply")
                return first
        return original(command)

    link = ScriptedLink(wakes_late)
    worker, _ = _worker(dp800, link)
    try:
        assert worker.idn is not None and worker.idn.model == "DP832A"
        assert link.cleared >= 1
    finally:
        worker.stop()
        worker.join(2)


def test_a_local_of_several_commands_sends_them_in_order(dp800: Profile, dp832: Dp832) -> None:
    """A DL3000 returns to local only through its virtual panel: two commands with a pause."""
    profile = dataclasses.replace(
        dp800, link=dataclasses.replace(dp800.link, local=("DEBug:KEY 1", "SYSTem:KEY 8"))
    )
    original = dp832.__call__

    def panel(command: str) -> str | None:
        return None if command in ("DEBug:KEY 1", "SYSTem:KEY 8") else original(command)

    link = ScriptedLink(panel)
    worker, _ = _worker(profile, link)
    try:
        assert ask(worker, "local") == {"ok": True}
        first = link.commands.index("DEBug:KEY 1")
        assert link.commands[first + 1] == "SYSTem:KEY 8"
    finally:
        worker.stop()
        worker.join(2)
    # Stopping hands the panel back the same way.
    assert link.commands[-2:] == ["DEBug:KEY 1", "SYSTem:KEY 8"]
