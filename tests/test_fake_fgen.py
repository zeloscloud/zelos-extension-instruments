"""The fake function generator a profile builds: settings per channel, keyed and quoted replies,
modulation types, switches, triggers and saved states."""

import copy
from typing import Any

import pytest

from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import Profile
from zelos_extension_instruments.profile_load import from_dict
from zelos_extension_instruments.sim.fake import FakeInstrument

EVIDENCE = {"badge": "manual", "source": "test", "date": "2026-10-22"}

SHAPES = {
    "sine": "SIN",
    "square": "SQU",
    "ramp": "RAMP",
    "pulse": "PULSE",
    "noise": "NOIS",
    "dc": "DC",
    "arbitrary": "USER",
}
MODULATIONS = {"am": "AM", "fm": "FM", "pm": "PM"}
RANGES: dict[str, Any] = {
    "frequency": {"min": 1e-6, "max": 25e6, "digits": 6},
    "amplitude": {"min": 0.001, "max": 10, "digits": 3},
    "offset": {"min": -5, "max": 5, "digits": 3},
    "high": {"min": -5, "max": 5, "digits": 3},
    "low": {"min": -5, "max": 5, "digits": 3},
    "phase": {"min": 0, "max": 360, "digits": 2},
    "duty": {"min": 0, "max": 100, "digits": 2},
    "symmetry": {"min": 0, "max": 100, "digits": 2},
    "width": {"min": 1e-8, "max": 1, "digits": 9},
    "modulation_depth": {"min": 0, "max": 120, "digits": 1},
    "modulation_deviation": {"min": 0, "max": 1e6, "digits": 3},
    "phase_deviation": {"min": 0, "max": 360, "digits": 2},
    "modulation_frequency": {"min": 0.002, "max": 1e5, "digits": 3},
    "burst_cycles": {"min": 1, "max": 1000, "digits": 0},
    "burst_period": {"min": 1e-6, "max": 500, "digits": 6},
    "sweep_time": {"min": 1e-3, "max": 500, "digits": 3},
}


def _switch(prefix: str) -> dict[str, Any]:
    return {"command": f"{prefix} {{on}}"}


def _value(command: str) -> dict[str, Any]:
    return {"command": f"{command} {{value}}"}


def _float(query: str, index: int | None = None) -> dict[str, Any]:
    reader: dict[str, Any] = {"query": query, "parse": "float"}
    if index is not None:
        reader["index"] = index
    return reader


def _label(query: str, words: dict[str, str], index: int | None = None) -> dict[str, Any]:
    reader: dict[str, Any] = {
        "query": query,
        "parse": "map",
        "map": {word: label for label, word in words.items()},
    }
    if index is not None:
        reader["index"] = index
    return reader


# SCPI SOURce style: one positional `APPL?` list, a modulation type named in the command, and a
# query per switch.
RIGOL_STYLE: dict[str, Any] = {
    "schema": 1,
    "id": "scpi-fgen",
    "name": "SCPI generator",
    "category": "fgen",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^FG\\d", "channels": 2},
    "errors": {"query": "SYST:ERR?"},
    "ranges": RANGES,
    "state": {
        "output": {"query": ":OUTP{ch}?", "parse": "bool", "map": {"ON": "on", "OFF": "off"}},
        "shape": _label(":SOUR{ch}:APPL?", SHAPES, 0),
        "frequency_set": _float(":SOUR{ch}:APPL?", 1),
        "amplitude_set": _float(":SOUR{ch}:APPL?", 2),
        "offset_set": _float(":SOUR{ch}:APPL?", 3),
        "phase_set": _float(":SOUR{ch}:APPL?", 4),
        "high_set": _float(":SOUR{ch}:VOLT:HIGH?"),
        "low_set": _float(":SOUR{ch}:VOLT:LOW?"),
        "duty_set": _float(":SOUR{ch}:FUNC:SQU:DCYC?"),
        "symmetry_set": _float(":SOUR{ch}:FUNC:RAMP:SYMM?"),
        "width_set": _float(":SOUR{ch}:FUNC:PULSE:WIDTH?"),
        "output_load": _label(":OUTP{ch}:LOAD?", {"fifty_ohm": "50", "high_z": "INF"}),
        "modulation": {
            "query": ":SOUR{ch}:MOD:STAT?",
            "parse": "bool",
            "map": {"ON": "on", "OFF": "off"},
        },
        "modulation_type": _label(":SOUR{ch}:MOD:TYP?", MODULATIONS),
        "burst": {
            "query": ":SOUR{ch}:BURS:STAT?",
            "parse": "bool",
            "map": {"ON": "on", "OFF": "off"},
        },
        "burst_cycles_set": _float(":SOUR{ch}:BURS:NCYC?"),
        "burst_period_set": _float(":SOUR{ch}:BURS:INT:PER?"),
        "sweep": {
            "query": ":SOUR{ch}:SWE:STAT?",
            "parse": "bool",
            "map": {"ON": "on", "OFF": "off"},
        },
        "sweep_start_set": _float(":SOUR{ch}:FREQ:STAR?"),
        "sweep_stop_set": _float(":SOUR{ch}:FREQ:STOP?"),
        "sweep_time_set": _float(":SOUR{ch}:SWE:TIME?"),
    },
    "setters": {
        "output": _switch(":OUTP{ch}"),
        "shape": {**_value(":SOUR{ch}:FUNC"), "map": SHAPES},
        "frequency": _value(":SOUR{ch}:FREQ"),
        "amplitude": _value(":SOUR{ch}:VOLT"),
        "offset": _value(":SOUR{ch}:VOLT:OFFS"),
        "high": _value(":SOUR{ch}:VOLT:HIGH"),
        "low": _value(":SOUR{ch}:VOLT:LOW"),
        "phase": _value(":SOUR{ch}:PHAS"),
        "duty": _value(":SOUR{ch}:FUNC:SQU:DCYC"),
        "symmetry": _value(":SOUR{ch}:FUNC:RAMP:SYMM"),
        "width": _value(":SOUR{ch}:FUNC:PULSE:WIDTH"),
        "output_load": {**_value(":OUTP{ch}:LOAD"), "map": {"fifty_ohm": "50", "high_z": "INF"}},
        "modulation": _switch(":SOUR{ch}:MOD:STAT"),
        "modulation_type": {**_value(":SOUR{ch}:MOD:TYP"), "map": MODULATIONS},
        "modulation_depth": {
            **_value(":SOUR{ch}:{modulation}:DEPT"),
            "verify": ":SOUR{ch}:{modulation}:DEPT?",
        },
        "modulation_deviation": {
            **_value(":SOUR{ch}:{modulation}:DEV"),
            "verify": ":SOUR{ch}:{modulation}:DEV?",
        },
        "phase_deviation": {
            **_value(":SOUR{ch}:{modulation}:DEV"),
            "verify": ":SOUR{ch}:{modulation}:DEV?",
        },
        "modulation_frequency": {
            **_value(":SOUR{ch}:{modulation}:INT:FREQ"),
            "verify": ":SOUR{ch}:{modulation}:INT:FREQ?",
        },
        "burst": _switch(":SOUR{ch}:BURS:STAT"),
        "burst_cycles": _value(":SOUR{ch}:BURS:NCYC"),
        "burst_period": _value(":SOUR{ch}:BURS:INT:PER"),
        "sweep": _switch(":SOUR{ch}:SWE:STAT"),
        "sweep_start": _value(":SOUR{ch}:FREQ:STAR"),
        "sweep_stop": _value(":SOUR{ch}:FREQ:STOP"),
        "sweep_time": _value(":SOUR{ch}:SWE:TIME"),
        "trigger": {"command": ":SOUR{ch}:BURS:TRIG"},
    },
}


def _keyed(query: str, key: str, words: dict[str, str] | None = None) -> dict[str, Any]:
    reader: dict[str, Any] = {"query": query, "parse": "keyed", "key": key}
    if words is not None:
        reader["map"] = {word: label for label, word in words.items()}
    return reader


_ON_OFF = {"on": "ON", "off": "OFF"}
SIGLENT_SHAPES = {
    "sine": "SINE",
    "square": "SQUARE",
    "ramp": "RAMP",
    "pulse": "PULSE",
    "noise": "NOISE",
    "dc": "DC",
    "arbitrary": "ARB",
}

# Key and value pairs after a header, with a unit on each number, and a command per key.
SIGLENT_STYLE: dict[str, Any] = {
    "schema": 1,
    "id": "keyed-fgen",
    "name": "Keyed generator",
    "category": "fgen",
    "evidence": EVIDENCE,
    "match": {"manufacturer": "(?i)^acme", "model": "^KG\\d", "channels": 2},
    "errors": {"query": "SYST:ERR?"},
    "ranges": RANGES,
    "state": {
        "output": {"query": "C{ch}:OUTP?", "parse": "bool", "map": {"ON": "on", "OFF": "off"}},
        "shape": _keyed("C{ch}:BSWV?", "WVTP", SIGLENT_SHAPES),
        "frequency_set": _keyed("C{ch}:BSWV?", "FRQ"),
        "amplitude_set": _keyed("C{ch}:BSWV?", "AMP"),
        "offset_set": _keyed("C{ch}:BSWV?", "OFST"),
        "high_set": _keyed("C{ch}:BSWV?", "HLEV"),
        "low_set": _keyed("C{ch}:BSWV?", "LLEV"),
        "phase_set": _keyed("C{ch}:BSWV?", "PHSE"),
        "duty_set": _keyed("C{ch}:BSWV?", "DUTY"),
        "modulation": _keyed("C{ch}:MDWV?", "STATE", _ON_OFF),
        "modulation_depth_set": _keyed("C{ch}:MDWV?", "DEPTH"),
        "burst": _keyed("C{ch}:BTWV?", "STATE", _ON_OFF),
        "burst_cycles_set": _keyed("C{ch}:BTWV?", "TIME"),
        "burst_period_set": _keyed("C{ch}:BTWV?", "PRD"),
        "sweep": _keyed("C{ch}:SWWV?", "STATE", _ON_OFF),
        "sweep_time_set": _keyed("C{ch}:SWWV?", "TIME"),
    },
    "setters": {
        "output": _switch("C{ch}:OUTP"),
        "shape": {"command": "C{ch}:BSWV WVTP,{value}", "map": SIGLENT_SHAPES},
        "frequency": {"command": "C{ch}:BSWV FRQ,{value}"},
        "amplitude": {"command": "C{ch}:BSWV AMP,{value}"},
        "offset": {"command": "C{ch}:BSWV OFST,{value}"},
        "high": {"command": "C{ch}:BSWV HLEV,{value}"},
        "low": {"command": "C{ch}:BSWV LLEV,{value}"},
        "phase": {"command": "C{ch}:BSWV PHSE,{value}"},
        "duty": {"command": "C{ch}:BSWV DUTY,{value}"},
        "modulation": {"command": "C{ch}:MDWV STATE,{on}"},
        "modulation_depth": {"command": "C{ch}:MDWV DEPTH,{value}"},
        "burst": {"command": "C{ch}:BTWV STATE,{on}"},
        "burst_cycles": {"command": "C{ch}:BTWV TIME,{value}"},
        "burst_period": {"command": "C{ch}:BTWV PRD,{value}"},
        "sweep": {"command": "C{ch}:SWWV STATE,{on}"},
        "sweep_time": {"command": "C{ch}:SWWV TIME,{value}"},
        "trigger": {"command": "C{ch}:BTWV MTRIG"},
    },
}


def build(raw: dict[str, Any]) -> Profile:
    return from_dict(raw, f"{raw['id']}.toml")


def ask(fake: FakeInstrument, command: str) -> str:
    reply = fake.handle_text(command)
    assert reply is not None, command
    return reply


def drain(fake: FakeInstrument) -> list[engine.InstrumentError]:
    errors = fake.profile.errors
    assert errors is not None
    found = []
    while (error := engine.read_error(errors, ask(fake, errors.query))) is not None:
        found.append(error)
    return found


def read(fake: FakeInstrument, field: str, ch: int = 1) -> Any:
    reader = fake.profile.reader(field)
    assert reader is not None and reader.query is not None
    reply = ask(fake, engine.render(reader.query, ch=ch))
    return engine.parse_reply(reader, reply, ch)[field]


@pytest.fixture
def scpi() -> FakeInstrument:
    return FakeInstrument(build(RIGOL_STYLE))


@pytest.fixture
def keyed() -> FakeInstrument:
    return FakeInstrument(build(SIGLENT_STYLE))


def test_a_generator_starts_with_its_output_off_on_every_channel(scpi: FakeInstrument) -> None:
    assert scpi.fgen is not None
    assert scpi.channels == 2
    assert [read(scpi, "output", ch) for ch in (1, 2)] == [False, False]
    assert read(scpi, "shape") == "sine"
    assert read(scpi, "output_load") == "high_z"


def test_a_setting_lands_on_its_channel_only(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR2:FREQ 2500")
    ask_none(scpi, ":OUTP2 ON")
    assert read(scpi, "frequency_set", 2) == 2500.0
    assert read(scpi, "frequency_set", 1) == 1000.0
    assert read(scpi, "output", 2) is True
    assert read(scpi, "output", 1) is False


def ask_none(fake: FakeInstrument, command: str) -> None:
    assert fake.handle_text(command) is None, command


def test_a_positional_query_answers_one_quoted_list(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:FUNC SQU")
    ask_none(scpi, ":SOUR1:FREQ 5000")
    ask_none(scpi, ":SOUR1:VOLT 3")
    ask_none(scpi, ":SOUR1:VOLT:OFFS -1")
    ask_none(scpi, ":SOUR1:PHAS 90")
    assert ask(scpi, ":SOUR1:APPL?") == '"SQU,5000,3,-1,90"'
    assert read(scpi, "shape") == "square"
    assert read(scpi, "frequency_set") == 5000.0
    assert read(scpi, "offset_set") == -1.0
    assert drain(scpi) == []


def test_a_single_field_query_is_not_quoted(scpi: FakeInstrument) -> None:
    assert ask(scpi, ":SOUR1:FUNC:SQU:DCYC?") == "50"


def test_high_and_low_follow_amplitude_and_offset_and_back(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:VOLT 4")
    ask_none(scpi, ":SOUR1:VOLT:OFFS 1")
    assert (read(scpi, "high_set"), read(scpi, "low_set")) == (3.0, -1.0)
    ask_none(scpi, ":SOUR1:VOLT:HIGH 2")
    ask_none(scpi, ":SOUR1:VOLT:LOW 0")
    assert read(scpi, "amplitude_set") == 2.0
    assert read(scpi, "offset_set") == 1.0
    assert (read(scpi, "high_set"), read(scpi, "low_set")) == (2.0, 0.0)


def test_labels_are_read_back_as_their_words_map(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":OUTP1:LOAD 50")
    ask_none(scpi, ":SOUR1:FUNC pulse")
    assert read(scpi, "output_load") == "fifty_ohm"
    assert read(scpi, "shape") == "pulse"
    assert ask(scpi, ":OUTP1:LOAD?") == "50"
    assert read(scpi, "output_load", 2) == "high_z"


def test_a_value_outside_its_range_queues_an_error_and_keeps_the_old_one(
    scpi: FakeInstrument,
) -> None:
    ask_none(scpi, ":SOUR1:VOLT 11")
    ask_none(scpi, ":SOUR1:VOLT:OFFS -6")
    ask_none(scpi, ":SOUR1:BURS:NCYC 0")
    assert [e.code for e in drain(scpi)] == [-222, -222, -222]
    assert read(scpi, "amplitude_set") == 1.0
    assert read(scpi, "offset_set") == 0.0


def test_a_negative_offset_is_taken_when_the_range_starts_below_zero(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:VOLT:OFFS -5")
    assert read(scpi, "offset_set") == -5.0
    assert drain(scpi) == []


def test_each_switch_is_its_own_and_per_channel(scpi: FakeInstrument) -> None:
    for command, field in (
        (":SOUR1:MOD:STAT", "modulation"),
        (":SOUR1:BURS:STAT", "burst"),
        (":SOUR1:SWE:STAT", "sweep"),
    ):
        ask_none(scpi, f"{command} ON")
        assert read(scpi, field) is True
    ask_none(scpi, ":SOUR1:BURS:STAT OFF")
    assert read(scpi, "burst") is False
    assert read(scpi, "modulation") is True
    assert read(scpi, "sweep", 2) is False


def test_burst_and_sweep_numbers_are_remembered(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:BURS:NCYC 25")
    ask_none(scpi, ":SOUR1:BURS:INT:PER 0.5")
    ask_none(scpi, ":SOUR1:FREQ:STAR 10")
    ask_none(scpi, ":SOUR1:FREQ:STOP 20000")
    ask_none(scpi, ":SOUR1:SWE:TIME 3")
    assert read(scpi, "burst_cycles_set") == 25.0
    assert read(scpi, "burst_period_set") == 0.5
    assert read(scpi, "sweep_start_set") == 10.0
    assert read(scpi, "sweep_stop_set") == 20000.0
    assert read(scpi, "sweep_time_set") == 3.0


def test_a_modulation_command_names_the_type_it_sets(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:AM:DEPT 40")
    ask_none(scpi, ":SOUR1:FM:DEV 2500")
    assert ask(scpi, ":SOUR1:AM:DEPT?") == "40"
    assert ask(scpi, ":SOUR1:FM:DEV?") == "2500"
    assert ask(scpi, ":SOUR1:PM:DEV?") == "90"
    assert ask(scpi, ":SOUR2:AM:DEPT?") == "100"
    assert drain(scpi) == []


def test_a_modulation_query_without_a_type_reads_the_type_now_selected(
    scpi: FakeInstrument,
) -> None:
    ask_none(scpi, ":SOUR1:MOD:TYP FM")
    ask_none(scpi, ":SOUR1:FM:INT:FREQ 7")
    assert read(scpi, "modulation_type") == "fm"
    assert ask(scpi, ":SOUR1:FM:INT:FREQ?") == "7"
    assert ask(scpi, ":SOUR1:AM:INT:FREQ?") == "10"


def test_a_type_word_the_profile_does_not_know_is_an_undefined_header(scpi: FakeInstrument) -> None:
    ask_none(scpi, ":SOUR1:FSK:DEPT 5")
    assert [e.code for e in drain(scpi)] == [-113]


def test_a_trigger_counts_on_its_channel(scpi: FakeInstrument) -> None:
    assert scpi.fgen is not None
    ask_none(scpi, ":SOUR2:BURS:TRIG")
    ask_none(scpi, ":SOUR2:BURS:TRIG")
    ask_none(scpi, ":SOUR1:BURS:TRIG")
    assert scpi.fgen.triggers == {1: 1, 2: 2}


def test_a_label_the_profile_cannot_spell_gives_way_to_one_it_can() -> None:
    raw = copy.deepcopy(RIGOL_STYLE)
    raw["state"]["shape"]["map"] = {"SQU": "square", "RAMP": "ramp"}
    raw["setters"]["shape"]["map"] = {"square": "SQU", "ramp": "RAMP"}
    fake = FakeInstrument(build(raw))
    assert read(fake, "shape") == "square"
    assert drain(fake) == []


def test_a_keyed_reply_is_a_header_and_each_reader_key_with_its_unit(keyed: FakeInstrument) -> None:
    assert (
        ask(keyed, "C1:BSWV?")
        == "C1:BSWV WVTP,SINE,FRQ,1000HZ,AMP,1V,OFST,0V,HLEV,0.5V,LLEV,-0.5V,PHSE,0,DUTY,50"
    )
    assert ask(keyed, "C2:MDWV?") == "C2:MDWV STATE,OFF,DEPTH,100"
    assert ask(keyed, "C1:BTWV?") == "C1:BTWV STATE,OFF,TIME,1,PRD,0.01S"
    assert ask(keyed, "C1:SWWV?") == "C1:SWWV STATE,OFF,TIME,1S"


def test_every_keyed_field_reads_back_through_the_engine(keyed: FakeInstrument) -> None:
    ask_none(keyed, "C1:BSWV WVTP,RAMP")
    ask_none(keyed, "C1:BSWV FRQ,12345.5")
    ask_none(keyed, "C1:BSWV AMP,2.5")
    ask_none(keyed, "C1:MDWV STATE,ON")
    ask_none(keyed, "C1:BTWV PRD,0.002")
    ask_none(keyed, "C1:OUTP ON")
    assert read(keyed, "shape") == "ramp"
    assert read(keyed, "frequency_set") == 12345.5
    assert read(keyed, "amplitude_set") == 2.5
    assert read(keyed, "modulation") is True
    assert read(keyed, "burst") is False
    assert read(keyed, "burst_period_set") == 0.002
    assert read(keyed, "output") is True
    assert read(keyed, "frequency_set", 2) == 1000.0
    assert drain(keyed) == []


def test_a_keyed_reply_without_a_colon_has_no_header() -> None:
    raw = copy.deepcopy(SIGLENT_STYLE)
    for reader in raw["state"].values():
        reader["query"] = reader["query"].replace("C{ch}:", "{ch}")
    raw["state"]["output"]["query"] = "OUTP{ch}?"
    fake = FakeInstrument(build(raw))
    assert ask(fake, "1MDWV?") == "STATE,OFF,DEPTH,100"
    assert read(fake, "modulation") is False


def test_a_scaled_keyed_reader_states_the_number_in_the_instrument_unit() -> None:
    raw = copy.deepcopy(SIGLENT_STYLE)
    raw["state"]["frequency_set"]["scale"] = 1000.0
    fake = FakeInstrument(build(raw))
    assert "FRQ,1HZ" in ask(fake, "C1:BSWV?")
    assert read(fake, "frequency_set") == 1000.0


def test_a_label_with_no_word_in_the_reader_reads_back_in_capitals() -> None:
    raw = copy.deepcopy(SIGLENT_STYLE)
    raw["state"]["shape"]["map"] = {"SINE": "sine", "SQUARE": "square"}
    fake = FakeInstrument(build(raw))
    ask_none(fake, "C1:BSWV WVTP,RAMP")
    assert "WVTP,RAMP," in ask(fake, "C1:BSWV?")


def test_reset_returns_every_setting_to_its_default_and_keeps_the_trigger_count(
    scpi: FakeInstrument,
) -> None:
    assert scpi.fgen is not None
    ask_none(scpi, ":SOUR1:FREQ 4000")
    ask_none(scpi, ":SOUR1:AM:DEPT 10")
    ask_none(scpi, ":OUTP1 ON")
    ask_none(scpi, ":SOUR1:BURS:TRIG")
    ask_none(scpi, "*RST")
    assert read(scpi, "frequency_set") == 1000.0
    assert ask(scpi, ":SOUR1:AM:DEPT?") == "100"
    assert read(scpi, "output") is False
    assert scpi.fgen.triggers[1] == 1


def test_a_saved_state_brings_back_the_waveform_and_the_output() -> None:
    raw = copy.deepcopy(RIGOL_STYLE)
    raw["system"] = {
        "save": "MEM:SAVE {slot}",
        "recall": "MEM:LOAD {slot}",
        "slots": {"min": 0, "max": 4},
    }
    fake = FakeInstrument(build(raw))
    ask_none(fake, ":SOUR1:FUNC SQU")
    ask_none(fake, ":SOUR1:FM:DEV 321")
    ask_none(fake, ":OUTP1 ON")
    ask_none(fake, "MEM:SAVE 2")
    ask_none(fake, "*RST")
    assert read(fake, "shape") == "sine"
    assert read(fake, "output") is False
    ask_none(fake, "MEM:LOAD 2")
    assert read(fake, "shape") == "square"
    assert read(fake, "output") is True
    assert ask(fake, ":SOUR1:FM:DEV?") == "321"
    assert drain(fake) == []


def test_the_fake_is_the_same_whatever_the_case_or_spacing(scpi: FakeInstrument) -> None:
    ask_none(scpi, "  :sour1:freq   2000  ")
    assert read(scpi, "frequency_set") == 2000.0


def test_a_modulation_switch_that_names_a_type_selects_that_type() -> None:
    raw = copy.deepcopy(RIGOL_STYLE)
    raw["setters"]["modulation"] = {"command": ":SOUR{ch}:{modulation}:STAT {on}"}
    fake = FakeInstrument(build(raw))
    ask_none(fake, ":SOUR1:FM:STAT ON")
    assert read(fake, "modulation") is True
    assert read(fake, "modulation_type") == "fm"
    ask_none(fake, ":SOUR1:AM:STAT OFF")
    assert read(fake, "modulation") is False
    assert read(fake, "modulation_type") == "fm"


def test_a_phase_modulation_deviation_is_a_phase_and_a_frequency_modulation_one_a_frequency(
    scpi: FakeInstrument,
) -> None:
    ask_none(scpi, ":SOUR1:PM:DEV 45")
    ask_none(scpi, ":SOUR1:FM:DEV 2500")
    assert ask(scpi, ":SOUR1:PM:DEV?") == "45"
    assert ask(scpi, ":SOUR1:FM:DEV?") == "2500"
    ask_none(scpi, ":SOUR1:MOD:TYP PM")
    assert ask(scpi, ":SOUR1:PM:DEV?") == "45"
    assert drain(scpi) == []
