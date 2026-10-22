"""The oscilloscope part of the profile vocabulary and format.

A scope records what its measurement slots show, per source channel, plus the channel and
acquisition settings. Which slots are on, what they measure and from which channel is read from
the instrument; the extension never turns a measurement on at connect. A scope without slots may
be asked for each measurement directly, and then records the ones the Measure action added.
"""

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from .profile_check import Check

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
    "acquisition_mode": None,
    "averages": None,
    "trigger_type": None,
    "pulse_width": "s",
    "pulse_condition": None,
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
ACQUISITION_MODE_LABELS = ("normal", "average", "peak", "hires", "envelope", "other")
TRIGGER_TYPE_LABELS = ("edge", "pulse", "other")
PULSE_CONDITION_LABELS = ("greater", "less", "other")
SCOPE_ENUMS: dict[str, tuple[str, ...]] = {
    "coupling": COUPLING_LABELS,
    "bandwidth_limit": BANDWIDTH_LABELS,
    "trigger_status": TRIGGER_STATUS_LABELS,
    "trigger_mode": TRIGGER_MODE_LABELS,
    "trigger_source": TRIGGER_SOURCE_LABELS,
    "trigger_slope": TRIGGER_SLOPE_LABELS,
    "acquisition_mode": ACQUISITION_MODE_LABELS,
    "trigger_type": TRIGGER_TYPE_LABELS,
    "pulse_condition": PULSE_CONDITION_LABELS,
}
# Setters: values, labels, a switch, and plain commands.
SCOPE_VALUE_SETTERS = (
    "scale",
    "offset",
    "probe",
    "timebase",
    "delay",
    "trigger_level",
    "averages",
    "pulse_width",
)
SCOPE_LABEL_SETTERS = (
    "coupling",
    "trigger_source",
    "trigger_slope",
    "trigger_mode",
    "bandwidth_limit",
    "acquisition_mode",
    "trigger_type",
    "pulse_condition",
)
SCOPE_COMMAND_SETTERS = ("run", "stop", "single", "autoset", "force")
SCOPE_SETTERS = (*SCOPE_VALUE_SETTERS, *SCOPE_LABEL_SETTERS, "visible", *SCOPE_COMMAND_SETTERS)
# Range keys a scope profile may bound.
SCOPE_RANGE_KEYS = ("scale", "timebase")
WAVEFORM_FORMATS = ("wavedesc", "linear")
Encoding = Literal["i1", "u1", "i2", "u2"]
ENCODINGS: tuple[Encoding, ...] = get_args(Encoding)
ByteOrder = Literal["big", "little"]
BYTE_ORDERS: tuple[ByteOrder, ...] = get_args(ByteOrder)
# The scaling terms of a linear waveform, in the order `[scope.waveform]` documents them.
LINEAR_TERMS = ("x_inc", "x_zero", "x_ref", "y_mult", "y_zero", "y_ref")
_CHANNEL_GROUP = re.compile(r"\(\?P<ch>")
_SLOT_GROUP = re.compile(r"\(\?P<slot>")


@dataclass(frozen=True, slots=True)
class Slots:
    """How the instrument's measurement slots are read: which are on, what and from where.

    A slot is on when its `enabled` query says so, or, for an instrument whose measurements are
    added and deleted, when the `list_query` reply names it; `count` is then the most it allows."""

    count: int
    enabled: str | None
    list_query: str | None
    list_pattern: re.Pattern[str] | None
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

    def listed(self, reply: str) -> list[int]:
        """The slot numbers a list reply names, lowest first."""
        assert self.list_pattern is not None
        numbers = {int(found["slot"]) for found in self.list_pattern.finditer(reply)}
        return sorted(n for n in numbers if n >= 1)


@dataclass(frozen=True, slots=True)
class Measure:
    """How a measurement is shown: put in a slot by `commands` and cleared by `off`, or, when
    `query` is given, asked for directly each time it is read."""

    commands: tuple[str, ...]
    off: str | None
    # The direct query with {type} and {ch}, the word {type} takes per measurement field, and
    # which comma-separated item of the reply holds the value.
    query: str | None = None
    words: dict[str, str] = dataclasses.field(default_factory=dict)
    index: int | None = None


@dataclass(frozen=True, slots=True)
class Part:
    """One number a linear scaling term adds up: a constant, a preamble field or a query's reply,
    then inverted, multiplied, divided, negated and multiplied by the transfer interval, in that
    order, each only when asked."""

    value: float | None
    field: int | None
    query: str | None
    invert: bool = False
    multiply: float = 1.0
    divide: float = 1.0
    negate: bool = False
    times_interval: bool = False

    def apply(self, number: float, interval: int) -> float:
        if self.invert:
            number = 1 / number if number else math.inf
        number = number * self.multiply / self.divide
        if self.negate:
            number = -number
        return number * interval if self.times_interval else number


@dataclass(frozen=True, slots=True)
class Linear:
    """How a linear waveform's codes become volts and seconds: V = (code - y_ref) * y_mult +
    y_zero and t = (i - x_ref) * x_inc + x_zero, each term the sum of its parts."""

    x_inc: tuple[Part, ...]
    x_zero: tuple[Part, ...]
    x_ref: tuple[Part, ...]
    y_mult: tuple[Part, ...]
    y_zero: tuple[Part, ...]
    y_ref: tuple[Part, ...]
    encoding: Encoding
    byte_order: ByteOrder

    def terms(self) -> dict[str, tuple[Part, ...]]:
        return {name: getattr(self, name) for name in LINEAR_TERMS}


@dataclass(frozen=True, slots=True)
class Waveform:
    """How one channel's samples are fetched and decoded."""

    format: str
    setup: tuple[str, ...]
    # A block query for `wavedesc`; for `linear`, an optional query with a comma-separated reply.
    preamble: str | None
    data: str
    max_points: str | None
    points: str | None
    linear: Linear | None = None
    # Bytes the instrument sends before the data block, such as a `DAT2,` label.
    skip: int = 0


@dataclass(frozen=True, slots=True)
class Scope:
    grid: int
    slots: Slots | None
    measure: Measure | None
    waveform: Waveform | None


def load_scope(c: "Check", raw: Any, category: str) -> Scope | None:
    """The `[scope]` table of a profile, checked."""
    if raw is None:
        if category == "scope":
            c.fail("scope", "missing: a scope profile describes its measurement slots.")
        return None
    if category != "scope":
        c.fail("scope", "is only for the scope category.")
        return None
    raw = c.table(raw, "scope")
    c.unknown(raw, [f.name for f in dataclasses.fields(Scope)], "scope")
    grid = c.integer(raw, "grid", 10, "scope", low=1)
    slots = _slots(c, raw.get("slots"))
    measure = _measure(c, raw.get("measure"), slots)
    waveform = _waveform(c, raw.get("waveform"))
    return Scope(grid=grid, slots=slots, measure=measure, waveform=waveform)


def _slots(c: "Check", raw: Any) -> Slots | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.slots")
    where = "scope.slots"
    c.unknown(
        raw,
        (
            "count",
            "enabled",
            "list",
            "list_pattern",
            "type",
            "source",
            "source_pattern",
            "value",
            "types",
            "ready",
        ),
        where,
    )
    count = c.integer(raw, "count", 0, where, low=1)
    enabled = c.string(raw, "enabled", None, where)
    list_query = c.string(raw, "list", None, where)
    if (enabled is None) == (list_query is None):
        c.fail(where, "needs exactly one of enabled (a query per slot) or list (one for all).")
    list_pattern = None
    if list_query is not None:
        c.template(list_query, set(), f"{where}.list")
        pattern_text = c.required(raw, "list_pattern", where)
        list_pattern = c.pattern(pattern_text, f"{where}.list_pattern")
        if not _SLOT_GROUP.search(pattern_text):
            c.fail(f"{where}.list_pattern", "must capture the slot number as (?P<slot>...).")
    elif "list_pattern" in raw:
        c.fail(f"{where}.list_pattern", "is only for list.")
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
    pattern = c.pattern(pattern_text, f"{where}.source_pattern")
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
        list_query=list_query,
        list_pattern=list_pattern,
        type=type_query,
        source=source,
        source_pattern=pattern,
        value=value,
        types=clean,
        words=words,
        ready_query=ready_query,
        ready_value=ready_value,
    )


def _measure(c: "Check", raw: Any, slots: Slots | None) -> Measure | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.measure")
    where = "scope.measure"
    c.unknown(raw, [f.name for f in dataclasses.fields(Measure)], where)
    if "query" in raw:
        return _direct(c, raw, slots)
    for key in ("words", "index"):
        if key in raw:
            c.fail(f"{where}.{key}", "is only for a direct measurement, which has a query.")
    if slots is None:
        c.fail(where, "needs scope.slots: a measurement is read back through the slots.")
    commands = c.commands(raw, "commands", where, required=True)
    for command in commands:
        c.template(command, {"slot", "ch", "type"}, f"{where}.commands")
    if commands and not any("{type}" in x for x in commands):
        c.fail(f"{where}.commands", "must contain {type} in one command.")
    if slots is not None and slots.list_query is not None:
        # The slot may not exist yet, so the commands create it and say what it measures.
        for name in ("{slot}", "{ch}"):
            if commands and not any(name in x for x in commands):
                c.fail(f"{where}.commands", f"must contain {name} in one command.")
    off = c.string(raw, "off", None, where)
    if off is not None:
        c.template(off, {"slot"}, f"{where}.off")
    return Measure(commands=tuple(commands), off=off)


def _direct(c: "Check", raw: dict[str, Any], slots: Slots | None) -> Measure:
    where = "scope.measure"
    if slots is not None:
        c.fail(where, "a direct measurement reads no slots: drop scope.slots or the query.")
    for key in ("commands", "off"):
        if key in raw:
            c.fail(f"{where}.{key}", "is only for slots; a direct measurement has a query.")
    query = c.required(raw, "query", where)
    if query:
        c.template(query, {"ch", "type"}, f"{where}.query")
        for name in ("{type}", "{ch}"):
            if name not in query:
                c.fail(f"{where}.query", f"must contain {name}.")
    words = c.text_table(raw, "words", where, "measurement fields to instrument words")
    if not words:
        c.fail(f"{where}.words", "must give the instrument word of at least one measurement.")
    c.one_of(words, MEASUREMENT_FIELDS, f"{where}.words")
    index = c.integer(raw, "index", 0, where) if "index" in raw else None
    return Measure(
        commands=(),
        off=None,
        query=query,
        words={k: v for k, v in words.items() if k in MEASUREMENT_FIELDS},
        index=index,
    )


def _waveform(c: "Check", raw: Any) -> Waveform | None:
    if raw is None:
        return None
    raw = c.table(raw, "scope.waveform")
    where = "scope.waveform"
    known = [f.name for f in dataclasses.fields(Waveform) if f.name != "linear"]
    c.unknown(raw, [*known, *LINEAR_TERMS, "encoding", "byte_order"], where)
    fmt = c.choice(raw, "format", WAVEFORM_FORMATS, "wavedesc", where)
    setup = c.commands(raw, "setup", where)
    for command in setup:
        c.template(command, {"ch", "points", "interval"}, f"{where}.setup")
    if fmt == "wavedesc":
        preamble: str | None = c.required(raw, "preamble", where)
    else:
        preamble = c.string(raw, "preamble", None, where)
    data = c.required(raw, "data", where)
    max_points = c.string(raw, "max_points", None, where)
    points = c.string(raw, "points", None, where)
    for key, text, allowed in (
        ("preamble", preamble, {"ch", "points"}),
        ("data", data, {"ch", "points"}),
        ("max_points", max_points, set()),
        ("points", points, set()),
    ):
        if text:
            c.template(text, allowed, f"{where}.{key}")
    linear = None
    if fmt == "linear":
        linear = _linear(c, raw, preamble is not None)
    else:
        for key in (*LINEAR_TERMS, "encoding", "byte_order"):
            if key in raw:
                c.fail(f"{where}.{key}", "is only for format linear.")
    return Waveform(
        format=fmt,
        setup=tuple(setup),
        preamble=preamble,
        data=data,
        max_points=max_points,
        points=points,
        linear=linear,
        skip=c.integer(raw, "skip", 0, where, low=0),
    )


def _linear(c: "Check", raw: dict[str, Any], has_preamble: bool) -> Linear:
    where = "scope.waveform"
    terms = [_term(c, raw.get(name), f"{where}.{name}", has_preamble) for name in LINEAR_TERMS]
    x_inc, x_zero, x_ref, y_mult, y_zero, y_ref = terms
    return Linear(
        x_inc=x_inc,
        x_zero=x_zero,
        x_ref=x_ref,
        y_mult=y_mult,
        y_zero=y_zero,
        y_ref=y_ref,
        encoding=c.choice(raw, "encoding", ENCODINGS, "u1", where),
        byte_order=c.choice(raw, "byte_order", BYTE_ORDERS, "little", where),
    )


def _term(c: "Check", raw: Any, where: str, has_preamble: bool) -> tuple[Part, ...]:
    """A number, one part, or a list of parts that add up."""
    if raw is None:
        c.fail(where, "missing: a number, a preamble field or a query.")
        return ()
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return (Part(value=float(raw), field=None, query=None),)
    items: list[Any] = raw if isinstance(raw, list) else [raw]
    tables = [item for item in items if isinstance(item, dict)]
    if not tables or len(tables) != len(items):
        c.fail(where, "must be a number, a table or a list of tables.")
        return ()
    return tuple(_part(c, table, where, has_preamble) for table in tables)


def _part(c: "Check", raw: dict[str, Any], where: str, has_preamble: bool) -> Part:
    c.unknown(raw, [f.name for f in dataclasses.fields(Part)], where)
    if sum(key in raw for key in ("value", "field", "query")) != 1:
        c.fail(where, "must give exactly one of value, field or query.")
    field_index = c.integer(raw, "field", 0, where) if "field" in raw else None
    if field_index is not None and not has_preamble:
        c.fail(f"{where}.field", "needs scope.waveform.preamble, whose reply it indexes.")
    query = c.string(raw, "query", None, where)
    if query is not None:
        c.template(query, {"ch", "points"}, f"{where}.query")
    multiply = c.number(raw, "multiply", 1.0, where)
    divide = c.number(raw, "divide", 1.0, where)
    if not divide:
        c.fail(f"{where}.divide", "must not be zero.")
    return Part(
        value=c.number(raw, "value", None, where),
        field=field_index,
        query=query,
        invert=c.boolean(raw, "invert", False, where),
        multiply=1.0 if multiply is None else multiply,
        divide=divide or 1.0,
        negate=c.boolean(raw, "negate", False, where),
        times_interval=c.boolean(raw, "times_interval", False, where),
    )
