"""A fake instrument that answers a profile's own commands from a simple physical model.

Each command template of the profile becomes an anchored, case-insensitive pattern, and each reply
is written in the form the profile's parser reads back, so the engine cannot tell the fake from
the instrument the profile describes.
"""

import importlib
import math
import random
import re
import struct
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from ..profile import (
    FUNCTION_LABELS,
    PROTECTION_LABELS,
    VALUE_SETTERS,
    Flag,
    Profile,
    Reader,
    Setter,
)

# re._parser is the stdlib's regex parser; it ships without type stubs.
_regex_parser: Any = importlib.import_module("re._parser")

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")
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
}
_NO_ERROR = '0,"No error"'
_UNDEFINED_HEADER = (-113, "Undefined header")
_OUT_OF_RANGE = (-222, "Data out of range")
_QUEUE_OVERFLOW = (-350, "Queue overflow")
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
_FALSE_LABELS = ("none", "false", "0")
_CLASS_CHARS = {"CATEGORY_DIGIT": "0", "CATEGORY_WORD": "A", "CATEGORY_SPACE": " "}

# One piece of a reply: its comma-separated position, then text, or bits to OR, and hex format.
Part = tuple[int, str | int, bool]
Entry = Callable[[int], list[Part]]


@dataclass(slots=True)
class _Channel:
    function: str | None
    setpoints: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(sorted(VALUE_SETTERS), 0.0)
    )
    output: bool = False
    trip: str = "none"
    load_ohms: float = 10.0
    source_volts: float = 12.0
    # The newer state fields: protection enables, sense, tracking, a load's range, a meter's
    # range, auto-range and integration time.
    extras: dict[str, Any] = field(
        default_factory=lambda: {
            "ovp_on": False,
            "ocp_on": False,
            "remote_sense": False,
            "tracking": "independent",
            "current_range": "high",
            "range": 10.0,
            "autorange": True,
            "nplc": 1.0,
        }
    )


@dataclass(frozen=True, slots=True)
class _Route:
    pattern: re.Pattern[str]
    act: Callable[[re.Match[str]], str | bytes | None]


class FakeInstrument:
    """One instrument of a profile's family, answering the commands the profile sends.

    A psu or AC source drives `load_ohms` up to its current setpoint. A load sinks from a source
    of `source_volts`; in cv mode `load_ohms` is that source's series resistance. A DMM reads a
    settable number per function, with a little noise. A protection trip turns the output off.
    Not thread-safe: FakeServer serializes its clients.
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
        self._rng = random.Random(0)
        self._readings = dict(_READINGS)
        self._errors: deque[tuple[int, str]] = deque()
        self._selected = 1
        self._state = [_Channel(self._default_function()) for _ in range(self.channels)]
        self.scope = FakeScope(profile, self.channels) if profile.category == "scope" else None
        self._routes = self._routes_for(profile)

    def handle_text(self, line: str) -> str | None:
        """`handle` for a command whose reply is text, never a block."""
        reply = self.handle(line)
        if isinstance(reply, bytes):
            raise TypeError(f"{line!r} answered with a block")
        return reply

    def handle(self, line: str) -> str | bytes | None:
        """The reply to one command, without terminators; None when the command has none. A
        waveform block comes back as bytes."""
        command = line.strip()
        if not command:
            return None
        for route in self._routes:
            match = route.pattern.fullmatch(command)
            if match is not None:
                reply = route.act(match)
                break
        else:
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

    def _routes_for(self, profile: Profile) -> list[_Route]:
        routes: list[_Route] = []

        def add(
            template: str,
            act: Callable[[re.Match[str]], str | bytes | None],
            words: Iterable[str] = (),
        ) -> None:
            routes.append(_Route(_pattern(template, words), act))

        add(profile.match.idn, lambda _: self.idn)
        if profile.errors is not None:
            add(profile.errors.query, lambda _: self._pop_error())
        if profile.link.select is not None:
            add(profile.link.select, self._select)
        if profile.match.channels_query is not None:
            add(profile.match.channels_query, lambda _: str(self.channels))
        for template, (key, bound) in _range_queries(profile).items():
            add(template, partial(self._range_reply, key, bound))
        for template, entries in self._queries(profile).items():
            add(template, partial(self._query, entries))
        for setter in profile.setters.values():
            for command in setter.commands:
                add(command, partial(self._set, setter), setter.map.values())
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
        add("*OPC?", lambda _: "1")
        add("*CLS", self._clear_errors)
        add("*RST", self._reset)
        return routes

    def _queries(self, profile: Profile) -> dict[str, list[Entry]]:
        queries: dict[str, list[Entry]] = {}
        for readers in (profile.signals, profile.state):
            for reader in readers:
                for flag in reader.flags:
                    queries.setdefault(flag.query, []).append(partial(self._flag_parts, flag))
                if reader.query is not None:
                    entry = (
                        partial(self._all_channels, reader)
                        if len(reader.mask) > 1
                        else partial(self._reader_parts, reader)
                    )
                    queries.setdefault(reader.query, []).append(entry)
        if profile.dmm is not None and profile.dmm.function_query is not None:
            queries.setdefault(profile.dmm.function_query, []).append(self._function_parts)
        for setter in profile.setters.values():
            if setter.verify is not None and setter.verify not in queries:
                queries[setter.verify] = [partial(self._verify_parts, setter)]
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

    def _query(self, entries: list[Entry], match: re.Match[str]) -> str | None:
        ch = self._channel(match)
        if ch is None:
            return None
        return _compose([part for entry in entries for part in entry(ch)])

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

    def _set(self, setter: Setter, match: re.Match[str]) -> None:
        if setter.per_channel:
            ch = self._channel(match)
            if ch is None:
                return
            targets = [ch]
        else:
            targets = list(range(1, self.channels + 1))
        groups = match.groupdict()
        if self.scope is not None and setter.name in self.scope.names | {
            "run",
            "stop",
            "single",
            "autoset",
        }:
            key = self.profile.range_key(setter) if setter.name in VALUE_SETTERS else None
            if key is not None:
                low, high = self.bounds(key, targets[0])
                if not low <= float(groups["value"]) <= high:
                    self._push_error(*_OUT_OF_RANGE)
                    return
            self.scope.set(setter, groups, targets)
        elif setter.name in VALUE_SETTERS:
            value = float(groups["value"]) * setter.scale
            key = self.profile.range_key(setter)
            for ch in targets:
                low, high = self.bounds(key, ch)
                if not low <= value <= high:
                    self._push_error(*_OUT_OF_RANGE)
                    return
            for ch in targets:
                self._state[ch - 1].setpoints[setter.name] = value
        elif setter.name == "output":
            on = (groups.get("on") or "").upper() == "ON" or groups.get("bit") == "1"
            for ch in targets:
                self._state[ch - 1].output = on
        elif setter.name in ("ovp_on", "ocp_on"):
            on = (groups.get("on") or "").upper() == "ON" or groups.get("bit") == "1"
            for ch in targets:
                self._state[ch - 1].extras[setter.name] = on
        elif setter.name == "function":
            word = groups["function"].casefold()
            label = next(k for k, v in setter.map.items() if v.casefold() == word)
            for ch in targets:
                self._state[ch - 1].function = label
        elif setter.name == "clear":
            for ch in targets:
                self._state[ch - 1].trip = "none"

    def _reset(self, _: re.Match[str]) -> None:
        for state in self._state:
            state.setpoints = dict.fromkeys(state.setpoints, 0.0)
            state.output = False

    def _push_error(self, code: int, text: str) -> None:
        if len(self._errors) >= _QUEUE_DEPTH:
            self._errors[-1] = _QUEUE_OVERFLOW
        else:
            self._errors.append((code, text))

    def _pop_error(self) -> str:
        if not self._errors:
            return _NO_ERROR
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
        state = self._state[ch - 1]
        if self.scope is not None and setter.name in self.scope.names:
            value = self.scope.value(setter.name, ch)
            if isinstance(value, bool):
                return [(0, "1" if value else "0", False)]
            if isinstance(value, str):
                return [(0, setter.map.get(value, value), False)]
            return [(0, _number(value), False)]
        if setter.name in VALUE_SETTERS:
            return [(0, _number(state.setpoints[setter.name] / setter.scale), False)]
        if setter.name == "output":
            return [(0, "1" if state.output else "0", False)]
        if setter.name in state.extras:
            return [(0, "1" if state.extras[setter.name] else "0", False)]
        return [(0, setter.map.get(state.function or "", ""), False)]

    def _value(self, name: str, ch: int) -> float | bool | str:
        state = self._state[ch - 1]
        if self.scope is not None and name in self.scope.names:
            return self.scope.value(name, ch)
        if name in state.extras:
            return state.extras[name]
        if name.endswith("_set"):
            return state.setpoints[name.removesuffix("_set")]
        if name == "output":
            return state.output
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
        if not state.output:
            return "off"
        return "cv" if state.setpoints["voltage"] <= self._limit(state) * state.load_ohms else "cc"

    def _limit(self, state: _Channel) -> float:
        return state.setpoints["current"] if "current" in self.profile.setters else math.inf

    def _measure(self, state: _Channel) -> dict[str, float]:
        if self.profile.category == "load":
            return self._sink(state)
        volts = amps = 0.0
        if state.output:
            volts = min(state.setpoints["voltage"], self._limit(state) * state.load_ohms)
            amps = volts / state.load_ohms
        return {
            "voltage": volts,
            "current": amps,
            "power": volts * amps,
            "apparent_power": volts * amps,
            "power_factor": 1.0 if amps > 0 else 0.0,
            "frequency": state.setpoints["frequency"] if state.output else 0.0,
        }

    def _sink(self, state: _Channel) -> dict[str, float]:
        volts = state.source_volts
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


class FakeScope:
    """A scope's settings, measurement slots and a synthetic waveform on every channel.

    Every channel sees the same 1 kHz, 2 V peak-to-peak sine about 0.5 V; the measurements are
    computed from it, so a transcript and a capture agree.
    """

    names = frozenset(
        {
            "visible",
            "scale",
            "offset",
            "coupling",
            "bandwidth_limit",
            "probe",
            "timebase",
            "delay",
            "sample_rate",
            "memory_depth",
            "points",
            "acquisitions",
            "trigger_status",
            "trigger_mode",
            "trigger_source",
            "trigger_level",
            "trigger_slope",
            "trigger_frequency",
        }
    )

    def __init__(self, profile: Profile, channels: int) -> None:
        self.profile = profile
        self.channels = channels
        self.per_channel: dict[int, dict[str, Any]] = {
            ch: {
                "visible": True,
                "scale": 0.5,
                "offset": 0.0,
                "coupling": "dc",
                "bandwidth_limit": "full",
                "probe": 1.0,
            }
            for ch in range(1, channels + 1)
        }
        self.shared: dict[str, Any] = {
            "timebase": 0.0005,
            "delay": 0.0,
            "sample_rate": 2e6,
            "memory_depth": 10_000.0,
            "points": 10_000.0,
            "acquisitions": 0,
            "trigger_status": "auto",
            "trigger_mode": "auto",
            "trigger_source": "ch1",
            "trigger_level": 0.5,
            "trigger_slope": "rising",
            "trigger_frequency": 1000.0,
        }
        scope = profile.scope
        count = scope.slots.count if scope is not None and scope.slots is not None else 0
        # slot -> (on, type word, source text)
        self.slots: dict[int, tuple[bool, str, str]] = dict.fromkeys(
            range(1, count + 1), (False, "", "C1")
        )
        if count:
            self.slots[1] = (True, "PKPK", "C1")
        self.mode_ready = True
        self.waveform_source = 1
        self.waveform_points = 0
        self.waveform_interval = 1
        self.running = True

    # 1 kHz sine, 2 Vpp about 0.5 V.
    amplitude = 1.0
    mid = 0.5
    frequency = 1000.0

    def value(self, name: str, ch: int) -> float | bool | str:
        if name in self.per_channel[ch]:
            return self.per_channel[ch][name]
        if name == "acquisitions":
            self.shared["acquisitions"] += 1
        return self.shared[name]

    def set(self, setter: Setter, groups: dict[str, str | None], targets: list[int]) -> None:
        name = setter.name
        if name in ("run", "stop", "single", "autoset"):
            self.running = name != "stop"
            self.shared["trigger_status"] = "stopped" if name == "stop" else "auto"
            if name == "single":
                self.shared["trigger_mode"] = "single"
            return
        if name == "visible":
            on = (groups.get("on") or "").upper() == "ON" or groups.get("bit") == "1"
            for ch in targets:
                self.per_channel[ch]["visible"] = on
            return
        raw = groups.get("value")
        if raw is None:
            return
        if setter.map:
            label = next((k for k, v in setter.map.items() if v.casefold() == raw.casefold()), None)
            if label is None:
                return
            value: Any = label
        else:
            value = float(raw)
        if name in self.shared:
            self.shared[name] = value
        else:
            for ch in targets:
                self.per_channel[ch][name] = value

    def measurement(self, word: str, source: str) -> float | None:
        """What the slot shows for the synthetic sine, by the profile's measurement field."""
        scope = self.profile.scope
        assert scope is not None and scope.slots is not None
        field_name = scope.slots.types.get(word.casefold())
        if field_name is None or not re.fullmatch(r"C\d+", source):
            return None
        a, mid, f = self.amplitude, self.mid, self.frequency
        table: dict[str, float] = {
            "pkpk": 2 * a,
            "maximum": mid + a,
            "minimum": mid - a,
            "amplitude": 2 * a,
            "top": mid + a,
            "base": mid - a,
            "mean": mid,
            "cycle_mean": mid,
            "rms": math.sqrt(mid**2 + a**2 / 2),
            "cycle_rms": math.sqrt(mid**2 + a**2 / 2),
            "stdev": a / math.sqrt(2),
            "overshoot": 0.0,
            "preshoot": 0.0,
            "period": 1 / f,
            "frequency": f,
            "positive_width": 0.5 / f,
            "negative_width": 0.5 / f,
            "duty": 50.0,
            "rise_time": 0.8 / (math.pi * f),
            "fall_time": 0.8 / (math.pi * f),
        }
        return table[field_name]

    def routes(self) -> list[tuple[str, Callable[[re.Match[str]], str | bytes | None]]]:
        scope = self.profile.scope
        assert scope is not None
        out: list[tuple[str, Callable[[re.Match[str]], str | bytes | None]]] = []
        slots = scope.slots
        if slots is not None:
            out.append((slots.enabled, lambda m: "ON" if self.slots[int(m["slot"])][0] else "OFF"))
            out.append((slots.type, lambda m: self.slots[int(m["slot"])][1]))
            out.append((slots.source, lambda m: self.slots[int(m["slot"])][2]))
            out.append((slots.value, self._slot_value))
            if slots.ready_query is not None:
                out.append((slots.ready_query, lambda _: str(slots.ready_value)))
        measure = scope.measure
        if measure is not None:
            for command in measure.commands:
                out.append((command, self._measure_command))
            if measure.off is not None:
                out.append((measure.off, self._slot_off))
        wf = scope.waveform
        if wf is not None:
            for command in wf.setup:
                out.append((command, self._waveform_setup))
            out.append((wf.preamble, lambda _: _block(self._wavedesc())))
            out.append((wf.data, lambda _: _block(self._wave_data()) + b"\n"))
            if wf.max_points is not None:
                out.append((wf.max_points, lambda _: "5000000"))
        return out

    def _slot_value(self, m: re.Match[str]) -> str | None:
        on, word, source = self.slots[int(m["slot"])]
        if not on:
            return None
        value = self.measurement(word, source)
        return _number(_OVERFLOW if value is None else value)

    def _measure_command(self, m: re.Match[str]) -> None:
        groups = m.groupdict()
        slot = groups.get("slot")
        if slot is None:
            return
        on, word, source = self.slots[int(slot)]
        if groups.get("type") is not None:
            word = str(groups["type"]).upper()
        if groups.get("ch") is not None:
            source = f"C{groups['ch']}"
        if "ON" in m.group(0).upper().split():
            on = True
        self.slots[int(slot)] = (on, word, source)

    def _slot_off(self, m: re.Match[str]) -> None:
        on, word, source = self.slots[int(m["slot"])]
        self.slots[int(m["slot"])] = (False, word, source)

    def _waveform_setup(self, m: re.Match[str]) -> None:
        groups = m.groupdict()
        if groups.get("ch") is not None:
            self.waveform_source = int(groups["ch"] or 1)
        if groups.get("points") is not None:
            self.waveform_points = int(groups["points"] or 0)
        if groups.get("interval") is not None:
            self.waveform_interval = max(1, int(groups["interval"] or 1))

    # 12-bit codes left-aligned in 16-bit words, as the SDS800X HD sends them.
    code_per_div = 7680.0

    def _count(self) -> int:
        total = int(self.shared["points"])
        step = self.waveform_interval
        available = max(1, total // step)
        return min(available, self.waveform_points) if self.waveform_points else available

    def _wavedesc(self) -> bytes:
        ch = self.waveform_source
        settings = self.per_channel[ch]
        desc = bytearray(346)
        desc[0:8] = b"WAVEDESC"
        desc[16:23] = b"WAVEACE"
        struct.pack_into("<h", desc, 32, 1)
        struct.pack_into("<h", desc, 34, 0)
        struct.pack_into("<i", desc, 36, 346)
        count = self._count()
        struct.pack_into("<i", desc, 60, count * 2)
        struct.pack_into("<i", desc, 116, count)
        struct.pack_into("<i", desc, 132, 0)
        struct.pack_into("<i", desc, 136, self.waveform_interval)
        probe = float(settings["probe"])
        struct.pack_into("<f", desc, 156, float(settings["scale"]) / probe)
        struct.pack_into("<f", desc, 160, float(settings["offset"]) / probe)
        struct.pack_into("<f", desc, 164, self.code_per_div)
        struct.pack_into("<h", desc, 172, 16)
        struct.pack_into("<f", desc, 176, 1 / float(self.shared["sample_rate"]))
        struct.pack_into("<d", desc, 180, float(self.shared["delay"]))
        struct.pack_into("<f", desc, 328, probe)
        return bytes(desc)

    def _wave_data(self) -> bytes:
        ch = self.waveform_source
        settings = self.per_channel[ch]
        scale = float(settings["scale"]) / float(settings["probe"])
        offset = float(settings["offset"]) / float(settings["probe"])
        count = self._count()
        pitch = self.waveform_interval / float(self.shared["sample_rate"])
        start = float(self.shared["delay"]) - float(self.shared["timebase"]) * 5
        gain = self.code_per_div / scale
        codes = []
        for i in range(count):
            t = start + i * pitch
            volts = (
                self.mid + self.amplitude * math.sin(2 * math.pi * self.frequency * t)
            ) / float(settings["probe"])
            code = round((volts + offset) * gain)
            codes.append(max(-32768, min(32767, code)))
        return struct.pack(f"<{count}h", *codes)


def _block(payload: bytes) -> bytes:
    """An IEEE 488.2 definite-length block."""
    length = str(len(payload))
    return f"#{len(length)}{length}".encode() + payload


def _pattern(template: str, words: Iterable[str] = ()) -> re.Pattern[str]:
    """The anchored pattern a template's commands match; placeholders become captures."""
    alternatives = sorted(words, key=len, reverse=True)
    spelled = "|".join(map(re.escape, alternatives)) or "(?!)"
    captures = {**_CAPTURES, "function": spelled}
    if alternatives and "{function}" not in template:
        # A label setter sends the instrument's word where a value setter sends a number.
        captures["value"] = spelled
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


def _fold(key: str) -> str:
    return key.strip().strip('"').casefold()


def _for_channel(values: tuple[int, ...], ch: int) -> int:
    """The mask or shift for a channel: the only entry, or the channel's own."""
    return values[0] if len(values) == 1 else values[min(ch, len(values)) - 1]


def _default_model(profile: Profile) -> str:
    if profile.generic:
        return "SIM"
    model = _example(profile.match.model)
    if model is None:
        raise ValueError(f"{profile.id}: cannot spell a model match.model accepts; pass model=")
    return model


def _default_manufacturer(profile: Profile) -> str:
    if profile.generic:
        return "Simulated"
    manufacturer = _example(profile.match.manufacturer)
    if manufacturer is None:
        raise ValueError(f"{profile.id}: cannot spell a match.manufacturer; pass idn=")
    return manufacturer


def _example(pattern: re.Pattern[str]) -> str | None:
    """The text the pattern's first alternatives and fewest repeats spell, if it matches."""
    try:
        text = _spell(_regex_parser.parse(pattern.pattern, pattern.flags))
    except (ValueError, IndexError, OverflowError):
        return None
    return text if pattern.search(text) else None


def _spell(items: Any) -> str:
    out: list[str] = []
    for op, av in items:
        name = op.name
        if name == "LITERAL":
            out.append(chr(av))
        elif name == "ANY":
            out.append("A")
        elif name == "IN":
            out.append(_spell_class(av))
        elif name == "BRANCH":
            out.append(_spell(av[1][0]))
        elif name == "SUBPATTERN":
            out.append(_spell(av[3]))
        elif name == "ATOMIC_GROUP":
            out.append(_spell(av))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            out.append(_spell(av[2]) * av[0])
        elif name != "AT":
            raise ValueError(f"cannot spell {name}")
    return "".join(out)


def _spell_class(items: Any) -> str:
    op, av = items[0]
    if op.name == "LITERAL":
        return chr(av)
    if op.name == "RANGE":
        return chr(av[0])
    if op.name == "CATEGORY" and av.name in _CLASS_CHARS:
        return _CLASS_CHARS[av.name]
    raise ValueError(f"cannot spell {op.name}")
