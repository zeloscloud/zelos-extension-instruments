"""The fake instrument a profile builds: replies, errors, scenarios and faults."""

import copy
import dataclasses
import math
import struct
import tomllib
import zlib
from pathlib import Path
from typing import Any

import pytest

from tests.test_daq_ops import KEYSIGHT_STYLE
from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import Profile, parse_idn
from zelos_extension_instruments.profile_load import from_dict, load_file
from zelos_extension_instruments.sim.fake import FakeInstrument, _pattern
from zelos_extension_instruments.sim.fake_daq import FakeDaq

EVIDENCE = {"badge": "manual", "source": "test", "date": "2026-10-20"}

SELECT_PSU: dict[str, Any] = {
    "schema": 1,
    "id": "select-psu",
    "name": "Select-style supply",
    "category": "psu",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": r"^PS\d0", "channels_query": "SYST:CHAN?"},
    "link": {"select": "INST:NSEL {ch}", "remote": "SYST:REM"},
    "errors": {"query": "SYST:ERR?"},
    "ranges": {
        "voltage": {"min_query": "VOLT? MIN", "max_query": "VOLT? MAX", "digits": 2},
        "current": {"max": 5, "digits": 3},
    },
    "signals": {
        "measure": {"query": "MEAS:ALL?", "parse": "floats", "fields": ["voltage", "current"]},
        "power": {"query": "MEAS:POW?", "parse": "float"},
    },
    "state": {
        "voltage_set": {"query": "APPL?", "parse": "float", "index": 1},
        "current_set": {"query": "APPL?", "parse": "float", "index": 2},
        "output": {"query": "STAT:OPER?", "parse": "bits", "mask": 1},
        "mode": {
            "query": "STAT:OPER?",
            "parse": "bits",
            "mask": 6,
            "shift": 1,
            "map": {"0": "off", "1": "cv", "2": "cc"},
        },
        "protection": {
            "flags": [
                {
                    "query": "STAT:QUES?",
                    "parse": "bits",
                    "mask": 16,
                    "format": "hex",
                    "value": "ovp",
                },
                {
                    "query": "STAT:QUES?",
                    "parse": "bits",
                    "mask": 32,
                    "format": "hex",
                    "value": "ocp",
                },
            ]
        },
    },
    "setters": {
        "voltage": {"command": "VOLT {value}"},
        "current": {"command": "CURR {value}"},
        "output": {"command": "OUTP {bit}"},
        "clear": {"command": "OUTP:PROT:CLE"},
    },
}

LOAD: dict[str, Any] = {
    "schema": 1,
    "id": "test-load",
    "name": "Test load",
    "category": "load",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^EL1"},
    "ranges": {
        "current": {"max": 30},
        "voltage": {"max": 150},
        "power": {"max": 300},
        "resistance": {"max": 1000},
    },
    "signals": {
        "measure": {
            "query": "MEAS:ALL?",
            "parse": "floats",
            "fields": ["voltage", "current", "power", "resistance"],
        }
    },
    "state": {
        "output": {"query": "INP?", "parse": "bool"},
        "mode": {
            "query": "FUNC?",
            "parse": "map",
            "map": {"CURR": "cc", "VOLT": "cv", "RES": "cr", "POW": "cp"},
        },
    },
    "setters": {
        "current": {"command": "CURR {value}"},
        "voltage": {"command": "VOLT {value}"},
        "power": {"command": "POW {value}"},
        "resistance": {"command": "RES {value}"},
        "function": {
            "command": "FUNC {function}",
            "map": {"cc": "CURR", "cv": "VOLT", "cr": "RES", "cp": "POW"},
        },
        "output": {"command": "INP {on}"},
    },
}

AC_SOURCE: dict[str, Any] = {
    "schema": 1,
    "id": "test-ac",
    "name": "Test AC source",
    "category": "acsource",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^AC1"},
    "ranges": {"voltage": {"max": 300}, "frequency": {"min": 40, "max": 500, "digits": 1}},
    "signals": {
        "measure": {
            "query": "MEAS:ALL?",
            "parse": "floats",
            "fields": [
                "voltage",
                "current",
                "power",
                "apparent_power",
                "power_factor",
                "frequency",
            ],
        }
    },
    "state": {"frequency_set": {"query": "FREQ?", "parse": "float"}},
    "setters": {
        "voltage": {"command": "VOLT {value}"},
        "frequency": {"command": "FREQ {value}"},
        "output": {"command": "OUTP {on}"},
    },
}

DMM_TOML = """
schema = 1
id = "test-dmm"
name = "Test DMM"
category = "dmm"

[evidence]
badge = "manual"
source = "test"
date = "2026-10-20"

[match]
manufacturer = '(?i)^acme'
model = '^DM1'

[errors]
query = "SYST:ERR?"

[signals.value]
query = "READ?"
parse = "float"

[state.function]
query = "FUNC?"
parse = "map"
map = { '"VOLT"' = "dc_voltage", '"VOLT:AC"' = "ac_voltage", '"RES"' = "resistance" }

[setters.function]
command = "FUNC {function}"
map = { dc_voltage = '"VOLT"', ac_voltage = '"VOLT:AC"', resistance = '"RES"' }

[dmm]
function_query = "FUNC?"
function_map = { '"VOLT"' = "dc_voltage", '"VOLT:AC"' = "ac_voltage", '"RES"' = "resistance" }
functions = { dc_voltage = "V", ac_voltage = "V", resistance = "Ohm" }
"""


def build(raw: dict[str, Any]) -> Profile:
    return from_dict(raw, f"{raw['id']}.toml")


def drain(fake: FakeInstrument) -> list[engine.InstrumentError]:
    errors = fake.profile.errors
    assert errors is not None
    found: list[engine.InstrumentError] = []
    while (error := engine.read_error(errors, fake.handle_text(errors.query) or "")) is not None:
        found.append(error)
    return found


def send(
    fake: FakeInstrument, selector: engine.Selector, ch: int | None, command: str
) -> str | None:
    select = selector.command_for(ch)
    if select is not None:
        assert fake.handle_text(select) is None
    return fake.handle_text(command)


def set_value(
    fake: FakeInstrument,
    name: str,
    ch: int,
    value: float,
    selector: engine.Selector | None = None,
) -> None:
    profile = fake.profile
    setter = profile.setters[name]
    range_ = profile.range_for(profile.range_key(setter) or name, fake.model, ch)
    command = engine.setter_command(setter, ch=ch, value=value, range_=range_)
    assert send(fake, selector or engine.Selector(None), ch, command) is None


def set_output(
    fake: FakeInstrument, ch: int, on: bool, selector: engine.Selector | None = None
) -> None:
    command = engine.setter_command(fake.profile.setters["output"], ch=ch, on=on)
    assert send(fake, selector or engine.Selector(None), ch, command) is None


def poll(
    fake: FakeInstrument, selector: engine.Selector | None = None
) -> dict[tuple[str, int | None], Any]:
    """Every field and flag the engine's plan reads, keyed by name and channel."""
    selector = selector or engine.Selector(fake.profile.link.select)
    readings: dict[tuple[str, int | None], Any] = {}
    for step in engine.compile_plan(fake.profile, fake.channels):
        reply = send(fake, selector, step.channel, step.query)
        assert reply is not None, step.query
        if step.flag is not None:
            readings[(step.flag.value, step.channel)] = engine.flag_is_set(step.flag, reply)
        else:
            for name, value in engine.parse_reply(step.reader, reply).items():
                readings[(name, step.channel)] = value
    return readings


def read(fake: FakeInstrument, field: str, ch: int = 1) -> Any:
    reader = fake.profile.reader(field)
    assert reader is not None and reader.query is not None
    reply = fake.handle_text(engine.render(reader.query, ch=ch))
    assert reply is not None
    return engine.parse_reply(reader, reply)[field]


def test_default_model_is_one_the_profile_matches(dp800):
    fake = FakeInstrument(dp800)
    idn = fake.handle_text("*IDN?")
    assert idn == fake.idn
    assert idn is not None and dp800.matches(parse_idn(idn))
    assert fake.channels == dp800.channels_for(fake.model)


def test_model_and_channels_can_be_given(dp800):
    assert FakeInstrument(dp800, "DP832").channels == 3
    assert FakeInstrument(dp800, "DP832", channels=2).channels == 2
    fake = FakeInstrument(dp800, "DP821", idn="RIGOL TECHNOLOGIES,DP821,DP8A000001,00.01.16")
    assert fake.handle_text("*IDN?") == "RIGOL TECHNOLOGIES,DP821,DP8A000001,00.01.16"


def test_a_model_pattern_it_cannot_spell_needs_an_explicit_model(dp800):
    raw = {**LOAD, "match": {"manufacturer": "(?i)^acme", "model": "^(?!X)EL"}}
    profile = build(raw)
    with pytest.raises(ValueError, match="model="):
        FakeInstrument(profile)
    assert FakeInstrument(profile, "EL100").model == "EL100"


def test_every_rigol_reader_round_trips(dp800):
    fake = FakeInstrument(dp800, "DP832")
    for ch, volts in ((1, 5.0), (2, 12.0), (3, 3.3)):
        set_value(fake, "voltage", ch, volts)
        set_value(fake, "current", ch, 1.0)
        set_output(fake, ch, True)
    fake.set_load_ohms(2, 6.0)
    fake.trip(3, "ocp")

    readings = poll(fake)

    assert readings[("voltage", 1)] == pytest.approx(5.0)
    assert readings[("current", 1)] == pytest.approx(0.5)
    assert readings[("power", 1)] == pytest.approx(2.5)
    assert readings[("mode", 1)] == "cv"
    assert readings[("voltage", 2)] == pytest.approx(6.0)
    assert readings[("current", 2)] == pytest.approx(1.0)
    assert readings[("mode", 2)] == "cc"
    assert readings[("voltage_set", 2)] == pytest.approx(12.0)
    assert readings[("current_set", 2)] == pytest.approx(1.0)
    assert readings[("output", 2)] is True
    assert readings[("output", 3)] is False
    assert readings[("voltage", 3)] == 0.0
    assert readings[("mode", 3)] == "cv"
    assert readings[("ocp", 3)] is True
    assert readings[("ovp", 3)] is False
    assert not any(readings[(label, ch)] for label in ("ovp", "ocp") for ch in (1, 2))
    assert drain(fake) == []


def test_rigol_setters_verify_by_readback(dp800):
    fake = FakeInstrument(dp800, "DP832")
    for name, value in (("voltage", 4.321), ("current", 2.5)):
        setter = dp800.setters[name]
        range_ = dp800.range_for(name, fake.model, 2)
        fake.handle_text(engine.setter_command(setter, ch=2, value=value, range_=range_))
        query = dp800.verify_query(setter)
        field_name = dp800.verify_field(setter)
        reader = dp800.reader(field_name)
        assert query is not None and reader is not None
        readback = engine.parse_reply(reader, fake.handle_text(engine.render(query, ch=2)) or "")
        assert engine.verified(value, readback[field_name], range_)
    for on in (True, False, True):
        set_output(fake, 2, on)
        assert read(fake, "output", 2) is on
    fake.trip(2, "ovp")
    assert read(fake, "output", 2) is False
    fake.handle_text(engine.setter_command(dp800.setters["clear"], ch=2))
    assert poll(fake)[("ovp", 2)] is False
    assert drain(fake) == []


def test_scpi_spelling_is_tolerated(dp800):
    fake = FakeInstrument(dp800, "DP832")
    assert fake.handle_text("sour1:volt 2.5") is None
    assert fake.handle_text("SOUR1:VOLT?") == "2.5"
    assert fake.handle_text("  :sour1:volt?  ") == "2.5"
    assert fake.handle_text(":OUTP   CH1,on") is None
    assert read(fake, "output") is True
    assert fake.handle_text("SYST:REMOTE") is None
    assert drain(fake) == []


def test_out_of_range_values_are_refused(dp800):
    fake = FakeInstrument(dp800, "DP832")
    assert fake.handle_text(":SOUR3:VOLT 6.000") is None
    assert read(fake, "voltage_set", 3) == 0.0
    assert fake.handle_text(":SOUR1:VOLT 6.000") is None
    assert read(fake, "voltage_set", 1) == 6.0
    fake.handle_text(":SOUR1:CURR -0.100")
    fake.handle_text(":SOUR4:VOLT?")
    assert [(e.code, e.text) for e in drain(fake)] == [(-222, "Data out of range")] * 3


def test_unknown_command_queues_undefined_header(dp800):
    fake = FakeInstrument(dp800)
    assert fake.handle_text("FOO:BAR?") is None
    assert fake.handle_text("SYST:ERR?") == '-113,"Undefined header"'
    assert fake.handle_text("SYST:ERR?") == '0,"No error"'
    assert fake.transcript[0] == ("FOO:BAR?", None)


def test_error_queue_is_first_in_first_out_and_bounded(dp800):
    fake = FakeInstrument(dp800)
    fake.handle_text("NOPE")
    fake.handle_text(":SOUR1:VOLT 99")
    assert [e.code for e in drain(fake)] == [-113, -222]
    fake.handle_text("NOPE")
    assert fake.handle_text("*CLS") is None
    assert drain(fake) == []
    for _ in range(30):
        fake.handle_text("NOPE")
    codes = [e.code for e in drain(fake)]
    assert len(codes) == 20
    assert codes[-1] == -350
    assert set(codes[:-1]) == {-113}


def test_common_commands(dp800):
    fake = FakeInstrument(dp800, "DP832")
    assert fake.handle_text("*OPC?") == "1"
    set_value(fake, "voltage", 1, 5.0)
    set_value(fake, "current", 1, 1.0)
    set_output(fake, 1, True)
    assert fake.handle_text("*RST") is None
    assert fake.transcript[-1] == ("*RST", None)
    assert read(fake, "voltage_set") == 0.0
    assert read(fake, "current_set") == 0.0
    assert read(fake, "output") is False
    assert drain(fake) == []


def test_select_style_resolves_channel_less_commands():
    profile = build(SELECT_PSU)
    fake = FakeInstrument(profile)
    assert fake.channels == 1
    fake = FakeInstrument(profile, channels=2)
    selector = engine.Selector(profile.link.select)
    set_value(fake, "voltage", 1, 5.0, selector)
    set_value(fake, "current", 1, 1.0, selector)
    set_value(fake, "voltage", 2, 9.0, selector)
    set_value(fake, "current", 2, 0.5, selector)
    set_output(fake, 1, True, selector)
    set_output(fake, 2, True, selector)
    fake.trip(2, "ocp")

    readings = poll(fake, selector)

    assert readings[("voltage_set", 1)] == 5.0
    assert readings[("current_set", 1)] == 1.0
    assert readings[("voltage_set", 2)] == 9.0
    assert readings[("voltage", 1)] == 5.0
    assert readings[("current", 1)] == 0.5
    assert readings[("power", 1)] == 2.5
    assert readings[("output", 1)] is True
    assert readings[("mode", 1)] == "cv"
    assert readings[("output", 2)] is False
    assert readings[("mode", 2)] == "off"
    assert readings[("ocp", 2)] is True
    assert readings[("ovp", 2)] is False
    assert readings[("ocp", 1)] is False
    assert fake.handle_text("STAT:QUES?") == "20"
    assert drain(fake) == []


def test_select_style_queries_and_limits():
    profile = build(SELECT_PSU)
    fake = FakeInstrument(profile, "PS20", channels=2)
    assert fake.handle_text("SYST:CHAN?") == "2"
    assert fake.handle_text("SYST:REM") is None
    assert engine.parse_float(fake.handle_text("VOLT? MIN") or "") == 0.0
    maximum = engine.parse_float(fake.handle_text("VOLT? MAX") or "")
    assert maximum > 0
    assert fake.handle_text(f"VOLT {maximum + 1:.2f}") is None
    assert fake.handle_text(f"VOLT {maximum:.2f}") is None
    assert fake.handle_text("CURR 5.001") is None
    assert fake.handle_text("INST:NSEL 3") is None
    assert [e.code for e in drain(fake)] == [-222, -222, -222]
    assert fake.handle_text("APPL?") == f"0,{maximum:g},0"
    assert fake.handle_text("INST:NSEL 2") is None
    assert fake.handle_text("APPL?") == "0,0,0"


def test_dmm_switches_function_and_reads_the_active_one(tmp_path: Path):
    path = tmp_path / "test-dmm.toml"
    path.write_text(DMM_TOML, encoding="utf-8")
    profile = load_file(path)
    fake = FakeInstrument(profile)
    assert fake.handle_text("FUNC?") == '"VOLT"'
    assert read(fake, "function") == "dc_voltage"
    fake.set_reading("dc_voltage", 3.3)
    fake.set_reading("ac_voltage", 230.0)
    assert read(fake, "value") == pytest.approx(3.3, rel=1e-3)

    command = engine.setter_command(profile.setters["function"], ch=1, function="ac_voltage")
    assert fake.handle_text(command) is None
    assert fake.handle_text("FUNC?") == '"VOLT:AC"'
    assert read(fake, "function") == "ac_voltage"
    values = {read(fake, "value") for _ in range(5)}
    assert all(v == pytest.approx(230.0, rel=1e-3) for v in values)
    assert len(values) > 1

    assert fake.handle_text('FUNC "BOGUS"') is None
    assert [e.code for e in drain(fake)] == [-113]
    with pytest.raises(ValueError):
        fake.set_reading("bogus", 1.0)


def test_load_current_follows_the_function():
    profile = build(LOAD)
    fake = FakeInstrument(profile)
    set_value(fake, "current", 1, 2.0)
    set_value(fake, "resistance", 1, 6.0)
    set_value(fake, "power", 1, 24.0)
    set_value(fake, "voltage", 1, 10.0)
    assert read(fake, "mode") == "cc"
    assert read(fake, "current") == 0.0
    assert read(fake, "voltage") == 12.0
    assert math.isnan(read(fake, "resistance"))
    set_output(fake, 1, True)
    for function in ("cc", "cr", "cp"):
        fake.handle_text(
            engine.setter_command(profile.setters["function"], ch=1, function=function)
        )
        assert read(fake, "mode") == function
        assert read(fake, "current") == pytest.approx(2.0)
        assert read(fake, "resistance") == pytest.approx(6.0)
    fake.handle_text(engine.setter_command(profile.setters["function"], ch=1, function="cv"))
    fake.set_load_ohms(1, 1.0)
    assert read(fake, "voltage") == pytest.approx(10.0)
    assert read(fake, "current") == pytest.approx(2.0)
    fake.handle_text(engine.setter_command(profile.setters["function"], ch=1, function="cr"))
    fake.set_source_volts(1, 24.0)
    assert read(fake, "current") == pytest.approx(4.0)
    assert read(fake, "power") == pytest.approx(96.0)


def test_ac_source_drives_its_load():
    profile = build(AC_SOURCE)
    fake = FakeInstrument(profile)
    set_value(fake, "voltage", 1, 230.0)
    set_value(fake, "frequency", 1, 50.0)
    fake.set_load_ohms(1, 100.0)
    assert read(fake, "voltage") == 0.0
    set_output(fake, 1, True)
    readings = poll(fake)
    assert readings[("voltage", 1)] == pytest.approx(230.0)
    assert readings[("current", 1)] == pytest.approx(2.3)
    assert readings[("power", 1)] == pytest.approx(529.0)
    assert readings[("apparent_power", 1)] == pytest.approx(529.0)
    assert readings[("power_factor", 1)] == 1.0
    assert readings[("frequency", 1)] == 50.0
    assert readings[("frequency_set", 1)] == 50.0
    assert fake.handle_text("FREQ 30.0") is None
    assert fake.handle_text("FREQ?") == "50"


def test_scenario_arguments_are_checked(dp800):
    fake = FakeInstrument(dp800)
    with pytest.raises(ValueError):
        fake.trip(1, "bogus")
    with pytest.raises(ValueError):
        fake.set_load_ohms(1, 0.0)
    with pytest.raises(ValueError):
        fake.clear_trip(2)


SYSTEM: dict[str, Any] = {
    "save": "MEM:SAVE {slot}",
    "recall": "MEM:LOAD {slot}",
    "slots": {"min": 1, "max": 5},
    "beep": "SYST:BEEP",
    "display": "DISP {on}",
    "display_text": 'DISP:TEXT "{text}"',
    "display_text_max": 12,
    "display_clear": "DISP:TEXT:CLE",
    "lock": "SYST:RWL",
    "unlock": "SYST:LOC",
    "reboot": "SYST:REB",
    "screenshot": ["HCOP:FORM PNG", "HCOP:DATA?"],
    "screenshot_cleanup": ["HCOP:DONE"],
    "setup_query": "SYST:SET?",
    "setup_load": "SYST:SET ",
}

NEW_PSU: dict[str, Any] = {
    "schema": 1,
    "id": "new-psu",
    "name": "Supply with every setter",
    "category": "psu",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": r"^NP\d", "channels": 2},
    "errors": {"query": "SYST:ERR?"},
    "ranges": {
        "voltage": {"max": 30, "digits": 2},
        "current": {"max": 5},
        "ovp_delay": {"max": 2, "digits": 3},
    },
    "signals": {
        "measure": {"query": "MEAS? {ch}", "parse": "floats", "fields": ["voltage", "current"]}
    },
    "state": {
        "voltage_set": {"query": "VOLT? {ch}", "parse": "float"},
        "output": {"query": "OUTP? {ch}", "parse": "bool"},
        "remote_sense": {"query": "SENS? {ch}", "parse": "bool"},
        "tracking": {
            "query": "TRAC? {ch}",
            "parse": "map",
            "map": {"INDEP": "independent", "SER": "series", "PAR": "parallel"},
        },
        "ovp_delay_set": {"query": "OVP:DEL? {ch}", "parse": "float"},
    },
    "setters": {
        "voltage": {"command": "VOLT {ch},{value}"},
        "output": {"command": "OUTP {ch},{on}"},
        "remote_sense": {"command": "SENS:ON {ch}", "off": "SENS:OFF {ch}"},
        "tracking": {
            "command": "TRAC {ch},{value}",
            "map": {"independent": "INDEP", "series": "SER", "parallel": "PAR"},
        },
        "ovp_delay": {"command": "OVP:DEL {ch},{value}"},
        "clear": {"command": "OUTP:PROT:CLE {ch}"},
    },
    "system": SYSTEM,
}

NEW_LOAD: dict[str, Any] = copy.deepcopy(LOAD)
NEW_LOAD.update({"id": "new-load", "errors": {"query": "SYST:ERR?"}, "system": SYSTEM})
NEW_LOAD["ranges"]["slew"] = {"max": 2.5e6, "digits": 3}
NEW_LOAD["state"].update(
    {
        "short": {"query": "INP:SHOR?", "parse": "bool"},
        "current_range": {
            "query": "CURR:RANG?",
            "parse": "map",
            "map": {"LOW": "low", "HIGH": "high"},
        },
        "voltage_range": {
            "query": "VOLT:RANG?",
            "parse": "map",
            "map": {"LOW": "low", "HIGH": "high"},
        },
        "slew_rise_set": {"query": "CURR:SLEW:POS?", "parse": "float", "scale": 1e6},
        "slew_fall_set": {"query": "CURR:SLEW:NEG?", "parse": "float", "scale": 1e6},
    }
)
NEW_LOAD["setters"].update(
    {
        "short": {"command": "INP:SHOR {on}"},
        "current_range": {"command": "CURR:RANG {value}", "map": {"low": "LOW", "high": "HIGH"}},
        "voltage_range": {"command": "VOLT:RANG {value}", "map": {"low": "LOW", "high": "HIGH"}},
        "slew_rise": {"command": "CURR:SLEW:POS {value}", "scale": 1e6},
        "slew_fall": {"command": "CURR:SLEW:NEG {value}", "scale": 1e6},
    }
)

ROOTS = {"dc_voltage": "VOLT:DC", "ac_voltage": "VOLT:AC", "resistance": "RES"}
RANGED_DMM: dict[str, Any] = {
    "schema": 1,
    "id": "ranged-dmm",
    "name": "Meter with ranges",
    "category": "dmm",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^DM2"},
    "errors": {"query": "SYST:ERR?"},
    "ranges": {"nplc": {"choices": [0.02, 0.2, 1, 10, 100]}},
    "signals": {"value": {"query": "READ?", "parse": "float"}},
    "state": {
        "function": {
            "query": "FUNC?",
            "parse": "map",
            "map": {word: label for label, word in ROOTS.items()},
        },
        "range": {"query": "SENS:RANG?", "parse": "float"},
        "autorange": {"query": "SENS:RANG:AUTO?", "parse": "bool"},
        "nplc": {"query": "SENS:NPLC?", "parse": "float"},
    },
    "setters": {
        "function": {"command": "CONF:{function}", "map": ROOTS},
        "range": {
            "command": "SENS:{function}:RANG {value}",
            "verify": "SENS:{function}:RANG?",
            "map": ROOTS,
        },
        "autorange": {
            "command": "SENS:{function}:RANG:AUTO {on}",
            "verify": "SENS:{function}:RANG:AUTO?",
            "map": ROOTS,
        },
        "nplc": {
            "command": "SENS:{function}:NPLC {value}",
            "verify": "SENS:{function}:NPLC?",
            "map": {"dc_voltage": "VOLT:DC", "resistance": "RES"},
        },
    },
    "dmm": {
        "function_query": "FUNC?",
        "function_map": {word: label for label, word in ROOTS.items()},
        "functions": {"dc_voltage": "V", "ac_voltage": "V", "resistance": "Ohm"},
    },
    "system": SYSTEM,
}
SIGLENT_SDS = Path(__file__).parents[1] / "profiles" / "scope" / "siglent-sds.toml"


def ask(fake: FakeInstrument, command: str) -> str:
    reply = fake.handle_text(command)
    assert reply is not None, command
    return reply


def run(fake: FakeInstrument, *commands: str) -> None:
    for command in commands:
        assert fake.handle_text(command) is None, command


def codes(fake: FakeInstrument) -> list[int | None]:
    return [e.code for e in drain(fake)]


def test_a_switch_with_its_own_off_command_routes_both():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "SENS:ON 1")
    assert read(fake, "remote_sense", 1) is True
    assert read(fake, "remote_sense", 2) is False
    run(fake, "SENS:OFF 1")
    assert read(fake, "remote_sense", 1) is False
    assert codes(fake) == []


def test_the_engine_sends_off_for_a_switch_with_its_own_off_command():
    profile = build(NEW_PSU)
    fake = FakeInstrument(profile)
    setter = profile.setters["remote_sense"]
    run(fake, engine.setter_command(setter, ch=2, on=True))
    assert read(fake, "remote_sense", 2) is True
    run(fake, engine.setter_command(setter, ch=2, on=False))
    assert read(fake, "remote_sense", 2) is False


def test_a_tracking_label_is_remembered_and_read_back_as_its_word():
    fake = FakeInstrument(build(NEW_PSU))
    assert ask(fake, "TRAC? 1") == "INDEP"
    run(fake, "TRAC 1,SER")
    assert read(fake, "tracking", 1) == "series"
    assert read(fake, "tracking", 2) == "independent"
    run(fake, "TRAC 1,BOGUS")
    assert codes(fake) == [-113]


def test_a_label_the_profile_does_not_spell_starts_as_one_it_does():
    raw = copy.deepcopy(NEW_PSU)
    raw["state"]["tracking"]["map"] = {"SER": "series", "PAR": "parallel"}
    fake = FakeInstrument(build(raw))
    assert read(fake, "tracking") == "series"


def test_an_over_voltage_delay_is_kept_in_seconds_and_bounded():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "OVP:DEL 1,0.25")
    assert read(fake, "ovp_delay_set", 1) == 0.25
    assert read(fake, "ovp_delay_set", 2) == 0.0
    run(fake, "OVP:DEL 1,2.5")
    assert read(fake, "ovp_delay_set", 1) == 0.25
    assert codes(fake) == [-222]


def test_a_shorted_load_reads_zero_volts_until_the_short_ends():
    fake = FakeInstrument(build(NEW_LOAD))
    assert read(fake, "voltage") == 12.0
    run(fake, "INP:SHOR ON")
    assert read(fake, "short") is True
    assert read(fake, "voltage") == 0.0
    assert read(fake, "power") == 0.0
    assert read(fake, "current") == pytest.approx(1.2)
    run(fake, "INP:SHOR OFF")
    assert read(fake, "short") is False
    assert read(fake, "voltage") == 12.0


def test_each_load_range_is_remembered_on_its_own():
    fake = FakeInstrument(build(NEW_LOAD))
    assert read(fake, "current_range") == "high"
    assert read(fake, "voltage_range") == "high"
    run(fake, "CURR:RANG LOW")
    assert read(fake, "current_range") == "low"
    assert read(fake, "voltage_range") == "high"
    run(fake, "VOLT:RANG LOW")
    assert read(fake, "voltage_range") == "low"


def test_a_load_slew_rate_is_sent_in_the_instrument_unit_and_kept_in_amps_per_second():
    profile = build(NEW_LOAD)
    fake = FakeInstrument(profile)
    setter = profile.setters["slew_rise"]
    range_ = profile.range_for("slew", fake.model, 1)
    run(fake, engine.setter_command(setter, ch=1, value=1.5e6, range_=range_))
    assert fake.transcript[-1][0] == "CURR:SLEW:POS 1.500"
    assert read(fake, "slew_rise_set") == 1.5e6
    assert read(fake, "slew_fall_set") == 0.0
    run(fake, "CURR:SLEW:NEG 2")
    assert read(fake, "slew_fall_set") == 2e6
    run(fake, "CURR:SLEW:POS 3")
    assert codes(fake) == [-222]
    assert read(fake, "slew_rise_set") == 1.5e6


def select_function(fake: FakeInstrument, word: str) -> None:
    run(fake, f"CONF:{word}")


def test_a_meter_picks_the_smallest_range_that_covers_the_request():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:DC:RANG 3")
    assert ask(fake, "SENS:VOLT:DC:RANG?") == "10"
    run(fake, "SENS:VOLT:DC:RANG 10")
    assert ask(fake, "SENS:VOLT:DC:RANG?") == "10"
    run(fake, "SENS:VOLT:DC:RANG 0.05")
    assert ask(fake, "SENS:VOLT:DC:RANG?") == "0.1"
    assert codes(fake) == []


def test_a_range_beyond_the_biggest_the_meter_has_is_refused():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:DC:RANG 1000", "SENS:VOLT:DC:RANG 1001", "SENS:RES:RANG 1e9")
    assert ask(fake, "SENS:VOLT:DC:RANG?") == "1000"
    assert codes(fake) == [-222, -222]


def test_each_function_keeps_its_own_range_autorange_and_integration_time():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:DC:RANG 5", "SENS:RES:RANG 5000", "SENS:RES:NPLC 10")
    assert ask(fake, "SENS:VOLT:DC:RANG?") == "10"
    assert ask(fake, "SENS:RES:RANG?") == "10000"
    assert ask(fake, "SENS:VOLT:AC:RANG?") == "10"
    assert ask(fake, "SENS:RES:NPLC?") == "10"
    assert ask(fake, "SENS:VOLT:DC:NPLC?") == "1"
    assert ask(fake, "SENS:VOLT:DC:RANG:AUTO?") == "0"
    assert ask(fake, "SENS:VOLT:AC:RANG:AUTO?") == "1"


def test_a_range_picked_by_hand_ends_autorange_and_autorange_can_return():
    fake = FakeInstrument(build(RANGED_DMM))
    assert read(fake, "autorange") is True
    run(fake, "SENS:VOLT:DC:RANG 1")
    assert read(fake, "autorange") is False
    run(fake, "SENS:VOLT:DC:RANG:AUTO ON")
    assert read(fake, "autorange") is True
    run(fake, "SENS:VOLT:DC:RANG:AUTO OFF")
    assert read(fake, "autorange") is False


def test_a_reader_without_a_function_reads_the_function_now_selected():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:DC:RANG 1", "SENS:RES:RANG 100", "SENS:RES:NPLC 100")
    assert read(fake, "range") == 1.0
    select_function(fake, "RES")
    assert read(fake, "function") == "resistance"
    assert read(fake, "range") == 100.0
    assert read(fake, "nplc") == 100.0
    assert read(fake, "autorange") is False


def test_an_integration_time_outside_the_choices_is_refused():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:DC:NPLC 0.2")
    assert ask(fake, "SENS:VOLT:DC:NPLC?") == "0.2"
    run(fake, "SENS:VOLT:DC:NPLC 5", "SENS:VOLT:DC:NPLC 0.02")
    assert ask(fake, "SENS:VOLT:DC:NPLC?") == "0.02"
    assert codes(fake) == [-222]


def test_a_function_without_an_integration_time_has_no_such_command():
    fake = FakeInstrument(build(RANGED_DMM))
    run(fake, "SENS:VOLT:AC:NPLC 1")
    assert codes(fake) == [-113]


def test_a_meter_command_without_a_map_names_the_function_by_its_reply_word():
    raw = copy.deepcopy(RANGED_DMM)
    for name in ("range", "autorange", "nplc"):
        del raw["setters"][name]["map"]
    fake = FakeInstrument(build(raw))
    run(fake, "SENS:VOLT:AC:RANG 100")
    assert ask(fake, "SENS:VOLT:AC:RANG?") == "100"
    run(fake, "SENS:RES:RANG 1000")
    assert codes(fake) == []


def test_a_meter_command_that_names_no_function_applies_to_the_one_selected():
    raw = copy.deepcopy(RANGED_DMM)
    raw["setters"]["range"] = {"command": "SENS:RANG {value}", "verify": "SENS:RANG?"}
    fake = FakeInstrument(build(raw))
    select_function(fake, "RES")
    run(fake, "SENS:RANG 5000")
    assert ask(fake, "SENS:RANG?") == "10000"
    assert read(fake, "range") == 10000.0
    select_function(fake, "VOLT:DC")
    assert read(fake, "range") == 10.0


def test_every_placeholder_a_template_can_hold_has_a_pattern():
    template = (
        'X {ch} {ch0} {value} {on} {bit} {slot} {type} {points} {interval} {src} "{text}" '
        "{channels} {tc} {count}"
    )
    found = _pattern(template).fullmatch(
        'X 1 0 2.5 ON 1 3 PKPK 100 2 4 "hello there" 101,102:105 K 6'
    )
    assert found is not None
    assert found["text"] == "hello there"
    assert found["channels"] == "101,102:105"
    assert _pattern("{modulation}", modulation=["AM", "FM"]).fullmatch("fm") is not None
    assert _pattern("{modulation}", modulation=["AM"]).fullmatch("FM") is None
    assert _pattern("{function}", functions=["VOLT:DC"]).fullmatch("volt:dc") is not None
    assert _pattern("{modulation}").fullmatch("AM") is None


def test_the_profiles_own_reset_runs_and_the_ieee_reset_is_only_its_default():
    raw = copy.deepcopy(NEW_PSU)
    raw["system"] = {**SYSTEM, "reset": "SYST:PRES"}
    fake = FakeInstrument(build(raw))
    run(fake, "VOLT 1,5", "SYST:PRES")
    assert read(fake, "voltage_set", 1) == 0.0
    run(fake, "*RST")
    assert codes(fake) == [-113]


def test_a_profile_without_a_reset_has_none():
    raw = copy.deepcopy(NEW_PSU)
    raw["system"] = {"reset": False}
    fake = FakeInstrument(build(raw))
    run(fake, "*RST")
    assert codes(fake) == [-113]


def test_a_reset_returns_settings_extras_and_outputs_to_their_defaults():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "VOLT 1,5", "OUTP 1,ON", "SENS:ON 2", "TRAC 1,PAR", "OVP:DEL 2,1")
    assert read(fake, "output", 1) is True
    run(fake, "*RST")
    assert read(fake, "voltage_set", 1) == 0.0
    assert read(fake, "output", 1) is False
    assert read(fake, "remote_sense", 2) is False
    assert read(fake, "tracking", 1) == "independent"
    assert read(fake, "ovp_delay_set", 2) == 0.0
    assert codes(fake) == []


def test_a_reset_clears_a_trip(dp800):
    fake = FakeInstrument(dp800, "DP832")
    fake.trip(1, "ocp")
    assert poll(fake)[("ocp", 1)] is True
    run(fake, "*RST")
    assert poll(fake)[("ocp", 1)] is False


def test_a_reset_leaves_the_world_around_the_channels_alone():
    fake = FakeInstrument(build(NEW_PSU))
    fake.set_load_ohms(1, 4.0)
    run(fake, "*RST", "VOLT 1,5", "OUTP 1,ON")
    assert read(fake, "current") == pytest.approx(1.25)
    load = FakeInstrument(build(NEW_LOAD))
    load.set_source_volts(1, 20.0)
    run(load, "INP:SHOR ON", "*RST")
    assert read(load, "voltage") == 20.0
    assert read(load, "short") is False


def test_a_reset_returns_a_meter_to_its_first_function_and_ranges():
    fake = FakeInstrument(build(RANGED_DMM))
    select_function(fake, "RES")
    run(fake, "SENS:RES:RANG 100", "*RST")
    assert read(fake, "function") == "dc_voltage"
    assert ask(fake, "SENS:RES:RANG?") == "100000"


def test_a_reset_keeps_the_error_queue():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "NOPE", "*RST")
    assert codes(fake) == [-113]


def test_the_operation_complete_query_answers_one_unless_the_profile_has_none():
    raw = copy.deepcopy(NEW_PSU)
    assert ask(FakeInstrument(build(raw)), "*OPC?") == "1"
    raw["system"] = {"opc": "SYST:DONE?"}
    fake = FakeInstrument(build(raw))
    assert ask(fake, "SYST:DONE?") == "1"
    run(fake, "*OPC?")
    assert codes(fake) == [-113]
    raw["system"] = {"opc": False}
    fake = FakeInstrument(build(raw))
    run(fake, "*OPC?")
    assert codes(fake) == [-113]


@pytest.mark.parametrize(
    ("ok", "reply"), [(None, "0"), (r"^\+0$", "+0"), (r"^PASS$", "PASS"), (r"^0,", "0,")]
)
def test_a_self_test_replies_in_the_form_the_profile_calls_a_pass(ok, reply):
    raw = copy.deepcopy(NEW_PSU)
    raw["system"] = {"self_test_ok": ok} if ok is not None else {}
    profile = build(raw)
    got = ask(FakeInstrument(profile), "*TST?")
    assert got == reply
    assert profile.system.self_test_ok.search(got)


def test_an_empty_error_queue_answers_in_the_form_the_profile_reads_as_none():
    raw = copy.deepcopy(NEW_PSU)
    raw["errors"] = {"query": "SYST:ERR?", "ok": r"^\+0,"}
    profile = build(raw)
    assert profile.errors is not None
    fake = FakeInstrument(profile)
    assert engine.read_error(profile.errors, ask(fake, "SYST:ERR?")) is None
    run(fake, "NOPE")
    error = engine.read_error(profile.errors, ask(fake, "SYST:ERR?"))
    assert error is not None and error.code == -113


def test_a_second_error_query_always_says_there_is_no_error():
    raw = copy.deepcopy(NEW_PSU)
    raw["errors"] = {"query": ["SYST:ERR?", "SYST:ERR:EXT?"]}
    fake = FakeInstrument(build(raw))
    run(fake, "NOPE")
    assert ask(fake, "SYST:ERR:EXT?") == '0,"No error"'
    assert ask(fake, "SYST:ERR?") == '-113,"Undefined header"'


def test_a_saved_state_comes_back_with_its_outputs_on():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "VOLT 1,5", "OUTP 1,ON", "SENS:ON 1", "TRAC 2,SER", "MEM:SAVE 3")
    run(fake, "VOLT 1,9", "OUTP 1,OFF", "SENS:OFF 1", "TRAC 2,PAR")
    run(fake, "MEM:LOAD 3")
    assert read(fake, "voltage_set", 1) == 5.0
    assert read(fake, "output", 1) is True
    assert read(fake, "remote_sense", 1) is True
    assert read(fake, "tracking", 2) == "series"
    assert codes(fake) == []


def test_each_slot_keeps_its_own_state_and_a_slot_can_be_recalled_twice():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "VOLT 1,1", "MEM:SAVE 1", "VOLT 1,2", "MEM:SAVE 2", "VOLT 1,3")
    run(fake, "MEM:LOAD 1")
    assert read(fake, "voltage_set") == 1.0
    run(fake, "VOLT 1,7", "MEM:LOAD 1")
    assert read(fake, "voltage_set") == 1.0
    run(fake, "MEM:LOAD 2")
    assert read(fake, "voltage_set") == 2.0


def test_a_recall_keeps_the_load_around_the_channel():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "VOLT 1,5", "OUTP 1,ON", "MEM:SAVE 1")
    fake.set_load_ohms(1, 4.0)
    run(fake, "MEM:LOAD 1")
    assert read(fake, "current") == pytest.approx(1.25)


def test_a_slot_outside_the_profiles_slots_is_out_of_range_and_an_empty_one_is_not_found():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "MEM:SAVE 0", "MEM:SAVE 6", "MEM:LOAD 9", "MEM:LOAD 2")
    assert codes(fake) == [-222, -222, -222, -256]


def test_an_unlock_spelled_like_the_remote_command_unlocks_and_the_remote_check_answers():
    raw = copy.deepcopy(NEW_PSU)
    raw["link"] = {
        "remote": "SYST:REM",
        "remote_check": {"query": "SYST:LOCK:OWN?", "expect": "REMOTE"},
    }
    raw["system"] = {**raw["system"], "lock": "SYST:RWL", "unlock": "SYST:REM"}
    fake = FakeInstrument(build(raw))
    run(fake, "SYST:RWL")
    assert fake.locked is True
    run(fake, "SYST:REM")
    assert fake.locked is False
    assert fake.handle_text("SYST:LOCK:OWN?") == "REMOTE"
    assert codes(fake) == []


def test_a_profile_without_save_has_no_save():
    fake = FakeInstrument(build(SELECT_PSU))
    run(fake, "MEM:SAVE 1")
    assert codes(fake) == [-113]


def test_the_panel_functions_are_accepted_and_remembered():
    fake = FakeInstrument(build(NEW_PSU))
    assert (fake.beeps, fake.display_on, fake.display_message) == (0, True, "")
    assert (fake.locked, fake.rebooted) == (False, False)
    run(fake, "SYST:BEEP", "SYST:BEEP", "DISP OFF", 'DISP:TEXT "BENCH 3"', "SYST:RWL")
    assert fake.beeps == 2
    assert fake.display_on is False
    assert fake.display_message == "BENCH 3"
    assert fake.locked is True
    run(fake, "SYST:LOC", "DISP ON", "DISP:TEXT:CLE", "SYST:REB")
    assert fake.locked is False
    assert fake.display_on is True
    assert fake.display_message == ""
    assert fake.rebooted is True
    assert codes(fake) == []


def test_a_text_longer_than_the_display_takes_is_too_much_data():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, 'DISP:TEXT "123456789012"')
    assert fake.display_message == "123456789012"
    run(fake, 'DISP:TEXT "1234567890123"')
    assert fake.display_message == "123456789012"
    assert codes(fake) == [-223]


def test_a_display_text_may_be_empty():
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, 'DISP:TEXT "HI"', 'DISP:TEXT ""')
    assert fake.display_message == ""


def test_a_display_that_takes_a_bit_reads_the_bit():
    raw = copy.deepcopy(NEW_PSU)
    raw["system"] = {**SYSTEM, "display": "DISP:ENAB {bit}"}
    fake = FakeInstrument(build(raw))
    run(fake, "DISP:ENAB 0")
    assert fake.display_on is False
    run(fake, "DISP:ENAB 1")
    assert fake.display_on is True


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_chunks(data: bytes) -> list[bytes]:
    assert data.startswith(PNG_MAGIC)
    kinds = []
    at = len(PNG_MAGIC)
    while at < len(data):
        (size,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4 : at + 8]
        body = data[at + 8 : at + 8 + size]
        (crc,) = struct.unpack_from(">I", data, at + 8 + size)
        assert crc == zlib.crc32(kind + body)
        kinds.append(kind)
        at += 12 + size
    assert at == len(data)
    return kinds


def screenshot(framing: str) -> bytes:
    raw = copy.deepcopy(NEW_PSU)
    raw["system"] = {**SYSTEM, "screenshot_framing": framing}
    fake = FakeInstrument(build(raw))
    run(fake, "HCOP:FORM PNG")
    reply = fake.handle("HCOP:DATA?")
    assert isinstance(reply, bytes)
    run(fake, "HCOP:DONE")
    assert codes(fake) == []
    return reply


def test_a_block_screenshot_is_a_definite_length_block_of_a_png():
    reply = screenshot("block")
    digits = int(reply[1:2])
    length = int(reply[2 : 2 + digits])
    payload = reply[2 + digits :]
    assert reply[:1] == b"#" and len(payload) == length
    assert png_chunks(payload) == [b"IHDR", b"IDAT", b"IEND"]


def test_a_png_screenshot_is_a_bare_png_that_ends_at_its_end_chunk():
    reply = screenshot("png")
    assert png_chunks(reply)[-1] == b"IEND"
    assert reply.endswith(b"IEND\xaeB`\x82")


def test_a_bmp_screenshot_states_its_own_size():
    reply = screenshot("bmp")
    assert reply[:2] == b"BM"
    assert struct.unpack_from("<I", reply, 2)[0] == len(reply)
    offset = struct.unpack_from("<I", reply, 10)[0]
    width, height, planes, bits = struct.unpack_from("<iiHH", reply, 18)
    assert (width, height, planes, bits) == (1, 1, 1, 24)
    assert len(reply) - offset >= 3


def test_a_screenshot_is_recorded_in_the_transcript_as_a_block():
    fake = FakeInstrument(build(NEW_PSU))
    fake.handle("HCOP:DATA?")
    command, reply = fake.transcript[-1]
    assert command == "HCOP:DATA?"
    assert reply is not None and reply.startswith("<block ")


def setup_block(fake: FakeInstrument) -> bytes:
    reply = fake.handle("SYST:SET?")
    assert isinstance(reply, bytes)
    return reply


def test_a_setup_block_carries_the_settings_and_restores_them_in_another_fake():
    profile = build(NEW_PSU)
    first = FakeInstrument(profile)
    run(first, "VOLT 1,12", "OUTP 2,ON", "TRAC 2,PAR", "SENS:ON 1")
    block = setup_block(first)
    digits = int(block[1:2])
    assert int(block[2 : 2 + digits]) == len(block) - 2 - digits
    second = FakeInstrument(profile)
    run(second, "SYST:SET " + block.decode())
    assert read(second, "voltage_set", 1) == 12.0
    assert read(second, "output", 2) is True
    assert read(second, "tracking", 2) == "parallel"
    assert read(second, "remote_sense", 1) is True
    assert codes(second) == []


def test_a_setup_load_may_follow_its_command_without_a_space():
    profile = build(NEW_PSU)
    first = FakeInstrument(profile)
    run(first, "VOLT 1,3")
    block = setup_block(first).decode()
    second = FakeInstrument(profile)
    run(second, "SYST:SET" + block)
    assert read(second, "voltage_set", 1) == 3.0


@pytest.mark.parametrize(
    "block",
    ["#0{}", "#9", "#215{}", "#3010{}", "#14{}", "#1x{}"],
    ids=["indefinite", "short", "too-short", "wrong", "not-json", "no-length"],
)
def test_a_bad_setup_block_is_refused_and_changes_nothing(block: str):
    fake = FakeInstrument(build(NEW_PSU))
    run(fake, "VOLT 1,4")
    run(fake, "SYST:SET " + block.format("{}"))
    assert read(fake, "voltage_set", 1) == 4.0
    assert codes(fake) == [-224]


def test_a_setup_for_another_number_of_channels_is_refused():
    profile = build(NEW_PSU)
    block = setup_block(FakeInstrument(profile, channels=1)).decode()
    fake = FakeInstrument(profile, channels=2)
    run(fake, "VOLT 1,4", "SYST:SET " + block)
    assert read(fake, "voltage_set", 1) == 4.0
    assert codes(fake) == [-224]


def siglent_with(**system: Any) -> Profile:
    scope = load_file(SIGLENT_SDS)
    return dataclasses.replace(scope, system=dataclasses.replace(scope.system, **system))


def test_a_scope_setup_block_carries_every_channels_settings():
    scope = siglent_with(setup_query="SYST:SET?", setup_load="SYST:SET ")
    first = FakeInstrument(scope)
    run(first, ":CHANnel1:SCALe 2", ":CHANnel2:SCALe 0.2")
    second = FakeInstrument(scope)
    run(second, "SYST:SET " + setup_block(first).decode())
    assert read(second, "scale", 1) == 2.0
    assert read(second, "scale", 2) == 0.2


def test_a_scope_reset_and_a_scope_recall_restore_its_channel_settings():
    fake = FakeInstrument(
        siglent_with(save="MEM:SAVE {slot}", recall="MEM:LOAD {slot}", slots=(1, 3))
    )
    run(fake, ":CHANnel1:SCALe 2", "MEM:SAVE 1", ":CHANnel1:SCALe 5")
    assert read(fake, "scale", 1) == 5.0
    run(fake, "MEM:LOAD 1")
    assert read(fake, "scale", 1) == 2.0
    run(fake, "*RST")
    assert read(fake, "scale", 1) == 0.5
    assert codes(fake) == []


def test_a_scope_still_answers_its_own_commands_after_a_reset():
    fake = FakeInstrument(load_file(SIGLENT_SDS))
    run(fake, "*RST", ":CHANnel1:SCALe 1")
    assert read(fake, "scale", 1) == 1.0


def daq_fake() -> FakeInstrument:
    raw = copy.deepcopy(KEYSIGHT_STYLE)
    raw["system"] = SYSTEM
    return FakeInstrument(build(raw))


def test_a_daq_profile_makes_a_fake_that_hands_its_own_commands_to_the_unit():
    fake = daq_fake()
    assert isinstance(fake.daq, FakeDaq)
    assert fake.handle_text("*IDN?") == fake.idn == fake.daq.idn
    run(fake, "CONF:VOLT:DC AUTO,DEF,(@101,102)", "ROUT:SCAN (@101,102)")
    assert fake.daq.scan_list == (101, 102)
    assert len((fake.handle_text("READ?") or "").split(",")) == 2
    assert fake.transcript is fake.daq.transcript
    assert [c for c, _ in fake.transcript][-1] == "READ?"


def test_a_daq_fakes_system_commands_run_here_and_their_errors_land_in_the_units_queue():
    fake = daq_fake()
    run(fake, "SYST:BEEP", "MEM:SAVE 9", "NOPE")
    assert fake.beeps == 1
    assert fake.handle_text("SYST:ERR?") == '-222,"Data out of range"'
    assert fake.handle_text("SYST:ERR?") == '-113,"Undefined header"'
    assert fake.handle_text("SYST:ERR?") == '0,"No error"'


def test_a_daq_reset_forgets_its_scan_and_a_recall_brings_it_back():
    fake = daq_fake()
    run(fake, "CONF:VOLT:DC AUTO,DEF,(@101,102)", "ROUT:SCAN (@101,102)", "ROUT:CLOSE (@103)")
    run(fake, "MEM:SAVE 1", "*RST")
    assert fake.daq is not None
    assert fake.daq.functions == {} and fake.daq.closed == set() and fake.daq.scan_list == ()
    run(fake, "MEM:LOAD 1")
    assert fake.daq.functions == {101: "dc_voltage", 102: "dc_voltage"}
    assert fake.daq.scan_list == (101, 102)
    assert fake.daq.closed == {103}
    assert fake.handle_text("SYST:ERR?") == '0,"No error"'


def test_a_daq_screenshot_and_setup_block_work_through_the_wrapper():
    fake = daq_fake()
    assert isinstance(fake.handle("HCOP:DATA?"), bytes)
    run(fake, "ROUT:CLOSE (@104)")
    other = daq_fake()
    run(other, "SYST:SET " + setup_block(fake).decode())
    assert other.daq is not None and other.daq.closed == {104}


def test_a_command_setter_may_send_a_list_and_each_command_is_routed_per_channel():
    raw = copy.deepcopy(NEW_PSU)
    raw["setters"]["clear"] = {"command": ["OUTP:PROT:CLE {ch}", "OVP:CLE {ch}"]}
    profile = build(raw)
    fake = FakeInstrument(profile)
    commands = engine.clear_commands(profile.setters["clear"], ch=2)
    assert commands == ["OUTP:PROT:CLE 2", "OVP:CLE 2"]
    run(fake, *commands)
    assert codes(fake) == []
    run(fake, "OVP:CLE 3")
    assert codes(fake) == [-222]


def test_a_command_setter_for_the_whole_instrument_clears_every_channel():
    raw = copy.deepcopy(NEW_PSU)
    raw["setters"]["clear"] = {"command": "PROT:CLE", "channel": False}
    raw["state"]["protection"] = {
        "flags": [{"query": "PROT? {ch}", "parse": "bool", "value": "ocp"}]
    }
    fake = FakeInstrument(build(raw))
    fake.trip(1, "ocp")
    fake.trip(2, "ocp")
    assert poll(fake)[("ocp", 2)] is True
    run(fake, "PROT:CLE")
    assert [poll(fake)[("ocp", ch)] for ch in (1, 2)] == [False, False]


def test_a_scope_trigger_command_may_name_its_source_channel():
    raw = tomllib.loads(SIGLENT_SDS.read_text(encoding="utf-8"))
    raw["id"] = "src-scope"
    raw["setters"]["trigger_level"]["command"] = ":TRIGger:EDGE:LEVel:CH{src} {value}"
    fake = FakeInstrument(build(raw))
    run(fake, ":TRIGger:EDGE:LEVel:CH2 0.25")
    assert read(fake, "trigger_level") == 0.25
    assert codes(fake) == []
