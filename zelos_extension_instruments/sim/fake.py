"""A fake instrument that answers a profile's own commands from a simple physical model.

Each command template of the profile becomes an anchored, case-insensitive pattern, and each reply
is written in the form the profile's parser reads back, so the engine cannot tell the fake from
the instrument the profile describes.
"""

import dataclasses
import json
import math
import random
import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from ..profile import (
    FUNCTION_LABELS,
    LABEL_SETTERS,
    PROTECTION_LABELS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    Flag,
    Master,
    Profile,
    Reader,
    Setter,
)
from ..profile_scope import SCOPE_COMMAND_SETTERS
from .fake_fgen import FakeFgen, default_label, keyed_reply
from .fake_scope import FakeScope
from .fake_system import block, bmp, daq_state, png, restore_daq, restore_scope, scope_state
from .spelling import example

if TYPE_CHECKING:
    from .fake_daq import FakeDaq


_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")
# A query that names a meter function or a modulation type needs its own pattern.
_SCOPED = re.compile(r"\{(function|modulation)\}")
_CAPTURES = {
    "ch": r"\d+",
    "ch0": r"\d+",
    "value": r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?",
    "on": "ON|OFF",
    "bit": "[01]",
    "slot": r"\d+",
    "type": r"[A-Za-z0-9]+",
    "points": r"\d+",
    "interval": r"\d+",
    "src": r"\d+",
    "text": r'[^"]*',
    "channels": r"\d+(?:\s*[,:]\s*\d+)*",
    "tc": "[BEJKNRST]",
    "count": r"\d+",
}
_NO_ERROR = '0,"No error"'
_UNDEFINED_HEADER = (-113, "Undefined header")
_OUT_OF_RANGE = (-222, "Data out of range")
_QUEUE_OVERFLOW = (-350, "Queue overflow")
_BAD_VALUE = (-224, "Illegal parameter value")
_TOO_MUCH = (-223, "Too much data")
_EMPTY_SLOT = (-256, "File name not found")
_QUEUE_DEPTH = 20
# SCPI's overflow reading, which the engine reads as not-a-number.
_OVERFLOW = 9.9e37
# The upper bound a range reports when the profile leaves it to a query.
_RATINGS = {
    "voltage": 30.0,
    "current": 3.0,
    "power": 300.0,
    "resistance": 10_000.0,
    "frequency": 1_000.0,
}
_READINGS = {
    "dc_voltage": 5.0,
    "ac_voltage": 120.0,
    "dc_current": 0.1,
    "ac_current": 0.1,
    "resistance": 1_000.0,
    "resistance_4w": 1_000.0,
    "frequency": 60.0,
    "period": 1 / 60,
    "temperature": 25.0,
    "capacitance": 1e-6,
    "diode": 0.6,
    "continuity": 0.5,
    "other": 0.0,
}
# The ranges a meter picks from, per function; it takes the smallest that covers a request.
_DMM_RANGES = {
    "dc_voltage": (0.1, 1.0, 10.0, 100.0, 1000.0),
    "ac_voltage": (0.1, 1.0, 10.0, 100.0, 750.0),
    "dc_current": (0.001, 0.01, 0.1, 1.0, 3.0, 10.0),
    "ac_current": (0.001, 0.01, 0.1, 1.0, 3.0, 10.0),
    "resistance": (1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8),
    "resistance_4w": (1e2, 1e3, 1e4, 1e5, 1e6, 1e7),
}
_DMM_DEFAULT_RANGES = (1.0, 10.0, 100.0, 1000.0)
# What a meter remembers for each function.
_METER = ("range", "autorange", "nplc")
_LABELS = {"tracking": "independent", "current_range": "high", "voltage_range": "high"}
_FALSE_LABELS = ("none", "false", "0")

# One piece of a reply: its comma-separated position, then text, or bits to OR, and hex format.
Part = tuple[int, str | int, bool]
Entry = Callable[[int], list[Part]]


@dataclass(slots=True)
class _Channel:
    function: str | None
    extras: dict[str, Any]
    setpoints: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(sorted(VALUE_SETTERS), 0.0)
    )
    output: bool = False
    trip: str = "none"
    load_ohms: float = 10.0
    source_volts: float = 12.0
    # A meter's range, autorange and integration time, by function.
    meter: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Route:
    pattern: re.Pattern[str]
    act: Callable[[re.Match[str]], str | bytes | None]


class FakeInstrument:
    """One instrument of a profile's family, answering the commands the profile sends.

    A psu or AC source drives `load_ohms` up to its current setpoint. A load sinks from a source
    of `source_volts`; in cv mode `load_ohms` is that source's series resistance, and a short
    reads zero volts. A DMM reads a settable number per function, with a little noise, and picks
    the smallest range that covers a request. A generator keeps its settings per channel in
    `fgen`, a scope in `scope`, a data-acquisition unit in `daq`. A protection trip turns the
    output off.

    The profile's system functions work on every category: `beeps`, `display_on`,
    `display_message`, `locked` and `rebooted` show what they did, and a saved state or a setup
    block brings every channel's settings back. Not thread-safe: FakeServer serializes its
    clients.
    """

    def __init__(
        self,
        profile: Profile,
        model: str | None = None,
        *,
        channels: int | None = None,
        idn: str | None = None,
    ) -> None:
        self.profile = profile
        self.model = model if model is not None else _default_model(profile)
        self.channels = channels if channels is not None else profile.channels_for(self.model)
        if self.channels < 1:
            raise ValueError("channels must be at least 1")
        if idn is None:
            idn = f"{_default_manufacturer(profile)},{self.model},SIM00001,1.0"
        self.idn = idn
        self.transcript: list[tuple[str, str | None]] = []
        self.beeps = 0
        self.display_on = True
        self.display_message = ""
        self.locked = False
        self.rebooted = False
        self._rng = random.Random(0)
        self._readings = dict(_READINGS)
        self._errors: deque[tuple[int, str]] = deque()
        self._no_error = _empty_queue_reply(profile)
        self._self_test_reply = _self_test_reply(profile)
        self._selected = 1
        self._state = [self._new_channel() for _ in range(self.channels)]
        # Without a master switch, a channel that is on delivers power.
        self._master = self._master_setter() is None
        self._saved: dict[int, str] = {}
        self.scope = FakeScope(profile, self.channels) if profile.category == "scope" else None
        self.fgen = FakeFgen(profile, self.channels) if profile.category == "fgen" else None
        self.daq = self._new_daq()
        if self.daq is not None:
            self.transcript = self.daq.transcript
        self._routes: list[_Route] = []
        self._build_routes()

    def handle_text(self, line: str) -> str | None:
        """`handle` for a command whose reply is text, never a block."""
        reply = self.handle(line)
        if isinstance(reply, bytes):
            raise TypeError(f"{line!r} answered with a block")
        return reply

    def handle(self, line: str) -> str | bytes | None:
        """The reply to one command, without terminators; None when the command has none. A
        waveform, a screenshot or a setup block comes back as bytes."""
        command = line.strip()
        if not command:
            return None
        for route in self._routes:
            match = route.pattern.fullmatch(command)
            if match is not None:
                reply = route.act(match)
                break
        else:
            if self.daq is not None:
                return self.daq.handle(line)
            self._push_error(*_UNDEFINED_HEADER)
            reply = None
        ack = self.profile.link.ack
        if reply is None and ack is not None and not command.endswith("?"):
            # An instrument that acknowledges every command answers a set with its ack word.
            reply = ack
        self.transcript.append(
            (command, f"<block {len(reply)} bytes>" if isinstance(reply, bytes) else reply)
        )
        return reply

    def set_load_ohms(self, ch: int, ohms: float) -> None:
        if not (math.isfinite(ohms) and ohms > 0):
            raise ValueError("load_ohms must be a positive finite number")
        self._channel_state(ch).load_ohms = ohms

    def set_source_volts(self, ch: int, volts: float) -> None:
        if not math.isfinite(volts):
            raise ValueError("source_volts must be a finite number")
        self._channel_state(ch).source_volts = volts

    def trip(self, ch: int, label: str) -> None:
        """Trip a protection on `ch`; the output turns off until it is turned on again."""
        if label not in PROTECTION_LABELS[1:]:
            raise ValueError(f"label must be one of {', '.join(PROTECTION_LABELS[1:])}")
        state = self._channel_state(ch)
        state.trip = label
        state.output = False

    def clear_trip(self, ch: int) -> None:
        self._channel_state(ch).trip = "none"

    def set_reading(self, function: str, value: float) -> None:
        """What a DMM reads while `function` is active."""
        if function not in FUNCTION_LABELS:
            raise ValueError(f"function must be one of {', '.join(FUNCTION_LABELS)}")
        self._readings[function] = value

    def _channel_state(self, ch: int) -> _Channel:
        if not 1 <= ch <= self.channels:
            raise ValueError(f"channel must be from 1 to {self.channels}")
        return self._state[ch - 1]

    def _default_function(self) -> str | None:
        if self.profile.category == "load":
            return "cc"
        if self.profile.category == "dmm":
            dmm = self.profile.dmm
            return next(iter(dmm.functions)) if dmm is not None else "dc_voltage"
        return None

    def _new_daq(self) -> "FakeDaq | None":
        if self.profile.category != "daq":
            return None
        # fake_daq takes helpers from this module, so it cannot be loaded first.
        from .fake_daq import FakeDaq

        return FakeDaq(self.profile, idn=self.idn)

    def _new_channel(self, old: _Channel | None = None) -> _Channel:
        """A channel at its defaults; the load and source around it stay as `old` has them."""
        extras: dict[str, Any] = {
            "ovp_on": False,
            "ocp_on": False,
            "remote_sense": False,
            "short": False,
            **{n: default_label(self.profile, n, label) for n, label in _LABELS.items()},
        }
        state = _Channel(self._default_function(), extras)
        if old is not None:
            state.load_ohms, state.source_volts = old.load_ohms, old.source_volts
        return state

    def _add(
        self,
        template: str,
        act: Callable[[re.Match[str]], str | bytes | None],
        words: Iterable[str] = (),
        functions: Iterable[str] = (),
    ) -> None:
        modulation = self.profile.setters.get("modulation_type")
        spelled = modulation.map.values() if modulation is not None else ()
        self._routes.append(_Route(_pattern(template, words, functions, spelled), act))

    def _build_routes(self) -> None:
        self._routes = []
        # A system command spelled like a link command (unlock as SYST:REM) must reach its state.
        self._system_routes()
        if self.daq is None:
            self._command_routes()

    def _command_routes(self) -> None:
        profile, add = self.profile, self._add
        add(profile.match.idn, lambda _: self.idn)
        if profile.link.remote_check is not None:
            query, expect = profile.link.remote_check
            add(query, lambda _: expect)
        if profile.errors is not None:
            add(profile.errors.query, lambda _: self._pop_error())
            for query in profile.errors.then:
                add(query, lambda _: self._no_error)
        if profile.link.select is not None:
            add(profile.link.select, self._select)
        if profile.match.channels_query is not None:
            add(profile.match.channels_query, lambda _: str(self.channels))
        for template, (key, bound) in _range_queries(profile).items():
            add(template, partial(self._range_reply, key, bound))
        quoted = _quoted_queries(profile)
        for template, entries in self._queries(profile).items():
            add(template, partial(self._query, entries, template in quoted))
        keyed: dict[str, list[Reader]] = {}
        for reader in (*profile.signals, *profile.state):
            if reader.parse == "keyed" and reader.query is not None:
                keyed.setdefault(reader.query, []).append(reader)
        for template, readers in keyed.items():
            add(template, partial(self._keyed_query, readers))
        for setter in profile.setters.values():
            words, functions = self._setter_words(setter)
            for command in setter.commands:
                add(command, partial(self._set, setter), words, functions)
            if setter.off is not None:
                add(setter.off, partial(self._set, setter, off=True), words, functions)
            if setter.verify is not None and _SCOPED.search(setter.verify):
                add(setter.verify, partial(self._verify_reply, setter), (), functions)
        if (master := self._master_setter()) is not None:
            add(master.command, self._set_master)
            add(master.query, lambda _: "1" if self._master else "0")
            add(master.armed, self._armed)
        for command in (
            profile.link.first,
            profile.link.remote,
            *profile.link.local,
            *profile.link.init,
        ):
            if command is not None:
                add(command, lambda _: None)
        if self.scope is not None:
            for template, act in self.scope.routes():
                add(template, act)
        add("*CLS", self._clear_errors)

    def _system_routes(self) -> None:
        system, add = self.profile.system, self._add
        if system.self_test is not None:
            add(system.self_test, lambda _: self._self_test_reply)
        if system.opc is not None:
            add(system.opc, lambda _: "1")
        if system.setup_query is not None:
            add(system.setup_query, lambda _: block(self._snapshot().encode()))
        if system.screenshot:
            add(system.screenshot[-1], lambda _: self._image())
        for command, act in (
            (system.reset, self._reset),
            (system.save, self._save),
            (system.recall, self._recall),
            (system.beep, self._beep),
            (system.display, self._display),
            (system.display_text, self._display_text),
            (system.display_clear, self._display_clear),
            (system.lock, partial(self._lock, True)),
            (system.unlock, partial(self._lock, False)),
            (system.reboot, self._reboot),
        ):
            if command is not None:
                add(command, act)
        for command in (*system.screenshot[:-1], *system.screenshot_cleanup):
            add(command, lambda _: None)
        if system.setup_load is not None:
            pattern = _pattern(system.setup_load).pattern + r"\s*(?P<block>#\d.*)"
            self._routes.append(_Route(re.compile(pattern, re.IGNORECASE), self._load_setup))

    def _setter_words(self, setter: Setter) -> tuple[list[str], list[str]]:
        """The instrument words a setter's `{value}` and `{function}` may spell. A meter's
        range, autorange and integration time name a function by its root word."""
        dmm = self.profile.dmm
        if dmm is not None and setter.name in _METER:
            if "{function}" not in setter.command:
                return [], []
            return [], [*(setter.map or dmm.words).values()]
        return [*setter.map.values()], []

    def _queries(self, profile: Profile) -> dict[str, list[Entry]]:
        queries: dict[str, list[Entry]] = {}
        for readers in (profile.signals, profile.state):
            for reader in readers:
                for flag in reader.flags:
                    queries.setdefault(flag.query, []).append(partial(self._flag_parts, flag))
                if reader.query is not None and reader.parse != "keyed":
                    entry = (
                        partial(self._all_channels, reader)
                        if len(reader.mask) > 1
                        else partial(self._reader_parts, reader)
                    )
                    queries.setdefault(reader.query, []).append(entry)
        if profile.dmm is not None and profile.dmm.function_query is not None:
            queries.setdefault(profile.dmm.function_query, []).append(self._function_parts)
        for setter in profile.setters.values():
            verify = setter.verify
            if verify is not None and verify not in queries and not _SCOPED.search(verify):
                queries[verify] = [partial(self._verify_parts, setter)]
        return queries

    def _channel(self, match: re.Match[str]) -> int | None:
        groups = match.groupdict()
        text = groups.get("ch")
        zero_based = groups.get("ch0")
        if text is None and zero_based is None:
            return self._selected
        ch = int(text) if text is not None else int(zero_based or 0) + 1
        if not 1 <= ch <= self.channels:
            self._push_error(*_OUT_OF_RANGE)
            return None
        return ch

    def _select(self, match: re.Match[str]) -> None:
        ch = self._channel(match)
        if ch is not None:
            self._selected = ch

    def _query(self, entries: list[Entry], quoted: bool, match: re.Match[str]) -> str | None:
        ch = self._channel(match)
        if ch is None:
            return None
        reply = _compose([part for entry in entries for part in entry(ch)])
        return f'"{reply}"' if quoted else reply

    def _keyed_query(self, readers: list[Reader], match: re.Match[str]) -> str | None:
        ch = self._channel(match)
        if ch is None:
            return None
        return keyed_reply(self.profile, match.string, readers, lambda name: self._value(name, ch))

    def _range_reply(self, key: str, bound: str, match: re.Match[str]) -> str | None:
        ch = self._channel(match)
        if ch is None:
            return None
        low, high = self.bounds(key, ch)
        return _number(low if bound == "min" else high)

    def bounds(self, key: str | None, ch: int) -> tuple[float, float]:
        range_ = self.profile.range_for(key, self.model, ch) if key is not None else None
        low = range_.min if range_ is not None and range_.min is not None else 0.0
        if range_ is not None and range_.max is not None:
            return low, range_.max
        return low, _RATINGS.get(key or "", math.inf)

    def _set(self, setter: Setter, match: re.Match[str], off: bool = False) -> None:
        if setter.per_channel:
            ch = self._channel(match)
            if ch is None:
                return
            targets = [ch]
        else:
            targets = list(range(1, self.channels + 1))
        groups = match.groupdict()
        name = setter.name
        if self.scope is not None and name in self.scope.names | set(SCOPE_COMMAND_SETTERS):
            key = self.profile.range_key(setter) if name in VALUE_SETTERS else None
            if key is not None:
                low, high = self.bounds(key, targets[0])
                if not low <= float(groups["value"]) <= high:
                    self._push_error(*_OUT_OF_RANGE)
                    return
            self.scope.set(setter, groups, targets)
            return
        value = float(groups["value"]) * setter.scale if name in VALUE_SETTERS else None
        if value is not None and any(self._refuses(setter, value, ch, groups) for ch in targets):
            self._push_error(*_OUT_OF_RANGE)
            return
        on: bool | None = None
        if name in SWITCH_SETTERS:
            # A switch with its own off command has no {on} or {bit} to read.
            on = _on(groups) if "on" in groups or "bit" in groups else not off
        if self.fgen is not None:
            self.fgen.apply(
                setter,
                targets,
                value=value,
                word=groups.get("value") if value is None and setter.map else None,
                on=on,
                modulation=groups.get("modulation"),
            )
            return
        for ch in targets:
            state = self._state[ch - 1]
            if self.profile.category == "dmm" and name in _METER:
                self._set_meter(setter, ch, groups, value, on)
            elif value is not None:
                state.setpoints[name] = value
            elif name == "output":
                state.output = bool(on)
            elif on is not None:
                state.extras[name] = on
            elif name in LABEL_SETTERS:
                state.extras[name] = _label_for(setter, groups["value"])
            elif name == "function":
                state.function = _label_for(setter, groups["function"])
            elif name == "clear":
                state.trip = "none"

    def _refuses(self, setter: Setter, value: float, ch: int, groups: dict[str, Any]) -> bool:
        """Whether the instrument refuses `value`: a fixed set takes one of its choices, a meter
        what its biggest range covers, anything else what its profile's range spans."""
        if self.profile.category == "dmm" and setter.name == "range":
            ladder = _DMM_RANGES.get(self._meter_function(setter, groups, ch) or "")
            return not 0 <= value <= (ladder or _DMM_DEFAULT_RANGES)[-1]
        key = self.profile.range_key(setter)
        if key is None:
            return False
        range_ = self.profile.range_for(key, self.model, ch)
        if range_ is not None and range_.choices:
            return not any(abs(value - c) <= 1e-9 * max(1.0, abs(c)) for c in range_.choices)
        low, high = self.bounds(key, ch)
        return not low <= value <= high

    def _meter_function(self, setter: Setter, groups: dict[str, Any], ch: int) -> str | None:
        """The function a meter command names by its root word, else the one now selected."""
        word = groups.get("function")
        dmm = self.profile.dmm
        selected = self._state[ch - 1].function
        if word is None or dmm is None:
            return selected
        words = setter.map or dmm.words
        found = next((k for k, v in words.items() if v.casefold() == word.casefold()), None)
        return found or selected

    def _meter(self, state: _Channel, function: str | None) -> dict[str, Any]:
        label = function or "other"
        if label not in state.meter:
            ladder = _DMM_RANGES.get(label, _DMM_DEFAULT_RANGES)
            state.meter[label] = {"range": ladder[len(ladder) // 2], "autorange": True, "nplc": 1.0}
        return state.meter[label]

    def _set_meter(
        self, setter: Setter, ch: int, groups: dict[str, Any], value: float | None, on: bool | None
    ) -> None:
        function = self._meter_function(setter, groups, ch)
        meter = self._meter(self._state[ch - 1], function)
        if setter.name == "autorange":
            meter["autorange"] = bool(on)
        elif value is not None and setter.name == "nplc":
            meter["nplc"] = value
        elif value is not None:
            ladder = _DMM_RANGES.get(function or "", _DMM_DEFAULT_RANGES)
            # A range picked by hand turns autorange off, as it does on a bench meter.
            meter["range"] = next(r for r in ladder if r >= value)
            meter["autorange"] = False

    def _master_setter(self) -> Master | None:
        output = self.profile.setters.get("output")
        return output.master if output is not None else None

    def _set_master(self, match: re.Match[str]) -> None:
        groups = match.groupdict()
        self._master = _on(groups)

    def _armed(self, match: re.Match[str]) -> str | None:
        ch = self._channel(match)
        return None if ch is None else "1" if self._state[ch - 1].output else "0"

    def _live(self, state: _Channel) -> bool:
        return state.output and self._master

    def _reset(self, match: re.Match[str]) -> None:
        self._state = [self._new_channel(old) for old in self._state]
        self._master = self._master_setter() is None
        if self.fgen is not None:
            self.fgen.reset()
        if self.daq is not None:
            self.daq._reset(match)
        if self.scope is not None:
            self.scope = FakeScope(self.profile, self.channels)
            self._build_routes()

    def _slot_number(self, match: re.Match[str]) -> int | None:
        slot = int(match.group("slot"))
        slots = self.profile.system.slots
        if slots is None or not slots[0] <= slot <= slots[1]:
            self._push_error(*_OUT_OF_RANGE)
            return None
        return slot

    def _save(self, match: re.Match[str]) -> None:
        slot = self._slot_number(match)
        if slot is not None:
            self._saved[slot] = self._snapshot()

    def _recall(self, match: re.Match[str]) -> None:
        slot = self._slot_number(match)
        if slot is None:
            return
        if slot not in self._saved:
            self._push_error(*_EMPTY_SLOT)
            return
        self._restore(json.loads(self._saved[slot]))

    def _snapshot(self) -> str:
        """Every channel's settings as JSON text, to keep in a slot or send as a setup block."""
        data: dict[str, Any] = {
            "channels": [dataclasses.asdict(state) for state in self._state],
            "master": self._master,
        }
        if self.fgen is not None:
            data["fgen"] = self.fgen.snapshot()
        if self.scope is not None:
            data["scope"] = scope_state(self.scope)
        if self.daq is not None:
            data["daq"] = daq_state(self.daq)
        return json.dumps(data)

    def _restore(self, data: dict[str, Any]) -> None:
        """Bring back what `_snapshot` kept; the load and source around each channel stay."""
        channels = [_Channel(**saved) for saved in data["channels"]]
        if len(channels) != len(self._state):
            raise ValueError("the setup is for another number of channels")
        for new, old in zip(channels, self._state, strict=True):
            new.load_ohms, new.source_volts, new.trip = old.load_ohms, old.source_volts, old.trip
        self._state = channels
        self._master = bool(data["master"])
        if self.fgen is not None:
            self.fgen.restore(data["fgen"])
        if self.scope is not None:
            restore_scope(self.scope, data["scope"])
        if self.daq is not None:
            restore_daq(self.daq, data["daq"])

    def _load_setup(self, match: re.Match[str]) -> None:
        block = match.group("block")
        try:
            digits = int(block[1])
            payload = block[2 + digits :]
            if len(payload) != int(block[2 : 2 + digits]):
                raise ValueError("the block is not as long as it says")
            self._restore(json.loads(payload))
        except (ValueError, KeyError, TypeError, IndexError):
            self._push_error(*_BAD_VALUE)

    def _beep(self, _: re.Match[str]) -> None:
        self.beeps += 1

    def _display(self, match: re.Match[str]) -> None:
        self.display_on = _on(match.groupdict())

    def _display_text(self, match: re.Match[str]) -> None:
        text = match.group("text")
        limit = self.profile.system.display_text_max
        if limit is not None and len(text) > limit:
            self._push_error(*_TOO_MUCH)
            return
        self.display_message = text

    def _display_clear(self, _: re.Match[str]) -> None:
        self.display_message = ""

    def _lock(self, on: bool, _: re.Match[str]) -> None:
        self.locked = on

    def _reboot(self, _: re.Match[str]) -> None:
        self.rebooted = True

    def _image(self) -> bytes:
        framing = self.profile.system.screenshot_framing
        if framing == "bmp":
            return bmp()
        return block(png()) if framing == "block" else png()

    def _push_error(self, code: int, text: str) -> None:
        if self.daq is not None:
            # The unit answers the error query, so it keeps the queue.
            self.daq._push_error(code, text)
        elif len(self._errors) >= _QUEUE_DEPTH:
            self._errors[-1] = _QUEUE_OVERFLOW
        else:
            self._errors.append((code, text))

    def _pop_error(self) -> str:
        if not self._errors:
            return self._no_error
        code, text = self._errors.popleft()
        return f'{code},"{text}"'

    def _clear_errors(self, _: re.Match[str]) -> None:
        self._errors.clear()

    def _all_channels(self, reader: Reader, _ch: int) -> list[Part]:
        """One status word that packs every channel, for a reader with a mask per channel."""
        return [p for ch in range(1, self.channels + 1) for p in self._reader_parts(reader, ch)]

    def _reader_parts(self, reader: Reader, ch: int) -> list[Part]:
        if reader.parse == "floats":
            return [
                (i, _number(float(self._value(f, ch)) / reader.scale), False)
                for i, f in enumerate(reader.fields)
            ]
        name = reader.fields[0]
        value = self._value(name, ch)
        index = reader.index or 0
        if reader.parse == "bits":
            mask, shift = _for_channel(reader.mask, ch), _for_channel(reader.shift, ch)
            return [(index, _bits(reader.map, mask, shift, name, value), reader.hex)]
        if reader.parse == "bool":
            return [(index, "1" if value else "0", False)]
        if reader.parse == "int":
            number = round(float(value))
            return [(index, format(number, "X") if reader.hex else str(number), False)]
        if reader.parse == "map":
            label = _label(reader.map, name, value)
            return [(index, reader.words.get(label) or label.upper(), False)]
        if reader.parse == "str":
            return [(index, str(value), False)]
        return [(index, _number(float(value) / reader.scale), False)]

    def _flag_parts(self, flag: Flag, ch: int) -> list[Part]:
        tripped = self._state[ch - 1].trip == flag.value
        index = flag.index or 0
        if flag.parse == "bool":
            return [(index, "1" if tripped else "0", False)]
        if not tripped:
            return [(index, 0, flag.hex)]
        mask, shift = _for_channel(flag.mask, ch), _for_channel(flag.shift, ch)
        if not flag.map:
            return [(index, mask, flag.hex)]
        keys = [k for k, v in flag.map.items() if v not in _FALSE_LABELS and k.isdigit()]
        key = next((k for k in keys if flag.map[k] == flag.value), keys[0] if keys else "0")
        return [(index, int(key) << shift, flag.hex)]

    def _function_parts(self, ch: int) -> list[Part]:
        dmm = self.profile.dmm
        assert dmm is not None
        label = self._state[ch - 1].function or "other"
        return [(0, dmm.words.get(label) or label.upper(), False)]

    def _verify_parts(self, setter: Setter, ch: int) -> list[Part]:
        if self.scope is not None and setter.name in self.scope.names:
            value = self.scope.value(setter.name, ch)
            if isinstance(value, bool):
                return [(0, "1" if value else "0", False)]
            if isinstance(value, str):
                return [(0, setter.map.get(value, value), False)]
            return [(0, _number(value), False)]
        return [(0, _readback(setter, self._value(self.profile.verify_field(setter), ch)), False)]

    def _verify_reply(self, setter: Setter, match: re.Match[str]) -> str | None:
        """The readback of a setter whose query names a meter function or a modulation type."""
        ch = self._channel(match)
        if ch is None:
            return None
        groups = match.groupdict()
        function = self._meter_function(setter, groups, ch) if "function" in groups else None
        modulation = self.fgen.label_of(groups.get("modulation")) if self.fgen else None
        field_name = self.profile.verify_field(setter)
        value = self._value(field_name, ch, function=function, modulation=modulation)
        return _readback(setter, value)

    def _value(
        self, name: str, ch: int, *, function: str | None = None, modulation: str | None = None
    ) -> float | bool | str:
        state = self._state[ch - 1]
        if self.fgen is not None and name in self.fgen.names:
            return self.fgen.value(name, ch, modulation)
        if self.scope is not None and name in self.scope.names:
            return self.scope.value(name, ch)
        if self.profile.category == "dmm" and name in _METER:
            return self._meter(state, function or state.function)[name]
        if name in state.extras:
            return state.extras[name]
        if name.endswith("_set"):
            return state.setpoints[name.removesuffix("_set")]
        if name == "output":
            return self._live(state)
        if name == "protection":
            return state.trip
        if name == "function":
            return state.function or "other"
        if name == "mode":
            return self._mode(state)
        if name == "value":
            reading = self._readings.get(state.function or "other", 0.0)
            return reading * (1 + self._rng.gauss(0, 1e-5))
        return self._measure(state)[name]

    def _mode(self, state: _Channel) -> str:
        category = self.profile.category
        if category == "load":
            return state.function or "cc"
        if category != "psu":
            return "unknown"
        if not self._live(state):
            return "off"
        return "cv" if state.setpoints["voltage"] <= self._limit(state) * state.load_ohms else "cc"

    def _limit(self, state: _Channel) -> float:
        return state.setpoints["current"] if "current" in self.profile.setters else math.inf

    def _measure(self, state: _Channel) -> dict[str, float]:
        if self.profile.category == "load":
            return self._sink(state)
        volts = amps = 0.0
        if self._live(state):
            volts = min(state.setpoints["voltage"], self._limit(state) * state.load_ohms)
            amps = volts / state.load_ohms
        return {
            "voltage": volts,
            "current": amps,
            "power": volts * amps,
            "apparent_power": volts * amps,
            "power_factor": 1.0 if amps > 0 else 0.0,
            "frequency": state.setpoints["frequency"] if self._live(state) else 0.0,
        }

    def _sink(self, state: _Channel) -> dict[str, float]:
        volts = state.source_volts
        if state.extras["short"]:
            return {
                "voltage": 0.0,
                "current": volts / state.load_ohms,
                "power": 0.0,
                "resistance": 0.0,
            }
        amps = 0.0
        set_ = state.setpoints
        if state.output:
            if state.function == "cc":
                amps = set_["current"]
            elif state.function == "cr" and set_["resistance"] > 0:
                amps = volts / set_["resistance"]
            elif state.function == "cp" and volts > 0:
                amps = set_["power"] / volts
            elif state.function == "cv":
                held = min(set_["voltage"], volts)
                amps = (volts - held) / state.load_ohms
                volts = held
        return {
            "voltage": volts,
            "current": amps,
            "power": volts * amps,
            "resistance": volts / amps if amps > 0 else _OVERFLOW,
        }


def _pattern(
    template: str,
    words: Iterable[str] = (),
    functions: Iterable[str] = (),
    modulation: Iterable[str] = (),
) -> re.Pattern[str]:
    """The anchored pattern a template's commands match; placeholders become captures. `words`
    are the instrument's spellings of a label, `functions` those of a meter function's root and
    `modulation` those of a modulation type."""
    alternatives = sorted(words, key=len, reverse=True)
    captures = {
        **_CAPTURES,
        "function": _spelled([*alternatives, *functions]),
        "modulation": _spelled([*modulation]),
    }
    if alternatives and "{function}" not in template:
        # A label setter sends the instrument's word where a value setter sends a number.
        captures["value"] = _spelled(alternatives)
    text = template.strip().removeprefix(":")
    out = [":?"]
    seen: set[str] = set()
    position = 0
    for found in _PLACEHOLDER.finditer(text):
        out.append(_literal(text[position : found.start()]))
        name = found.group(1)
        out.append(f"(?P={name})" if name in seen else f"(?P<{name}>{captures[name]})")
        seen.add(name)
        position = found.end()
    out.append(_literal(text[position:]))
    return re.compile("".join(out), re.IGNORECASE)


def _spelled(words: list[str]) -> str:
    """A pattern for any of the words, longest first; one that matches nothing without any."""
    return "|".join(map(re.escape, sorted(words, key=len, reverse=True))) or "(?!)"


def _literal(text: str) -> str:
    return r"\s+".join(re.escape(part) for part in re.split(r"\s+", text))


def _range_queries(profile: Profile) -> dict[str, tuple[str, str]]:
    """Each range query template, with the range key and bound it reports."""
    queries: dict[str, tuple[str, str]] = {}
    ranges = [*profile.ranges.items()]
    ranges += [item for override in profile.range_overrides for item in override.ranges.items()]
    for key, range_ in ranges:
        for bound, template in (("min", range_.min_query), ("max", range_.max_query)):
            if template is not None:
                queries.setdefault(template, (key, bound))
    return queries


def _quoted_queries(profile: Profile) -> set[str]:
    """The queries whose reply is one quoted list of positions, as a generator's `APPL?`."""
    if profile.category != "fgen":
        return set()
    positions: dict[str, set[int]] = {}
    for reader in profile.state:
        if reader.query is not None and reader.index is not None and reader.parse != "keyed":
            positions.setdefault(reader.query, set()).add(reader.index)
    return {query for query, found in positions.items() if len(found) > 1}


def _empty_queue_reply(profile: Profile) -> str:
    """What an empty error queue says, in a form the profile reads as no error."""
    ok = profile.errors.ok if profile.errors is not None else None
    if ok is None or ok.search(_NO_ERROR):
        return _NO_ERROR
    return example(ok) or _NO_ERROR


def _self_test_reply(profile: Profile) -> str:
    """A passing self-test's reply, as the profile's own pattern reads it."""
    ok = profile.system.self_test_ok
    return next((r for r in ("0", "+0") if ok.search(r)), example(ok) or "0")


def _label_for(setter: Setter, word: str) -> str:
    """The label a setter's map gives the instrument's word."""
    return next(k for k, v in setter.map.items() if v.casefold() == word.casefold())


def _readback(setter: Setter, value: float | bool | str) -> str:
    """A setter's readback in the form its query answers: a number in the instrument's unit, a
    label as its word, a switch as 1 or 0."""
    if setter.name in VALUE_SETTERS:
        return _number(float(value) / setter.scale)
    if isinstance(value, str):
        return setter.map.get(value, "")
    return "1" if value else "0"


def _compose(parts: list[Part]) -> str:
    """A comma-separated reply: text first come first served, bits OR-ed per position."""
    tokens: dict[int, str] = {}
    numbers: dict[int, tuple[int, bool]] = {}
    for index, part, hex_ in parts:
        if isinstance(part, str):
            tokens.setdefault(index, part)
        else:
            number, first_hex = numbers.get(index, (0, hex_))
            numbers[index] = (number | part, first_hex)
    for index, (number, hex_) in numbers.items():
        tokens.setdefault(index, format(number, "X") if hex_ else str(number))
    return ",".join(tokens.get(i, "0") for i in range(max(tokens) + 1))


def _number(value: float | bool | str) -> str:
    return f"{float(value):.10g}"


def _label(mapping: dict[str, str], name: str, value: float | bool | str) -> str:
    label = str(value)
    # A supply without an "off" code reports the regulation mode it would hold, as real ones do.
    if name == "mode" and label == "off" and "off" not in mapping.values():
        return "cv"
    return label


def _bits(mapping: dict[str, str], mask: int, shift: int, name: str, value: Any) -> int:
    if not mapping:
        return mask if value else 0
    label = _label(mapping, name, value)
    key = next((k for k, v in mapping.items() if v == label and k.isdigit()), "0")
    return int(key) << shift


def _on(groups: dict[str, Any]) -> bool:
    """A switch command's state, from its {on} or {bit} placeholder."""
    return (groups.get("on") or "").upper() == "ON" or groups.get("bit") == "1"


def _for_channel(values: tuple[int, ...], ch: int) -> int:
    """The mask or shift for a channel: the only entry, or the channel's own."""
    return values[0] if len(values) == 1 else values[min(ch, len(values)) - 1]


def _default_model(profile: Profile) -> str:
    if profile.generic:
        return "SIM"
    model = example(profile.match.model)
    if model is None:
        raise ValueError(f"{profile.id}: cannot spell a model match.model accepts; pass model=")
    return model


def _default_manufacturer(profile: Profile) -> str:
    if profile.generic:
        return "Simulated"
    manufacturer = example(profile.match.manufacturer)
    if manufacturer is None:
        raise ValueError(f"{profile.id}: cannot spell a match.manufacturer; pass idn=")
    return manufacturer
