"""The oscilloscope part of the fake instrument: settings, measurements and a synthetic waveform,
answered in the profile's own formats."""

import math
import re
import struct
from collections.abc import Callable
from functools import partial
from typing import Any

import numpy as np

from .. import engine
from ..profile import Profile, Setter
from ..profile_scope import (
    ACQUISITION_FIELDS,
    CHANNEL_STATE_FIELDS,
    LINEAR_TERMS,
    MEASUREMENT_FIELDS,
    SCOPE_COMMAND_SETTERS,
    Linear,
    Measure,
    Part,
)

# SCPI's overflow reading, which the engine reads as not-a-number.
_OVERFLOW = 9.9e37

_SLOT_GROUP = re.compile(r"\(\?P<slot>[^)]*\)")

Route = tuple[str, Callable[[re.Match[str]], str | bytes | None]]


class FakeScope:
    """A scope's settings, measurement slots and a synthetic waveform on every channel.

    Every channel sees the same 1 kHz, 2 V peak-to-peak sine about 0.5 V; the measurements are
    computed from it, so a transcript and a capture agree. A linear waveform's scaling terms are
    worked out from the profile: a term the profile reads from a state query takes the setting
    that query reports, and the scope picks the rest.
    """

    names = frozenset(CHANNEL_STATE_FIELDS) | frozenset(ACQUISITION_FIELDS)

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
            "acquisition_mode": "normal",
            "averages": 16.0,
            "trigger_type": "edge",
            "pulse_width": 1e-6,
            "pulse_condition": "greater",
        }
        scope = profile.scope
        slots = scope.slots if scope is not None else None
        self._source_format = _source_format(scope.measure) if scope is not None else "C{ch}"
        # slot -> (on, type word, source text); a listed slot exists only while it is on.
        self.slots: dict[int, tuple[bool, str, str]] = {}
        if slots is not None and slots.list_query is None:
            self.slots = dict.fromkeys(range(1, slots.count + 1), (False, "", self._source(1)))
        if slots is not None:
            word = slots.words.get("pkpk") or next(iter(slots.words), "")
            self.slots[1] = (True, word.upper(), self._source(1))
        self.waveform_source = 1
        self.waveform_points = 0
        self.waveform_interval = 1
        waveform = scope.waveform if scope is not None else None
        setup = waveform.setup if waveform is not None else ()
        self._paced = any("{interval}" in command for command in setup)
        self._skip = waveform.skip if waveform is not None else 0
        self.running = True
        self._read_by: dict[str, tuple[str, float]] = {}
        for reader in (*profile.signals, *profile.state):
            if reader.query is not None and reader.fields[0] in self.names:
                self._read_by.setdefault(_key(reader.query), (reader.fields[0], reader.scale))

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
        if name == "force":
            self.shared["trigger_status"] = "triggered"
            return
        if name in SCOPE_COMMAND_SETTERS:
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

    def measurement(self, field_name: str) -> float:
        """A measurement field of the synthetic sine."""
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

    def routes(self) -> list[Route]:
        scope = self.profile.scope
        assert scope is not None
        out: list[Route] = []
        slots = scope.slots
        if slots is not None:
            if slots.enabled is not None:
                out.append(
                    (slots.enabled, lambda m: "ON" if self.slots[int(m["slot"])][0] else "OFF")
                )
            if slots.list_query is not None:
                out.append((slots.list_query, self._slot_list))
            out.append((slots.type, lambda m: self._slot(m)[1]))
            out.append((slots.source, lambda m: self._slot(m)[2]))
            out.append((slots.value, self._slot_value))
            if slots.ready_query is not None:
                out.append((slots.ready_query, lambda _: str(slots.ready_value)))
        measure = scope.measure
        if measure is not None and measure.query is not None:
            for field_name, word in measure.words.items():
                template = measure.query.replace("{type}", word)
                out.append((template, partial(self._direct_value, measure, field_name, word)))
        elif measure is not None:
            for command in measure.commands:
                out.append((command, self._measure_command))
            if measure.off is not None:
                out.append((measure.off, self._slot_off))
        wf = scope.waveform
        if wf is not None:
            for command in wf.setup:
                out.append((command, self._waveform_setup))
            if wf.linear is not None:
                out.extend(self._linear_routes(wf.preamble, wf.data, wf.linear))
            else:
                assert wf.preamble is not None
                out.append((wf.preamble, lambda _: _block(self._wavedesc())))
                out.append((wf.data, lambda _: _block(self._wave_data()) + b"\n"))
            if wf.max_points is not None:
                out.append((wf.max_points, lambda _: "5000000"))
        return out

    def _source(self, ch: int) -> str:
        return self._source_format.replace("{ch}", str(ch))

    def _slot(self, m: re.Match[str]) -> tuple[bool, str, str]:
        """A slot's state. A scope that lists its measurements creates one when it is first
        addressed, as a Tektronix does."""
        number = int(m["slot"])
        scope = self.profile.scope
        assert scope is not None and scope.slots is not None
        if number not in self.slots and scope.slots.list_query is not None:
            self.slots[number] = (
                True,
                scope.slots.words.get("period", "").upper(),
                self._source(1),
            )
        return self.slots[number]

    def _slot_list(self, m: re.Match[str]) -> str:
        scope = self.profile.scope
        assert scope is not None and scope.slots is not None
        assert scope.slots.list_pattern is not None
        if not self.slots:
            return "NONE"
        # The pattern is a literal around the slot number's group, which is filled in.
        shape = _SLOT_GROUP.sub("{}", scope.slots.list_pattern.pattern)
        return ",".join(shape.format(n) for n in sorted(self.slots))

    def _slot_value(self, m: re.Match[str]) -> str | None:
        on, word, source = self._slot(m)
        if not on:
            return None
        scope = self.profile.scope
        assert scope is not None and scope.slots is not None
        field_name = scope.slots.types.get(word.casefold())
        if field_name is None or scope.slots.source_pattern.search(source) is None:
            return engine.format_value(_OVERFLOW, None)
        return engine.format_value(self.measurement(field_name), None)

    def _direct_value(self, measure: Measure, field_name: str, word: str, m: re.Match[str]) -> str:
        number = engine.format_value(self.measurement(field_name), None)
        if measure.index is None:
            return number
        # Items before the value repeat the type word, as `PKPK,2.0V` does.
        return ",".join([word] * measure.index + [number + (MEASUREMENT_FIELDS[field_name] or "")])

    def _measure_command(self, m: re.Match[str]) -> None:
        groups = m.groupdict()
        slot = groups.get("slot")
        if slot is None:
            return
        on, word, source = self._slot(m)
        if groups.get("type") is not None:
            word = str(groups["type"]).upper()
        if groups.get("ch") is not None:
            source = self._source(int(groups["ch"]))
        if "ON" in m.group(0).upper().split():
            on = True
        self.slots[int(slot)] = (on, word, source)

    def _slot_off(self, m: re.Match[str]) -> None:
        number = int(m["slot"])
        scope = self.profile.scope
        assert scope is not None and scope.slots is not None
        if scope.slots.list_query is not None:
            self.slots.pop(number, None)
            return
        on, word, source = self.slots[number]
        self.slots[number] = (False, word, source)

    def _waveform_setup(self, m: re.Match[str]) -> None:
        groups = m.groupdict()
        if groups.get("ch") is not None:
            self.waveform_source = int(groups["ch"] or 1)
        if groups.get("points") is not None:
            self.waveform_points = int(groups["points"] or 0)
        if groups.get("interval") is not None:
            self.waveform_interval = max(1, int(groups["interval"] or 1))
        elif groups.get("points") and not self._paced:
            # A scope that takes only a point count spreads them over its record itself.
            whole = int(self.shared["points"])
            self.waveform_interval = max(1, whole // self.waveform_points)

    # 12-bit codes left-aligned in 16-bit words, as the SDS800X HD sends them.
    code_per_div = 7680.0

    def _count(self) -> int:
        total = int(self.shared["points"])
        step = self.waveform_interval
        available = max(1, total // step)
        return min(available, self.waveform_points) if self.waveform_points else available

    def _volts(self, t: float) -> float:
        """The sine at `t` seconds from the trigger, at the probe tip."""
        return self.mid + self.amplitude * math.sin(2 * math.pi * self.frequency * t)

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
            volts = self._volts(start + i * pitch) / float(settings["probe"])
            code = round((volts + offset) * gain)
            codes.append(max(-32768, min(32767, code)))
        return struct.pack(f"<{count}h", *codes)

    def _linear_routes(self, preamble: str | None, data: str, linear: Linear) -> list[Route]:
        out: list[Route] = []
        if preamble is not None:
            out.append((preamble, partial(self._preamble_line, linear)))
        queries = {
            part.query
            for parts in linear.terms().values()
            for part in parts
            if part.query is not None and _key(part.query) not in self._read_by
        }
        for query in sorted(queries):
            out.append((query, partial(self._term_reply, linear, query)))
        out.append((data, partial(self._linear_data, linear)))
        return out

    def _preamble_line(self, linear: Linear, m: re.Match[str]) -> str:
        self._waveform_setup(m)
        _, fields, _ = self._linear_scale(linear, self.waveform_source)
        items = ["0"] * (max(fields, default=0) + 1)
        for index, raw in fields.items():
            items[index] = engine.format_value(raw, None)
        return ",".join(items)

    def _term_reply(self, linear: Linear, query: str, m: re.Match[str]) -> str:
        ch = int(m["ch"]) if m.groupdict().get("ch") is not None else self.waveform_source
        _, _, replies = self._linear_scale(linear, ch)
        return engine.format_value(replies.get(query, 0.0), None)

    def _linear_data(self, linear: Linear, m: re.Match[str]) -> bytes:
        self._waveform_setup(m)
        ch = self.waveform_source
        terms, _, _ = self._linear_scale(linear, ch)
        count = self._count()
        times = (np.arange(count) - terms["x_ref"]) * terms["x_inc"] + terms["x_zero"]
        volts = np.array([self._volts(float(t)) for t in times])
        dtype = np.dtype(f"{'>' if linear.byte_order == 'big' else '<'}{linear.encoding}")
        info = np.iinfo(dtype)
        codes = np.rint((volts - terms["y_zero"]) / terms["y_mult"] + terms["y_ref"])
        data = np.clip(codes, info.min, info.max).astype(dtype).tobytes()
        return b"D" * self._skip + _block(data) + b"\n"

    def _linear_scale(
        self, linear: Linear, ch: int
    ) -> tuple[dict[str, float], dict[int, float], dict[str, float]]:
        """The six terms as the profile will read them, and the preamble fields and query replies
        that make them so: a term read from a state query takes that setting, and the first part
        the scope answers itself takes up the rest of the term's chosen value."""
        settings = self.per_channel[ch]
        bits = 8 * int(linear.encoding[1])
        signed = linear.encoding[0] == "i"
        chosen = {
            "x_inc": self.waveform_interval / float(self.shared["sample_rate"]),
            "x_zero": float(self.shared["delay"])
            - float(self.shared["timebase"]) * self._grid() / 2,
            "x_ref": 0.0,
            # The code range spans ten divisions.
            "y_mult": float(settings["scale"]) * 10 / 2**bits,
            "y_zero": -float(settings["offset"]),
            "y_ref": 0.0 if signed else float(2 ** (bits - 1)),
        }
        terms: dict[str, float] = {}
        fields: dict[int, float] = {}
        replies: dict[str, float] = {}
        for name in LINEAR_TERMS:
            parts = linear.terms()[name]
            fixed = 0.0
            free: list[Part] = []
            for part in parts:
                read = self._read_by.get(_key(part.query)) if part.query is not None else None
                if part.value is not None:
                    fixed += part.apply(part.value, self.waveform_interval)
                elif read is not None:
                    field_name, scale = read
                    number = float(self.value(field_name, ch)) / scale
                    fixed += part.apply(number, self.waveform_interval)
                else:
                    free.append(part)
            total = fixed
            for i, part in enumerate(free):
                share = chosen[name] - fixed if i == 0 else 0.0
                raw = _unapply(part, share, self.waveform_interval)
                total += part.apply(raw, self.waveform_interval)
                if part.field is not None:
                    fields[part.field] = raw
                elif part.query is not None:
                    replies[part.query] = raw
            terms[name] = total
        return terms, fields, replies

    def _grid(self) -> int:
        scope = self.profile.scope
        return scope.grid if scope is not None else 10


def _unapply(part: Part, share: float, interval: int) -> float:
    """The raw number `part.apply` turns into `share`."""
    if part.times_interval:
        share /= interval
    if part.negate:
        share = -share
    share = share * part.divide / part.multiply if part.multiply else 0.0
    if part.invert:
        return 1 / share if share else 0.0
    return share


def _source_format(measure: Measure | None) -> str:
    """How the scope spells a channel as a source: the word the measure commands end with."""
    for command in measure.commands if measure is not None else ():
        if "{ch}" in command:
            return command.split()[-1]
    return "C{ch}"


def _key(query: str) -> str:
    return " ".join(query.strip().removeprefix(":").casefold().split())


def _block(payload: bytes) -> bytes:
    """An IEEE 488.2 definite-length block."""
    length = str(len(payload))
    return f"#{len(length)}{length}".encode() + payload
