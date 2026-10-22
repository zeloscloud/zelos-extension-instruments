"""Every shipped profile runs against the fake built from it.

This proves that the engine executes the profile: every query renders, every reply parses, every
setter round-trips, and every system function runs. It proves nothing about the instrument; a
transcript does that. The same checks run on profiles built in the tests, so the setters and
formats no shipped profile uses yet are held to them too.
"""

import copy
import dataclasses
import math
import struct
import tomllib
from typing import Any

import pytest

from tests.conftest import (
    RecordingEmitter,
    ScriptedLink,
    ask,
    assert_field_type,
    instrument_config,
    opener_for,
    wait_until,
)
from tests.test_daq_ops import BUFFER_STYLE, KEYSIGHT_STYLE
from tests.test_fake_fgen import RIGOL_STYLE, SIGLENT_STYLE
from tests.test_sim_fake import LOAD, NEW_LOAD, NEW_PSU, RANGED_DMM, SYSTEM
from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import (
    LABEL_SETTERS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    Profile,
    Range,
    Setter,
)
from zelos_extension_instruments.profile_load import SHIPPED_DIR, from_dict, shipped
from zelos_extension_instruments.profile_scope import MEASUREMENT_FIELDS
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.sim.fake_scope import FakeScope
from zelos_extension_instruments.waveform import block_payload
from zelos_extension_instruments.worker import InstrumentWorker

PROFILES = shipped()
# The meter setters that name a function by its root word.
METER = ("range", "autorange", "nplc")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _inline() -> list[Profile]:
    """Profiles the tests build, with every system function the fake has."""
    raws = [LOAD, NEW_PSU, NEW_LOAD, RANGED_DMM, RIGOL_STYLE, SIGLENT_STYLE]
    raws += [KEYSIGHT_STYLE, BUFFER_STYLE]
    found = []
    for raw in raws:
        raw = copy.deepcopy(raw)
        raw.setdefault("system", SYSTEM)
        found.append(from_dict(raw, f"{raw['id']}.toml"))
    return found


def _direct_scope() -> Profile:
    """The shipped scope with its slots swapped for a measurement asked for directly."""
    raw = tomllib.loads((SHIPPED_DIR / "scope" / "siglent-sds.toml").read_text(encoding="utf-8"))
    raw["id"] = "direct-scope"
    del raw["scope"]["slots"]
    words = {"pkpk": "VPP", "frequency": "FREQ", "rms": "VRMS"}
    raw["scope"]["measure"] = {"query": ":MEAS:{type}? CHAN{ch}", "words": words}
    return from_dict(raw, "direct-scope.toml")


INLINE = [*_inline(), _direct_scope()]
EVERY = [*PROFILES, *INLINE]
EVERY_IDS = [p.id for p in EVERY]


class Bench:
    """A profile's fake with the engine's way of talking to it: a channel is selected first when
    the profile selects, and a placeholder a setter needs comes from the profile."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.fake = FakeInstrument(profile)
        self.selector = engine.Selector(profile.link.select)
        modulation = profile.setters.get("modulation_type")
        self.modulation = next(iter(modulation.map.items()), None) if modulation else None

    def send(self, command: str, ch: int | None = None) -> str | None:
        select = self.selector.command_for(ch)
        if select is not None:
            self.fake.handle_text(select)
        return self.fake.handle_text(command)

    def drained(self, where: str) -> None:
        errors = self.profile.errors
        if errors is not None:
            reply = self.fake.handle_text(errors.query) or ""
            assert engine.read_error(errors, reply) is None, (self.profile.id, where, reply)

    def fill(self, function: str | None = None, modulation: bool = False) -> dict[str, Any]:
        """The placeholders beyond the channel and the value a command may hold."""
        found: dict[str, Any] = {"src": 1}
        if modulation and self.modulation is not None:
            found["modulation"] = self.modulation[1]
        if function is not None:
            found["function"] = self.function_word(function)
        return found

    def function_word(self, function: str) -> str:
        dmm = self.profile.dmm
        assert dmm is not None
        # Any meter setter that names a function by its root word has the map.
        for name in METER:
            setter = self.profile.setters.get(name)
            if setter is not None and function in setter.map:
                return setter.map[function]
        return dmm.words[function]

    def uses(self, setter: Setter, placeholder: str) -> bool:
        texts = (setter.command, setter.off or "", setter.verify or "")
        return any(f"{{{placeholder}}}" in text for text in texts)

    def functions(self, setter: Setter) -> list[str | None]:
        """The functions a meter setter is tried on, each selected first when it can be."""
        dmm = self.profile.dmm
        if dmm is None or setter.name not in METER or not self.uses(setter, "function"):
            return [None]
        offered = [*setter.map] or [*dmm.functions]
        selector = self.profile.setters.get("function")
        return [f for f in offered if selector is None or f in selector.map]

    def select(self, setter: Setter, ch: int, function: str | None) -> bool:
        """Make `function` the one a reader without a function reads; whether that holds."""
        dmm = self.profile.dmm
        selector = self.profile.setters.get("function")
        if function is None or dmm is None:
            return True
        if selector is None:
            return function == next(iter(dmm.functions))
        self.send(engine.setter_command(selector, ch=ch, function=function), ch)
        return True

    def tune(self, setter: Setter, ch: int) -> None:
        """Make the modulation type the setter's command names the one now selected."""
        label = self.profile.setters.get("modulation_type")
        if label is not None and self.modulation is not None and self.uses(setter, "modulation"):
            self.send(engine.render(label.command, ch=ch, value=self.modulation[1]), ch)

    def command(
        self,
        setter: Setter,
        ch: int,
        *,
        value: float | None = None,
        range_: Range | None = None,
        on: bool | None = None,
        function: str | None = None,
    ) -> str:
        fill = self.fill(function, modulation=True)
        if function is None:
            return engine.setter_command(
                setter,
                ch=ch,
                value=value,
                range_=range_,
                on=on,
                src=1,
                modulation=fill.get("modulation"),
            )
        text = None if value is None else engine.format_value(value / setter.scale, None)
        command = setter.off if on is False and setter.off is not None else setter.command
        return engine.render(
            command, ch=ch if setter.per_channel else None, value=text, on=on, **fill
        )

    def readback(self, setter: Setter, ch: int, function: str | None = None) -> str | None:
        query = self.profile.verify_query(setter)
        if query is None:
            return None
        return self.send(engine.render(query, ch=ch, **self.fill(function, modulation=True)), ch)

    def number(self, setter: Setter, ch: int, reply: str) -> float:
        """A value setter's readback in SI units: through the reader that shares the query."""
        field_name = self.profile.verify_field(setter)
        reader = self.profile.reader(field_name)
        if setter.verify is None and reader is not None:
            return engine.parse_reply(reader, reply, ch)[field_name]
        return engine.parse_float(reply) * setter.scale

    def boolean(self, setter: Setter, ch: int, reply: str) -> bool:
        field_name = self.profile.verify_field(setter)
        reader = self.profile.reader(field_name)
        if setter.verify is None and reader is not None:
            return engine.parse_reply(reader, reply, ch)[field_name]
        return engine.parse_bool(reply)

    def value_for(
        self, setter: Setter, ch: int, fraction: float = 0.25
    ) -> tuple[float, Range | None]:
        """A value inside the setter's range, a quarter of the way up by default."""
        key = self.profile.range_key(setter)
        range_ = self.profile.range_for(key, self.fake.model, ch) if key else None
        if range_ is not None and range_.choices:
            return range_.choices[int(fraction * len(range_.choices))], range_
        low, high = self.fake.bounds(key or setter.name, ch)
        bounded = math.isfinite(low) and math.isfinite(high)
        return (round(low + (high - low) * fraction, 3) if bounded else 1.0), range_

    def write_value(
        self, setter: Setter, ch: int, fraction: float = 0.25, function: str | None = None
    ) -> float:
        value, range_ = self.value_for(setter, ch, fraction)
        self.send(
            self.command(setter, ch, value=value, range_=range_, function=function),
            ch if setter.per_channel else None,
        )
        return value

    def round_trip_value(self, setter: Setter, ch: int) -> None:
        for function in self.functions(setter):
            self.tune(setter, ch)
            readable = self.select(setter, ch, function)
            value, range_ = self.value_for(setter, ch)
            self.write_value(setter, ch, function=function)
            verify = self.profile.verify_query(setter)
            if verify is None or not (readable or self.uses(setter, "function")):
                continue
            readback = self.number(setter, ch, self.readback(setter, ch, function) or "")
            where = (self.profile.id, setter.name, function, value, readback)
            if setter.name == "range" and self.profile.dmm is not None:
                # A meter takes the range that covers the request.
                assert readback >= value, where
            else:
                assert engine.verified(value, readback, range_), where

    def round_trip_switch(self, setter: Setter, ch: int) -> None:
        for function in self.functions(setter):
            self.tune(setter, ch)
            readable = self.select(setter, ch, function)
            for on in (True, False):
                self.send(self.command(setter, ch, on=on, function=function), ch)
                if setter.master is not None:
                    # An armed channel delivers power only once the master is on.
                    assert self.send(engine.render(setter.master.armed, ch=ch), ch) == str(int(on))
                    self.send(engine.render(setter.master.command, on=True))
                    assert self.send(setter.master.query) == "1"
                reply = self.readback(setter, ch, function)
                if reply is not None and (readable or self.uses(setter, "function")):
                    assert self.boolean(setter, ch, reply) is on, (
                        self.profile.id,
                        setter.name,
                        reply,
                    )

    def round_trip_label(self, setter: Setter, ch: int) -> None:
        field_name = self.profile.verify_field(setter)
        reader = self.profile.reader(field_name)
        self.tune(setter, ch)
        for label, word in setter.map.items():
            self.send(engine.render(setter.command, ch=ch, value=word, **self.fill(None, True)), ch)
            reply = self.readback(setter, ch)
            if reply is None:
                continue
            if setter.verify is None and reader is not None:
                got = engine.parse_reply(reader, reply, ch)[field_name]
            else:
                got = label if reply.strip().strip('"').casefold() == word.casefold() else reply
            assert got == label, (self.profile.id, setter.name, label, reply)

    def run_setters(self) -> None:
        for name, setter in self.profile.setters.items():
            for ch in range(1, self.fake.channels + 1):
                self.run_setter(name, setter, ch)
            self.drained(name)

    def run_setter(self, name: str, setter: Setter, ch: int) -> None:
        if name in VALUE_SETTERS:
            self.round_trip_value(setter, ch)
        elif name in SWITCH_SETTERS:
            self.round_trip_switch(setter, ch)
        elif name == "function":
            for label in setter.map:
                self.send(engine.setter_command(setter, ch=ch, function=label), ch)
        elif name in LABEL_SETTERS:
            self.round_trip_label(setter, ch)
        elif name == "clear":
            for command in engine.clear_commands(setter, ch=ch, src=1):
                self.send(command, ch if setter.per_channel else None)
        else:
            for command in setter.commands:
                rendered = engine.render(command, ch=ch if setter.per_channel else None, src=1)
                self.send(rendered, ch if setter.per_channel else None)


def round_trip(profile: Profile) -> FakeInstrument:
    """Every query parses, every setter reads back and every system function runs."""
    bench = Bench(profile)
    for step in engine.compile_plan(profile, bench.fake.channels):
        reply = bench.send(step.query, step.channel)
        assert reply is not None, f"{profile.id}: no reply to {step.query}"
        where = f"{profile.id}: {step.query} -> {reply!r}"
        if step.flag is not None:
            assert engine.flag_is_set(step.flag, reply, step.channel) is False, where
        else:
            values = engine.parse_reply(step.reader, reply, step.channel)
            assert values, where
            for name, value in values.items():
                assert_field_type(where, name, value)
    bench.run_setters()
    if profile.daq is not None:
        daq_round_trip(bench)
    system_round_trip(bench)
    bench.drained("the end")
    return bench.fake


def daq_round_trip(bench: Bench) -> None:
    """Each function is configured on two addresses, scanned and read, and integration times
    answer."""
    profile, daq = bench.profile, bench.profile.daq
    assert daq is not None
    addresses = [n for n in range(1, 10_000) if daq.channel_pattern.fullmatch(str(n))][:2]
    assert addresses, f"{profile.id}: channel_pattern matches no address"
    listed = ",".join(map(str, addresses))
    for function, commands in daq.configure.items():
        for command in commands:
            bench.send(engine.render(command, channels=listed, tc="K"))
        if daq.nplc is not None and function in daq.nplc.map:
            nplc = engine.render(
                daq.nplc.command, channels=listed, value="1", function=daq.nplc.map[function]
            )
            bench.send(nplc)
        for command in daq.scan:
            bench.send(engine.render(command, channels=listed, count=len(addresses)))
        for command in daq.read[:-1]:
            assert bench.send(engine.render(command, count=len(addresses))) is None
        reply = bench.send(engine.render(daq.read[-1], count=len(addresses)))
        assert reply is not None, f"{profile.id}: a scan of {function} read nothing"
        readings = [engine.parse_float(part) for part in reply.split(",")]
        assert len(readings) == len(addresses), (profile.id, function, reply)
        bench.drained(f"configure {function}")


def probe(bench: Bench) -> tuple[Setter, int] | None:
    """A setting the fake keeps, that a saved state can be shown to bring back."""
    setters = [
        s
        for s in bench.profile.setters.values()
        if bench.profile.verify_query(s) is not None
        and not any(bench.uses(s, p) for p in ("function", "modulation"))
    ]
    profile, model = bench.profile, bench.fake.model
    values = [
        s
        for s in setters
        if s.name in VALUE_SETTERS
        and (key := profile.range_key(s)) is not None
        and profile.range_for(key, model, 1) is not None
    ]
    switches = [s for s in setters if s.name in SWITCH_SETTERS and s.name != "output"]
    chosen = values or switches
    return (chosen[0], 1) if chosen else None


def system_round_trip(bench: Bench) -> None:
    """Each system function the profile has runs against the fake and does what it says."""
    profile, fake = bench.profile, bench.fake
    system = profile.system
    if system.opc is not None:
        assert bench.send(system.opc) == "1"
    if system.self_test is not None:
        reply = bench.send(system.self_test)
        assert reply is not None and system.self_test_ok.search(reply), (profile.id, reply)
    if system.beep is not None:
        before = fake.beeps
        bench.send(system.beep)
        assert fake.beeps == before + 1
    if system.display is not None:
        for on in (False, True):
            bench.send(engine.render(system.display, on=on))
            assert fake.display_on is on
    if system.display_text is not None:
        assert system.display_text_max is not None
        text = "HELLO WORLD"[: system.display_text_max]
        bench.send(engine.render(system.display_text, text=text))
        assert fake.display_message == text
    if system.display_clear is not None:
        bench.send(system.display_clear)
        assert fake.display_message == ""
    if system.lock is not None and system.unlock is not None:
        bench.send(system.lock)
        assert fake.locked is True
        bench.send(system.unlock)
        assert fake.locked is False
    if system.screenshot:
        for command in system.screenshot[:-1]:
            reply = bench.send(command)
            assert ("?" in command) == (reply is not None), (profile.id, command)
        assert_image(system.screenshot_framing, fake.handle(system.screenshot[-1]))
        for command in system.screenshot_cleanup:
            assert bench.send(command) is None
    if system.setup_query is not None and system.setup_load is not None:
        block = fake.handle(system.setup_query)
        assert isinstance(block, bytes) and len(block_payload(block)) > 0
        assert bench.send(system.setup_load + block.decode()) is None
    if system.save is not None and system.recall is not None and system.slots is not None:
        saved_and_recalled(bench, system.slots[0])
    if system.reset is not None:
        reset_leaves_outputs_off(bench)
    if system.reboot is not None:
        bench.send(system.reboot)
        assert fake.rebooted is True
    bench.drained("the system functions")


def saved_and_recalled(bench: Bench, slot: int) -> None:
    system = bench.profile.system
    assert system.save is not None and system.recall is not None
    found = probe(bench)
    if found is None:
        bench.send(engine.render(system.save, slot=slot))
        bench.send(engine.render(system.recall, slot=slot))
        return
    setter, ch = found
    if setter.name in VALUE_SETTERS:
        first = bench.write_value(setter, ch, 0.25)
        bench.send(engine.render(system.save, slot=slot))
        second = bench.write_value(setter, ch, 0.75)
        assert second != first
        bench.send(engine.render(system.recall, slot=slot))
        got = bench.number(setter, ch, bench.readback(setter, ch) or "")
        assert got == pytest.approx(first, rel=1e-3), (bench.profile.id, setter.name)
    else:
        bench.send(bench.command(setter, ch, on=True), ch)
        bench.send(engine.render(system.save, slot=slot))
        bench.send(bench.command(setter, ch, on=False), ch)
        bench.send(engine.render(system.recall, slot=slot))
        assert bench.boolean(setter, ch, bench.readback(setter, ch) or "") is True


def reset_leaves_outputs_off(bench: Bench) -> None:
    profile, system = bench.profile, bench.profile.system
    assert system.reset is not None
    output = profile.setters.get("output")
    if output is not None:
        bench.send(bench.command(output, 1, on=True), 1)
    bench.send(system.reset)
    if system.opc is not None:
        assert bench.send(system.opc) == "1"
    if output is not None:
        reply = bench.readback(output, 1)
        if reply is not None:
            assert bench.boolean(output, 1, reply) is False, (profile.id, reply)


def assert_image(framing: str, reply: str | bytes | None) -> None:
    assert isinstance(reply, bytes), "a screenshot comes back as bytes"
    if framing == "block":
        reply = block_payload(reply)
    if framing == "bmp":
        assert reply[:2] == b"BM"
        assert struct.unpack_from("<I", reply, 2)[0] == len(reply)
        return
    assert reply.startswith(PNG_MAGIC)
    if framing == "png":
        assert reply.endswith(b"IEND\xaeB`\x82")


@pytest.mark.parametrize("profile", EVERY, ids=EVERY_IDS)
def test_every_query_parses_and_every_setter_round_trips(profile: Profile) -> None:
    round_trip(profile)


@pytest.mark.parametrize("framing", ["block", "bmp", "png"])
def test_a_screenshot_comes_back_in_the_profiles_framing(framing: str) -> None:
    profile = next(p for p in INLINE if p.id == "new-psu")
    system = dataclasses.replace(profile.system, screenshot_framing=framing)
    round_trip(dataclasses.replace(profile, system=system))


def test_the_inline_profiles_cover_every_new_setter_and_system_function() -> None:
    """If a profile here drops a feature, the round trip above stops proving it."""
    setters = {name for p in INLINE for name in p.setters}
    for name in (
        "remote_sense",
        "ovp_delay",
        "tracking",
        "short",
        "current_range",
        "voltage_range",
        "slew_rise",
        "slew_fall",
        "range",
        "autorange",
        "nplc",
        "shape",
        "frequency",
        "amplitude",
        "offset",
        "high",
        "low",
        "output_load",
        "modulation",
        "modulation_type",
        "modulation_depth",
        "modulation_deviation",
        "burst",
        "sweep",
        "trigger",
        "relay",
    ):
        assert name in setters, name
    assert any(p.setters["remote_sense"].off for p in INLINE if "remote_sense" in p.setters)
    assert any(p.category == "daq" for p in INLINE)
    system = next(p for p in INLINE if p.id == "new-psu").system
    assert system.save and system.screenshot and system.setup_query and system.reboot


# A worker-level daq test lives with the daq worker.
WORKER_PROFILES = [p for p in [*PROFILES, INLINE[-1]] if p.category != "daq"]


@pytest.mark.parametrize("profile", WORKER_PROFILES, ids=lambda p: p.id)
def test_the_worker_records_every_field_from_the_fake(profile: Profile) -> None:
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="unit", profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
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
    assert profile.scope is not None
    direct = profile.scope.slots is None
    wide = set(profile.instrument_fields)
    assert wait_until(
        lambda: wide <= {k for a in emitter.acquisitions for k in a[1]}, timeout_s=10
    ), f"{profile.id}: never read {wide - {k for a in emitter.acquisitions for k in a[1]}}"
    if direct:
        _measure_direct(profile, worker, emitter)
    else:
        _measure_in_slots(profile, worker, emitter)
    state = ask(worker, "state")
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
    # Settings without a range are not refused: nothing a scope sets is an output.
    wanted: dict[str, Any] = {
        "scale": 0.2,
        "coupling": "ac",
        "timebase": 0.001,
        "trigger_level": 0.25,
        "offset": -0.1,
        "visible": False,
    }
    for name, value in wanted.items():
        if name not in profile.setters:
            continue
        got = ask(worker, "set", channel=1, **{name: value})[name]
        if isinstance(value, float):
            readback = got["readback"]
            assert readback is None or readback == pytest.approx(value), (profile.id, name, got)
        else:
            assert got == value, (profile.id, name, got)
    assert ask(worker, "command", name="stop") == {"ok": True, "command": "stop"}
    assert ask(worker, "command", name="run")["ok"] is True


def _measure_in_slots(
    profile: Profile, worker: InstrumentWorker, emitter: RecordingEmitter
) -> None:
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


def _measure_direct(profile: Profile, worker: InstrumentWorker, emitter: RecordingEmitter) -> None:
    """A scope that is asked for each measurement shows none until a measure adds the pair, and
    reads it on every pass after."""
    scope = profile.scope
    assert scope is not None and scope.measure is not None
    kind = "frequency" if "frequency" in scope.measure.words else next(iter(scope.measure.words))
    assert not any(r[1] == 2 and kind in r[2] for r in emitter.rows)
    shown = ask(worker, "measure", channel=2, type=kind)
    assert shown["channel"] == 2 and shown["type"] == kind, shown
    assert shown["value"] == pytest.approx(FakeScope(profile, 2).measurement(kind))
    assert wait_until(lambda: any(r[1] == 2 and kind in r[2] for r in emitter.rows), timeout_s=10)
