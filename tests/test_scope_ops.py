"""Scope measurement slots and waveform capture: the slot scan against an action, the slot a
measurement takes, and the blocks a capture reads."""

import dataclasses
import math
import re
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
    profile_from,
    wait_until,
)
from tests.test_fake_scope import keysight_like, legacy_like, listed_like
from zelos_extension_instruments import config, engine, scope_ops
from zelos_extension_instruments.clock import NS_PER_S, HostClock
from zelos_extension_instruments.config import InstrumentConfig
from zelos_extension_instruments.engine import RangeError
from zelos_extension_instruments.profile import Profile, by_id
from zelos_extension_instruments.profile_load import ProfileError, shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.sim.fake_scope import _block
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
    assert profile.scope.waveform.preamble is not None
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


def test_a_wavedesc_capture_skips_the_bytes_before_the_data_block(scope: Profile) -> None:
    assert scope.scope is not None and scope.scope.waveform is not None
    labeled = dataclasses.replace(
        scope,
        scope=dataclasses.replace(
            scope.scope, waveform=dataclasses.replace(scope.scope.waveform, skip=5)
        ),
    )
    fake = FakeInstrument(scope)

    def handler(command: str) -> str | bytes | None:
        reply = fake.handle(command)
        if command == data_of(scope):
            assert isinstance(reply, bytes)
            return b"DAT2," + reply
        return reply

    worker, link, emitter = connected(labeled, handler, name="scope", profile=scope.id)
    assert scope_ops.capture(worker, 1, {"points": POINTS})["samples"] == POINTS
    assert not link.raw and len(emitter.waveforms) == 1


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


def direct_worker(
    raw: dict[str, Any], handler: Callable[[FakeInstrument, str], str | bytes | None] | None = None
) -> tuple[InstrumentWorker, FakeInstrument, ScriptedLink]:
    profile = profile_from(raw)
    fake = FakeInstrument(profile)

    def answer(command: str) -> str | bytes | None:
        return fake.handle(command) if handler is None else handler(fake, command)

    worker, link, _ = connected(profile, answer, name="scope", profile=profile.id)
    return worker, fake, link


@pytest.mark.parametrize("make", [keysight_like, legacy_like])
def test_a_direct_measurement_is_recorded_from_the_moment_it_is_asked_for(
    make: Callable[[], dict[str, Any]],
) -> None:
    worker, _, _ = direct_worker(make())
    assert worker._slots == {}
    shown = scope_ops.measure(worker, 2, {"type": "frequency"})
    assert shown == {"slot": 1, "channel": 2, "type": "frequency", "value": 1000.0}
    assert scope_ops.measure(worker, 1, {"type": "pkpk"})["slot"] == 2
    # Asking again for the same measurement keeps its number.
    assert scope_ops.measure(worker, 2, {"type": "frequency"})["slot"] == 1
    assert worker._slots == {1: (2, "frequency"), 2: (1, "pkpk")}
    rows: dict[int | None, dict[str, Any]] = {}
    scope_ops.read_slots(worker, rows)
    assert rows == {2: {"frequency": 1000.0}, 1: {"pkpk": 2.0}}


def test_a_direct_measurement_turned_off_is_no_longer_asked_for() -> None:
    worker, _, link = direct_worker(keysight_like())
    scope_ops.measure(worker, 2, {"type": "frequency"})
    scope_ops.measure(worker, 1, {"type": "pkpk"})
    off = scope_ops.measure(worker, 2, {"type": "frequency", "off": True})
    assert off == {"slot": 1, "channel": 2, "type": "frequency", "off": True}
    assert worker._slots == {2: (1, "pkpk")}
    asked = len(link.commands)
    rows: dict[int | None, dict[str, Any]] = {}
    scope_ops.read_slots(worker, rows)
    assert link.commands[asked:] == [":MEASure:VPP? CHANnel1"]
    # Turning off what is not measured changes nothing.
    again = scope_ops.measure(worker, 2, {"type": "frequency", "off": True})
    assert again["slot"] is None and worker._slots == {2: (1, "pkpk")}


def test_a_slot_scan_keeps_the_direct_measurements_it_was_asked_for() -> None:
    worker, _, _ = direct_worker(keysight_like())
    scope_ops.measure(worker, 2, {"type": "mean"})
    assert scope_ops.scan_slots(worker) == {1: (2, "mean")}
    for _ in worker._slow_round():
        pass
    assert worker._slots == {1: (2, "mean")}


def test_a_direct_reply_without_a_number_reads_as_not_a_number() -> None:
    def unmeasurable(fake: FakeInstrument, command: str) -> str | bytes | None:
        return "FREQ,****" if command == "C2:PAVA? FREQ" else fake.handle(command)

    worker, _, _ = direct_worker(legacy_like(), unmeasurable)
    shown = scope_ops.measure(worker, 2, {"type": "frequency"})
    assert shown["value"] is None and worker._slots == {1: (2, "frequency")}
    rows: dict[int | None, dict[str, Any]] = {}
    scope_ops.read_slots(worker, rows)
    assert math.isnan(rows[2]["frequency"])


def test_a_direct_measurement_the_instrument_does_not_answer_is_not_recorded() -> None:
    def silent(fake: FakeInstrument, command: str) -> str | bytes | None:
        return None if command.startswith(":MEASure") else fake.handle(command)

    worker, _, _ = direct_worker(keysight_like(), silent)
    with pytest.raises(RangeError, match="the instrument did not measure frequency on ch2"):
        scope_ops.measure(worker, 2, {"type": "frequency"})
    assert worker._slots == {}


def test_a_direct_measurement_the_profile_has_no_word_for_is_refused() -> None:
    worker, _, _ = direct_worker(keysight_like())
    with pytest.raises(RangeError, match="the profile acme-scope has no measurement for rms"):
        scope_ops.measure(worker, 1, {"type": "rms"})


def test_a_running_worker_records_a_direct_measurement_every_fast_pass() -> None:
    profile = profile_from(legacy_like())
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="scope", profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        shown = ask(worker, "measure", channel=2, type="duty")
        assert shown["value"] == 50.0
        assert wait_until(
            lambda: sum(r[1] == 2 and r[2].get("duty") == 50.0 for r in emitter.rows) >= 3,
            timeout_s=10,
        )
        assert ask(worker, "state")["slots"] == {"1": {"channel": 2, "type": "duty"}}
    finally:
        worker.stop()
        worker.join(5)


@pytest.mark.parametrize(
    ("measure", "complaint"),
    [
        ({"query": ":MEAS:{type}?", "words": {"pkpk": "VPP"}}, "query: must contain {ch}"),
        ({"query": ":MEAS:VPP? {ch}", "words": {"pkpk": "VPP"}}, "query: must contain {type}"),
        ({"query": ":MEAS:{type}? {ch}", "words": {}}, "words: must give the instrument word"),
        ({"query": ":MEAS:{type}? {ch}", "words": {"bogus": "X"}}, "'bogus' is not one of pkpk"),
        (
            {"query": ":MEAS:{type}? {ch}", "words": {"pkpk": "VPP"}, "off": "X"},
            "off: is only for slots",
        ),
        ({"query": ":MEAS:{type}? {ch}", "words": {"pkpk": "VPP"}, "index": -1}, "index: must"),
        ({"commands": ":MEAS {type}", "words": {"pkpk": "VPP"}}, "words: is only for a direct"),
    ],
)
def test_a_direct_measurement_table_that_cannot_be_asked_is_refused(
    measure: dict[str, Any], complaint: str
) -> None:
    raw = keysight_like()
    raw["scope"]["measure"] = measure
    with pytest.raises(ProfileError, match=re.escape(complaint)):
        profile_from(raw)


def test_a_direct_measurement_and_measurement_slots_exclude_each_other() -> None:
    raw = keysight_like()
    raw["scope"]["slots"] = {
        "count": 1,
        "enabled": "P{slot}?",
        "type": "P{slot}:TYPE?",
        "source": "P{slot}:SOUR?",
        "source_pattern": r"^C(?P<ch>\d+)$",
        "value": "P{slot}:VAL?",
        "types": {"PKPK": "pkpk"},
    }
    with pytest.raises(ProfileError, match="a direct measurement reads no slots"):
        profile_from(raw)


def listed_worker(
    raw: dict[str, Any] | None = None,
) -> tuple[InstrumentWorker, FakeInstrument, ScriptedLink]:
    return direct_worker(raw or listed_like())


def define(fake: FakeInstrument, slot: int, word: str, ch: int) -> None:
    """A measurement the user made on the instrument itself."""
    fake.handle(f'MEASUrement:ADDNew "MEAS{slot}"')
    fake.handle(f"MEASUrement:MEAS{slot}:TYPe {word}")
    fake.handle(f"MEASUrement:MEAS{slot}:SOUrce1 CH{ch}")


def sent(link: ScriptedLink, since: int) -> list[str]:
    """The commands, not the queries, sent after the first `since` lines."""
    return [c for c in link.commands[since:] if "?" not in c]


def test_a_listed_scan_reads_only_the_slots_the_list_names() -> None:
    worker, fake, link = listed_worker()
    assert fake.scope is not None
    define(fake, 3, "FREQuency", 2)
    assert scope_ops.scan_slots(worker) == {1: (1, "pkpk"), 3: (2, "frequency")}
    assert worker._busy_slots == {1, 3}
    # Asking for a measurement that is not there would create it.
    assert sorted(fake.scope.slots) == [1, 3]
    assert not any("MEAS2" in c for c in link.commands)


def test_a_listed_scan_of_a_scope_with_no_measurements_finds_none() -> None:
    worker, fake, _ = listed_worker()
    fake.handle('MEASUrement:DELete "MEAS1"')
    assert fake.handle_text("MEASUrement:LIST?") == "NONE"
    assert scope_ops.scan_slots(worker) == {}
    assert worker._busy_slots == set()


def test_a_listed_slot_the_profile_does_not_record_is_in_use_but_not_read() -> None:
    worker, fake, _ = listed_worker()
    define(fake, 2, "AREA", 1)
    assert scope_ops.scan_slots(worker) == {1: (1, "pkpk")}
    assert worker._busy_slots == {1, 2}
    assert scope_ops.measure(worker, 2, {"type": "mean"})["slot"] == 3


def test_a_listed_measurement_takes_the_lowest_slot_the_list_does_not_name() -> None:
    worker, fake, link = listed_worker()
    assert fake.scope is not None
    define(fake, 3, "PERIod", 1)
    asked = len(link.commands)
    shown = scope_ops.measure(worker, 2, {"type": "mean"})
    assert shown == {"slot": 2, "channel": 2, "type": "mean", "value": 0.5}
    assert sent(link, asked) == [
        'MEASUrement:ADDNew "MEAS2"',
        "MEASUrement:MEAS2:TYPe MEAN",
        "MEASUrement:MEAS2:SOUrce1 CH2",
    ]
    assert fake.scope.slots[2] == (True, "MEAN", "CH2")
    assert fake.handle_text("SYSTem:ERRor?") == '0,"No error"'
    assert worker._slots == {1: (1, "pkpk"), 2: (2, "mean"), 3: (1, "period")}


def test_a_listed_measurement_already_shown_is_not_added_again() -> None:
    worker, _, link = listed_worker()
    first = scope_ops.measure(worker, 2, {"type": "frequency"})
    asked = len(link.commands)
    assert scope_ops.measure(worker, 2, {"type": "frequency"})["slot"] == first["slot"]
    assert scope_ops.measure(worker, 2, {"type": "frequency", "slot": first["slot"]}) == first
    assert not sent(link, asked)


def test_a_listed_slot_asked_for_something_else_is_deleted_then_added() -> None:
    worker, fake, link = listed_worker()
    assert fake.scope is not None
    asked = len(link.commands)
    shown = scope_ops.measure(worker, 2, {"type": "frequency", "slot": 1})
    assert shown["slot"] == 1 and shown["value"] == 1000.0
    assert sent(link, asked) == [
        'MEASUrement:DELete "MEAS1"',
        'MEASUrement:ADDNew "MEAS1"',
        "MEASUrement:MEAS1:TYPe FREQuency",
        "MEASUrement:MEAS1:SOUrce1 CH2",
    ]
    assert fake.scope.slots == {1: (True, "FREQUENCY", "CH2")}


def test_a_listed_slot_in_use_is_refused_when_the_profile_cannot_delete() -> None:
    raw = listed_like()
    del raw["scope"]["measure"]["off"]
    worker, _, _ = listed_worker(raw)
    with pytest.raises(RangeError, match="slot 1 is in use and the profile cannot clear a slot"):
        scope_ops.measure(worker, 2, {"type": "frequency", "slot": 1})


def test_a_listed_measurement_is_deleted_and_no_longer_read() -> None:
    worker, fake, link = listed_worker()
    assert fake.scope is not None
    scope_ops.measure(worker, 2, {"type": "frequency"})
    off = scope_ops.measure(worker, 2, {"type": "frequency", "slot": 2, "off": True})
    assert off == {"slot": 2, "off": True}
    assert 'MEASUrement:DELete "MEAS2"' in link.commands
    assert sorted(fake.scope.slots) == [1]
    assert worker._slots == {1: (1, "pkpk")}
    # The number is free again.
    assert scope_ops.measure(worker, 1, {"type": "mean"})["slot"] == 2


def test_listed_slots_are_read_under_their_channels() -> None:
    worker, fake, _ = listed_worker()
    define(fake, 4, "MEAN", 2)
    worker._slots = scope_ops.scan_slots(worker)
    rows: dict[int | None, dict[str, Any]] = {}
    scope_ops.read_slots(worker, rows)
    assert rows == {1: {"pkpk": 2.0}, 2: {"mean": 0.5}}


def test_a_listed_scope_is_full_at_its_slot_count() -> None:
    worker, fake, _ = listed_worker()
    for slot in range(2, 9):
        define(fake, slot, "PERIod", 1)
    with pytest.raises(RangeError, match="every slot shows something; give a slot to replace"):
        scope_ops.measure(worker, 2, {"type": "mean"})
    with pytest.raises(RangeError, match="slot must be from 1 to 8"):
        scope_ops.measure(worker, 2, {"type": "mean", "slot": 9})


def test_a_listed_slot_that_does_not_take_the_measurement_is_reported() -> None:
    def ignoring(fake: FakeInstrument, command: str) -> str | bytes | None:
        return None if "SOUrce1 " in command else fake.handle(command)

    worker, _, _ = direct_worker(listed_like(), ignoring)
    with pytest.raises(RangeError, match="slot 2 did not take the measurement"):
        scope_ops.measure(worker, 2, {"type": "mean"})


def listed_slots(**changes: Any) -> dict[str, Any]:
    raw = listed_like()
    slots = raw["scope"]["slots"]
    for key, value in changes.items():
        if value is None:
            del slots[key]
        else:
            slots[key] = value
    return raw


@pytest.mark.parametrize(
    ("raw", "complaint"),
    [
        (
            listed_slots(enabled="MEAS{slot}:STATE?"),
            "needs exactly one of enabled (a query per slot) or list",
        ),
        (listed_slots(list=None), "needs exactly one of enabled"),
        (listed_slots(list_pattern=None), "list_pattern: missing or empty"),
        (listed_slots(list_pattern=r"MEAS(\d+)"), "must capture the slot number as (?P<slot>"),
        (listed_slots(list="MEAS{slot}?"), "list: unknown placeholder slot"),
        (listed_slots(type="MEASUrement:TYPe?"), "type: must contain {slot}"),
    ],
)
def test_a_list_of_slots_that_cannot_be_read_is_refused(
    raw: dict[str, Any], complaint: str
) -> None:
    with pytest.raises(ProfileError, match=re.escape(complaint)):
        profile_from(raw)


def test_a_pattern_without_a_list_is_refused() -> None:
    raw = listed_slots(list=None, enabled="P{slot}?")
    with pytest.raises(ProfileError, match="list_pattern: is only for list"):
        profile_from(raw)


@pytest.mark.parametrize(
    ("commands", "complaint"),
    [
        (["ADD {type} {ch}"], "commands: must contain {slot} in one command"),
        (["ADD {slot} {type}"], "commands: must contain {ch} in one command"),
    ],
)
def test_commands_that_cannot_create_a_listed_slot_are_refused(
    commands: list[str], complaint: str
) -> None:
    raw = listed_like()
    raw["scope"]["measure"]["commands"] = commands
    with pytest.raises(ProfileError, match=re.escape(complaint)):
        profile_from(raw)


def test_slots_are_read_per_slot_unless_the_profile_lists_them(scope: Profile) -> None:
    assert scope.scope is not None and scope.scope.slots is not None
    assert scope.scope.slots.enabled is not None and scope.scope.slots.list_query is None
    listed = profile_from(listed_like()).scope
    assert listed is not None and listed.slots is not None
    assert listed.slots.enabled is None and listed.slots.listed("MEAS1,MEAS12") == [1, 12]
    assert listed.slots.listed("NONE") == []


@pytest.mark.parametrize("make", [keysight_like, legacy_like])
def test_a_linear_capture_places_the_samples_around_the_moment_the_data_was_requested(
    make: Callable[[], dict[str, Any]],
) -> None:
    profile = profile_from(make())
    assert profile.scope is not None and profile.scope.waveform is not None
    data = engine.render(profile.scope.waveform.data, ch=1)
    fake = FakeInstrument(profile)
    now = {"ns": 5 * NS_PER_S}

    def handler(command: str) -> str | bytes | None:
        # Every scaling query is answered by 7 s, before the data is requested; the data takes 3 s.
        now["ns"] = 10 * NS_PER_S if command == data else 7 * NS_PER_S
        return fake.handle(command)

    worker, link, emitter = connected(profile, handler, name="scope", profile=profile.id)
    worker._now = lambda: now["ns"]
    worker._clock = HostClock(now=worker._now, wall=lambda: EPOCH_NS + now["ns"])
    asked = len(link.commands)
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    sent = link.commands[asked:]
    assert sent.index(data) == len([c for c in sent if c != ":SYSTem:ERRor?"]) - 1
    [(_, _, samples)] = emitter.waveforms
    assert samples[0][0] == EPOCH_NS + 7 * NS_PER_S + round(captured["first_s"] * NS_PER_S)
    pitch_ns = captured["sample_interval_s"] * NS_PER_S
    assert samples[-1][0] - samples[0][0] == pytest.approx((POINTS - 1) * pitch_ns, abs=1)
    assert captured["fetched_in_s"] == 3.0
    assert captured["volts_per_div"] is None


def test_scaling_replies_with_units_and_prefixes_are_read() -> None:
    """A legacy scope answers with units: 2.00MSa/s, 500mV, 500us."""
    replies = {"SARA?": "2.00MSa/s", "C1:VDIV?": "500mV", "TDIV?": "500us", "C1:OFST?": "0V"}

    def with_units(fake: FakeInstrument, command: str) -> str | bytes | None:
        return replies.get(command) or fake.handle(command)

    worker, _, _ = direct_worker(legacy_like(), with_units)
    captured = scope_ops.capture(worker, 1, {"points": POINTS})
    assert captured["sample_interval_s"] == pytest.approx(50 / 2e6)
    assert captured["first_s"] == pytest.approx(-0.0035)
    assert captured["maximum"] == pytest.approx(1.5, abs=0.02)


@pytest.mark.parametrize(
    ("reply", "complaint"),
    [
        ("4,0,200", "the waveform scale could not be read: the preamble has no field 4"),
        ("4,0,200,1,X,0,0,1e-4,0,32768", "the waveform scale could not be read"),
        ("4,0,200,1,0,0,0,1e-4,0,32768", "the waveform scale is not usable"),
        ("4,0,200,1,1e-6,0,0,0,0,32768", "the waveform scale is not usable"),
    ],
)
def test_a_preamble_that_cannot_scale_the_samples_fails_the_capture(
    reply: str, complaint: str
) -> None:
    def broken(fake: FakeInstrument, command: str) -> str | bytes | None:
        return reply if command == ":WAVeform:PREamble?" else fake.handle(command)

    worker, fake, link = direct_worker(keysight_like(), broken)
    with pytest.raises(RangeError, match=re.escape(complaint)):
        scope_ops.capture(worker, 1, {"points": POINTS})
    assert ":WAVeform:DATA?" not in link.commands
    assert worker._query("*IDN?") == fake.idn
