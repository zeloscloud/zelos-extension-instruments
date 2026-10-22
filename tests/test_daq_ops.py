"""Data-acquisition units: the `[daq]` table, and the scan a worker sets up and reads."""

import copy
import math
import re
from typing import Any

import pytest

from tests.conftest import RecordingEmitter, ScriptedLink, instrument_config, opener_for
from zelos_extension_instruments import daq_ops
from zelos_extension_instruments.engine import ParseError, RangeError
from zelos_extension_instruments.profile_daq import Nplc
from zelos_extension_instruments.profile_load import ProfileError, from_dict
from zelos_extension_instruments.sim.fake_daq import FakeDaq
from zelos_extension_instruments.worker import InstrumentWorker

EVIDENCE = {"badge": "manual", "source": "test", "date": "2026-10-22"}

# Slot-and-channel addresses, a configure per function, and READ? for one scan.
KEYSIGHT_STYLE: dict[str, Any] = {
    "schema": 1,
    "id": "acme-daq",
    "name": "Acme scanner",
    "category": "daq",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^D1$"},
    "errors": {"query": "SYST:ERR?"},
    "setters": {
        "relay": {
            "command": "ROUT:CLOSE (@{ch})",
            "off": "ROUT:OPEN (@{ch})",
            "verify": "ROUT:CLOSE? (@{ch})",
        }
    },
    "daq": {
        "configure": {
            "dc_voltage": "CONF:VOLT:DC AUTO,DEF,(@{channels})",
            "resistance": "CONF:RES AUTO,DEF,(@{channels})",
            "temperature": "CONF:TEMP TC,{tc},(@{channels})",
        },
        "nplc": {
            "command": "SENS:{function}:NPLC {value},(@{channels})",
            "map": {"dc_voltage": "VOLT:DC", "resistance": "RES", "temperature": "TEMP"},
        },
        "scan": "ROUT:SCAN (@{channels})",
        "read": "READ?",
        "channel_pattern": r"[1-3]\d\d",
    },
}

# A function per channel list, several commands for a thermocouple, and a buffer read.
BUFFER_STYLE: dict[str, Any] = {
    "schema": 1,
    "id": "acme-daq-buffer",
    "name": "Acme buffered scanner",
    "category": "daq",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^D2$"},
    "errors": {"query": "SYST:ERR?"},
    "daq": {
        "configure": {
            "dc_voltage": 'SENS:FUNC "VOLT:DC",(@{channels})',
            "temperature": [
                'SENS:FUNC "TEMP",(@{channels})',
                "SENS:TEMP:TRAN TC,(@{channels})",
                "SENS:TEMP:TC:TYPE {tc},(@{channels})",
            ],
        },
        "nplc": {
            "command": "SENS:{function}:NPLC {value},(@{channels})",
            "map": {"dc_voltage": "VOLT", "temperature": "TEMP"},
        },
        "scan": ["ROUT:SCAN:CRE (@{channels})", "ROUT:SCAN:COUN:SCAN 1"],
        "read": ["INIT", "*WAI", 'TRAC:DATA? 1,{count},"defbuffer1",READ'],
        "channel_pattern": r"[12](0[1-9]|[1-5]\d|60)",
    },
}

SCAN = [
    {"channel": 105, "function": "temperature", "thermocouple": "T"},
    {"channel": 103, "function": "temperature", "thermocouple": "K"},
    {"channel": 101, "function": "dc_voltage", "nplc": 10},
    {"channel": 104, "function": "resistance", "nplc": 1},
    {"channel": 102, "function": "dc_voltage", "nplc": 10},
]


def with_daq(raw: dict[str, Any], **daq: Any) -> dict[str, Any]:
    """`raw` with keys of its `[daq]` table replaced; None removes one."""
    out = copy.deepcopy(raw)
    for key, value in daq.items():
        if value is None:
            out["daq"].pop(key, None)
        else:
            out["daq"][key] = value
    return out


def problems_of(raw: dict[str, Any]) -> list[str]:
    with pytest.raises(ProfileError) as caught:
        from_dict(raw, f"{raw['id']}.toml")
    return caught.value.problems


def bench(
    raw: dict[str, Any], scan: list[dict[str, Any]], relays: tuple[int, ...] = ()
) -> tuple[InstrumentWorker, FakeDaq, ScriptedLink]:
    """A worker linked to a fake unit, with the profile in place and nothing sent yet."""
    profile = from_dict(raw, f"{raw['id']}.toml")
    fake = FakeDaq(profile)
    link = ScriptedLink(fake.handle, "daq")
    cfg = instrument_config(
        name="daq", profile=profile.id, advanced={"scan": scan, "relays": list(relays)}
    )
    worker = InstrumentWorker(cfg, [profile], RecordingEmitter(), open=opener_for(link))
    worker.profile = profile
    worker._link = link
    return worker, fake, link


def test_a_daq_table_loads() -> None:
    profile = from_dict(KEYSIGHT_STYLE, "acme-daq.toml")
    daq = profile.daq
    assert daq is not None
    assert daq.configure["temperature"] == ("CONF:TEMP TC,{tc},(@{channels})",)
    assert daq.nplc == Nplc(
        command="SENS:{function}:NPLC {value},(@{channels})",
        map={"dc_voltage": "VOLT:DC", "resistance": "RES", "temperature": "TEMP"},
    )
    assert daq.scan == ("ROUT:SCAN (@{channels})",)
    assert daq.read == ("READ?",)
    assert daq.channel_pattern.fullmatch("101")


def test_a_buffered_daq_table_loads() -> None:
    daq = from_dict(BUFFER_STYLE, "acme-daq-buffer.toml").daq
    assert daq is not None
    assert len(daq.configure["temperature"]) == 3
    assert daq.read[-1] == 'TRAC:DATA? 1,{count},"defbuffer1",READ'
    assert daq.nplc is not None


def test_a_daq_profile_needs_a_daq_table() -> None:
    raw = copy.deepcopy(KEYSIGHT_STYLE)
    del raw["daq"]
    assert problems_of(raw) == [
        "daq: missing: a daq profile describes how a scan is set up and read."
    ]


def test_a_daq_table_is_only_for_the_daq_category() -> None:
    raw = {
        "schema": 1,
        "id": "acme-psu",
        "name": "Acme PSU",
        "category": "psu",
        "evidence": EVIDENCE,
        "match": {"manufacturer": "(?i)^acme", "model": "^P1$"},
        "signals": {"voltage": {"query": "MEAS:VOLT? {ch}", "parse": "float"}},
        "daq": KEYSIGHT_STYLE["daq"],
    }
    assert problems_of(raw) == ["daq: is only for the daq category."]


@pytest.mark.parametrize(
    ("daq", "problem"),
    [
        ({"bogus": 1}, "daq: unknown key bogus."),
        ({"configure": None}, "daq.configure: missing: name the commands for each function"),
        ({"configure": {}}, "daq.configure: must be a table that names at least one function."),
        ({"configure": "CONF (@{channels})"}, "daq.configure: must be a table that names"),
        (
            {"configure": {"voltage": "CONF:VOLT (@{channels})"}},
            "daq.configure: 'voltage' is not one of dc_voltage,",
        ),
        (
            {"configure": {"dc_voltage": "CONF:VOLT:DC"}},
            "daq.configure.dc_voltage: must contain {channels} in every command.",
        ),
        (
            {"configure": {"dc_voltage": "CONF:VOLT:DC {tc},(@{channels})"}},
            "daq.configure.dc_voltage: unknown placeholder tc.",
        ),
        (
            {"configure": {"temperature": "CONF:TEMP TC,K,(@{channels})"}},
            "daq.configure.temperature: must contain {tc}, the thermocouple type, in one command.",
        ),
        (
            {"configure": {"dc_voltage": []}},
            "daq.configure.dc_voltage: missing: at least one command.",
        ),
        (
            {"nplc": "SENS:VOLT:DC:NPLC {value},(@{channels})"},
            "daq.nplc: must be a table.",
        ),
        (
            {"nplc": {"command": "SENS:VOLT:DC:NPLC 1,(@{channels})", "map": {"dc_voltage": "V"}}},
            "daq.nplc.command: must contain {channels} and {value}.",
        ),
        (
            {"nplc": {"command": "NPLC {value},(@{channels})", "map": {}}},
            "daq.nplc.map: must name each function that takes an integration time.",
        ),
        (
            {"nplc": {"command": "NPLC {value},(@{channels})", "map": {"frequency": "FREQ"}}},
            "daq.nplc.map: 'frequency' is not a function daq.configure sets.",
        ),
        (
            {"nplc": {"command": "NPLC {value},(@{channels})", "map": {"x": "y"}, "y": 1}},
            "daq.nplc: unknown key y.",
        ),
        ({"scan": "ROUT:SCAN:COUN 1"}, "daq.scan: must contain {channels} in one command."),
        ({"scan": None}, "daq.scan: missing: at least one command."),
        ({"scan": "ROUT:SCAN (@{ch})"}, "daq.scan: unknown placeholder ch."),
        ({"read": "INIT"}, "daq.read: must end with the query that returns the readings."),
        ({"read": ["FETC?", "READ?"]}, "daq.read: may hold only one query, the last."),
        ({"read": None}, "daq.read: missing: at least one command."),
        ({"card": "SYST:CTYP? {slot}"}, "daq: unknown key card."),
        ({"channel_pattern": None}, "daq.channel_pattern: missing or empty."),
        ({"channel_pattern": "[1-"}, "daq.channel_pattern: is not a valid regular expression"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_each_daq_mistake_is_named(daq: dict[str, Any], problem: str) -> None:
    found = problems_of(with_daq(KEYSIGHT_STYLE, **daq))
    assert any(p.startswith(problem) for p in found), found


def test_channels_are_the_scan_channels_and_relays_ascending() -> None:
    cfg = instrument_config(
        advanced={"scan": [{"channel": 103, "function": "dc_voltage"}], "relays": [120, 101]}
    )
    assert daq_ops.channels(cfg) == [101, 103, 120]


def test_setup_configures_each_group_then_the_integration_times_then_the_scan() -> None:
    worker, fake, link = bench(KEYSIGHT_STYLE, SCAN, relays=(110,))
    daq_ops.check(worker)
    daq_ops.setup(worker)
    assert link.commands == [
        "CONF:VOLT:DC AUTO,DEF,(@101,102)",
        "CONF:TEMP TC,K,(@103)",
        "CONF:RES AUTO,DEF,(@104)",
        "CONF:TEMP TC,T,(@105)",
        "SENS:VOLT:DC:NPLC 10,(@101,102)",
        "SENS:RES:NPLC 1,(@104)",
        "ROUT:SCAN (@101,102,103,104,105)",
    ]
    assert fake.functions == {
        101: "dc_voltage",
        102: "dc_voltage",
        103: "temperature",
        104: "resistance",
        105: "temperature",
    }
    assert fake.thermocouples == {103: "K", 105: "T"}
    assert fake.nplc == {101: 10.0, 102: 10.0, 104: 1.0}
    assert fake.scan_list == (101, 102, 103, 104, 105)
    assert fake.handle("SYST:ERR?") == '0,"No error"'


def test_read_fills_each_scan_channel_under_its_function() -> None:
    worker, fake, link = bench(KEYSIGHT_STYLE, SCAN, relays=(110,))
    daq_ops.setup(worker)
    for ch, value in ((101, 1.5), (102, -0.25), (103, 21.0), (104, 470.0), (105, 30.5)):
        fake.set_reading(ch, value)
    rows: dict[int | None, dict[str, Any]] = {}
    sent = len(link.commands)
    daq_ops.read(worker, rows)
    assert link.commands[sent:] == ["READ?"]
    assert rows == {
        101: {"dc_voltage": 1.5},
        102: {"dc_voltage": -0.25},
        103: {"temperature": 21.0},
        104: {"resistance": 470.0},
        105: {"temperature": 30.5},
    }


def test_a_buffered_unit_is_set_up_and_read_with_its_own_commands() -> None:
    scan = [
        {"channel": 102, "function": "temperature", "thermocouple": "J", "nplc": 1},
        {"channel": 101, "function": "dc_voltage", "nplc": 0.5},
        {"channel": 103, "function": "dc_voltage", "nplc": 0.5},
    ]
    worker, fake, link = bench(BUFFER_STYLE, scan)
    daq_ops.check(worker)
    daq_ops.setup(worker)
    fake.set_reading(102, 22.5)
    rows: dict[int | None, dict[str, Any]] = {}
    daq_ops.read(worker, rows)
    assert link.commands == [
        'SENS:FUNC "VOLT:DC",(@101,103)',
        'SENS:FUNC "TEMP",(@102)',
        "SENS:TEMP:TRAN TC,(@102)",
        "SENS:TEMP:TC:TYPE J,(@102)",
        "SENS:VOLT:NPLC 0.5,(@101,103)",
        "SENS:TEMP:NPLC 1,(@102)",
        "ROUT:SCAN:CRE (@101,102,103)",
        "ROUT:SCAN:COUN:SCAN 1",
        "INIT",
        "*WAI",
        'TRAC:DATA? 1,3,"defbuffer1",READ',
    ]
    assert rows == {
        101: {"dc_voltage": 5.0},
        102: {"temperature": 22.5},
        103: {"dc_voltage": 5.0},
    }
    assert fake.handle("SYST:ERR?") == '0,"No error"'


def test_an_overload_reads_as_not_a_number() -> None:
    worker, fake, _ = bench(KEYSIGHT_STYLE, SCAN)
    daq_ops.setup(worker)
    fake.set_reading(104, 9.9e37)
    rows: dict[int | None, dict[str, Any]] = {}
    daq_ops.read(worker, rows)
    assert math.isnan(rows[104]["resistance"])
    assert rows[101]["dc_voltage"] == 5.0


def test_a_reply_with_the_wrong_count_of_readings_is_refused() -> None:
    worker, fake, link = bench(KEYSIGHT_STYLE, SCAN[:2])
    daq_ops.setup(worker)
    fake.scan_list = (103,)
    rows: dict[int | None, dict[str, Any]] = {}
    with pytest.raises(ParseError, match=re.escape("expected 2 readings, got 1")):
        daq_ops.read(worker, rows)
    assert rows == {}
    assert link.commands[-1] == "READ?"


def test_a_relay_only_unit_sends_nothing_to_set_up_or_read() -> None:
    worker, _, link = bench(KEYSIGHT_STYLE, [], relays=(101, 102))
    daq_ops.check(worker)
    daq_ops.setup(worker)
    rows: dict[int | None, dict[str, Any]] = {}
    daq_ops.read(worker, rows)
    assert link.commands == []
    assert rows == {}


def test_an_address_the_unit_does_not_have_is_refused() -> None:
    worker, _, link = bench(KEYSIGHT_STYLE, SCAN[:1], relays=(401,))
    with pytest.raises(RangeError, match="channel 401 is not an address this instrument has"):
        daq_ops.check(worker)
    assert link.commands == []


def test_a_function_the_profile_cannot_read_is_refused() -> None:
    worker, _, _ = bench(KEYSIGHT_STYLE, [{"channel": 101, "function": "frequency"}])
    with pytest.raises(RangeError, match="channel 101: this profile cannot read frequency"):
        daq_ops.check(worker)


def test_an_integration_time_the_profile_cannot_set_is_refused() -> None:
    raw = with_daq(KEYSIGHT_STYLE, nplc=None)
    worker, _, _ = bench(raw, [{"channel": 101, "function": "dc_voltage", "nplc": 1}])
    with pytest.raises(RangeError, match="cannot set the integration time of dc_voltage"):
        daq_ops.check(worker)
