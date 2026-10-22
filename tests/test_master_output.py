"""A supply whose channels deliver power only while a master switch is also on."""

from collections.abc import Callable, Iterator

import pytest

from tests.conftest import ScriptedLink, instrument_config, opener_for, settings_for, wait_until
from tests.test_worker import RecordingEmitter, ask
from zelos_extension_instruments.profile import by_id, shipped
from zelos_extension_instruments.sim import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker


@pytest.fixture
def nge103() -> FakeInstrument:
    profile = by_id(shipped(), "rohde-schwarz-nge100")
    assert profile is not None
    return FakeInstrument(profile, "NGE103B")


@pytest.fixture
def start(nge103: FakeInstrument) -> Iterator[Callable[[], tuple[InstrumentWorker, ScriptedLink]]]:
    """Starts the worker once the test has set the fake up, so its channel selection is known."""
    workers: list[InstrumentWorker] = []

    def run() -> tuple[InstrumentWorker, ScriptedLink]:
        link = ScriptedLink(nge103.handle, "nge")
        cfg = instrument_config(name="psu", profile=nge103.profile.id, rate_hz=50)
        worker = InstrumentWorker(
            cfg, [nge103.profile], RecordingEmitter(), settings_for(cfg), open=opener_for(link)
        )
        workers.append(worker)
        worker.start()
        assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
        return worker, link

    yield run
    for worker in workers:
        worker.stop()
        worker.join(2)


def _arm(fake: FakeInstrument, *channels: int) -> None:
    for ch in channels:
        fake.handle(f"INST:NSEL {ch}")
        fake.handle("OUTP:SEL 1")


def _writes(link: ScriptedLink, before: int) -> list[str]:
    return [c for c in link.commands[before:] if "?" not in c and not c.startswith("INST:NSEL")]


def test_an_armed_channel_delivers_only_with_the_master_on(nge103: FakeInstrument) -> None:
    _arm(nge103, 1)
    assert nge103.handle("OUTP?") == "0"
    nge103.handle("OUTP:GEN 1")
    assert nge103.handle("OUTP?") == "1"


def test_switching_on_raises_the_master_after_the_channel(start, nge103: FakeInstrument) -> None:
    worker, link = start()
    before = len(link.commands)
    assert ask(worker, "output", channel=2, on=True) == {"output": True}
    assert _writes(link, before) == ["OUTP:SEL 1", "OUTP:GEN 1"]
    assert nge103.handle("OUTP:GEN?") == "1"


def test_switching_on_is_refused_while_another_channel_is_armed(
    start, nge103: FakeInstrument
) -> None:
    """Raising the master would switch on every armed channel, not only the one asked for."""
    _arm(nge103, 1, 3)
    worker, link = start()
    before = len(link.commands)
    assert ask(worker, "output", channel=2, on=True) == {
        "error": "the master output is off, and raising it would also switch on ch1, ch3; "
        "switch those off first"
    }
    assert _writes(link, before) == []
    assert nge103.handle("OUTP:GEN?") == "0"


def test_with_the_master_on_only_the_channel_is_switched(start, nge103: FakeInstrument) -> None:
    _arm(nge103, 1)
    nge103.handle("OUTP:GEN 1")
    worker, link = start()
    before = len(link.commands)
    assert ask(worker, "output", channel=2, on=True) == {"output": True}
    assert _writes(link, before) == ["OUTP:SEL 1"]
    assert "OUTP:SEL?" not in link.commands[before:]


def test_switching_off_leaves_the_master_alone(start, nge103: FakeInstrument) -> None:
    worker, link = start()
    ask(worker, "output", channel=1, on=True)
    before = len(link.commands)
    assert ask(worker, "output", channel=1, on=False) == {"output": False}
    assert _writes(link, before) == ["OUTP:SEL 0"]
    assert nge103.handle("OUTP:GEN?") == "1"
