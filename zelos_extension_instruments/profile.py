"""Profiles: what an instrument family can do, as the loader in `profile_load` builds them.

A profile holds no code. Every command string in it comes from the instrument's programming
manual, and `docs/profiles.md` is the format's reference.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .profile_scope import (
    ACQUISITION_FIELDS,
    CHANNEL_STATE_FIELDS,
    MEASUREMENT_FIELDS,
    SCOPE_COMMAND_SETTERS,
    SCOPE_ENUMS,
    SCOPE_LABEL_SETTERS,
    SCOPE_RANGE_KEYS,
    SCOPE_SETTERS,
    SCOPE_VALUE_SETTERS,
    Scope,
)

Category = Literal["psu", "load", "dmm", "acsource", "scope"]
CATEGORIES: tuple[Category, ...] = ("psu", "load", "dmm", "acsource", "scope")
Badge = Literal["manual", "transcript", "hardware"]
BADGES: tuple[Badge, ...] = ("manual", "transcript", "hardware")
Parse = Literal["float", "floats", "int", "bool", "map", "bits", "str"]
PARSES: tuple[Parse, ...] = ("float", "floats", "int", "bool", "map", "bits", "str")

# The vocabulary: field name to unit, per category. None is a unitless field.
SIGNAL_FIELDS: dict[str, dict[str, str | None]] = {
    "psu": {"voltage": "V", "current": "A", "power": "W"},
    "load": {"voltage": "V", "current": "A", "power": "W", "resistance": "Ohm"},
    "acsource": {
        "voltage": "V",
        "current": "A",
        "power": "W",
        "apparent_power": "VA",
        "power_factor": None,
        "frequency": "Hz",
    },
    "dmm": {"value": None},
    "scope": MEASUREMENT_FIELDS,
}
STATE_FIELDS: dict[str, dict[str, str | None]] = {
    "psu": {
        "voltage_set": "V",
        "current_set": "A",
        "ovp_set": "V",
        "ocp_set": "A",
        "ovp_on": None,
        "ocp_on": None,
        "output": None,
        "mode": None,
        "protection": None,
        "remote_sense": None,
        "tracking": None,
    },
    "load": {
        "current_set": "A",
        "voltage_set": "V",
        "power_set": "W",
        "resistance_set": "Ohm",
        "slew_set": "A/s",
        "current_range": None,
        "output": None,
        "mode": None,
        "protection": None,
    },
    "acsource": {
        "voltage_set": "V",
        "frequency_set": "Hz",
        "current_set": "A",
        "output": None,
        "protection": None,
    },
    "dmm": {"function": None, "range": None, "autorange": None, "nplc": None},
    "scope": CHANNEL_STATE_FIELDS,
}
# Fields read once for the whole instrument, not per channel.
INSTRUMENT_FIELDS: dict[str, dict[str, str | None]] = {
    "psu": {},
    "load": {},
    "acsource": {},
    "dmm": {},
    "scope": ACQUISITION_FIELDS,
}
BOOL_FIELDS = frozenset({"output", "visible", "ovp_on", "ocp_on", "remote_sense", "autorange"})
# Enumerated fields and their labels, in value-table order.
MODE_LABELS = ("off", "cv", "cc", "cp", "cr", "unregulated", "unknown")
PROTECTION_LABELS = ("none", "ovp", "ocp", "opp", "otp", "other")
FUNCTION_LABELS = (
    "dc_voltage",
    "ac_voltage",
    "dc_current",
    "ac_current",
    "resistance",
    "resistance_4w",
    "frequency",
    "period",
    "temperature",
    "capacitance",
    "diode",
    "continuity",
    "other",
)
TRACKING_LABELS = ("independent", "series", "parallel", "unknown")
CURRENT_RANGE_LABELS = ("low", "mid", "high", "auto", "unknown")
ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "mode": MODE_LABELS,
    "protection": PROTECTION_LABELS,
    "function": FUNCTION_LABELS,
    "tracking": TRACKING_LABELS,
    "current_range": CURRENT_RANGE_LABELS,
    **SCOPE_ENUMS,
}
SETTERS: dict[str, tuple[str, ...]] = {
    "psu": ("voltage", "current", "ovp", "ocp", "ovp_on", "ocp_on", "output", "clear"),
    "load": ("current", "voltage", "power", "resistance", "slew", "function", "output", "clear"),
    "acsource": ("voltage", "frequency", "current", "output", "clear"),
    "dmm": ("function",),
    "scope": SCOPE_SETTERS,
}
# Which range key bounds a value setter, when the setter's own name is not a range key.
SETTER_RANGE = {"ovp": "voltage", "ocp": "current"}
RANGE_KEYS = (
    "voltage",
    "current",
    "power",
    "resistance",
    "frequency",
    "ovp",
    "ocp",
    "slew",
    *SCOPE_RANGE_KEYS,
)
VALUE_SETTERS = frozenset(
    {
        "voltage",
        "current",
        "power",
        "resistance",
        "frequency",
        "ovp",
        "ocp",
        "slew",
        *SCOPE_VALUE_SETTERS,
    }
)
# Setters that take one of a field's labels, sent as the instrument's word for it.
LABEL_SETTERS = frozenset(SCOPE_LABEL_SETTERS)
# Setters that switch something on or off.
SWITCH_SETTERS = frozenset({"output", "visible", "ovp_on", "ocp_on"})
# Setters that are a bare command.
COMMAND_SETTERS = frozenset({"clear", *SCOPE_COMMAND_SETTERS})
# The state field a value setter verifies with, by default.
VERIFY_FIELD = {
    "voltage": "voltage_set",
    "current": "current_set",
    "power": "power_set",
    "resistance": "resistance_set",
    "frequency": "frequency_set",
    "ovp": "ovp_set",
    "ocp": "ocp_set",
    "output": "output",
    "function": "mode",
    "ovp_on": "ovp_on",
    "ocp_on": "ocp_on",
    "slew": "slew_set",
    "visible": "visible",
    "scale": "scale",
    "offset": "offset",
    "probe": "probe",
    "coupling": "coupling",
    "timebase": "timebase",
    "delay": "delay",
    "trigger_level": "trigger_level",
    "trigger_source": "trigger_source",
    "trigger_slope": "trigger_slope",
    "trigger_mode": "trigger_mode",
}


@dataclass(frozen=True, slots=True)
class Idn:
    """The four fields of an identity reply, each stripped."""

    manufacturer: str
    model: str
    serial: str
    firmware: str

    @property
    def text(self) -> str:
        return ",".join((self.manufacturer, self.model, self.serial, self.firmware))


def is_identity(reply: str) -> bool:
    """Whether a reply reads as an identity: a maker's name, a comma, then the rest. A reading
    such as `12.0,0.5,6.0` that arrived out of step is not one."""
    maker, comma, _ = reply.partition(",")
    return bool(comma) and any(c.isalpha() for c in maker)


def parse_idn(reply: str) -> Idn:
    """Split an `*IDN?` reply; a missing field is empty."""
    parts = [p.strip() for p in reply.strip().split(",")]
    parts += [""] * (4 - len(parts))
    return Idn(parts[0], parts[1], parts[2], ",".join(parts[3:]))


@dataclass(frozen=True, slots=True)
class Evidence:
    badge: Badge
    source: str
    url: str | None
    firmware: str | None
    date: str


@dataclass(frozen=True, slots=True)
class ModelChannels:
    model: re.Pattern[str]
    channels: int


@dataclass(frozen=True, slots=True)
class Match:
    manufacturer: re.Pattern[str]
    model: re.Pattern[str]
    idn: str
    channels: int
    channels_query: str | None
    models: tuple[ModelChannels, ...]


@dataclass(frozen=True, slots=True)
class LinkDefaults:
    write_terminator: str
    read_terminator: str
    timeout_s: float
    delay_ms: int
    # The pause after a command that sends no reply, when longer than `delay_ms`.
    command_delay_ms: int
    lan_port: int
    baud: int
    data_bits: int
    parity: str
    stop_bits: float
    flow_control: str
    remote: str | None
    # The commands that hand the front panel back, in order; empty when the family has none.
    local: tuple[str, ...]
    locks_panel: bool
    # A command the instrument needs before it answers anything, sent before the identity
    # query (an address on a multi-drop link).
    first: str | None
    # Queries fail unless the instrument is in remote mode, so the panel is never handed back.
    needs_remote: bool
    # Whether a VISA device clear is safe; some scopes hang on one.
    device_clear: bool
    select: str | None
    init: tuple[str, ...]
    single_client: bool
    # The reply every command (not a query) gets, read and checked after each one; None for none.
    ack: str | None


@dataclass(frozen=True, slots=True)
class Errors:
    query: str
    ok: re.Pattern[str]
    drain: int


@dataclass(frozen=True, slots=True)
class Range:
    min: float | None
    max: float | None
    digits: int
    step: float | None
    min_query: str | None
    max_query: str | None

    def with_bounds(self, low: float | None, high: float | None) -> "Range":
        return Range(low, high, self.digits, self.step, self.min_query, self.max_query)


@dataclass(frozen=True, slots=True)
class RangeOverride:
    model: re.Pattern[str]
    channel: int | None
    ranges: dict[str, Range]


@dataclass(frozen=True, slots=True)
class Flag:
    query: str
    parse: Parse
    value: str
    map: dict[str, str]
    # Per channel when the profile gives a list, else the same for every channel.
    mask: tuple[int, ...]
    shift: tuple[int, ...]
    hex: bool
    index: int | None


@dataclass(frozen=True, slots=True)
class Reader:
    """One query and how its reply fills fields."""

    name: str
    query: str | None
    parse: Parse
    fields: tuple[str, ...]
    index: int | None
    map: dict[str, str]
    # The instrument's own spelling for each label, as the profile first wrote it.
    words: dict[str, str]
    mask: tuple[int, ...]
    shift: tuple[int, ...]
    hex: bool
    optional: bool
    per_channel: bool
    flags: tuple[Flag, ...]
    # Multiplies a parsed number: a slew rate the instrument states in A/us is 1e6 A/s.
    scale: float = 1.0


@dataclass(frozen=True, slots=True)
class Master:
    """A switch that delivers power on every armed channel at once."""

    command: str
    query: str
    # Whether a channel is armed, asked with that channel selected.
    armed: str


@dataclass(frozen=True, slots=True)
class Setter:
    name: str
    command: str
    # `clear` may send several commands; every other setter sends `command` alone.
    commands: tuple[str, ...]
    verify: str | None
    range: str | None
    map: dict[str, str]
    per_channel: bool
    # The value is sent divided by this: a slew rate the instrument takes in A/us is 1e6.
    scale: float = 1.0
    # The output setter only arms a channel when the instrument has a master switch.
    master: Master | None = None


@dataclass(frozen=True, slots=True)
class Dmm:
    function_query: str | None
    function_map: dict[str, str]
    words: dict[str, str]
    functions: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    category: Category
    generic: bool
    notes: str | None
    evidence: Evidence
    match: Match
    link: LinkDefaults
    errors: Errors | None
    ranges: dict[str, Range]
    range_overrides: tuple[RangeOverride, ...]
    signals: tuple[Reader, ...]
    state: tuple[Reader, ...]
    setters: dict[str, Setter]
    dmm: Dmm | None
    scope: Scope | None = None
    path: Path | None = field(default=None, compare=False)

    def matches(self, idn: Idn) -> bool:
        """Whether an identity reply names this family. A generic profile never matches."""
        if self.generic:
            return False
        return bool(
            self.match.manufacturer.search(idn.manufacturer) and self.match.model.search(idn.model)
        )

    def channels_for(self, model: str) -> int:
        """The channel count for a model string, from the first matching override."""
        for entry in self.match.models:
            if entry.model.search(model):
                return entry.channels
        return self.match.channels

    def range_for(self, key: str, model: str, channel: int) -> Range | None:
        """The range bounding `key` on `channel` of `model`."""
        for override in self.range_overrides:
            if key not in override.ranges or not override.model.search(model):
                continue
            if override.channel is None or override.channel == channel:
                return override.ranges[key]
        return self.ranges.get(key)

    def reader(self, field_name: str) -> Reader | None:
        """The reader that fills `field_name`, in signals or state."""
        for reader in (*self.signals, *self.state):
            if field_name in reader.fields:
                return reader
        return None

    def verify_query(self, setter: Setter) -> str | None:
        """The readback query for a setter: its own, else its state field's."""
        if setter.verify is not None:
            return setter.verify
        field_name = VERIFY_FIELD.get(setter.name, "")
        if setter.name == "function" and self.category == "dmm":
            field_name = "function"
        reader = self.reader(field_name)
        return reader.query if reader is not None else None

    def range_key(self, setter: Setter) -> str | None:
        """The range that bounds a setter: its own, the profile's range of its name, else the
        output range a protection level falls back to."""
        if setter.range is not None:
            return setter.range
        name = setter.name
        own = name in self.ranges or any(name in o.ranges for o in self.range_overrides)
        if name in RANGE_KEYS and (own or name not in SETTER_RANGE):
            return name
        return SETTER_RANGE.get(name)

    @property
    def fields(self) -> dict[str, str | None]:
        """Every per-channel field the profile reads, with its unit, signals first. A scope's
        measurements come from its slots, so every measurement the slots can name is a field."""
        units = {**SIGNAL_FIELDS[self.category], **STATE_FIELDS[self.category]}
        found = {
            name: units[name]
            for reader in (*self.signals, *self.state)
            for name in reader.fields
            if name in units
        }
        if self.scope is not None and self.scope.slots is not None:
            measured = {name: units[name] for name in self.scope.slots.types.values()}
            found = {**measured, **found}
        return found

    @property
    def instrument_fields(self) -> dict[str, str | None]:
        """The instrument-wide fields the profile reads, with their units."""
        units = INSTRUMENT_FIELDS[self.category]
        return {
            name: units[name] for reader in self.state for name in reader.fields if name in units
        }


def find(profiles: Iterable[Profile], idn: Idn) -> Profile | None:
    """The profile for an identity reply: the match with the longest model pattern wins."""
    matches = [p for p in profiles if p.matches(idn)]
    if not matches:
        return None
    return max(matches, key=lambda p: (len(p.match.model.pattern), p.id))


def by_id(profiles: Iterable[Profile], profile_id: str) -> Profile | None:
    return next((p for p in profiles if p.id == profile_id), None)
