"""The transcript recorder against the simulator."""

from itertools import pairwise

from tests.conftest import ScriptedLink, minimal, profile_from
from tests.test_serve import Meter, meter
from zelos_extension_instruments.profile import by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.record import Recorder
from zelos_extension_instruments.sim.fake import FakeInstrument


def _recorder(
    profile_id: str, model: str | None = None
) -> tuple[Recorder, ScriptedLink, FakeInstrument]:
    profile = by_id(shipped(), profile_id)
    assert profile is not None
    fake = FakeInstrument(profile, model) if model else FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile_id)
    return Recorder(link, profile), link, fake


def test_the_identity_is_asked_after_the_first_command() -> None:
    """A Genesys answers nothing until ADR has answered OK."""
    recorder, link, _ = _recorder("tdk-lambda-genesys")
    idn = recorder.identify()
    assert idn is not None and idn.manufacturer
    assert link.commands[:2] == ["ADR 6", "IDN?"]


def test_set_rewrites_each_setpoint_to_the_value_it_read() -> None:
    """A slew the load states in A/us is read back and sent again in A/us, not divided by 1e3."""
    recorder, link, fake = _recorder("gw-instek-pel-3000")
    recorder.run(recorder.identify(), 1, allow_set=True)
    assert not recorder.failures, recorder.failures
    profile = by_id(shipped(), "gw-instek-pel-3000")
    assert profile is not None
    slew = profile.setters["slew"]
    verify = profile.verify_query(slew)
    assert verify is not None
    sent = [c for c in link.commands if c.split(" ")[0] == slew.command.split(" ")[0]]
    assert sent, link.commands
    read = fake.handle(verify.replace("{ch}", "1"))
    assert read is not None and float(sent[0].split(" ")[-1]) == float(read)


def test_a_channel_is_selected_once_for_its_queries() -> None:
    recorder, link, _ = _recorder("rohde-schwarz-hmp", "HMP4040")
    recorder.run(recorder.identify(), 4, allow_set=False)
    selects = [i for i, c in enumerate(link.commands) if c.startswith("INST:NSEL")]
    assert selects
    assert all(link.commands[i] != link.commands[j] for i, j in pairwise(selects))


def test_set_rewrites_only_levels_never_a_switch_label_or_command() -> None:
    raw = minimal()
    raw["errors"] = {"query": "SYST:ERR?"}
    raw["ranges"] = {"voltage": {"max": 30}}
    raw["state"] = {"tracking": {"query": "TRAC?", "parse": "map", "map": {"0": "independent"}}}
    raw["setters"] = {
        "voltage": {"command": "VOLT {value}", "verify": "VOLT?"},
        "output": {"command": "OUTP {bit}", "verify": "OUTP?"},
        "remote_sense": {"command": "SENS ON", "off": "SENS OFF", "verify": "SENS?"},
        "tracking": {"command": "TRAC {value}", "map": {"independent": "0"}},
        "clear": {"command": "PROT:CLE"},
    }
    profile = profile_from(raw)
    replies = {
        "*IDN?": "ACME,P1,SN1,1.0",
        "MEAS:VOLT? 1": "5.0",
        "TRAC?": "0",
        "VOLT?": "5.0",
        "OUTP?": "1",
        "SENS?": "1",
        "SYST:ERR?": '0,"No error"',
    }
    link = ScriptedLink(replies.get)
    recorder = Recorder(link, profile)
    recorder.run(recorder.identify(), 1, allow_set=True)
    assert [c for c in link.commands if "?" not in c] == ["VOLT 5.000"]


def test_set_skips_a_level_whose_command_names_the_instruments_state() -> None:
    unit = Meter()
    link = ScriptedLink(lambda c: "ACME,M1,SN1,1.0" if c == "*IDN?" else unit(c) or "0")
    recorder = Recorder(link, meter())
    recorder.run(recorder.identify(), 1, allow_set=True)
    assert "# range skipped: it names instrument state besides {ch}" in recorder.lines
    assert not any(":RANG " in c for c in link.commands)
