"""The transcript recorder against the simulator."""

from itertools import pairwise

from tests.conftest import ScriptedLink
from zelos_extension_instruments.profile import by_id, shipped
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
