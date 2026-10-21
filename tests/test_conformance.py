"""Every shipped profile runs against the fake built from it.

This proves that the engine executes the profile: every query renders, every reply parses, every
setter round-trips. It proves nothing about the instrument; a transcript does that.
"""

import pytest

from tests.conftest import ScriptedLink, instrument_config, opener_for, settings_for, wait_until
from tests.test_worker import RecordingEmitter, ask
from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import (
    BOOL_FIELDS,
    ENUM_FIELDS,
    VALUE_SETTERS,
    Profile,
    shipped,
)
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
            fake.handle(select)
        return fake.handle(command)

    for step in plan:
        select = selector.command_for(step.channel)
        if select is not None:
            fake.handle(select)
        reply = fake.handle(step.query)
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
                    assert isinstance(value, float)
    for name, setter in profile.setters.items():
        for ch in range(1, channels + 1):
            if setter.per_channel:
                select = selector.command_for(ch)
                if select is not None:
                    fake.handle(select)
            key = profile.range_key(setter)
            range_ = profile.range_for(key, fake.model, ch) if key else None
            if name in VALUE_SETTERS:
                low, high = fake.bounds(key or name, ch)
                value = round(low + (high - low) * 0.25, 3)
                fake.handle(engine.setter_command(setter, ch=ch, value=value, range_=range_))
                verify = profile.verify_query(setter)
                if verify is not None:
                    readback = engine.parse_float(fake.handle(engine.render(verify, ch=ch)) or "")
                    assert engine.verified(value, readback, range_), (
                        profile.id,
                        name,
                        value,
                        readback,
                    )
            elif name == "output":
                for on in (True, False):
                    fake.handle(engine.setter_command(setter, ch=ch, on=on))
                    verify = profile.verify_query(setter)
                    if verify is not None:
                        reader = profile.reader("output")
                        reply = fake.handle(engine.render(verify, ch=ch)) or ""
                        got = (
                            engine.parse_reply(reader, reply, ch)["output"]
                            if reader is not None and reader.query == verify
                            else engine.parse_bool(reply)
                        )
                        assert got is on, (profile.id, reply)
            elif name == "function":
                for label in setter.map:
                    fake.handle(engine.setter_command(setter, ch=ch, function=label))
            else:
                for command in engine.clear_commands(setter, ch=ch):
                    fake.handle(command)
        if profile.errors is not None:
            reply = fake.handle(profile.errors.query) or ""
            assert engine.read_error(profile.errors, reply) is None, (profile.id, name, reply)
    if profile.errors is not None:
        reply = fake.handle(profile.errors.query) or ""
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
        dropped = {key.split("@")[0] for key in worker._dropped}
        assert not dropped, f"{profile.id}: the fake refused {dropped}"
        assert wait_until(
            lambda: expected <= {k for r in emitter.rows for k in r[2]}, timeout_s=10
        ), f"{profile.id}: never read {expected - {k for r in emitter.rows for k in r[2]}}"
        if profile.category == "dmm":
            assert wait_until(lambda: bool(emitter.readings), timeout_s=10)
        state = ask(worker, "state")
        assert state["counters"]["timeouts"] == 0
        assert state["counters"]["instrument_errors"] == 0, state["counters"]["last_error"]
        read = ask(worker, "read", channel=1)
        assert "error" not in read, read
    finally:
        worker.stop()
        worker.join(5)
