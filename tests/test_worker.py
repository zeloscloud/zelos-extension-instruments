"""The instrument worker against a scripted link: connect, poll, schedule, reconnect, hand back."""

import dataclasses
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import (
    Dp832,
    RecordingEmitter,
    ScriptedLink,
    ask,
    connected,
    instrument_config,
    minimal,
    opener_for,
    profile_from,
    start,
    start_open,
    wait_until,
)
from tests.test_serve import Echo, generator, served
from zelos_extension_instruments.clock import NS_PER_S
from zelos_extension_instruments.link import LinkError, LinkTimeout
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import from_dict, load_file, shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker


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


@pytest.mark.usefixtures("fast_schedule")
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
    assert off["mode"] == "off" and off["output"] is False


@pytest.mark.usefixtures("fast_schedule")
def test_timestamps_increase_and_status_reports_rate(running) -> None:
    worker, link, emitter, dp832 = running
    assert wait_until(lambda: len(emitter.statuses) >= 2, timeout_s=5)
    times = [r[3] for r in emitter.rows if r[1] == 1 and "voltage" in r[2]]
    assert times == sorted(times)
    status = emitter.statuses[-1]
    assert status["connected"] is True and status["rate_hz"] > 0 and status["errors"] == 0
    assert math.isfinite(status["rtt_ms"])


@pytest.mark.usefixtures("fast_schedule")
def test_external_setpoint_change_is_logged_but_our_own_is_not(
    dp800: Profile, dp832: Dp832
) -> None:
    """After our own set the instrument reads back one stale setpoint, then the new one: both
    reads differ from the stored value, yet neither is an outside change."""
    original = dp832.__call__
    after_set = {"armed": False, "reads": 0}

    def lagging(command: str) -> str | None:
        reply = original(command)
        if command == ":SOUR1:VOLT 9.000":
            after_set["armed"] = True
        elif command == ":SOUR1:VOLT?" and after_set["armed"]:
            after_set["reads"] += 1
            # The first read is the set's own readback; the next poll sees the old setpoint.
            if after_set["reads"] == 2:
                return "7.500"
        return reply

    worker, emitter = start_open(dp800, ScriptedLink(lagging))
    try:
        assert wait_until(lambda: any("voltage_set" in r[2] for r in emitter.rows))
        dp832.volt[1] = 7.5
        assert wait_until(
            lambda: "ch1 voltage_set changed 5 to 7.5 (external)" in emitter.messages(),
            timeout_s=5,
        )
        ask(worker, "set", channel=1, voltage=9.0)
        assert wait_until(
            lambda: any(r[2].get("voltage_set") == 9.0 and "mode" in r[2] for r in emitter.rows),
            timeout_s=5,
        )
        assert after_set["reads"] >= 3
        assert not any(
            "changed 9 to 7.5" in m or "changed 7.5 to 9" in m for m in emitter.messages()
        )
    finally:
        worker.stop()
        worker.join(2)


@pytest.mark.usefixtures("fast_schedule")
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


@pytest.mark.usefixtures("fast_schedule")
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


@pytest.mark.usefixtures("fast_schedule")
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


@pytest.mark.usefixtures("fast_schedule")
def test_a_refused_open_sets_health_with_advice_and_keeps_trying(dp800: Profile) -> None:
    emitter = RecordingEmitter()
    attempts = 0

    def opener(address: Any, settings: Any) -> ScriptedLink:
        nonlocal attempts
        attempts += 1
        raise LinkError("connection refused.", "refused")

    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(cfg, [dp800], emitter, open=opener)
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
    worker = InstrumentWorker(cfg, profiles, emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, profiles, emitter, open=opener_for(link))
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
        cfg, [from_dict(GENERIC, "probe.toml")], emitter, open=opener_for(link)
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


def test_an_error_a_range_query_raised_does_not_cost_an_optional_field() -> None:
    errors: list[str] = []

    def unit(command: str) -> str | None:
        if command == "*IDN?":
            return "ACME,PSU-1,1,1"
        if command == "SYST:ERR?":
            return errors.pop(0) if errors else '0,"No error"'
        if command == "VOLT? MAX":
            errors.append('-222,"Data out of range"')
            return "20.000"
        if command in ("MEAS:VOLT?", "MEAS:POW?", "VOLT?"):
            return "3.300"
        return None

    worker, _, _ = connected(from_dict(GENERIC, "probe.toml"), unit, profile="probe")
    assert worker._dropped == set()
    assert worker._channel[1].ranges["voltage"].max == 20.0


def test_one_silent_reply_while_probing_does_not_cost_the_field() -> None:
    """An event cannot gain a field later, so a field dropped at connect is gone for good."""
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
    worker = InstrumentWorker(cfg, [dp800], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [dmm], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [dp800], emitter, open=opener_for(link))
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


@pytest.mark.usefixtures("fast_schedule")
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
    worker = InstrumentWorker(cfg, [dp800], emitter, open=opener)
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
    worker = InstrumentWorker(cfg, [dp800], emitter, open=opener_for(link))
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
    worker = InstrumentWorker(cfg, shipped(), RecordingEmitter(), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        assert worker.profile is not None and worker.profile.id == "rohde-schwarz-hmp"
        assert ask(worker, "set", channel=3, output=True) == {"output": True}
        states = []
        for ch in (1, 2, 3, 4):
            fake.handle(f"INST:NSEL {ch}")
            states.append(fake.handle("OUTP?"))
        assert states == ["0", "0", "1", "0"]
    finally:
        worker.stop()
        worker.join(2)


def test_requests_between_passes_keep_the_reading_schedule(dp800: Profile, dp832: Dp832) -> None:
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
    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(
        cfg, [dp800], RecordingEmitter(), open=opener_for(ScriptedLink(Dp832()))
    )
    abandoned = worker.request("state")
    abandoned.cancel()
    waiting = worker.request("state")
    worker._fail_pending("the extension is stopping")
    assert waiting.result(timeout=0) == {"error": "the extension is stopping"}


def test_a_request_the_action_gave_up_on_never_reaches_the_instrument(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, emitter = start_open(dp800, link)
    try:
        future = worker.request("set", channel=1, voltage=4.2)
        future.cancel()
        future2 = worker.request("read", channel=1)
        assert "voltage" in future2.result(timeout=5)
        assert ":SOUR1:VOLT 4.200" not in link.commands
        assert any("dropped" in m for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(2)


def test_a_silent_query_in_a_request_fails_the_request_and_keeps_the_link(
    dp800: Profile, dp832: Dp832
) -> None:
    original = dp832.__call__

    def mostly(command: str) -> str | None:
        if command == "NOPE?":
            raise LinkTimeout("no reply")
        return original(command)

    link = ScriptedLink(mostly)
    worker, emitter = start_open(dp800, link)
    try:
        result = ask(worker, "scpi", command="NOPE?")
        assert "no reply" in result["error"]
        recorded = len(emitter.rows)
        assert wait_until(lambda: len(emitter.rows) > recorded + 6, timeout_s=5)
        assert worker.state == "open"
        assert not any("disconnected" in m for m in emitter.messages())
        assert ask(worker, "scpi", command="*IDN?")["reply"].startswith("RIGOL")
    finally:
        worker.stop()
        worker.join(2)


def test_a_lost_cycle_waits_the_backoff_before_reconnecting(dp800: Profile) -> None:
    opened: list[float] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        opened.append(time.monotonic())
        dp832 = Dp832()
        original = dp832.__call__

        def broken(command: str) -> str | None:
            if command.startswith(":MEAS"):
                raise LinkError("the connection was lost", "other")
            return original(command)

        return ScriptedLink(broken)

    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(cfg, [dp800], RecordingEmitter(), open=opener)
    worker.start()
    try:
        assert wait_until(lambda: len(opened) >= 3, timeout_s=15), opened
        gaps = [b - a for a, b in zip(opened, opened[1:], strict=False)]
        # Never sooner than the first backoff, and each loss waits longer than the one before.
        assert gaps[0] >= 0.5 and gaps[1] > gaps[0], gaps
    finally:
        worker.stop()
        worker.join(2)


def test_readings_are_stamped_at_their_own_query(dp800: Profile, dp832: Dp832) -> None:
    """A slow request that arrives between two channels' queries waits for the end of the pass,
    so it moves neither reading in time."""
    original = dp832.__call__
    armed = threading.Event()
    asked: list[Any] = []

    def slow_set(command: str) -> str | None:
        reply = original(command)
        if command == ":MEAS:ALL? CH1" and armed.is_set() and not asked:
            asked.append(worker.request("set", channel=2, voltage=1.0))
        if command.startswith(":SOUR2:VOLT ") and "?" not in command:
            time.sleep(0.4)
        return reply

    link = ScriptedLink(slow_set)
    worker, emitter = start_open(dp800, link)
    try:
        fast = lambda: [r for r in emitter.rows if "voltage" in r[2]]  # noqa: E731
        assert wait_until(lambda: len(fast()) >= 9, timeout_s=5)
        armed.set()
        assert wait_until(lambda: bool(asked), timeout_s=5)
        assert asked[0].result(timeout=5)["voltage"]["readback"] == 1.0
        recorded = len(fast())
        assert wait_until(lambda: len(fast()) >= recorded + 9, timeout_s=5)
        rows = fast()
        rows = rows[next(i for i, r in enumerate(rows) if r[1] == 1) :]
        cycles = [rows[i : i + 3] for i in range(0, len(rows) - 2, 3)]
        assert all([r[1] for r in cycle] == [1, 2, 3] for cycle in cycles)
        inside = [c[i + 1][3] - c[i][3] for c in cycles for i in range(2)]
        between = [b[0][3] - a[2][3] for a, b in zip(cycles, cycles[1:], strict=False)]
        # The 400 ms set shows once as a gap between passes, never inside one.
        assert max(inside) < max(between)
    finally:
        worker.stop()
        worker.join(2)


def test_the_panel_is_handed_back_on_the_worker_thread(dp800: Profile, dp832: Dp832) -> None:
    writers: list[str] = []
    original = dp832.__call__

    def noting(command: str) -> str | None:
        if command == "SYST:LOCAL":
            writers.append(threading.current_thread().name)
        return original(command)

    link = ScriptedLink(noting)
    worker, _ = start_open(dp800, link, advanced={"keep_panel": False})
    worker.stop()
    worker.join(3)
    assert writers and all(w == worker.name for w in writers)


@pytest.mark.usefixtures("fast_schedule")
def test_keep_panel_hands_the_panel_back_every_slow_round(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link)
    try:
        assert wait_until(lambda: link.commands.count("SYST:LOCAL") >= 1)
    finally:
        worker.stop()
        worker.join(2)


def test_the_link_is_flushed_and_the_error_queue_drained_before_the_first_poll(
    dp800: Profile, dp832: Dp832
) -> None:
    """Bytes a banner or another client left, and errors another session queued, are not ours."""
    dp832.errors.append('-113,"Undefined header"')
    dp832.errors.append('-222,"Data out of range"')
    link = ScriptedLink(dp832)
    worker, emitter = start_open(dp800, link)
    try:
        assert link.cleared >= 1
        assert not dp832.errors
        state = ask(worker, "state")
        assert state["counters"]["instrument_errors"] == 2
        assert not any("instrument error" in m for m in emitter.messages())
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
    worker, emitter = start_open(dp800, link)
    try:
        assert wait_until(
            lambda: any("replies were out of step" in m for m in emitter.messages()), timeout_s=5
        )
        ch2 = lambda: [r for r in emitter.rows if r[1] == 2 and "voltage" in r[2]]  # noqa: E731
        assert wait_until(lambda: len(ch2()) >= 3, timeout_s=5)
        # Channel 2 is off: its voltage is 0, never channel 1's 5 V shifted down by one reply.
        rows = ch2()
        assert all(abs(r[2]["voltage"]) < 1e-6 for r in rows), rows[-3:]
        assert ask(worker, "state")["counters"]["timeouts"] == 1
    finally:
        worker.stop()
        worker.join(2)


def test_resync_accepts_only_the_identity(dp800: Profile, dp832: Dp832) -> None:
    """Decoys that arrive after the clear, ahead of the identity, are read past."""
    original = dp832.__call__
    decoys = {"armed": False}

    def with_decoys(command: str) -> str | None:
        if command == "NOPE?":
            decoys["armed"] = True
            raise LinkTimeout("no reply")
        if command == "*IDN?" and decoys["armed"]:
            decoys["armed"] = False
            link.push("5.000,1.000,5.000")
            link.push("another,comma,reply")
        return original(command)

    link = ScriptedLink(with_decoys)
    worker, emitter = start_open(dp800, link)
    try:
        # The silent query makes the worker resynchronize on its own thread.
        assert "no reply" in ask(worker, "scpi", command="NOPE?")["error"]
        assert wait_until(
            lambda: any("replies were out of step" in m for m in emitter.messages()), timeout_s=5
        )
        accepted = [reply for _, command, reply in list(worker._recent) if command == "*IDN?"]
        assert accepted[-1].startswith("RIGOL TECHNOLOGIES,DP832A")
    finally:
        worker.stop()
        worker.join(2)


@pytest.mark.usefixtures("fast_schedule")
def test_keep_panel_is_skipped_for_a_meter_that_needs_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    rounds: list[None] = []
    keep_panel = InstrumentWorker._keep_panel

    def counted(self: InstrumentWorker) -> None:
        rounds.append(None)
        keep_panel(self)

    monkeypatch.setattr(InstrumentWorker, "_keep_panel", counted)
    link = ScriptedLink(meter)
    emitter = RecordingEmitter()
    cfg = instrument_config(rate_hz=50, profile="agilent-34401a")
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        # Each slow round ends in a keep-panel pass: let several run.
        assert wait_until(lambda: len(rounds) >= 3, timeout_s=10)
        assert profile.link.remote in commands
        assert not any(command in commands for command in profile.link.local)
    finally:
        worker.stop()
        worker.join(2)


def test_stop_hands_the_panel_back(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = start_open(dp800, link, advanced={"keep_panel": False})
    worker.stop()
    worker.join(2)
    assert "SYST:LOCAL" in link.commands[-4:]


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
    worker, emitter = start_open(dp800, link)
    try:
        assert wait_until(
            lambda: any("replies were out of step" in m for m in emitter.messages()), timeout_s=5
        )
        after = link.commands[link.commands.index(":MEAS:ALL? CH1") + 1 :]
        assert after[after.index("SYST:REMOTE") + 1] == "*IDN?"
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
    worker, _ = start_open(profile, link)
    try:
        assert ask(worker, "local") == {"ok": True}
        first = link.commands.index("DEBug:KEY 1")
        assert link.commands[first + 1] == "SYSTem:KEY 8"
    finally:
        worker.stop()
        worker.join(2)
    # Stopping hands the panel back the same way.
    assert link.commands[-2:] == ["DEBug:KEY 1", "SYSTem:KEY 8"]


DP832_IDN = "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
LATE_READING = "5.0000,0.5000,2.500"


def assert_in_step(worker: InstrumentWorker) -> None:
    """The next query gets its own reply, not one left over from an earlier query."""
    assert worker._query("*IDN?") == DP832_IDN


@pytest.mark.parametrize(
    ("query", "recorded"),
    [(":MEAS:ALL? CH1", {}), (":OUTP:MODE? CH1", {"mode": "unknown"})],
)
def test_a_reply_that_does_not_parse_is_resynchronized_before_the_next_query(
    dp800: Profile, dp832: Dp832, query: str, recorded: dict[str, str]
) -> None:
    held: list[ScriptedLink] = []
    armed = {"on": False}

    def handler(command: str) -> str | None:
        if armed["on"] and command == query:
            armed["on"] = False
            held[0].push("garbled")
            held[0].push(LATE_READING)
            return None
        return dp832(command)

    worker, link, emitter = connected(dp800, handler)
    held.append(link)
    assert worker._plan is not None
    step = next(s for s in (*worker._plan.fast, *worker._plan.slow) if s.query == query)
    rows: dict[int | None, dict[str, Any]] = {}
    armed["on"] = True
    cleared = link.cleared
    worker._run_step(step, rows)
    assert rows.get(1, {}) == recorded
    assert link.cleared > cleared
    assert any("replies were out of step" in m and "garbled" in m for m in emitter.messages())
    assert_in_step(worker)


@pytest.mark.parametrize("fault", ["timeout", "parse"])
def test_a_poll_row_that_fails_is_resynchronized_and_read_again_next_time(
    dp800: Profile, dp832: Dp832, fault: str
) -> None:
    held: list[ScriptedLink] = []
    armed = {"on": False}

    def handler(command: str) -> str | None:
        if command != "SYST:TEMP?":
            return dp832(command)
        if not armed["on"]:
            return "41.5"
        armed["on"] = False
        if fault == "timeout":
            held[0].push("39.0")
            raise LinkTimeout("no reply")
        held[0].push("hot")
        held[0].push("39.0")
        return None

    poll = {"poll": [{"name": "temp", "query": "SYST:TEMP?", "unit": "degC", "every_s": 0.1}]}
    worker, link, emitter = connected(dp800, handler, advanced=poll)
    held.append(link)
    armed["on"] = True
    now = worker._now()
    worker._poll_table(now)
    assert emitter.polls == [] and worker._poll_values == {}
    assert any("replies were out of step" in m for m in emitter.messages())
    assert_in_step(worker)
    worker._poll_table(now + 10 * NS_PER_S)
    assert [p[:3] for p in emitter.polls] == [("psu", "temp", 41.5)]


@pytest.mark.parametrize("fault", ["timeout", "unexpected reply"])
def test_an_error_queue_that_misanswers_is_resynchronized_before_the_next_query(
    dp800: Profile, dp832: Dp832, fault: str
) -> None:
    held: list[ScriptedLink] = []
    armed = {"on": False}

    def handler(command: str) -> str | None:
        if not (armed["on"] and command == "SYST:ERR?"):
            return dp832(command)
        armed["on"] = False
        if fault == "timeout":
            held[0].push(LATE_READING)
            raise LinkTimeout("no reply")
        held[0].push(LATE_READING)
        held[0].push('0,"No error"')
        return None

    worker, link, emitter = connected(dp800, handler)
    held.append(link)
    armed["on"] = True
    assert worker._drain_errors() == 0
    assert worker._errors == 0
    assert any("replies were out of step" in m for m in emitter.messages())
    assert_in_step(worker)


@pytest.mark.parametrize(("decoys", "reads"), [(0, 0), (9, 8)], ids=["silent", "flooded"])
def test_a_resync_that_never_sees_the_identity_gives_up_and_clears_the_link(
    dp800: Profile, dp832: Dp832, decoys: int, reads: int
) -> None:
    """At most eight replies are read for the identity; what is still on its way is cleared so
    it cannot answer the next query."""
    held: list[ScriptedLink] = []
    armed = {"on": False}

    def handler(command: str) -> str | None:
        if not (armed["on"] and command == "*IDN?"):
            return dp832(command)
        for _ in range(decoys):
            held[0].push(LATE_READING)
        return None

    worker, link, _ = connected(dp800, handler)
    held.append(link)
    armed["on"] = True
    read_before = len(link.timeouts_asked)
    with pytest.raises(LinkError, match="replies stayed out of step"):
        worker._resync("probe")
    assert len(link.timeouts_asked) - read_before == reads
    cleared = link.cleared
    worker._recover("probe")
    assert link.cleared == cleared + 2
    armed["on"] = False
    assert_in_step(worker)


def test_a_resync_on_a_link_that_fails_is_cleared_and_does_not_raise_from_recover(
    dp800: Profile, dp832: Dp832
) -> None:
    armed = {"on": False}

    def handler(command: str) -> str | None:
        if armed["on"] and command == "*IDN?":
            raise LinkError("the link dropped")
        return dp832(command)

    worker, link, _ = connected(dp800, handler)
    armed["on"] = True
    cleared = link.cleared
    worker._recover("probe")
    assert link.cleared == cleared + 2
    armed["on"] = False
    assert_in_step(worker)


@pytest.mark.usefixtures("fast_schedule")
def test_an_unexpected_exception_in_the_poll_loop_reconnects_the_worker(
    dp800: Profile, dp832: Dp832, caplog: pytest.LogCaptureFixture
) -> None:
    opened: list[ScriptedLink] = []
    crashed = {"done": False}

    def handler(command: str) -> str | None:
        if command == ":MEAS:ALL? CH1" and not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("boom")
        return dp832(command)

    def opener(address: Any, settings: Any) -> ScriptedLink:
        opened.append(ScriptedLink(handler))
        return opened[-1]

    emitter = RecordingEmitter()
    worker = InstrumentWorker(instrument_config(rate_hz=50), [dp800], emitter, open=opener)
    worker.start()
    try:
        assert wait_until(lambda: len(opened) == 2 and worker.state == "open", timeout_s=5)
        assert any("an unexpected error" in m for m in emitter.messages())
        assert any("unexpected error; reconnecting" in r.message for r in caplog.records)
        before = len(emitter.rows)
        assert wait_until(lambda: len(emitter.rows) > before + 3)
    finally:
        worker.stop()
        worker.join(2)


@pytest.mark.usefixtures("fast_schedule")
def test_a_link_that_drops_inside_a_request_answers_it_and_reconnects(
    dp800: Profile, dp832: Dp832
) -> None:
    opened: list[ScriptedLink] = []

    def handler(command: str) -> str | None:
        if command == "DROP?":
            raise LinkError("the link dropped", "other")
        return dp832(command)

    def opener(address: Any, settings: Any) -> ScriptedLink:
        opened.append(ScriptedLink(handler))
        return opened[-1]

    emitter = RecordingEmitter()
    worker = InstrumentWorker(instrument_config(rate_hz=50), [dp800], emitter, open=opener)
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=5)
        assert ask(worker, "scpi", command="DROP?") == {"error": "the link dropped"}
        assert wait_until(lambda: len(opened) == 2 and worker.state == "open", timeout_s=5)
        assert any("disconnected" in m and "the link dropped" in m for m in emitter.messages())
        assert opened[0].closed
    finally:
        worker.stop()
        worker.join(2)


def test_a_request_that_raises_unexpectedly_is_answered_with_its_message(
    dp800: Profile, dp832: Dp832, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(command: str) -> str | None:
        if command == "BOOM?":
            raise RuntimeError("kaboom")
        return dp832(command)

    worker, emitter = start_open(dp800, ScriptedLink(handler))
    try:
        assert ask(worker, "scpi", command="BOOM?") == {"error": "scpi failed: kaboom"}
        assert any("scpi failed" in r.message for r in caplog.records)
        assert ask(worker, "scpi", command="*IDN?") == {"reply": DP832_IDN}
        assert worker.state == "open"
        assert not any("disconnected" in m for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(2)


class HangingLink(ScriptedLink):
    """A link whose reads block, once told to, until it is closed."""

    def __init__(self, handler: Callable[[str], str | None]) -> None:
        super().__init__(handler, "hanging")
        self.hang = False
        self.reading = threading.Event()
        self.freed = threading.Event()

    def read(self, timeout_s: float | None = None) -> str:
        if self.hang:
            self.reading.set()
            self.freed.wait(10)
            raise LinkError("closed while reading")
        return super().read(timeout_s)

    def close(self) -> None:
        super().close()
        self.freed.set()


def test_stop_closes_a_link_that_is_still_blocked_in_a_read(dp800: Profile, dp832: Dp832) -> None:
    link = HangingLink(dp832)
    worker, _ = start_open(dp800, link, advanced={"timeout_s": 0.1})
    try:
        link.hang = True
        assert link.reading.wait(5)
        worker.stop()
        worker.join(2)
        assert not worker.is_alive()
        assert link.closed
    finally:
        link.freed.set()


def remote_checked(answer: str) -> tuple[InstrumentWorker, ScriptedLink]:
    """A supply that says who holds remote control when asked."""
    raw = minimal()
    raw["link"] = {
        "remote": "SYST:REM",
        "remote_check": {"query": "SYST:LOCK:OWN?", "expect": "REMOTE"},
    }
    profile = profile_from(raw)
    replies = {"*IDN?": "ACME,P1,SN1,1.0", "SYST:LOCK:OWN?": answer, "MEAS:VOLT? 1": "5.0"}
    link = ScriptedLink(replies.get)
    cfg = instrument_config(profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], RecordingEmitter(), open=opener_for(link))
    return worker, link


def test_an_instrument_that_refuses_remote_control_fails_the_open() -> None:
    worker, link = remote_checked("LOCAL")
    assert not worker._connect()
    assert worker.state == "failed"
    assert worker.health == (
        f"Cannot open {worker.where}: the instrument refused remote control "
        "(SYST:LOCK:OWN? answered LOCAL). Retrying."
    )
    assert link.closed
    assert link.commands == ["*IDN?", "SYST:REM", "SYST:LOCK:OWN?"]


def test_an_instrument_that_takes_remote_control_opens() -> None:
    worker, link = remote_checked("REMOTE ")
    assert worker._connect(), worker.health
    assert link.commands[1:3] == ["SYST:REM", "SYST:LOCK:OWN?"]


@pytest.mark.usefixtures("fast_schedule")
def test_a_worker_let_go_for_a_while_waits_that_long_before_reconnecting(
    dp800: Profile, dp832: Dp832
) -> None:
    opened: list[float] = []
    links: list[ScriptedLink] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        opened.append(time.monotonic())
        links.append(ScriptedLink(dp832, address.where))
        return links[-1]

    emitter = RecordingEmitter()
    worker = InstrumentWorker(instrument_config(rate_hz=50), [dp800], emitter, open=opener)
    assert worker._connect()
    health = "Rebooting; reconnecting in about 0.5 s."
    worker.disconnect_for(0.5, health)
    let_go = time.monotonic()
    assert links[0].closed and "SYST:LOCAL" not in links[0].commands
    assert (worker.state, worker.health) == ("connecting", health)
    assert emitter.statuses[-1]["connected"] is False
    worker.start()
    try:
        assert ask(worker, "read", channel=1) == {"error": health}
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        assert len(opened) == 2 and opened[1] - let_go >= 0.5
    finally:
        worker.stop()
        worker.join(2)


def test_a_locked_panel_is_not_handed_back(dp800: Profile, dp832: Dp832) -> None:
    worker, link, _ = connected(dp800, dp832)
    worker._panel_locked = True
    worker._keep_panel()
    assert "SYST:LOCAL" not in link.commands
    worker._panel_locked = False
    worker._keep_panel()
    assert link.commands[-1] == "SYST:LOCAL"


def test_the_error_drain_asks_every_error_query_in_turn() -> None:
    raw = minimal()
    raw["errors"] = {"query": ["CMR?", "EXR?"]}
    queued: dict[str, list[str]] = {"CMR?": [], "EXR?": []}

    def unit(command: str) -> str | None:
        if command in queued:
            return queued[command].pop(0) if queued[command] else "0"
        return {"*IDN?": "ACME,P1,SN1,1.0", "MEAS:VOLT? 1": "5.0"}.get(command)

    worker, link, _ = connected(profile_from(raw), unit)
    queued["CMR?"].append("3,Command error")
    queued["EXR?"].extend(['-222,"Data out of range"', '-221,"Settings conflict"'])
    sent = len(link.commands)
    assert worker._drain_errors(quiet=True) == 3
    assert link.commands[sent:] == ["CMR?", "CMR?", "EXR?", "EXR?", "EXR?"]
    assert worker._last_error == "-221,Settings conflict"


def test_a_system_request_drains_the_error_queue_before_it_runs() -> None:
    raw = minimal()
    raw["errors"] = {"query": "SYST:ERR?"}
    raw["system"] = {"beep": "SYST:BEEP"}
    replies = {"*IDN?": "ACME,P1,SN1,1.0", "MEAS:VOLT? 1": "5.0", "SYST:ERR?": '0,"No error"'}
    worker, link, _ = connected(profile_from(raw), replies.get)
    sent = len(link.commands)
    assert "error" not in served(worker, "system", op="beep")
    assert link.commands[sent : sent + 2] == ["SYST:ERR?", "SYST:BEEP"]


def test_a_generator_reads_its_settings_and_reports_no_reading_rate() -> None:
    unit = Echo(
        {
            "*IDN?": "ACME,G1,SN1,1.0",
            "SOUR1:MOD:TYPE?": "AM",
            "SOUR2:MOD:TYPE?": "FM",
            "C1:BTWV?": "C1:BTWV STATE,ON",
            "C2:BTWV?": "C2:BTWV STATE,OFF",
        }
    )
    profile = generator({"command": "*TRG", "channel": False})
    worker, link, emitter = connected(profile, unit, profile=profile.id)
    settled = "Connected to ACME G1. Reading 2 settings on 2 channels."
    assert worker.health == settled
    worker._cycle()
    worker._cycle()
    assert worker._channel[1].values == {"modulation_type": "am", "burst": True}
    assert worker._channel[2].values == {"modulation_type": "fm", "burst": False}
    assert worker.health == settled
    assert unit.writes == []
    assert emitter.statuses and emitter.statuses[-1]["connected"] is True


def test_a_supply_channel_whose_output_is_off_records_mode_off(dp800) -> None:
    worker, _, emitter = connected(dp800, Dp832())
    worker._record(1, {"output": False, "mode": "cv"}, 0)
    worker._record(2, {"output": True, "mode": "cv"}, 0)
    worker._record(1, {"mode": "cc"}, 0)
    modes = [(ch, values["mode"]) for _, ch, values, _ in emitter.rows if "mode" in values]
    assert modes[-3:] == [(1, "off"), (2, "cv"), (1, "off")]
