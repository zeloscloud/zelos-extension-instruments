"""The fake instrument a profile builds: replies, errors, scenarios and faults."""

import math
from pathlib import Path
from typing import Any

import pytest

from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import VERIFY_FIELD, Profile, parse_idn
from zelos_extension_instruments.profile_load import from_dict, load_file
from zelos_extension_instruments.sim.fake import FakeInstrument

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
        reader = dp800.reader(VERIFY_FIELD[name])
        assert query is not None and reader is not None
        readback = engine.parse_reply(reader, fake.handle_text(engine.render(query, ch=2)) or "")
        assert engine.verified(value, readback[VERIFY_FIELD[name]], range_)
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
