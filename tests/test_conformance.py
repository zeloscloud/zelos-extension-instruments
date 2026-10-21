"""Every shipped profile runs against the fake built from it.

This proves that the engine executes the profile: every query renders, every reply parses, every
setter round-trips. It proves nothing about the instrument; a transcript does that.
"""

import math

import pytest

from tests.conftest import ScriptedLink, instrument_config, opener_for, settings_for, wait_until
from tests.test_worker import RecordingEmitter, ask
from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import (
    BOOL_FIELDS,
    ENUM_FIELDS,
    LABEL_SETTERS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    VERIFY_FIELD,
    Profile,
    shipped,
)
from zelos_extension_instruments.profile_scope import MEASUREMENT_FIELDS
from zelos_extension_instruments.sim import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker

PROFILES = shipped()


@pytest.mark.parametrize("profile", PROFILES, ids=[p.id for p in PROFILES])
def test_every_query_parses_and_every_setter_round_trips(profile: Profile) -> None:
    fake = FakeInstrument(profile)
    channels = fake.channels
    plan = engine.compile_plan(profile, channels)
    selector = engine.Selector(profile.link.select)

    def send(command: str) -> str | None:
        select = selector.command_for(None)
        if select is not None:
            fake.handle_text(select)
        return fake.handle_text(command)

    for step in plan:
        select = selector.command_for(step.channel)
        if select is not None:
            fake.handle_text(select)
        reply = fake.handle_text(step.query)
        assert reply is not None, f"{profile.id}: no reply to {step.query}"
        if step.flag is not None:
            engine.flag_is_set(step.flag, reply, step.channel)
        else:
            values = engine.parse_reply(step.reader, reply, step.channel)
            for name, value in values.items():
                if name in ENUM_FIELDS:
                    assert value in ENUM_FIELDS[name], f"{profile.id}: {name} read {value!r}"
                elif name in BOOL_FIELDS:
                    assert isinstance(value, bool)
                else:
                    assert isinstance(value, int | float) and not isinstance(value, bool)
    for name, setter in profile.setters.items():
        for ch in range(1, channels + 1):
            if setter.per_channel:
                select = selector.command_for(ch)
                if select is not None:
                    fake.handle_text(select)
            key = profile.range_key(setter)
            range_ = profile.range_for(key, fake.model, ch) if key else None
            if name in VALUE_SETTERS:
                low, high = fake.bounds(key or name, ch)
                bounded = math.isfinite(low) and math.isfinite(high)
                value = round(low + (high - low) * 0.25, 3) if bounded else 1.0
                fake.handle_text(engine.setter_command(setter, ch=ch, value=value, range_=range_))
                verify = profile.verify_query(setter)
                if verify is not None:
                    reply = fake.handle_text(engine.render(verify, ch=ch)) or ""
                    readback = engine.parse_float(reply) * setter.scale
                    assert engine.verified(value, readback, range_), (
                        profile.id,
                        name,
                        value,
                        readback,
                    )
            elif name in SWITCH_SETTERS:
                for on in (True, False):
                    fake.handle_text(engine.setter_command(setter, ch=ch, on=on))
                    verify = profile.verify_query(setter)
                    if verify is not None:
                        reader = profile.reader(name)
                        reply = fake.handle_text(engine.render(verify, ch=ch)) or ""
                        got = (
                            engine.parse_reply(reader, reply, ch)[name]
                            if reader is not None and reader.query == verify
                            else engine.parse_bool(reply)
                        )
                        assert got is on, (profile.id, reply)
            elif name == "function":
                for label in setter.map:
                    fake.handle_text(engine.setter_command(setter, ch=ch, function=label))
            elif name in LABEL_SETTERS:
                field_name = VERIFY_FIELD[name]
                reader = profile.reader(field_name)
                for label, word in setter.map.items():
                    fake.handle_text(engine.render(setter.command, ch=ch, value=word))
                    if reader is not None and reader.query is not None:
                        reply = fake.handle_text(engine.render(reader.query, ch=ch)) or ""
                        got = engine.parse_reply(reader, reply, ch)[field_name]
                        assert got == label, (profile.id, name, label, reply)
            elif name == "clear":
                for command in engine.clear_commands(setter, ch=ch):
                    fake.handle_text(command)
            else:
                for command in setter.commands:
                    fake.handle_text(engine.render(command, ch=ch if setter.per_channel else None))
        if profile.errors is not None:
            reply = fake.handle_text(profile.errors.query) or ""
            assert engine.read_error(profile.errors, reply) is None, (profile.id, name, reply)
    if profile.errors is not None:
        reply = fake.handle_text(profile.errors.query) or ""
        assert engine.read_error(profile.errors, reply) is None, (profile.id, reply)


@pytest.mark.parametrize("profile", PROFILES, ids=[p.id for p in PROFILES])
def test_the_worker_records_every_field_from_the_fake(profile: Profile) -> None:
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="unit", profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, settings_for(cfg), open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        expected = set(profile.fields) - {"value"}
        if profile.category == "scope":
            # A measurement is read only while a slot shows it; the round trip below covers that.
            expected -= set(MEASUREMENT_FIELDS)
        dropped = {key.split("@")[0] for key in worker._dropped}
        assert not dropped, f"{profile.id}: the fake refused {dropped}"
        assert wait_until(
            lambda: expected <= {k for r in emitter.rows for k in r[2]}, timeout_s=10
        ), f"{profile.id}: never read {expected - {k for r in emitter.rows for k in r[2]}}"
        if profile.category == "dmm":
            assert wait_until(lambda: bool(emitter.readings), timeout_s=10)
        if profile.category == "scope":
            _scope_round_trip(profile, worker, emitter)
        state = ask(worker, "state")
        assert state["counters"]["timeouts"] == 0
        assert state["counters"]["instrument_errors"] == 0, state["counters"]["last_error"]
        read = ask(worker, "read", channel=1)
        assert "error" not in read, read
    finally:
        worker.stop()
        worker.join(5)


def _scope_round_trip(
    profile: Profile, worker: InstrumentWorker, emitter: RecordingEmitter
) -> None:
    """A scope records its acquisition settings, its live slot, a measurement it is asked for,
    and a capture."""
    wide = set(profile.instrument_fields)
    assert wait_until(
        lambda: wide <= {k for a in emitter.acquisitions for k in a[1]}, timeout_s=10
    ), f"{profile.id}: never read {wide - {k for a in emitter.acquisitions for k in a[1]}}"
    # The fake's slot 1 shows PKPK on C1 from the start, untouched by the connect.
    assert wait_until(
        lambda: any(r[1] == 1 and "pkpk" in r[2] for r in emitter.rows), timeout_s=10
    ), f"{profile.id}: never read the live slot"
    assert not any("pkpk" in r[2] for r in emitter.rows if r[1] != 1)
    shown = ask(worker, "measure", channel=2, type="frequency")
    assert shown["channel"] == 2 and shown["type"] == "frequency", shown
    assert shown["value"] == pytest.approx(1000.0)
    assert wait_until(
        lambda: any(r[1] == 2 and "frequency" in r[2] for r in emitter.rows), timeout_s=10
    )
    state = ask(worker, "state")
    assert state["slots"][str(shown["slot"])] == {"channel": 2, "type": "frequency"}
    assert state["acquisition"]["timebase"] == pytest.approx(0.0005)
    captured = ask(worker, "waveform", channel=1, points=200)
    assert captured["samples"] == 200, captured
    assert captured["maximum"] == pytest.approx(1.5, abs=0.01)
    assert captured["minimum"] == pytest.approx(-0.5, abs=0.01)
    name, ch, samples = emitter.waveforms[-1]
    assert ch == 1 and len(samples) == 200
    times = [t for t, _ in samples]
    assert times == sorted(times) and times[-1] - times[0] == pytest.approx(
        captured["sample_interval_s"] * 199 * 1e9, rel=1e-6
    )
    assert ask(worker, "set", channel=1, scale=0.2, coupling="ac") == {
        "scale": {"set": 0.2, "readback": 0.2},
        "coupling": "ac",
    }
    assert ask(worker, "set", channel=1, timebase=0.001)["timebase"]["readback"] == 0.001
    # Settings without a range are not refused: nothing a scope sets is an output.
    assert ask(worker, "set", channel=1, trigger_level=0.25, offset=-0.1) == {
        "trigger_level": {"set": 0.25, "readback": 0.25},
        "offset": {"set": -0.1, "readback": -0.1},
    }
    assert ask(worker, "set", channel=1, visible=False) == {"visible": False}
    assert ask(worker, "command", name="stop") == {"ok": True, "command": "stop"}
    assert ask(worker, "command", name="run")["ok"] is True
