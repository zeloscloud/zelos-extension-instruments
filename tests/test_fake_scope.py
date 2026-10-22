"""The fake scope answers a profile in each waveform and measurement format the way the profile
reads it back: settings, direct measurements and linear waveforms."""

import math
from typing import Any

import pytest

from tests.conftest import ScriptedLink, connected, profile_from
from zelos_extension_instruments import engine, scope_ops
from zelos_extension_instruments.profile import Profile
from zelos_extension_instruments.record import Recorder
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.waveform import block_payload

POINTS = 200


def scope_profile(scope: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A scope profile with the given `[scope]` table; `extra` adds or replaces top-level keys."""
    return {
        "schema": 1,
        "id": "acme-scope",
        "name": "Acme scope",
        "category": "scope",
        "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-20"},
        "match": {"manufacturer": "(?i)^acme", "model": "^S1$", "channels": 2},
        "scope": scope,
        **extra,
    }


def keysight_like() -> dict[str, Any]:
    """A preamble of ten comma-separated fields, unsigned 16-bit little-endian codes, and one
    measurement query per type."""
    return scope_profile(
        {
            "measure": {
                "query": ":MEASure:{type} CHANnel{ch}",
                "words": {"pkpk": "VPP?", "frequency": "FREQuency?", "mean": "VAVerage? DISPlay,"},
            },
            "waveform": {
                "format": "linear",
                "setup": [
                    ":WAVeform:SOURce CHANnel{ch}",
                    ":WAVeform:FORMat WORD",
                    ":WAVeform:BYTeorder LSBFirst",
                    ":WAVeform:UNSigned ON",
                    ":WAVeform:POINts:MODE NORMal",
                    ":WAVeform:POINts {points}",
                ],
                "preamble": ":WAVeform:PREamble?",
                "data": ":WAVeform:DATA?",
                "encoding": "u2",
                "byte_order": "little",
                "x_inc": {"field": 4},
                "x_zero": {"field": 5},
                "x_ref": {"field": 6},
                "y_mult": {"field": 7},
                "y_zero": {"field": 8},
                "y_ref": {"field": 9},
            },
        },
        errors={"query": ":SYSTem:ERRor?"},
        state={"timebase": {"query": ":TIMebase:SCALe?", "parse": "float"}},
    )


def legacy_like() -> dict[str, Any]:
    """No preamble: signed 8-bit codes scaled by the channel's volts per division over 25 and its
    offset, samples one sample period times the transfer interval apart, the first one half of
    the 14-division screen before the trigger delay. A measurement reply names its type first."""
    return scope_profile(
        {
            "grid": 14,
            "measure": {
                "query": "C{ch}:PAVA? {type}",
                "words": {"pkpk": "PKPK", "frequency": "FREQ", "duty": "DUTY"},
                "index": 1,
            },
            "waveform": {
                "format": "linear",
                "setup": "WFSU SP,{interval},NP,{points},FP,0",
                "data": "C{ch}:WF? DAT2",
                "points": "SANU? C1",
                "encoding": "i1",
                "x_inc": {"query": "SARA?", "invert": True, "times_interval": True},
                "x_zero": [{"query": "TRDL?"}, {"query": "TDIV?", "multiply": -7}],
                "x_ref": 0,
                "y_mult": {"query": "C{ch}:VDIV?", "divide": 25},
                "y_zero": {"query": "C{ch}:OFST?", "negate": True},
                "y_ref": 0,
            },
        },
        state={
            "scale": {"query": "C{ch}:VDIV?", "parse": "float"},
            "offset": {"query": "C{ch}:OFST?", "parse": "float"},
            "timebase": {"query": "TDIV?", "parse": "float"},
            "delay": {"query": "TRDL?", "parse": "float"},
            "sample_rate": {"query": "SARA?", "parse": "float"},
            "points": {"query": "SANU? C1", "parse": "float"},
        },
    )


def tek_like() -> dict[str, Any]:
    """Each scaling term from its own query, signed 16-bit big-endian codes."""
    return scope_profile(
        {
            "waveform": {
                "format": "linear",
                "setup": [
                    "DATa:SOUrce CH{ch}",
                    "DATa:ENCdg RIBinary",
                    "DATa:STARt 1",
                    "DATa:STOP {points}",
                ],
                "data": "CURVe?",
                "encoding": "i2",
                "byte_order": "big",
                "x_inc": {"query": "WFMOutpre:XINcr?"},
                "x_zero": {"query": "WFMOutpre:XZEro?"},
                "x_ref": {"query": "WFMOutpre:PT_Off?"},
                "y_mult": {"query": "WFMOutpre:YMUlt?"},
                "y_zero": {"query": "WFMOutpre:YZEro?"},
                "y_ref": {"query": "WFMOutpre:YOFf?"},
            }
        },
        state={"timebase": {"query": "HORizontal:SCAle?", "parse": "float"}},
    )


def listed_like() -> dict[str, Any]:
    """Measurements that are added and deleted, named by one list query: a type, a source and a
    value per measurement, the slot created first and then given its type and source."""
    return scope_profile(
        {
            "slots": {
                "count": 8,
                "list": "MEASUrement:LIST?",
                "list_pattern": r"MEAS(?P<slot>\d+)",
                "type": "MEASUrement:MEAS{slot}:TYPe?",
                "source": "MEASUrement:MEAS{slot}:SOUrce1?",
                "source_pattern": r"^CH(?P<ch>\d+)$",
                "value": "MEASUrement:MEAS{slot}:RESUlts:CURRentacq:MEAN?",
                "types": {
                    "PK2Pk": "pkpk",
                    "PK2PK": "pkpk",
                    "FREQuency": "frequency",
                    "FREQ": "frequency",
                    "MEAN": "mean",
                    "PERIod": "period",
                    "PERI": "period",
                },
            },
            "measure": {
                "commands": [
                    'MEASUrement:ADDNew "MEAS{slot}"',
                    "MEASUrement:MEAS{slot}:TYPe {type}",
                    "MEASUrement:MEAS{slot}:SOUrce1 CH{ch}",
                ],
                "off": 'MEASUrement:DELete "MEAS{slot}"',
            },
        },
        errors={"query": "SYSTem:ERRor?"},
    )


STYLES = {"keysight": keysight_like, "legacy": legacy_like, "tek": tek_like}


def sine(fake: FakeInstrument, t: float) -> float:
    assert fake.scope is not None
    scope = fake.scope
    return scope.mid + scope.amplitude * math.sin(2 * math.pi * scope.frequency * t)


@pytest.mark.parametrize("style", list(STYLES))
def test_a_linear_capture_reads_back_the_sine_the_fake_scope_sees(style: str) -> None:
    """Every sample lands on the synthetic sine at its own time: the decoded volts and the
    decoded times agree with what the fake drew, whichever way the profile scales them."""
    profile = profile_from(STYLES[style]())
    fake = FakeInstrument(profile)
    assert fake.scope is not None
    if style != "legacy":
        # Without a transfer interval, 200 samples span a whole cycle only at a slow rate.
        fake.scope.shared["sample_rate"] = 2e5
    worker, _, emitter = connected(profile, fake.handle, name="scope", profile=profile.id)
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    [(_, ch, samples)] = emitter.waveforms
    assert ch == 1 and captured["samples"] == len(samples) == POINTS
    first = samples[0][0]
    resolution = 0.02 if style == "legacy" else 0.001
    for t_ns, volts in samples:
        t = captured["first_s"] + (t_ns - first) / 1e9
        assert volts == pytest.approx(sine(fake, t), abs=resolution)
    assert captured["maximum"] == pytest.approx(1.5, abs=resolution)
    assert captured["minimum"] == pytest.approx(-0.5, abs=resolution)


def test_a_term_read_from_a_state_query_takes_the_setting_that_query_reports() -> None:
    """The legacy profile reads its scale from the channel's own settings: changing them changes
    the codes the fake sends, and the capture still reads the same sine."""
    profile = profile_from(legacy_like())
    fake = FakeInstrument(profile)
    assert fake.scope is not None
    fake.scope.per_channel[1].update(scale=1.0, offset=0.25)
    fake.scope.shared.update(delay=0.001, timebase=0.001)
    worker, _, _ = connected(profile, fake.handle, name="scope", profile=profile.id)
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    assert captured["first_s"] == pytest.approx(0.001 - 0.007)
    assert captured["sample_interval_s"] == pytest.approx(50 / 2e6)
    assert captured["maximum"] == pytest.approx(1.5, abs=0.04)
    data = fake.handle("C1:WF? DAT2")
    assert isinstance(data, bytes)
    codes = [b - 256 if b > 127 else b for b in block_payload(data)]
    # V = code * VDIV / 25 - OFST: 1.5 V at 1 V/div and a 0.25 V offset is code 43.75.
    assert max(codes) == 44


def test_the_preamble_holds_each_term_at_the_field_the_profile_names() -> None:
    profile = profile_from(keysight_like())
    fake = FakeInstrument(profile)
    fake.handle(":WAVeform:POINts 100")
    reply = fake.handle_text(":WAVeform:PREamble?")
    assert reply is not None
    fields = [float(x) for x in reply.split(",")]
    assert len(fields) == 10
    # 100 points spread over the 10,000-point record: every 100th sample.
    assert fields[4] == pytest.approx(100 / 2e6)
    assert fields[5] == pytest.approx(-0.0025)
    assert (fields[6], fields[9]) == (0.0, 32768.0)
    assert fields[7] == pytest.approx(0.5 * 10 / 65536)


@pytest.mark.parametrize(
    ("style", "query", "reply"),
    [
        ("keysight", ":MEASure:FREQuency? CHANnel2", "1000"),
        ("keysight", ":MEASure:VAVerage? DISPlay, CHANnel1", "0.5"),
        ("legacy", "C2:PAVA? FREQ", "FREQ,1000Hz"),
        ("legacy", "C1:PAVA? DUTY", "DUTY,50%"),
    ],
)
def test_a_direct_measurement_is_answered_in_the_profile_s_reply_form(
    style: str, query: str, reply: str
) -> None:
    fake = FakeInstrument(profile_from(STYLES[style]()))
    assert fake.handle_text(query) == reply


def test_a_measure_query_the_profile_does_not_name_is_an_undefined_header() -> None:
    fake = FakeInstrument(profile_from(keysight_like()))
    assert fake.handle(":MEASure:VRMS? CHANnel1") is None
    assert fake.handle_text(":SYSTem:ERRor?") == '-113,"Undefined header"'


def settings_profile() -> Profile:
    raw = keysight_like()
    raw["state"] = {
        "acquisition_mode": {
            "query": ":ACQuire:TYPE?",
            "parse": "map",
            "map": {"NORM": "normal", "AVER": "average", "PEAK": "peak", "HRES": "hires"},
        },
        "averages": {"query": ":ACQuire:COUNt?", "parse": "float"},
        "trigger_type": {
            "query": ":TRIGger:MODE?",
            "parse": "map",
            "map": {"EDGE": "edge", "GLIT": "pulse"},
        },
        "pulse_width": {"query": ":TRIGger:GLITch:GREaterthan?", "parse": "float"},
        "pulse_condition": {
            "query": ":TRIGger:GLITch:QUALifier?",
            "parse": "map",
            "map": {"GRE": "greater", "LESS": "less"},
        },
        "bandwidth_limit": {
            "query": ":CHANnel{ch}:BWLimit?",
            "parse": "map",
            "map": {"0": "full", "1": "20mhz"},
        },
        "trigger_status": {
            "query": ":TER?",
            "parse": "map",
            "map": {"0": "armed", "1": "triggered", "AUTO": "auto"},
        },
    }
    raw["setters"] = {
        "acquisition_mode": {
            "command": ":ACQuire:TYPE {value}",
            "channel": False,
            "map": {
                "normal": "NORMal",
                "average": "AVERage",
                "peak": "PEAK",
                "hires": "HRESolution",
            },
        },
        "averages": {"command": ":ACQuire:COUNt {value}", "channel": False},
        "trigger_type": {
            "command": ":TRIGger:MODE {value}",
            "channel": False,
            "map": {"edge": "EDGE", "pulse": "GLITch"},
        },
        "pulse_width": {"command": ":TRIGger:GLITch:GREaterthan {value}", "channel": False},
        "pulse_condition": {
            "command": ":TRIGger:GLITch:QUALifier {value}",
            "channel": False,
            "map": {"greater": "GREaterthan", "less": "LESSthan"},
        },
        "bandwidth_limit": {
            "command": ":CHANnel{ch}:BWLimit {value}",
            "map": {"full": "0", "20mhz": "1"},
        },
        "force": {"command": ":TRIGger:FORCe", "channel": False},
    }
    return profile_from(raw)


def read(fake: FakeInstrument, profile: Profile, field_name: str, ch: int = 1) -> Any:
    reader = profile.reader(field_name)
    assert reader is not None and reader.query is not None
    reply = fake.handle_text(engine.render(reader.query, ch=ch))
    assert reply is not None
    return engine.parse_reply(reader, reply, ch)[field_name]


def test_the_acquisition_and_trigger_settings_start_at_a_scope_s_defaults() -> None:
    profile = settings_profile()
    fake = FakeInstrument(profile)
    assert read(fake, profile, "acquisition_mode") == "normal"
    assert read(fake, profile, "averages") == 16.0
    assert read(fake, profile, "trigger_type") == "edge"
    assert read(fake, profile, "pulse_width") == 1e-6
    assert read(fake, profile, "pulse_condition") == "greater"
    assert read(fake, profile, "bandwidth_limit", ch=2) == "full"


@pytest.mark.parametrize(
    ("command", "field_name", "expected"),
    [
        (":ACQuire:TYPE AVERage", "acquisition_mode", "average"),
        (":ACQuire:COUNt 64", "averages", 64.0),
        (":TRIGger:MODE GLITch", "trigger_type", "pulse"),
        (":TRIGger:GLITch:GREaterthan 2e-06", "pulse_width", 2e-6),
        (":TRIGger:GLITch:QUALifier LESSthan", "pulse_condition", "less"),
        (":CHANnel1:BWLimit 1", "bandwidth_limit", "20mhz"),
        (":TRIGger:FORCe", "trigger_status", "triggered"),
    ],
)
def test_each_new_setter_changes_what_the_fake_reads_back(
    command: str, field_name: str, expected: Any
) -> None:
    profile = settings_profile()
    fake = FakeInstrument(profile)
    assert fake.handle(command) is None
    assert fake.handle_text(":SYSTem:ERRor?") == '0,"No error"'
    assert read(fake, profile, field_name) == expected


def test_a_listed_slot_exists_only_after_it_is_added() -> None:
    fake = FakeInstrument(profile_from(listed_like()))
    assert fake.handle_text("MEASUrement:LIST?") == "MEAS1"
    assert fake.handle('MEASUrement:ADDNew "MEAS3"') is None
    assert fake.handle_text("MEASUrement:LIST?") == "MEAS1,MEAS3"
    assert fake.handle_text("MEASUrement:MEAS3:TYPe?") == "PERIOD"
    assert fake.handle_text("MEASUrement:MEAS3:SOUrce1?") == "CH1"
    fake.handle("MEASUrement:MEAS3:TYPe FREQuency")
    fake.handle("MEASUrement:MEAS3:SOUrce1 CH2")
    assert fake.handle_text("MEASUrement:MEAS3:TYPe?") == "FREQUENCY"
    assert fake.handle_text("MEASUrement:MEAS3:SOUrce1?") == "CH2"
    assert fake.handle_text("MEASUrement:MEAS3:RESUlts:CURRentacq:MEAN?") == "1000"
    fake.handle('MEASUrement:DELete "MEAS1"')
    fake.handle('MEASUrement:DELete "MEAS3"')
    assert fake.handle_text("MEASUrement:LIST?") == "NONE"
    assert fake.handle_text("SYSTem:ERRor?") == '0,"No error"'


def test_addressing_a_measurement_that_does_not_exist_creates_it() -> None:
    fake = FakeInstrument(profile_from(listed_like()))
    assert fake.handle_text("MEASUrement:MEAS5:TYPe?") == "PERIOD"
    assert fake.handle_text("MEASUrement:LIST?") == "MEAS1,MEAS5"


def test_a_transcript_of_listed_slots_asks_only_for_the_slots_the_list_names() -> None:
    profile = profile_from(listed_like())
    fake = FakeInstrument(profile)
    fake.handle('MEASUrement:ADDNew "MEAS4"')
    link = ScriptedLink(fake.handle, profile.id)
    recorder = Recorder(link, profile)
    recorder.run(recorder.identify(), 2, allow_set=False)
    assert not recorder.failures, recorder.failures
    asked = [c for c in link.commands if c.startswith("MEASUrement")]
    assert asked[0] == "MEASUrement:LIST?"
    assert {c.split(":")[1] for c in asked[1:]} == {"MEAS1", "MEAS4"}


def test_a_label_the_scope_sends_before_the_block_is_skipped() -> None:
    raw = legacy_like()
    raw["scope"]["waveform"]["skip"] = 5
    profile = profile_from(raw)
    fake = FakeInstrument(profile)
    worker, _, _ = connected(profile, fake.handle, name="scope", profile=profile.id)
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    assert captured["samples"] == POINTS
    assert captured["maximum"] == pytest.approx(1.5, abs=0.02)
