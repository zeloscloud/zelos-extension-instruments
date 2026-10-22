"""Profiles: what an instrument family can do, as the loader in `profile_load` builds them.

A profile holds no code. Every command string in it comes from the instrument's programming
manual, and `docs/profiles.md` is the format's reference.
"""

import dataclasses
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from .config import Connection
from .link import FlowControl, Parity
from .profile_daq import DAQ_SIGNAL_FIELDS, DAQ_STATE_FIELDS, Daq
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

Category = Literal["psu", "load", "dmm", "acsource", "scope", "fgen", "daq"]
CATEGORIES: tuple[Category, ...] = get_args(Category)
Badge = Literal["manual", "transcript", "hardware"]
BADGES: tuple[Badge, ...] = get_args(Badge)
Parse = Literal["float", "floats", "int", "bool", "map", "bits", "str", "keyed"]
PARSES: tuple[Parse, ...] = get_args(Parse)

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
    "fgen": {},
    "daq": DAQ_SIGNAL_FIELDS,
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
        "ovp_delay_set": "s",
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
        "short": None,
        "slew_rise_set": "A/s",
        "slew_fall_set": "A/s",
        "voltage_range": None,
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
    "fgen": {
        "output": None,
        "shape": None,
        "frequency_set": "Hz",
        "amplitude_set": "Vpp",
        "offset_set": "V",
        "high_set": "V",
        "low_set": "V",
        "phase_set": "deg",
        "duty_set": "%",
        "symmetry_set": "%",
        "width_set": "s",
        "output_load": None,
        "modulation": None,
        "modulation_type": None,
        "modulation_depth_set": "%",
        "modulation_deviation_set": "Hz",
        "phase_deviation_set": "deg",
        "modulation_frequency_set": "Hz",
        "burst": None,
        "burst_cycles_set": None,
        "burst_period_set": "s",
        "sweep": None,
        "sweep_start_set": "Hz",
        "sweep_stop_set": "Hz",
        "sweep_time_set": "s",
    },
    "daq": DAQ_STATE_FIELDS,
}
# Fields read once for the whole instrument, not per channel.
INSTRUMENT_FIELDS: dict[str, dict[str, str | None]] = {
    "psu": {},
    "load": {},
    "acsource": {},
    "dmm": {},
    "scope": ACQUISITION_FIELDS,
    "fgen": {},
    "daq": {},
}
BOOL_FIELDS = frozenset(
    {
        "output",
        "visible",
        "ovp_on",
        "ocp_on",
        "remote_sense",
        "autorange",
        "short",
        "modulation",
        "burst",
        "sweep",
        "closed",
    }
)
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
SHAPE_LABELS = ("sine", "square", "ramp", "triangle", "pulse", "noise", "dc", "arbitrary", "other")
LOAD_LABELS = ("fifty_ohm", "high_z", "other")
MODULATION_LABELS = ("am", "fm", "pm", "fsk", "pwm", "other")
# A reader map key that labels every reply word the map does not name.
MAP_ANY = "*"
ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "mode": MODE_LABELS,
    "protection": PROTECTION_LABELS,
    "function": FUNCTION_LABELS,
    "tracking": TRACKING_LABELS,
    "current_range": CURRENT_RANGE_LABELS,
    "voltage_range": CURRENT_RANGE_LABELS,
    "shape": SHAPE_LABELS,
    "output_load": LOAD_LABELS,
    "modulation_type": MODULATION_LABELS,
    **SCOPE_ENUMS,
}
SETTERS: dict[str, tuple[str, ...]] = {
    "psu": (
        "voltage",
        "current",
        "ovp",
        "ocp",
        "ovp_on",
        "ocp_on",
        "output",
        "clear",
        "remote_sense",
        "ovp_delay",
        "tracking",
    ),
    "load": (
        "current",
        "voltage",
        "power",
        "resistance",
        "slew",
        "function",
        "output",
        "clear",
        "short",
        "current_range",
        "voltage_range",
        "slew_rise",
        "slew_fall",
    ),
    "acsource": ("voltage", "frequency", "current", "output", "clear"),
    "dmm": ("function", "range", "autorange", "nplc", "clear"),
    "scope": (*SCOPE_SETTERS, "clear"),
    "fgen": (
        "output",
        "shape",
        "frequency",
        "amplitude",
        "offset",
        "high",
        "low",
        "phase",
        "duty",
        "symmetry",
        "width",
        "output_load",
        "modulation",
        "modulation_type",
        "modulation_depth",
        "modulation_deviation",
        "phase_deviation",
        "modulation_frequency",
        "burst",
        "burst_cycles",
        "burst_period",
        "sweep",
        "sweep_start",
        "sweep_stop",
        "sweep_time",
        "trigger",
        "clear",
    ),
    "daq": ("relay", "clear"),
}
# Which range key bounds a value setter, when the setter's own name is not a range key.
SETTER_RANGE = {
    "ovp": "voltage",
    "ocp": "current",
    "sweep_start": "frequency",
    "sweep_stop": "frequency",
    "slew_rise": "slew",
    "slew_fall": "slew",
}
RANGE_KEYS = (
    "voltage",
    "current",
    "power",
    "resistance",
    "frequency",
    "ovp",
    "ocp",
    "slew",
    "amplitude",
    "high",
    "low",
    "phase",
    "duty",
    "symmetry",
    "width",
    "modulation_depth",
    "modulation_deviation",
    "phase_deviation",
    "modulation_frequency",
    "burst_cycles",
    "burst_period",
    "sweep_time",
    "ovp_delay",
    "nplc",
    "averages",
    "pulse_width",
    "offset",
    *SCOPE_RANGE_KEYS,
)
# Range keys that bound their setter only when the profile gives the range: a scope's offset has
# none and stays unchecked, while a generator's offset is bounded by its profile.
_GIVEN_RANGE_KEYS = frozenset(
    {
        "amplitude",
        "offset",
        "high",
        "low",
        "phase",
        "duty",
        "symmetry",
        "width",
        "modulation_depth",
        "modulation_deviation",
        "phase_deviation",
        "modulation_frequency",
        "burst_cycles",
        "burst_period",
        "sweep_time",
        "ovp_delay",
        "nplc",
        "averages",
        "pulse_width",
    }
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
        "amplitude",
        "high",
        "low",
        "phase",
        "duty",
        "symmetry",
        "width",
        "modulation_depth",
        "modulation_deviation",
        "phase_deviation",
        "modulation_frequency",
        "burst_cycles",
        "burst_period",
        "sweep_start",
        "sweep_stop",
        "sweep_time",
        "ovp_delay",
        "slew_rise",
        "slew_fall",
        "range",
        "nplc",
        *SCOPE_VALUE_SETTERS,
    }
)
# Setters that take one of a field's labels, sent as the instrument's word for it.
LABEL_SETTERS = frozenset(
    {
        "tracking",
        "current_range",
        "voltage_range",
        "shape",
        "output_load",
        "modulation_type",
        *SCOPE_LABEL_SETTERS,
    }
)
# Setters that switch something on or off.
SWITCH_SETTERS = frozenset(
    {
        "output",
        "visible",
        "ovp_on",
        "ocp_on",
        "remote_sense",
        "short",
        "autorange",
        "modulation",
        "burst",
        "sweep",
        "relay",
    }
)
# Setters that are a bare command.
COMMAND_SETTERS = frozenset({"clear", "trigger", *SCOPE_COMMAND_SETTERS})
# The field a setter reads back in, where neither `<name>_set` nor `<name>` is a field.
VERIFY_FIELD = {"function": "mode", "relay": "closed"}


def readback_field(category: str, name: str) -> str:
    """The field a setter named `name` reads back in: `<name>_set`, else `<name>`, else the
    irregular name `VERIFY_FIELD` gives; empty when it has none."""
    fields = {**SIGNAL_FIELDS[category], **STATE_FIELDS[category], **INSTRUMENT_FIELDS[category]}
    for candidate in (f"{name}_set", name):
        if candidate in fields:
            return candidate
    return VERIFY_FIELD.get(name, "")


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
    # The connections the family's protocol runs on; empty for any.
    connections: tuple[Connection, ...] = ()


@dataclass(frozen=True, slots=True)
class LinkDefaults:
    write_terminator: str
    read_terminator: str
    timeout_s: float
    delay_ms: int
    command_delay_ms: int
    lan_port: int
    baud: int
    data_bits: int
    parity: Parity
    stop_bits: float
    flow_control: FlowControl
    remote: str | None
    # The commands that hand the front panel back, in order; empty when the family has none.
    local: tuple[str, ...]
    locks_panel: bool
    # Sent before the identity query, such as an address on a multi-drop link.
    first: str | None
    # Queries fail unless the instrument is in remote mode, so the panel is never handed back.
    needs_remote: bool
    device_clear: bool
    select: str | None
    init: tuple[str, ...]
    single_client: bool
    # The reply every command (not a query) gets, read and checked after each one; None for none.
    ack: str | None
    # A query asked after `remote` and the reply it must give, or the open fails.
    remote_check: tuple[str, str] | None = None
    # An empty line where a reply is expected is dropped, not taken as the reply.
    skip_blank: bool = False


@dataclass(frozen=True, slots=True)
class Errors:
    query: str
    ok: re.Pattern[str]
    drain: int
    # Further error queries, asked in order after `query`.
    then: tuple[str, ...] = ()
    # The queries answer a bare number, the error code, instead of `code,"text"`.
    bare: bool = False


@dataclass(frozen=True, slots=True)
class Range:
    min: float | None
    max: float | None
    digits: int
    step: float | None
    min_query: str | None
    max_query: str | None
    # The only values a setter may send, sorted, when the instrument takes a fixed set.
    choices: tuple[float, ...] = ()

    def with_bounds(self, low: float | None, high: float | None) -> "Range":
        return dataclasses.replace(self, min=low, max=high)


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
    # For parse = "keyed": the key whose value fills the field.
    key: str | None = None
    # For parse = "keyed": the key is looked for only before the first token equal to this.
    until: str | None = None


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
    # A command setter may send several commands; every other setter sends `command` alone.
    commands: tuple[str, ...]
    verify: str | None
    range: str | None
    map: dict[str, str]
    per_channel: bool
    # The value is sent divided by this: a slew rate the instrument takes in A/us is 1e6.
    scale: float = 1.0
    # The output setter only arms a channel when the instrument has a master switch.
    master: Master | None = None
    # A switch's command for off, when `command` only switches on.
    off: str | None = None


@dataclass(frozen=True, slots=True)
class Dmm:
    function_query: str | None
    function_map: dict[str, str]
    words: dict[str, str]
    functions: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class System:
    """The functions an instrument has beside its readings and settings: reset, self-test, saved
    states, display and panel, reboot and screenshots. None or empty means it has none."""

    reset: str | None = "*RST"
    self_test: str | None = "*TST?"
    self_test_ok: re.Pattern[str] = re.compile(r"^\s*\+?0\s*$")
    self_test_timeout_s: float = 30.0
    # Asked after reset and recall, until it answers.
    opc: str | None = "*OPC?"
    # How long a reset or recall may take.
    settle_timeout_s: float = 10.0
    save: str | None = None
    recall: str | None = None
    # The first and last slot that save and recall take.
    slots: tuple[int, int] | None = None
    beep: str | None = None
    display: str | None = None
    display_text: str | None = None
    display_text_max: int | None = None
    display_clear: str | None = None
    lock: str | None = None
    unlock: str | None = None
    reboot: str | None = None
    # How long a reboot takes.
    reboot_s: float = 30.0
    # The last command is the one the instrument answers with the image.
    screenshot: tuple[str, ...] = ()
    screenshot_framing: Literal["block", "bmp", "png"] = "block"
    screenshot_cleanup: tuple[str, ...] = ()
    # Returns the setup as a definite-length block.
    setup_query: str | None = None
    # The setup block is written right after this text.
    setup_load: str | None = None


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
    system: System = field(default_factory=System)
    daq: Daq | None = None
    path: Path | None = field(default=None, compare=False)

    def matches(self, idn: Idn, connection: Connection | None = None) -> bool:
        """Whether an identity reply, read over `connection`, names this family. A generic
        profile never matches."""
        if self.generic:
            return False
        if (
            connection is not None
            and self.match.connections
            and connection not in self.match.connections
        ):
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

    def verify_field(self, setter: Setter) -> str:
        """The field a setter's readback lands in; a meter's function is its own."""
        return readback_field(self.category, setter.name)

    def verify_query(self, setter: Setter) -> str | None:
        """The readback query for a setter: its own, else its state field's."""
        if setter.verify is not None:
            return setter.verify
        reader = self.reader(self.verify_field(setter))
        return reader.query if reader is not None else None

    def range_key(self, setter: Setter) -> str | None:
        """The range that bounds a setter: its own, the profile's range of its name, else the
        output range a protection level falls back to. A range only some families have bounds
        its setter only when the profile gives it."""
        if setter.range is not None:
            return setter.range
        name = setter.name
        own = name in self.ranges or any(name in o.ranges for o in self.range_overrides)
        if name in RANGE_KEYS and (
            own or (name not in SETTER_RANGE and name not in _GIVEN_RANGE_KEYS)
        ):
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


def find(
    profiles: Iterable[Profile], idn: Idn, connection: Connection | None = None
) -> Profile | None:
    """The profile for an identity reply: the match with the longest model pattern wins."""
    matches = [p for p in profiles if p.matches(idn, connection)]
    if not matches:
        return None
    return max(matches, key=lambda p: (len(p.match.model.pattern), p.id))


def by_id(profiles: Iterable[Profile], profile_id: str) -> Profile | None:
    return next((p for p in profiles if p.id == profile_id), None)
