"""Scope measurement slots and waveform capture: the slot scan against an action, the slot a
measurement takes, and the blocks a capture reads."""

import dataclasses
import struct
from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import (
    RecordingEmitter,
    ScriptedLink,
    ask,
    connected,
    instrument_config,
    opener_for,
    wait_until,
)
from zelos_extension_instruments import config, scope_ops
from zelos_extension_instruments.clock import NS_PER_S, HostClock
from zelos_extension_instruments.config import InstrumentConfig
from zelos_extension_instruments.engine import RangeError
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument, _block
from zelos_extension_instruments.sim.server import FakeServer
from zelos_extension_instruments.waveform import block_payload
from zelos_extension_instruments.worker import InstrumentWorker

POINTS = 200
EPOCH_NS = 1_700_000_000 * NS_PER_S


@pytest.fixture
def scope(profiles: list[Profile]) -> Profile:
    found = by_id(profiles, "siglent-sds")
    assert found is not None
    return found


def preamble_of(profile: Profile) -> str:
    assert profile.scope is not None and profile.scope.waveform is not None
    return profile.scope.waveform.preamble


def data_of(profile: Profile) -> str:
    assert profile.scope is not None and profile.scope.waveform is not None
    return profile.scope.waveform.data


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


class GarblesPreamble(FakeInstrument):
    """A scope whose next descriptor reply is not a block, once armed."""

    garble = False

    def handle(self, line: str) -> str | bytes | None:
        reply = super().handle(line)
        if self.garble and line.strip() == preamble_of(self.profile):
            self.garble = False
            return b"#0garbled"
        return reply


def served_config(profile: Profile, server: FakeServer, connection: str) -> InstrumentConfig:
    if connection == "serial":
        item = {
            "connection": "serial",
            "name": "scope",
            "profile": profile.id,
            "port": f"socket://127.0.0.1:{server.port}",
            "rate_hz": 50,
        }
        return config.from_dict({"instruments": [item]}).instruments[0]
    return instrument_config(name="scope", profile=profile.id, port=server.port, rate_hz=50)


@pytest.mark.parametrize("connection", ["lan", "serial"])
def test_a_waveform_is_captured_over_a_real_link(scope: Profile, connection: str) -> None:
    emitter = RecordingEmitter()
    with FakeServer(FakeInstrument(scope)) as server:
        worker = InstrumentWorker(served_config(scope, server, connection), [scope], emitter)
        worker.start()
        try:
            assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
            captured = ask(worker, "waveform", channel=1, points=POINTS)
            state = ask(worker, "state")
        finally:
            worker.stop()
            worker.join(5)
    assert captured["samples"] == POINTS, captured
    assert captured["maximum"] == pytest.approx(1.5, abs=0.01)
    assert captured["minimum"] == pytest.approx(-0.5, abs=0.01)
    [(_, ch, samples)] = emitter.waveforms
    assert ch == 1 and len(samples) == POINTS
    times = [t for t, _ in samples]
    assert times == sorted(times)
    assert state["counters"]["timeouts"] == 0 and state["state"] == "open"


@pytest.mark.parametrize("connection", ["lan", "serial"])
def test_a_malformed_block_fails_one_capture_and_the_next_one_succeeds(
    scope: Profile, connection: str
) -> None:
    emitter = RecordingEmitter()
    fake = GarblesPreamble(scope)
    with FakeServer(fake) as server:
        worker = InstrumentWorker(served_config(scope, server, connection), [scope], emitter)
        worker.start()
        try:
            assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
            fake.garble = True
            failed = ask(worker, "waveform", channel=1, points=POINTS)
            captured = ask(worker, "waveform", channel=1, points=POINTS)
        finally:
            worker.stop()
            worker.join(5)
    assert "did not answer with a block" in failed["error"]
    assert captured["samples"] == POINTS
    assert not any("disconnected" in m for m in emitter.messages())


def descriptor_of(fake: FakeInstrument, profile: Profile) -> bytearray:
    reply = fake.handle(preamble_of(profile))
    assert isinstance(reply, bytes)
    return bytearray(block_payload(reply))


def with_probe(probe: float) -> Callable[[bytearray], bytes]:
    def patched(desc: bytearray) -> bytes:
        struct.pack_into("<f", desc, 328, probe)
        return bytes(desc)

    return patched


def with_code_per_div(code_per_div: float) -> Callable[[bytearray], bytes]:
    def patched(desc: bytearray) -> bytes:
        struct.pack_into("<f", desc, 164, code_per_div)
        return bytes(desc)

    return patched


BAD_DESCRIPTORS = {
    "garbled": (lambda desc: b"G" * len(desc), "not a WAVEDESC descriptor"),
    "truncated": (lambda desc: bytes(desc[:100]), "not a WAVEDESC descriptor"),
    "zero probe": (with_probe(0.0), "a probe factor of 0.0"),
    "negative probe": (with_probe(-10.0), "a probe factor of -10.0"),
    "no code scale": (with_code_per_div(0.0), "no code_per_div"),
}


@pytest.mark.parametrize(
    ("make", "complaint"), list(BAD_DESCRIPTORS.values()), ids=list(BAD_DESCRIPTORS)
)
def test_a_bad_descriptor_fails_the_capture_and_clears_the_link(
    scope: Profile, make: Callable[[bytearray], bytes], complaint: str
) -> None:
    """Bytes still on their way behind the descriptor must not become the next query's reply."""
    fake = FakeInstrument(scope)
    held: list[ScriptedLink] = []

    def handler(command: str) -> str | bytes | None:
        if command == preamble_of(scope):
            held[0].push(_block(make(descriptor_of(fake, scope))))
            held[0].push("STRAY")
            return None
        return fake.handle(command)

    worker, link, emitter = connected(scope, handler, name="scope", profile=scope.id)
    held.append(link)
    cleared = link.cleared
    with pytest.raises(RangeError, match=f"the waveform could not be read: .*{complaint}"):
        scope_ops.capture(worker, 1, {"points": POINTS})
    assert link.cleared == cleared + 1
    assert worker._query("*IDN?") == fake.idn
    assert emitter.waveforms == []


def test_a_data_block_that_never_arrives_fails_the_capture_and_keeps_the_link(
    scope: Profile,
) -> None:
    fake = FakeInstrument(scope)

    def handler(command: str) -> str | bytes | None:
        return None if command == data_of(scope) else fake.handle(command)

    link = ScriptedLink(handler, "scope")
    cfg = instrument_config(name="scope", profile=scope.id, rate_hz=50)
    emitter = RecordingEmitter()
    worker = InstrumentWorker(cfg, [scope], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        failed = ask(worker, "waveform", channel=1, points=POINTS)
        assert "no reply" in failed["error"]
        assert ask(worker, "scpi", command="*IDN?") == {"reply": fake.idn}
        assert not any("disconnected" in m for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(5)
    assert emitter.waveforms == []


def test_the_samples_are_placed_around_the_moment_the_data_was_requested(scope: Profile) -> None:
    """The trigger sits at the time of the data request, not the start or the end of the
    transfer; the left edge of the screen is half a sweep before it."""
    fake = FakeInstrument(scope)
    now = {"ns": 5 * NS_PER_S}

    def handler(command: str) -> str | bytes | None:
        # The descriptor reply ends before the data is requested, and the data takes three seconds.
        if command == preamble_of(scope):
            now["ns"] = 7 * NS_PER_S
        elif command == data_of(scope):
            now["ns"] = 10 * NS_PER_S
        return fake.handle(command)

    worker, _, emitter = connected(scope, handler, name="scope", profile=scope.id)
    worker._now = lambda: now["ns"]
    worker._clock = HostClock(now=worker._now, wall=lambda: EPOCH_NS + now["ns"])
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    [(_, _, samples)] = emitter.waveforms
    first_ns = EPOCH_NS + 7 * NS_PER_S - 2_500_000
    assert samples[0][0] == first_ns
    pitch_ns = round(captured["sample_interval_s"] * NS_PER_S)
    assert samples[-1][0] == pytest.approx(first_ns + (POINTS - 1) * pitch_ns, abs=POINTS)
    assert captured["first_s"] == pytest.approx(-0.0025)
    assert captured["fetched_in_s"] == 3.0
    assert any("in 3.00 s" in m for m in emitter.messages())


def scope_worker(profile: Profile, fake: FakeInstrument) -> InstrumentWorker:
    worker, _, _ = connected(profile, fake.handle, name="scope", profile=profile.id)
    return worker


def test_a_profile_without_waveform_commands_cannot_capture(scope: Profile) -> None:
    assert scope.scope is not None
    bare = dataclasses.replace(scope, scope=dataclasses.replace(scope.scope, waveform=None))
    worker = scope_worker(bare, FakeInstrument(scope))
    with pytest.raises(RangeError, match="the profile siglent-sds cannot fetch waveforms"):
        scope_ops.capture(worker, 1, {})


def test_a_capture_asks_for_at_least_one_point(scope: Profile) -> None:
    worker = scope_worker(scope, FakeInstrument(scope))
    with pytest.raises(RangeError, match="points must be at least 1"):
        scope_ops.capture(worker, 1, {"points": -3})


def test_a_profile_that_reads_no_timebase_cannot_place_the_samples_in_time(
    scope: Profile,
) -> None:
    unclocked = dataclasses.replace(
        scope, state=tuple(r for r in scope.state if "timebase" not in r.fields)
    )
    worker = scope_worker(unclocked, FakeInstrument(scope))
    with pytest.raises(RangeError, match="reads no timebase"):
        scope_ops.capture(worker, 1, {"points": POINTS})


def test_a_profile_without_measurement_commands_cannot_show_a_measurement(scope: Profile) -> None:
    assert scope.scope is not None
    bare = dataclasses.replace(scope, scope=dataclasses.replace(scope.scope, measure=None))
    worker = scope_worker(bare, FakeInstrument(scope))
    with pytest.raises(RangeError, match="cannot configure measurements"):
        scope_ops.measure(worker, 1, {"type": "pkpk"})


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        ({"type": "bogus"}, "type must be one of pkpk"),
        ({"type": "pkpk", "slot": 13}, "slot must be from 1 to 12"),
        ({"type": "pkpk", "slot": 0}, "slot must be from 1 to 12"),
    ],
)
def test_a_measurement_that_names_no_known_type_or_slot_is_refused(
    scope: Profile, args: dict[str, Any], complaint: str
) -> None:
    worker = scope_worker(scope, FakeInstrument(scope))
    with pytest.raises(RangeError, match=complaint):
        scope_ops.measure(worker, 1, args)


def test_a_measurement_is_refused_when_every_slot_shows_something_else(scope: Profile) -> None:
    fake = FakeInstrument(scope)
    assert fake.scope is not None
    for slot in fake.scope.slots:
        fake.scope.slots[slot] = (True, "DELAY", "C1")
    worker = scope_worker(scope, fake)
    with pytest.raises(RangeError, match="every slot shows something; give a slot to replace"):
        scope_ops.measure(worker, 1, {"type": "pkpk"})


def test_a_slot_that_does_not_take_the_measurement_is_reported(scope: Profile) -> None:
    fake = FakeInstrument(scope)

    def ignoring(command: str) -> str | bytes | None:
        if command.startswith(":MEASure") and "?" not in command:
            return None
        return fake.handle(command)

    worker, _, _ = connected(scope, ignoring, name="scope", profile=scope.id)
    with pytest.raises(RangeError, match="slot 2 did not take the measurement"):
        scope_ops.measure(worker, 1, {"type": "frequency"})
