"""Scope measurement slots: the slot scan against an action, and the slot a measurement takes."""

from tests.conftest import (
    RecordingEmitter,
    ScriptedLink,
    ask,
    instrument_config,
    opener_for,
    wait_until,
)
from zelos_extension_instruments import scope_ops
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker


def test_a_slot_scan_an_action_overtook_never_overwrites_its_result(
    profiles: list[Profile],
) -> None:
    """A slow round scans slots between fast passes; a Measure in between must survive it."""
    scope = next(p for p in profiles if p.id == "siglent-sds")
    fake = FakeInstrument(scope)
    link = ScriptedLink(fake.handle, "scope")
    cfg = instrument_config(name="scope", profile=scope.id)
    worker = InstrumentWorker(cfg, [scope], RecordingEmitter(), open=opener_for(link))
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
    worker = InstrumentWorker(cfg, [scope], RecordingEmitter(), open=opener_for(link))
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


def test_measure_reuses_the_slot_that_already_shows_the_measurement() -> None:
    profile = by_id(shipped(), "siglent-sds")
    assert profile is not None
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="unit", profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        first = ask(worker, "measure", channel=1, type="pkpk")
        again = ask(worker, "measure", channel=1, type="pkpk")
        assert first["slot"] == again["slot"] == 1
        # A slot that shows something the profile does not record is never taken over.
        assert fake.scope is not None
        fake.scope.slots[2] = (True, "DELAY", "C1")
        other = ask(worker, "measure", channel=2, type="rms")
        assert other["slot"] == 3
    finally:
        worker.stop()
        worker.join(5)
