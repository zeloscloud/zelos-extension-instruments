"""The oscilloscope part of the profile vocabulary and format.

A scope records what its measurement slots show, per source channel, plus the channel and
acquisition settings. Which slots are on, what they measure and from which channel is read from
the instrument; the extension never turns a measurement on at connect.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

# Measurement fields, per channel. Amplitude fields are in the channel's unit, volts by default.
MEASUREMENT_FIELDS: dict[str, str | None] = {
    "pkpk": "V",
    "maximum": "V",
    "minimum": "V",
    "amplitude": "V",
    "top": "V",
    "base": "V",
    "mean": "V",
    "cycle_mean": "V",
    "rms": "V",
    "cycle_rms": "V",
    "stdev": "V",
    "overshoot": "%",
    "preshoot": "%",
    "period": "s",
    "frequency": "Hz",
    "positive_width": "s",
    "negative_width": "s",
    "duty": "%",
    "rise_time": "s",
    "fall_time": "s",
}
# Channel settings, per channel.
CHANNEL_STATE_FIELDS: dict[str, str | None] = {
    "visible": None,
    "scale": "V",
    "offset": "V",
    "coupling": None,
    "bandwidth_limit": None,
    "probe": None,
}
# Acquisition and trigger settings, one set for the instrument.
ACQUISITION_FIELDS: dict[str, str | None] = {
    "timebase": "s",
    "delay": "s",
    "sample_rate": "Sa/s",
    "memory_depth": "pts",
    "points": "pts",
    "acquisitions": None,
    "trigger_status": None,
    "trigger_mode": None,
    "trigger_source": None,
    "trigger_level": "V",
    "trigger_slope": None,
    "trigger_frequency": "Hz",
}
COUPLING_LABELS = ("dc", "ac", "gnd", "unknown")
BANDWIDTH_LABELS = ("full", "20mhz", "25mhz", "100mhz", "200mhz", "250mhz", "other")
TRIGGER_STATUS_LABELS = ("armed", "ready", "auto", "triggered", "stopped", "roll", "unknown")
TRIGGER_MODE_LABELS = ("auto", "normal", "single", "forced", "unknown")
TRIGGER_SOURCE_LABELS = (
    "ch1",
    "ch2",
    "ch3",
    "ch4",
    "ch5",
    "ch6",
    "ch7",
    "ch8",
    "line",
    "external",
    "digital",
    "unknown",
)
TRIGGER_SLOPE_LABELS = ("rising", "falling", "either", "unknown")
SCOPE_ENUMS: dict[str, tuple[str, ...]] = {
    "coupling": COUPLING_LABELS,
    "bandwidth_limit": BANDWIDTH_LABELS,
    "trigger_status": TRIGGER_STATUS_LABELS,
    "trigger_mode": TRIGGER_MODE_LABELS,
    "trigger_source": TRIGGER_SOURCE_LABELS,
    "trigger_slope": TRIGGER_SLOPE_LABELS,
}
# Setters: values, labels, a switch, and plain commands.
SCOPE_VALUE_SETTERS = ("scale", "offset", "probe", "timebase", "delay", "trigger_level")
SCOPE_LABEL_SETTERS = ("coupling", "trigger_source", "trigger_slope", "trigger_mode")
SCOPE_COMMAND_SETTERS = ("run", "stop", "single", "autoset")
SCOPE_SETTERS = (*SCOPE_VALUE_SETTERS, *SCOPE_LABEL_SETTERS, "visible", *SCOPE_COMMAND_SETTERS)
# Range keys a scope profile may bound.
SCOPE_RANGE_KEYS = ("scale", "timebase")
WAVEFORM_FORMATS = ("wavedesc",)
_CHANNEL_GROUP = re.compile(r"\(\?P<ch>")


class Checker(Protocol):
    """The part of the profile loader's checker this module uses."""

    def fail(self, key: str, message: str) -> None: ...
    def table(self, raw: Any, key: str) -> dict[str, Any]: ...
    def string(
        self, raw: dict[str, Any], key: str, default: str | None, where: str
    ) -> str | None: ...
    def required(self, raw: dict[str, Any], key: str, where: str) -> str: ...
    def integer(
        self, raw: dict[str, Any], key: str, default: int, where: str, low: int = 0
    ) -> int: ...
    def choice(
        self, raw: dict[str, Any], key: str, choices: Any, default: str, where: str
    ) -> str: ...
    def unknown(self, raw: dict[str, Any], known: Any, where: str) -> None: ...
    def template(self, text: str, allowed: set[str], where: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Slots:
    """How the instrument's measurement slots are read: which are on, what and from where."""

    count: int
    enabled: str
    type: str
    source: str
    source_pattern: re.Pattern[str]
    value: str
    # Instrument word to measurement field, and back.
    types: dict[str, str]
    words: dict[str, str]
    # A query and the reply that means the slots are live (a measurement mode), if any.
    ready_query: str | None
    ready_value: str | None


@dataclass(frozen=True, slots=True)
class Measure:
    """The commands that put a measurement in a slot, and the one that clears it."""

    commands: tuple[str, ...]
    off: str | None


@dataclass(frozen=True, slots=True)
class Waveform:
    """How one channel's samples are fetched and decoded."""

    format: str
    setup: tuple[str, ...]
    preamble: str
    data: str
    max_points: str | None
    points: str | None


@dataclass(frozen=True, slots=True)
class Scope:
    grid: int
    slots: Slots | None
    measure: Measure | None
    waveform: Waveform | None


def load_scope(c: Checker, raw: Any, category: str) -> Scope | None:
    """The `[scope]` table of a profile, checked."""
    if raw is None:
        if category == "scope":
            c.fail("scope", "missing: a scope profile describes its measurement slots.")
        return None
    if category != "scope":
        c.fail("scope", "is only for the scope category.")
        return None
    raw = c.table(raw, "scope")
    c.unknown(raw, ("grid", "slots", "measure", "waveform"), "scope")
    grid = c.integer(raw, "grid", 10, "scope", low=1)
    slots = _slots(c, raw.get("slots"))
    measure = _measure(c, raw.get("measure"), slots)
    waveform = _waveform(c, raw.get("waveform"))
    return Scope(grid=grid, slots=slots, measure=measure, waveform=waveform)


def _slots(c: Checker, raw: Any) -> Slots | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.slots")
    where = "scope.slots"
    c.unknown(
        raw,
        ("count", "enabled", "type", "source", "source_pattern", "value", "types", "ready"),
        where,
    )
    count = c.integer(raw, "count", 0, where, low=1)
    enabled = c.required(raw, "enabled", where)
    type_query = c.required(raw, "type", where)
    source = c.required(raw, "source", where)
    value = c.required(raw, "value", where)
    for key, text in (
        ("enabled", enabled),
        ("type", type_query),
        ("source", source),
        ("value", value),
    ):
        if text:
            c.template(text, {"slot"}, f"{where}.{key}")
            if "{slot}" not in text:
                c.fail(f"{where}.{key}", "must contain {slot}.")
    pattern_text = c.required(raw, "source_pattern", where)
    try:
        pattern = re.compile(pattern_text)
    except re.error as e:
        c.fail(f"{where}.source_pattern", f"is not a valid regular expression: {e}.")
        pattern = re.compile("$^")
    if not _CHANNEL_GROUP.search(pattern_text):
        c.fail(f"{where}.source_pattern", "must capture the channel number as (?P<ch>...).")
    types = raw.get("types")
    if not isinstance(types, dict) or not types:
        c.fail(
            f"{where}.types", "must be a non-empty table of instrument word to measurement field."
        )
        types = {}
    clean: dict[str, str] = {}
    for word, field_name in types.items():
        if not isinstance(word, str) or not isinstance(field_name, str):
            c.fail(f"{where}.types", "must map instrument words to measurement fields.")
            continue
        if field_name not in MEASUREMENT_FIELDS:
            c.fail(
                f"{where}.types", f"'{field_name}' is not one of {', '.join(MEASUREMENT_FIELDS)}."
            )
            continue
        clean[word.strip().casefold()] = field_name
    words: dict[str, str] = {}
    for word, field_name in types.items():
        if isinstance(word, str) and isinstance(field_name, str):
            words.setdefault(field_name, word.strip())
    ready = raw.get("ready")
    ready_query = ready_value = None
    if ready is not None:
        ready = c.table(ready, f"{where}.ready")
        c.unknown(ready, ("query", "value"), f"{where}.ready")
        ready_query = c.required(ready, "query", f"{where}.ready")
        ready_value = c.required(ready, "value", f"{where}.ready")
    return Slots(
        count=count,
        enabled=enabled,
        type=type_query,
        source=source,
        source_pattern=pattern,
        value=value,
        types=clean,
        words=words,
        ready_query=ready_query,
        ready_value=ready_value,
    )


def _measure(c: Checker, raw: Any, slots: Slots | None) -> Measure | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.measure")
    where = "scope.measure"
    c.unknown(raw, ("commands", "off"), where)
    if slots is None:
        c.fail(where, "needs scope.slots: a measurement is read back through the slots.")
    commands = raw.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(x, str) and x for x in commands)
    ):
        c.fail(f"{where}.commands", "must be a non-empty list of commands.")
        commands = []
    for command in commands:
        c.template(command, {"slot", "ch", "type"}, f"{where}.commands")
    if commands and not any("{type}" in x for x in commands):
        c.fail(f"{where}.commands", "must contain {type} in one command.")
    off = c.string(raw, "off", None, where)
    if off is not None:
        c.template(off, {"slot"}, f"{where}.off")
    return Measure(commands=tuple(commands), off=off)


def _waveform(c: Checker, raw: Any) -> Waveform | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.waveform")
    where = "scope.waveform"
    c.unknown(raw, ("format", "setup", "preamble", "data", "max_points", "points"), where)
    fmt = c.choice(raw, "format", WAVEFORM_FORMATS, "wavedesc", where)
    setup = raw.get("setup", [])
    if not isinstance(setup, list) or not all(isinstance(x, str) and x for x in setup):
        c.fail(f"{where}.setup", "must be a list of commands.")
        setup = []
    for command in setup:
        c.template(command, {"ch", "points", "interval"}, f"{where}.setup")
    preamble = c.required(raw, "preamble", where)
    data = c.required(raw, "data", where)
    max_points = c.string(raw, "max_points", None, where)
    points = c.string(raw, "points", None, where)
    for key, text in (
        ("preamble", preamble),
        ("data", data),
        ("max_points", max_points),
        ("points", points),
    ):
        if text:
            c.template(text, set(), f"{where}.{key}")
    return Waveform(
        format=fmt,
        setup=tuple(setup),
        preamble=preamble,
        data=data,
        max_points=max_points,
        points=points,
    )
