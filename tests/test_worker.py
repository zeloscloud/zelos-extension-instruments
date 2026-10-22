"""The instrument worker against a scripted link: connect, poll, set, errors, reconnect."""

import math
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import (
    ScriptedLink,
    instrument_config,
    opener_for,
    settings_for,
    wait_until,
)
from zelos_extension_instruments import scope_ops
from zelos_extension_instruments.emitter import Emitter
from zelos_extension_instruments.link import LinkError, LinkTimeout
from zelos_extension_instruments.profile import Profile, by_id, from_dict, load_file, shipped
from zelos_extension_instruments.sim import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker


class Dp832:
    """A three-channel supply that answers like a Rigol DP832 and keeps its own state."""

    def __init__(self) -> None:
        self.volt = {1: 5.0, 2: 0.0, 3: 0.0}
        self.curr = {1: 1.0, 2: 0.5, 3: 0.5}
        self.out = {1: True, 2: False, 3: False}
        self.load = {1: 10.0, 2: 10.0, 3: 10.0}
        self.errors: list[str] = []
        self.ovp_tripped = {1: False, 2: False, 3: False}
        self.ovp_level = {1: 33.0, 2: 33.0, 3: 5.5}
        self.ocp_level = {1: 3.3, 2: 3.3, 3: 3.3}
        self.ovp_on = {1: False, 2: False, 3: False}
        self.ocp_on = {1: False, 2: False, 3: False}
        self.silent = False
        # An error the next command (not a query) queues, as a refused setting does.
        self.fail_next_set: str | None = None
        self.calls: list[str] = []

    def measured(self, ch: int) -> tuple[float, float, float]:
        if not self.out[ch]:
            return 0.0, 0.0, 0.0
        v = min(self.volt[ch], self.curr[ch] * self.load[ch])
        i = v / self.load[ch]
        return v, i, v * i

    def mode(self, ch: int) -> str:
        if not self.out[ch]:
            return "UR"
        return "CV" if self.volt[ch] <= self.curr[ch] * self.load[ch] else "CC"

    def __call__(self, command: str) -> str | None:
        self.calls.append(command)
        if self.fail_next_set is not None and not command.endswith("?"):
            self.errors.append(self.fail_next_set)
            self.fail_next_set = None
        if self.silent:
            raise LinkTimeout("no reply from dp832")
        head, _, arg = command.partition(" ")
        if head == "*IDN?":
            return "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
        if head == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if head == ":MEAS:ALL?":
            v, i, p = self.measured(int(arg[2:]))
            return f"{v:.4f},{i:.4f},{p:.3f}"
        if head == ":OUTP?":
            return "ON" if self.out[int(arg[2:])] else "OFF"
        if head == ":OUTP:MODE?":
            return self.mode(int(arg[2:]))
        if head == ":OUTP:OVP:QUES?":
            return "1" if self.ovp_tripped[int(arg[2:])] else "0"
        if head == ":OUTP:OCP:QUES?":
            return "0"
        if head in (":OUTP:OVP:VAL?", ":OUTP:OCP:VAL?"):
            table = self.ovp_level if "OVP" in head else self.ocp_level
            return f"{table[int(arg[2:])]:.3f}"
        if head in (":OUTP:OVP?", ":OUTP:OCP?"):
            table = self.ovp_on if "OVP" in head else self.ocp_on
            return "ON" if table[int(arg[2:])] else "OFF"
        if head == ":OUTP:SENS?":
            return "OFF"
        if head in (":OUTP:OVP:VAL", ":OUTP:OCP:VAL"):
            ch, _, level = arg.partition(",")
            table = self.ovp_level if "OVP" in head else self.ocp_level
            table[int(ch[2:])] = float(level)
            return None
        if head in (":OUTP:OVP", ":OUTP:OCP"):
            ch, _, state = arg.partition(",")
            table = self.ovp_on if "OVP" in head else self.ocp_on
            table[int(ch[2:])] = state == "ON"
            return None
        if head == ":OUTP":
            ch, _, state = arg.partition(",")
            self.out[int(ch[2:])] = state == "ON"
            return None
        if head in (":OUTP:OCP:CLEAR", ":OUTP:OVP:CLEAR"):
            self.ovp_tripped[int(arg[2:])] = False
            return None
        if head in ("SYST:REMOTE", "SYST:LOCAL", "*RST"):
            return None
        if head.startswith(":SOUR"):
            ch = int(head[5])
            field = head.split(":")[2]
            table = self.volt if field.startswith("VOLT") else self.curr
            if field.endswith("?"):
                return f"{table[ch]:.3f}"
            value = float(arg)
            limit = 30.0 if table is self.volt else 3.0
            if value > limit:
                self.errors.append('-222,"Data out of range"')
                return None
            table[ch] = value
            return None
        self.errors.append('-113,"Undefined header"')
        return None


@pytest.fixture
def dp832() -> Dp832:
    return Dp832()


class RecordingEmitter(Emitter):
    """Records what the worker writes instead of a trace."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, int]] = []
        self.rows: list[tuple[str, int, dict[str, Any], int]] = []
        self.readings: list[tuple[str, str, float, int]] = []
        self.polls: list[tuple[str, str, float, int]] = []
        self.acquisitions: list[tuple[str, dict[str, Any], int]] = []
        self.waveforms: list[tuple[str, int, list[tuple[int, float]]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.notes: list[tuple[str, str, str]] = []
        self._fields: dict[str, dict[str, str | None]] = {}
        self.channel_fields: dict[str, dict[int, set[str]]] = {}
        self.identities: list[tuple[str, dict[str, str]]] = []

    def add_instrument(self, name: str, profile: Profile, channels: int, poll: Any = ()) -> None:
        self.registered.append((name, profile.id, channels))
        self._fields[name] = profile.fields

    def add_channels(self, name: str, fields: dict[int, set[str]]) -> None:
        self.channel_fields[name] = {ch: set(names) for ch, names in fields.items()}

    def identity(self, name: str, values: dict[str, str], time_ns: int) -> None:
        self.identities.append((name, dict(values)))

    def row(self, name: str, ch: int, values: dict[str, Any], time_ns: int) -> None:
        self.rows.append((name, ch, dict(values), time_ns))

    def reading(self, name: str, function: str, value: float, time_ns: int) -> None:
        self.readings.append((name, function, value, time_ns))

    def poll_value(self, name: str, row_name: str, value: float, time_ns: int) -> None:
        self.polls.append((name, row_name, value, time_ns))

    def acquisition_row(self, name: str, values: dict[str, Any], time_ns: int) -> None:
        self.acquisitions.append((name, dict(values), time_ns))

    def waveform(self, name: str, ch: int, time_ns: Any, volts: Any) -> None:
        self.waveforms.append((name, ch, list(zip(time_ns.tolist(), volts.tolist(), strict=True))))

    def status(
        self, name: str, rtt_ms: float, rate_hz: float, errors: int, connected: bool, time_ns: int
    ) -> None:
        self.statuses.append(
            {"rtt_ms": rtt_ms, "rate_hz": rate_hz, "errors": errors, "connected": connected}
        )

    def note(
        self, name: str, level: str, message: str, time_ns: int, source: str = "instruments"
    ) -> None:
        self.notes.append((level, message, source))

    def fields(self, name: str) -> dict[str, str | None]:
        return dict(self._fields.get(name, {}))

    def messages(self) -> list[str]:
        return [m for _, m, _ in self.notes]


def start(
    dp800: Profile,
    handler: Callable[[str], str | None],
    emitter: RecordingEmitter,
    *,
    opener: Any = None,
    **overrides: Any,
) -> tuple[InstrumentWorker, ScriptedLink]:
    link = ScriptedLink(handler, "127.0.0.1:5555")
    overrides.setdefault("rate_hz", 50)
    cfg = instrument_config(**overrides)
    worker = InstrumentWorker(
        cfg, [dp800], emitter, settings_for(cfg), open=opener or opener_for(link)
    )
    worker.start()
    return worker, link


def ask(worker: InstrumentWorker, kind: Any, **args: Any) -> dict[str, Any]:
    return worker.request(kind, **args).result(timeout=5)


@pytest.fixture
def running(dp800: Profile, dp832: Dp832):
    emitter = RecordingEmitter()
    worker, link = start(dp800, dp832, emitter)
    assert wait_until(lambda: worker.state == "open")
    yield worker, link, emitter, dp832
    worker.stop()
    worker.join(2)


def test_connects_identifies_and_registers_three_channels(running) -> None:
    worker, link, emitter, dp832 = running
    assert worker.idn is not None and worker.idn.model == "DP832A"
    assert worker.channels == 3
    assert emitter.registered == [("psu", "rigol-dp800", 3)]
    assert link.commands[:2] == ["*IDN?", "SYST:REMOTE"]
    assert "connected to 127.0.0.1:5555" in emitter.messages()[0]
    assert worker.health.startswith("Connected to RIGOL TECHNOLOGIES DP832A.")
    assert emitter.identities == [
        (
            "psu",
            {
                "manufacturer": "RIGOL TECHNOLOGIES",
                "model": "DP832A",
                "serial": "DP8C000001",
                "firmware": "00.01.16",
                "profile": "rigol-dp800",
                "address": "127.0.0.1:5555",
            },
        )
    ]
    assert set(emitter.channel_fields["psu"]) == {1, 2, 3}


def test_polls_measurements_and_state_for_every_channel(running) -> None:
    worker, link, emitter, dp832 = running
    assert wait_until(lambda: sum(1 for r in emitter.rows if "voltage" in r[2]) >= 6)
    fast = [r for r in emitter.rows if "voltage" in r[2]]
    ch1 = next(r for r in fast if r[1] == 1)[2]
    assert ch1 == {"voltage": 5.0, "current": 0.5, "power": 2.5}
    assert wait_until(lambda: any("mode" in r[2] for r in emitter.rows))
    slow = next(r for r in emitter.rows if "mode" in r[2] and r[1] == 1)[2]
    assert slow["mode"] == "cv" and slow["output"] is True and slow["protection"] == "none"
    assert slow["voltage_set"] == 5.0 and slow["current_set"] == 1.0
    off = next(r for r in emitter.rows if "mode" in r[2] and r[1] == 2)[2]
    assert off["mode"] == "unregulated" and off["output"] is False


def test_timestamps_increase_and_status_reports_rate(running) -> None:
    worker, link, emitter, dp832 = running
    assert wait_until(lambda: len(emitter.statuses) >= 2, timeout_s=5)
    times = [r[3] for r in emitter.rows if r[1] == 1 and "voltage" in r[2]]
    assert times == sorted(times)
    status = emitter.statuses[-1]
    assert status["connected"] is True and status["rate_hz"] > 0 and status["errors"] == 0
    assert math.isfinite(status["rtt_ms"])


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
    assert ask(worker, "output", channel=2, on=True) == {"output": True}
    assert dp832.out[2] is True
    assert ask(worker, "output", channel=2, on=False) == {"output": False}
    assert ("info", "set ch2 output off", "action") in emitter.notes


def test_external_setpoint_change_is_logged_but_our_own_is_not(running) -> None:
    worker, link, emitter, dp832 = running
    assert wait_until(lambda: any("voltage_set" in r[2] for r in emitter.rows))
    dp832.volt[1] = 7.5
    assert wait_until(
        lambda: "ch1 voltage_set changed 5 to 7.5 (external)" in emitter.messages(), timeout_s=5
    )
    ask(worker, "set", channel=1, voltage=9.0)
    assert wait_until(
        lambda: any(r[2].get("voltage_set") == 9.0 and "mode" in r[2] for r in emitter.rows),
        timeout_s=5,
    )
    assert not any("changed 7.5 to 9" in m for m in emitter.messages())


def test_mode_and_protection_changes_are_logged(running) -> None:
    worker, link, emitter, dp832 = running
    assert wait_until(lambda: any("mode" in r[2] for r in emitter.rows))
    dp832.load[1] = 1.0
    assert wait_until(lambda: "ch1 mode cv -> cc" in emitter.messages(), timeout_s=5)
    dp832.ovp_tripped[1] = True
    assert wait_until(
        lambda: ("warn", "ch1 protection none -> ovp", "instruments") in emitter.notes, timeout_s=5
    )
    assert ask(worker, "clear", channel=1) == {"ok": True, "errors_cleared": 0}
    assert dp832.ovp_tripped[1] is False


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


def test_instrument_errors_in_the_queue_are_logged_and_counted(running) -> None:
    worker, link, emitter, dp832 = running
    dp832.errors.append('-350,"Queue overflow"')
    assert wait_until(
        lambda: ("warn", "instrument error -350,Queue overflow", "instrument") in emitter.notes,
        timeout_s=5,
    )
    state = ask(worker, "state")
    assert state["counters"]["instrument_errors"] == 1
    assert state["counters"]["last_error"] == "-350,Queue overflow"
    assert "Last instrument error: -350,Queue overflow." in worker.health


def test_state_report_has_identity_profile_channels_and_ranges(running) -> None:
    worker, link, emitter, dp832 = running
    state = ask(worker, "state")
    assert state["identity"] == "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
    assert state["profile"]["id"] == "rigol-dp800" and state["profile"]["badge"] == "manual"
    assert state["channels"]["ch3"]["ranges"]["voltage"]["max"] == 5.0
    assert state["channels"]["ch1"]["ranges"]["voltage"]["max"] == 30.0
    assert state["counters"]["target_hz"] == 50
    assert state["recent"][-1]["command"]


def test_three_timeouts_in_a_row_reconnect_and_log_the_gap(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    links: list[ScriptedLink] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        link = ScriptedLink(dp832, address.where)
        links.append(link)
        return link

    worker, _ = start(dp800, dp832, emitter, opener=opener)
    try:
        assert wait_until(lambda: worker.state == "open")
        dp832.silent = True
        assert wait_until(lambda: worker.state != "open", timeout_s=5)
        assert links[0].closed
        assert any("3 queries in a row got no reply" in m for m in emitter.messages())
        assert emitter.statuses[-1]["connected"] is False
        dp832.silent = False
        assert wait_until(lambda: worker.state == "open" and len(links) >= 2, timeout_s=10)
        assert any(m.startswith("connected to") for m in emitter.messages()[1:])
    finally:
        worker.stop()
        worker.join(2)


def test_a_refused_open_sets_health_with_advice_and_keeps_trying(dp800: Profile) -> None:
    emitter = RecordingEmitter()
    attempts = 0

    def opener(address: Any, settings: Any) -> ScriptedLink:
        nonlocal attempts
        attempts += 1
        raise LinkError("connection refused.", "refused")

    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener)
    worker.start()
    try:
        assert wait_until(lambda: attempts >= 2, timeout_s=5)
        assert worker.state == "failed"
        assert worker.health == (
            "Cannot open 127.0.0.1:5555: connection refused. Nothing answers on that port; "
            "check the host and port, and that the instrument's LAN socket is on. Retrying."
        )
        assert ask(worker, "read", channel=1) == {"error": worker.health}
    finally:
        worker.stop()
        worker.join(2)


def test_release_stops_polling_and_acquire_reopens(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    links: list[ScriptedLink] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        link = ScriptedLink(dp832, address.where)
        links.append(link)
        return link

    worker, _ = start(dp800, dp832, emitter, opener=opener)
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "release") == {"ok": True}
        assert worker.state == "released" and links[0].closed
        before = len(dp832.calls)
        assert ask(worker, "read", channel=1)["error"].startswith("Released")
        assert len(dp832.calls) == before
        assert ask(worker, "acquire") == {"ok": True}
        assert worker.state == "open" and len(links) == 2
        assert ask(worker, "read", channel=1)["voltage"] == 5.0
    finally:
        worker.stop()
        worker.join(2)


def test_auto_profile_matches_the_identity(dp832: Dp832, profiles: list[Profile]) -> None:
    emitter = RecordingEmitter()
    link = ScriptedLink(dp832)
    cfg = instrument_config(profile="auto", rate_hz=50)
    worker = InstrumentWorker(cfg, profiles, emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert worker.profile is not None and worker.profile.id == "rigol-dp800"
    finally:
        worker.stop()
        worker.join(2)


def test_auto_profile_with_no_match_fails_with_advice(profiles: list[Profile]) -> None:
    emitter = RecordingEmitter()
    link = ScriptedLink(lambda c: "ACME,PSU-1,1,1" if c == "*IDN?" else None)
    cfg = instrument_config(profile="auto", rate_hz=50)
    worker = InstrumentWorker(cfg, profiles, emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: "no profile matches ACME PSU-1" in worker.health, timeout_s=5)
    finally:
        worker.stop()
        worker.join(2)


def test_unknown_profile_id_fails_at_once(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    worker, _ = start(dp800, dp832, emitter, profile="nope")
    try:
        assert wait_until(lambda: worker.state == "failed")
        assert worker.health == "No profile is named nope. Pick one from the list, or auto."
    finally:
        worker.stop()
        worker.join(2)


GENERIC = {
    "schema": 1,
    "id": "probe",
    "name": "Probe",
    "category": "psu",
    "generic": True,
    "evidence": {"badge": "manual", "source": "SCPI-99", "date": "2026-10-20"},
    "errors": {"query": "SYST:ERR?"},
    "ranges": {"voltage": {"max_query": "VOLT? MAX", "digits": 3}},
    "signals": {
        "voltage": {"query": "MEAS:VOLT?", "parse": "float", "optional": True},
        "power": {"query": "MEAS:POW?", "parse": "float", "optional": True},
    },
    "state": {"voltage_set": {"query": "VOLT?", "parse": "float", "optional": True}},
    "setters": {"voltage": {"command": "VOLT {value}"}},
}


def test_generic_profile_probes_and_drops_what_the_instrument_refuses() -> None:
    errors: list[str] = []

    def unit(command: str) -> str | None:
        if command == "*IDN?":
            return "ACME,PSU-1,1,1"
        if command == "SYST:ERR?":
            return errors.pop(0) if errors else '0,"No error"'
        if command == "MEAS:VOLT?":
            return "3.300"
        if command == "VOLT?":
            return "3.300"
        if command == "VOLT? MAX":
            return "20.000"
        if command.startswith("VOLT "):
            return None
        errors.append('-113,"Undefined header"')
        return None

    emitter = RecordingEmitter()
    link = ScriptedLink(unit)
    cfg = instrument_config(profile="probe", rate_hz=50)
    worker = InstrumentWorker(
        cfg, [from_dict(GENERIC, "probe.toml")], emitter, settings_for(cfg), open=opener_for(link)
    )
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert any(
            "not answered by this instrument, so not recorded: power" in m
            for m in emitter.messages()
        )
        # A field the instrument refuses is never registered, so it is no empty signal.
        assert "power" not in emitter.channel_fields["psu"][1]
        assert "voltage" in emitter.channel_fields["psu"][1]
        assert wait_until(lambda: any(r[2] == {"voltage": 3.3} for r in emitter.rows))
        assert ask(worker, "set", channel=1, voltage=25.0) == {
            "error": "voltage must be from 0 to 20"
        }
        assert ask(worker, "set", channel=1, voltage=3.3)["voltage"]["readback"] == 3.3
    finally:
        worker.stop()
        worker.join(2)


def test_one_silent_reply_while_probing_does_not_cost_the_field() -> None:
    """A field dropped at connect is never registered, so its values were lost for good."""
    silent_once = ["MEAS:VOLT?"]

    def unit(command: str) -> str | None:
        if command in silent_once:
            silent_once.remove(command)
            raise LinkTimeout("no reply")
        if command == "*IDN?":
            return "ACME,PSU-1,1,1"
        if command == "SYST:ERR?":
            return '0,"No error"'
        if command in ("MEAS:VOLT?", "VOLT?"):
            return "3.300"
        if command == "MEAS:POW?":
            return "1.0"
        if command == "VOLT? MAX":
            return "20.000"
        return None

    emitter = RecordingEmitter()
    cfg = instrument_config(profile="probe", rate_hz=50)
    worker = InstrumentWorker(
        cfg,
        [from_dict(GENERIC, "probe.toml")],
        emitter,
        settings_for(cfg),
        open=opener_for(ScriptedLink(unit)),
    )
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert not silent_once
        assert "voltage" in emitter.channel_fields["psu"][1]
    finally:
        worker.stop()
        worker.join(2)


def test_poll_table_rows_are_read_and_recorded(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    original = dp832.__call__

    def with_temp(command: str) -> str | None:
        if command == "SYST:TEMP?":
            return "41.5"
        return original(command)

    link = ScriptedLink(with_temp)
    cfg = instrument_config(
        rate_hz=50,
        advanced={
            "poll": [{"name": "temp", "query": "SYST:TEMP?", "unit": "degC", "every_s": 0.1}]
        },
    )
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: len(emitter.polls) >= 2, timeout_s=5)
        assert emitter.polls[0][:3] == ("psu", "temp", 41.5)
    finally:
        worker.stop()
        worker.join(2)


def test_stop_ends_the_thread_and_fails_pending_requests(running) -> None:
    worker, link, emitter, dp832 = running
    future = worker.request("state")
    worker.stop()
    worker.join(2)
    assert not worker.is_alive() and worker.state == "stopped"
    result = future.result(timeout=1)
    assert result.get("state") == "open" or result == {"error": "the extension is stopping"}


def test_a_dmm_routes_readings_to_the_active_function() -> None:
    dmm = from_dict(
        {
            "schema": 1,
            "id": "meter",
            "name": "Meter",
            "category": "dmm",
            "evidence": {"badge": "manual", "source": "x", "date": "2026-10-20"},
            "match": {"manufacturer": "ACME", "model": "DMM"},
            "signals": {"value": {"query": "READ?", "parse": "float"}},
            "state": {
                "function": {
                    "query": "FUNC?",
                    "parse": "map",
                    "map": {'"VOLT"': "dc_voltage", '"CURR"': "dc_current"},
                }
            },
            "setters": {
                "function": {
                    "command": "CONF:{function}",
                    "map": {"dc_voltage": "VOLT", "dc_current": "CURR"},
                }
            },
            "dmm": {
                "function_query": "FUNC?",
                "function_map": {'"VOLT"': "dc_voltage", '"CURR"': "dc_current"},
                "functions": {"dc_voltage": "V", "dc_current": "A"},
            },
        },
        "meter.toml",
    )
    function = ['"VOLT"']

    def meter(command: str) -> str | None:
        if command == "*IDN?":
            return "ACME,DMM,1,1"
        if command == "READ?":
            return "+1.23456E+00"
        if command == "FUNC?":
            return function[0]
        if command.startswith("CONF:"):
            function[0] = '"CURR"' if command.endswith("CURR") else '"VOLT"'
            return None
        return None

    emitter = RecordingEmitter()
    link = ScriptedLink(meter)
    cfg = instrument_config(profile="meter", rate_hz=50)
    worker = InstrumentWorker(cfg, [dmm], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: any(r[1] == "dc_voltage" for r in emitter.readings))
        assert ask(worker, "set", channel=1, function="dc_current") == {"function": "dc_current"}
        assert wait_until(lambda: any(r[1] == "dc_current" for r in emitter.readings), timeout_s=5)
        assert ask(worker, "read", channel=1) == {"value": 1.23456, "function": "dc_current"}
    finally:
        worker.stop()
        worker.join(2)


def test_select_style_sends_the_select_only_when_the_channel_changes(dp800: Profile) -> None:
    raw = {
        "schema": 1,
        "id": "sel",
        "name": "Select",
        "category": "psu",
        "evidence": {"badge": "manual", "source": "x", "date": "2026-10-20"},
        "match": {"manufacturer": "ACME", "model": "SEL", "channels": 2},
        "link": {"select": "INST:NSEL {ch}"},
        "ranges": {"voltage": {"max": 30}},
        "signals": {"voltage": {"query": "MEAS:VOLT?", "parse": "float"}},
        "state": {"voltage_set": {"query": "VOLT?", "parse": "float"}},
        "setters": {"voltage": {"command": "VOLT {value}"}},
    }
    profile = from_dict(raw, "sel.toml")
    selected = [1]
    volts = {1: 1.0, 2: 2.0}
    sent: list[str] = []

    def unit(command: str) -> str | None:
        sent.append(command)
        if command == "*IDN?":
            return "ACME,SEL,1,1"
        if command.startswith("INST:NSEL"):
            selected[0] = int(command.split()[1])
            return None
        if command == "MEAS:VOLT?" or command == "VOLT?":
            return f"{volts[selected[0]]:.3f}"
        if command.startswith("VOLT "):
            volts[selected[0]] = float(command.split()[1])
            return None
        return None

    emitter = RecordingEmitter()
    link = ScriptedLink(unit)
    cfg = instrument_config(profile="sel", rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(
            lambda: any(r[1] == 2 and r[2].get("voltage") == 2.0 for r in emitter.rows)
        )
        assert ask(worker, "set", channel=2, voltage=4.0)["voltage"]["readback"] == 4.0
        assert volts == {1: 1.0, 2: 4.0}
        selects = [c for c in sent if c.startswith("INST:NSEL")]
        assert selects and len(selects) < len(sent) / 2
    finally:
        worker.stop()
        worker.join(2)


def test_ack_profiles_read_the_reply_to_every_command() -> None:
    raw = {
        "schema": 1,
        "id": "ack",
        "name": "Ack",
        "category": "psu",
        "evidence": {"badge": "manual", "source": "x", "date": "2026-10-20"},
        "match": {"manufacturer": "LAMBDA", "model": "GEN", "idn": "IDN?"},
        "link": {"ack": "OK", "write_terminator": "\r", "read_terminator": "\r"},
        "ranges": {"voltage": {"max": 60}},
        "signals": {"voltage": {"query": "MV?", "parse": "float"}},
        "state": {"voltage_set": {"query": "PV?", "parse": "float"}},
        "setters": {"voltage": {"command": "PV {value}"}},
    }
    profile = from_dict(raw, "ack.toml")
    volts = [10.0]

    def unit(command: str) -> str | None:
        if command == "IDN?":
            return "LAMBDA,GEN60-12.5"
        if command in ("MV?", "PV?"):
            return f"{volts[0]:.2f}"
        if command.startswith("PV "):
            volts[0] = float(command.split()[1])
            return "OK"
        return "C01"

    emitter = RecordingEmitter()
    link = ScriptedLink(unit)
    cfg = instrument_config(profile="ack", rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "set", channel=1, voltage=12.0)["voltage"]["readback"] == 12.0
        assert ask(worker, "scpi", command="BAD")["error"].endswith(" answered 'C01' to BAD")
    finally:
        worker.stop()
        worker.join(2)


def test_requests_from_several_threads_are_served_in_order(running) -> None:
    worker, link, emitter, dp832 = running
    results: list[float] = []

    def setter(value: float) -> None:
        results.append(ask(worker, "set", channel=1, voltage=value)["voltage"]["readback"])

    threads = [threading.Thread(target=setter, args=(v,)) for v in (1.0, 2.0, 3.0, 4.0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.perf
def test_cadence_holds_at_the_target_rate(dp800: Profile, dp832: Dp832) -> None:
    emitter = RecordingEmitter()
    worker, _ = start(dp800, dp832, emitter, rate_hz=20)
    try:
        assert wait_until(
            lambda: len([r for r in emitter.rows if r[1] == 1 and "voltage" in r[2]]) >= 40,
            timeout_s=5,
        )
        times = [r[3] for r in emitter.rows if r[1] == 1 and "voltage" in r[2]][:40]
        gaps = sorted(b - a for a, b in zip(times, times[1:], strict=False))
        p99 = gaps[int(len(gaps) * 0.99) - 1]
        assert p99 < 2 * 50_000_000
    finally:
        worker.stop()
        worker.join(2)


def test_load_file_profile_path_in_config(dp832: Dp832, tmp_path: Any) -> None:
    path = tmp_path / "mine.toml"
    path.write_text(
        (load_file.__globals__["SHIPPED_DIR"] / "psu" / "rigol-dp800.toml")
        .read_text(encoding="utf-8")
        .replace('id = "rigol-dp800"', 'id = "mine"'),
        encoding="utf-8",
    )
    emitter = RecordingEmitter()
    link = ScriptedLink(dp832)
    cfg = instrument_config(profile=str(path), rate_hz=50)
    worker = InstrumentWorker(cfg, [], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert worker.profile is not None and worker.profile.id == "mine"
    finally:
        worker.stop()
        worker.join(2)


def test_a_reply_out_of_step_is_detected_and_the_link_resynchronized(
    dp800: Profile, dp832: Dp832
) -> None:
    """A command the instrument answers leaves every later reply one behind, as a scope did
    when a query carried a parameter after its `?`."""
    original = dp832.__call__

    def chatty(command: str) -> str | None:
        if command == "ECHO":
            return "ECHOED"
        return original(command)

    emitter = RecordingEmitter()
    link = ScriptedLink(chatty)
    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open")
        assert ask(worker, "scpi", command="ECHO") == {"ok": True}
        assert any("replies were out of step" in m for m in emitter.messages())
        assert ask(worker, "scpi", command="*IDN?")["reply"].startswith("RIGOL")
        assert ask(worker, "read", channel=1)["voltage"] == 5.0
        assert ask(worker, "state")["counters"]["instrument_errors"] == 0
        assert ask(worker, "scpi", command=":SOUR1:VOLT? MAX")["reply"] == "5.000"
    finally:
        worker.stop()
        worker.join(2)


@pytest.mark.parametrize("profile", ["rigol-dp800", "auto"])
def test_a_connection_the_instrument_accepts_but_never_answers_is_a_failed_open(
    dp800: Profile, profile: str
) -> None:
    """A SCPI socket held by another client accepts the connection and says nothing."""
    emitter = RecordingEmitter()
    links: list[ScriptedLink] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        link = ScriptedLink(lambda command: (_ for _ in ()).throw(LinkTimeout("no reply")))
        links.append(link)
        return link

    cfg = instrument_config(rate_hz=50, profile=profile)
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener)
    worker.start()
    try:
        assert wait_until(lambda: len(links) >= 2, timeout_s=5)
        assert worker.state == "failed"
        dialect = (
            "; an instrument that speaks a dialect without it needs its profile picked"
            if profile == "auto"
            else ""
        )
        assert worker.health == (
            "Cannot open 127.0.0.1:5555: accepted the connection but did not answer "
            f"*IDN?{dialect}. "
            "Instruments serve one client on their SCPI socket: close the other program that "
            "uses it, or power-cycle the instrument. Retrying."
        )
        assert links[0].closed
        assert not any(m.startswith("connected to") for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(2)


def test_per_channel_poll_rows_get_a_field_per_channel_and_join_read(
    dp800: Profile, dp832: Dp832
) -> None:
    emitter = RecordingEmitter()
    original = dp832.__call__

    def with_temp(command: str) -> str | None:
        if command.startswith("SYST:TEMP? CH"):
            return f"4{command[-1]}.5"
        return original(command)

    link = ScriptedLink(with_temp)
    cfg = instrument_config(
        rate_hz=50,
        advanced={
            "poll": [{"name": "temp", "query": "SYST:TEMP? CH{ch}", "unit": "degC", "every_s": 0.1}]
        },
    )
    worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: len(emitter.polls) >= 3, timeout_s=5)
        assert {(p[1], p[2]) for p in emitter.polls[:3]} == {
            ("temp_ch1", 41.5),
            ("temp_ch2", 42.5),
            ("temp_ch3", 43.5),
        }
        reading = ask(worker, "read", channel=2)
        assert reading["temp_ch2"] == 42.5
        assert "temp_ch1" not in reading
        assert worker.health.startswith(
            "Connected to RIGOL TECHNOLOGIES DP832A. Reading 13 signals and 1 extra readings on "
            "3 channels"
        )
    finally:
        worker.stop()
        worker.join(2)


def test_the_slow_round_runs_between_fast_readings_without_delaying_them(
    dp800: Profile, dp832: Dp832
) -> None:
    """The slow round ran whole before a due fast pass, holding that reading back for its whole
    length. A round longer than the interval must now be split between fast passes."""

    def slow_instrument(command: str) -> str | None:
        time.sleep(0.008)
        return dp832(command)

    emitter = RecordingEmitter()
    worker, link = start(dp800, slow_instrument, emitter, rate_hz=10)
    try:
        assert wait_until(lambda: worker.state == "open")
        assert worker._plan is not None
        fast = {step.query for step in worker._plan.fast}
        slow = {step.query for step in worker._plan.slow}
        # 33 settings queries take about 270 ms: more than two intervals at 10 Hz.
        assert len(slow) * 0.008 > 0.2
        # The first round runs whole before the first reading, so a meter knows its function.
        assert wait_until(lambda: any("voltage" in v for _, _, v, _ in emitter.rows))
        settled = len(emitter.rows)
        # Later rounds yield to the fast schedule but always finish: every channel's settings.
        assert wait_until(
            lambda: (
                {ch for _, ch, v, _ in emitter.rows[settled:] if "voltage_set" in v} == {1, 2, 3}
            ),
            timeout_s=10,
        )
        sent = list(link.commands)
    finally:
        worker.stop()
        worker.join(2)
    after_first = sent[next(i for i, command in enumerate(sent) if command in fast) :]
    runs: list[int] = []
    run = 0
    for command in after_first:
        if command in fast:
            runs.append(run)
            run = 0
        elif command in slow:
            run += 1
    runs.append(run)
    assert max(runs) < len(slow), f"{max(runs)} settings queries in one gap"


def test_a_slot_scan_an_action_overtook_never_overwrites_its_result(
    profiles: list[Profile],
) -> None:
    """A slow round scans slots between fast passes; a Measure in between must survive it."""
    scope = next(p for p in profiles if p.id == "siglent-sds")
    fake = FakeInstrument(scope)
    link = ScriptedLink(fake.handle, "scope")
    cfg = instrument_config(name="scope", profile=scope.id)
    worker = InstrumentWorker(
        cfg, [scope], RecordingEmitter(), settings_for(cfg), open=opener_for(link)
    )
    assert worker._connect()
    assert worker._plan is not None
    round_ = worker._slow_round()
    for _ in range(len(worker._plan.slow) + 2):
        next(round_)
    assert link.commands[-1] == ":MEASure:ADVanced:P1?"
    shown = scope_ops.measure(worker, 2, {"type": "frequency"})
    for _ in round_:
        pass
    assert worker._slots[shown["slot"]] == (2, "frequency")


def test_a_slot_turned_off_during_a_slot_scan_stays_off(profiles: list[Profile]) -> None:
    scope = next(p for p in profiles if p.id == "siglent-sds")
    fake = FakeInstrument(scope)
    link = ScriptedLink(fake.handle, "scope")
    cfg = instrument_config(name="scope", profile=scope.id)
    worker = InstrumentWorker(
        cfg, [scope], RecordingEmitter(), settings_for(cfg), open=opener_for(link)
    )
    assert worker._connect()
    assert worker._plan is not None
    shown = scope_ops.measure(worker, 2, {"type": "frequency"})
    round_ = worker._slow_round()
    # Step the round until its slot scan has read this slot as on.
    enabled = f":MEASure:ADVanced:P{shown['slot']}?"
    while enabled not in link.commands[-3:]:
        next(round_)
    scope_ops.measure(worker, 2, {"type": "frequency", "slot": shown["slot"], "off": True})
    for _ in round_:
        pass
    assert shown["slot"] not in worker._slots


@pytest.mark.parametrize("profile_choice", ["auto", "rohde-schwarz-hmp"])
def test_a_channel_command_selects_its_channel_whichever_way_the_profile_was_chosen(
    profile_choice: str,
) -> None:
    """Under auto the identity is asked with a generic profile that selects nothing; the matched
    profile's select must still go before every channel command."""
    profile = by_id(shipped(), "rohde-schwarz-hmp")
    assert profile is not None
    fake = FakeInstrument(profile, "HMP4040")
    link = ScriptedLink(fake.handle, "hmp")
    cfg = instrument_config(name="psu", profile=profile_choice, rate_hz=50)
    worker = InstrumentWorker(
        cfg, shipped(), RecordingEmitter(), settings_for(cfg), open=opener_for(link)
    )
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        assert worker.profile is not None and worker.profile.id == "rohde-schwarz-hmp"
        assert ask(worker, "output", channel=3, on=True) == {"output": True}
        states = []
        for ch in (1, 2, 3, 4):
            fake.handle(f"INST:NSEL {ch}")
            states.append(fake.handle("OUTP?"))
        assert states == ["0", "0", "1", "0"]
    finally:
        worker.stop()
        worker.join(2)


def test_requests_between_passes_keep_the_reading_schedule(dp800: Profile, dp832: Dp832) -> None:
    """Twenty requests in one second at 1 Hz each ran a pass and pushed the next one an interval
    later, so the readings stopped for about twenty seconds."""
    worker, link = start(dp800, dp832, RecordingEmitter(), rate_hz=1)
    try:
        assert wait_until(lambda: worker.state == "open")
        passes = lambda: sum(c.startswith(":MEAS:ALL?") for c in link.commands)  # noqa: E731
        before = passes()
        for _ in range(20):
            ask(worker, "state")
            time.sleep(0.05)
        during = passes() - before
        assert during <= 2 * worker.channels, during
        # The schedule holds: the next pass comes within about one interval.
        assert wait_until(lambda: passes() - before > during, timeout_s=2.5)
    finally:
        worker.stop()
        worker.join(2)


def test_stopping_answers_every_pending_request_even_after_one_was_abandoned(
    dp800: Profile,
) -> None:
    """A future the action cancelled took set_result and raised, so the requests queued behind
    it went unanswered."""
    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(
        cfg, [dp800], RecordingEmitter(), settings_for(cfg), open=opener_for(ScriptedLink(Dp832()))
    )
    abandoned = worker.request("state")
    abandoned.cancel()
    waiting = worker.request("state")
    worker._fail_pending("the extension is stopping")
    assert waiting.result(timeout=0) == {"error": "the extension is stopping"}
