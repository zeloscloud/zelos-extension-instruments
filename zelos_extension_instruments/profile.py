"""Profiles: what an instrument family can do, loaded from TOML and checked.

A profile holds no code. Every command string in it comes from the instrument's programming
manual, and `docs/profiles.md` is the format's reference.
"""

import dataclasses
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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
    load_scope,
)

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "profiles"


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
PLACEHOLDERS = {
    "ch",
    "ch0",
    "value",
    "on",
    "bit",
    "function",
    "slot",
    "type",
    "points",
    "interval",
}
# Commands that change what an output does; none may appear in `init`.
_UNSAFE_HEADS = (
    r"(PRES(ET)?|OUTP(UT)?|INP(UT)?|SOUR(CE)?|VOLT(AGE)?|CURR(ENT)?|POW(ER)?|RES(ISTANCE)?"
    r"|FUNC(TION)?|APPL(Y)?|TRIG(GER)?|INIT(IATE)?|ABOR(T)?|MODE)\b"
)
_UNSAFE_INIT = re.compile(
    r"(?i)^\s*(\*(RST|TRG|RCL)\b|:?\s*"
    + _UNSAFE_HEADS
    + r")|[:;]\s*(\*(RST|TRG|RCL)\b|"
    + _UNSAFE_HEADS
    + ")"
)
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


def _head(command: str) -> str:
    """A command's header, as the instrument would read it: the first word, case-folded."""
    return command.strip().split()[0].casefold() if command.strip() else ""


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")
_PARITIES = ("none", "even", "odd", "mark", "space")
_FLOW = ("none", "rts/cts", "xon/xoff", "dtr/dsr")


class ProfileError(Exception):
    """A profile file that cannot be used; one sentence per problem, each naming the key."""

    def __init__(self, path: Path | str, problems: list[str]) -> None:
        self.path = Path(path)
        self.problems = problems
        super().__init__(f"{self.path.name}: " + " ".join(problems))


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


def load_file(path: Path | str) -> Profile:
    """Read and check one profile; raises ProfileError."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ProfileError(path, [f"cannot be read: {e.strerror or e}."]) from e
    except UnicodeDecodeError as e:
        raise ProfileError(path, ["is not UTF-8 text."]) from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(path, [f"is not valid TOML: {e}."]) from e
    return from_dict(raw, path)


def load_dir(directory: Path | str) -> list[Profile]:
    """Every `*.toml` under a directory, sorted by path; raises on the first bad file."""
    return [load_file(p) for p in sorted(Path(directory).rglob("*.toml"))]


def shipped() -> list[Profile]:
    """The profiles that ship with the extension."""
    return load_dir(SHIPPED_DIR)


def find(profiles: Iterable[Profile], idn: Idn) -> Profile | None:
    """The profile for an identity reply: the match with the longest model pattern wins."""
    matches = [p for p in profiles if p.matches(idn)]
    if not matches:
        return None
    return max(matches, key=lambda p: (len(p.match.model.pattern), p.id))


def by_id(profiles: Iterable[Profile], profile_id: str) -> Profile | None:
    return next((p for p in profiles if p.id == profile_id), None)


class _Check:
    """Collects problems while a raw TOML table is turned into a Profile."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.problems: list[str] = []

    def fail(self, key: str, message: str) -> None:
        self.problems.append(f"{key}: {message}")

    def table(self, raw: Any, key: str) -> dict[str, Any]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.fail(key, "must be a table.")
            return {}
        return raw

    def string(self, raw: dict[str, Any], key: str, default: str | None, where: str) -> str | None:
        value = raw.get(key, default)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            self.fail(f"{where}.{key}" if where else key, "must be non-empty text.")
            return default
        return value

    def required(self, raw: dict[str, Any], key: str, where: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            self.fail(f"{where}.{key}" if where else key, "missing or empty.")
            return ""
        return value

    def number(
        self, raw: dict[str, Any], key: str, default: float | None, where: str
    ) -> float | None:
        value = raw.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.fail(f"{where}.{key}", "must be a number.")
            return default
        return float(value)

    def integer(self, raw: dict[str, Any], key: str, default: int, where: str, low: int = 0) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            self.fail(f"{where}.{key}", f"must be a whole number of at least {low}.")
            return default
        return value

    def boolean(self, raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
        value = raw.get(key, default)
        if not isinstance(value, bool):
            self.fail(f"{where}.{key}", "must be true or false.")
            return default
        return value

    def choice(
        self, raw: dict[str, Any], key: str, choices: tuple[str, ...], default: str, where: str
    ) -> str:
        value = raw.get(key, default)
        if value not in choices:
            self.fail(f"{where}.{key}", f"must be one of {', '.join(choices)}.")
            return default
        return value

    def regex(self, raw: dict[str, Any], key: str, where: str) -> re.Pattern[str]:
        text = self.required(raw, key, where)
        try:
            return re.compile(text)
        except re.error as e:
            self.fail(f"{where}.{key}", f"is not a valid regular expression ({e}).")
            return re.compile(r"(?!)")

    def unknown(self, raw: dict[str, Any], known: Iterable[str], where: str) -> None:
        extra = sorted(set(raw) - set(known))
        if extra:
            self.fail(where or "profile", f"unknown key {', '.join(extra)}.")

    def template(self, text: str, allowed: set[str], where: str) -> None:
        names = set(_PLACEHOLDER.findall(text))
        if "ch" in allowed:
            # The same channel, numbered from 0 for instruments that count that way.
            allowed = allowed | {"ch0"}
        if names - allowed:
            self.fail(where, f"unknown placeholder {', '.join(sorted(names - allowed))}.")
        stray = re.sub(_PLACEHOLDER, "", text)
        if "{" in stray or "}" in stray:
            self.fail(where, "has an unmatched brace.")


def from_dict(raw: dict[str, Any], path: Path | str = "profile.toml") -> Profile:
    """Turn a parsed TOML table into a Profile; raises ProfileError with every problem found."""
    path = Path(path)
    c = _Check(path)
    c.unknown(
        raw,
        (
            "schema",
            "id",
            "name",
            "category",
            "generic",
            "notes",
            "evidence",
            "match",
            "link",
            "errors",
            "ranges",
            "signals",
            "state",
            "setters",
            "dmm",
            "scope",
        ),
        "",
    )
    if raw.get("schema") != 1:
        c.fail("schema", "must be 1.")
    profile_id = c.required(raw, "id", "")
    if profile_id and not _ID.fullmatch(profile_id):
        c.fail("id", "use lower-case letters, digits and hyphens.")
    if profile_id and path.suffix == ".toml" and path.stem != profile_id:
        c.fail("id", f"must equal the file name, {path.stem}.")
    name = c.required(raw, "name", "")
    category = c.choice(raw, "category", CATEGORIES, "psu", "")
    generic = c.boolean(raw, "generic", False, "")
    notes = c.string(raw, "notes", None, "")

    evidence = _evidence(c, c.table(raw.get("evidence"), "evidence"))
    match = _match(c, c.table(raw.get("match"), "match"), generic)
    link = _link(c, c.table(raw.get("link"), "link"))
    errors = _errors(c, raw.get("errors"))
    ranges, overrides = _ranges(c, c.table(raw.get("ranges"), "ranges"))
    signal_units = SIGNAL_FIELDS[category]
    state_units = STATE_FIELDS[category]
    signals = _readers(c, c.table(raw.get("signals"), "signals"), "signals", signal_units)
    state = _readers(
        c,
        c.table(raw.get("state"), "state"),
        "state",
        {**state_units, **INSTRUMENT_FIELDS[category]},
    )
    # An instrument-wide field is read once, whatever the reader's channel key says.
    wide = INSTRUMENT_FIELDS[category]
    state = tuple(
        dataclasses.replace(reader, per_channel=False)
        if any(f in wide for f in reader.fields)
        else reader
        for reader in state
    )
    for reader in state:
        if not reader.per_channel and reader.query is not None and "{ch}" in reader.query:
            c.fail(
                f"state.{reader.name}",
                "reads an instrument-wide field, so its query takes no {ch}.",
            )
    seen: dict[str, str] = {}
    for reader in (*signals, *state):
        for name_ in reader.fields:
            if name_ in seen:
                c.fail(f"{reader.name}", f"reads {name_}, which {seen[name_]} reads too.")
            seen[name_] = reader.name
    setters = _setters(c, c.table(raw.get("setters"), "setters"), category, ranges, overrides)
    heads = {_head(cmd) for s in setters.values() for cmd in s.commands}
    for key, commands in (("init", link.init), ("first", (link.first,))):
        for command in commands:
            if command is not None and _head(command) in heads:
                c.fail(
                    f"link.{key}", f"'{command}' is a setter's command; it may not run at connect."
                )
    dmm = _dmm(c, raw.get("dmm"), category)
    scope = load_scope(c, raw.get("scope"), category)
    if category == "dmm" and not any("value" in r.fields for r in signals):
        c.fail("signals.value", "missing: a DMM profile reads value.")
    if not signals and not generic and category != "scope":
        c.fail("signals", "missing: a profile reads at least one signal.")
    if c.problems:
        raise ProfileError(path, c.problems)
    return Profile(
        id=profile_id,
        name=name,
        category=category,  # type: ignore[arg-type]
        generic=generic,
        notes=notes,
        evidence=evidence,
        match=match,
        link=link,
        errors=errors,
        ranges=ranges,
        range_overrides=overrides,
        signals=signals,
        state=state,
        setters=setters,
        dmm=dmm,
        scope=scope,
        path=path,
    )


def _evidence(c: _Check, raw: dict[str, Any]) -> Evidence:
    c.unknown(raw, ("badge", "source", "url", "firmware", "date"), "evidence")
    badge = c.choice(raw, "badge", BADGES, "manual", "evidence")
    source = c.required(raw, "source", "evidence")
    date = c.required(raw, "date", "evidence")
    if date and not _DATE.fullmatch(date):
        c.fail("evidence.date", "must be YYYY-MM-DD.")
    return Evidence(
        badge=badge,  # type: ignore[arg-type]
        source=source,
        url=c.string(raw, "url", None, "evidence"),
        firmware=c.string(raw, "firmware", None, "evidence"),
        date=date,
    )


def _match(c: _Check, raw: dict[str, Any], generic: bool) -> Match:
    c.unknown(
        raw, ("manufacturer", "model", "idn", "channels", "channels_query", "models"), "match"
    )
    if generic:
        manufacturer = model = re.compile(r"(?!)")
        for key in ("manufacturer", "model"):
            if key in raw:
                c.fail(f"match.{key}", "a generic profile is chosen by category, not matched.")
    else:
        manufacturer = c.regex(raw, "manufacturer", "match")
        model = c.regex(raw, "model", "match")
    models: list[ModelChannels] = []
    entries = raw.get("models", [])
    if not isinstance(entries, list):
        c.fail("match.models", "must be a list of tables.")
        entries = []
    for i, entry in enumerate(entries):
        entry = c.table(entry, f"match.models[{i}]")
        c.unknown(entry, ("model", "channels"), f"match.models[{i}]")
        models.append(
            ModelChannels(
                c.regex(entry, "model", f"match.models[{i}]"),
                c.integer(entry, "channels", 1, f"match.models[{i}]", low=1),
            )
        )
    return Match(
        manufacturer=manufacturer,
        model=model,
        idn=c.string(raw, "idn", "*IDN?", "match") or "*IDN?",
        channels=c.integer(raw, "channels", 1, "match", low=1),
        channels_query=c.string(raw, "channels_query", None, "match"),
        models=tuple(models),
    )


def _link(c: _Check, raw: dict[str, Any]) -> LinkDefaults:
    keys = (
        "write_terminator",
        "read_terminator",
        "timeout_s",
        "delay_ms",
        "command_delay_ms",
        "lan_port",
        "baud",
        "data_bits",
        "parity",
        "stop_bits",
        "flow_control",
        "remote",
        "local",
        "locks_panel",
        "first",
        "needs_remote",
        "device_clear",
        "select",
        "init",
        "single_client",
        "ack",
    )
    c.unknown(raw, keys, "link")
    init_raw = raw.get("init", [])
    if not isinstance(init_raw, list) or not all(isinstance(s, str) and s for s in init_raw):
        c.fail("link.init", "must be a list of commands.")
        init_raw = []
    for command in init_raw:
        if _UNSAFE_INIT.search(command):
            c.fail("link.init", f"'{command}' could change an output; init commands may not.")
    select = c.string(raw, "select", None, "link")
    if select is not None:
        c.template(select, {"ch"}, "link.select")
        if "{ch}" not in select and "{ch0}" not in select:
            c.fail("link.select", "must contain {ch} or {ch0}.")
    local_raw = raw.get("local", [])
    if isinstance(local_raw, str):
        local_raw = [local_raw]
    if not isinstance(local_raw, list) or not all(isinstance(s, str) and s for s in local_raw):
        c.fail("link.local", "must be a command or a list of commands.")
        local_raw = []
    for command in local_raw:
        c.template(command, set(), "link.local")
    for key in ("remote", "first"):
        if (value := raw.get(key)) is not None and isinstance(value, str):
            c.template(value, set(), f"link.{key}")
            if key == "first" and _UNSAFE_INIT.search(value):
                c.fail("link.first", f"'{value}' could change an output.")
    timeout = c.number(raw, "timeout_s", 2.0, "link")
    if timeout is not None and timeout <= 0:
        c.fail("link.timeout_s", "must be positive.")
    return LinkDefaults(
        write_terminator=raw.get("write_terminator", "\n")
        if isinstance(raw.get("write_terminator", "\n"), str)
        else "\n",
        read_terminator=raw.get("read_terminator", "\n")
        if isinstance(raw.get("read_terminator", "\n"), str)
        else "\n",
        timeout_s=timeout or 2.0,
        delay_ms=c.integer(raw, "delay_ms", 0, "link"),
        command_delay_ms=c.integer(raw, "command_delay_ms", 0, "link"),
        lan_port=c.integer(raw, "lan_port", 5025, "link", low=1),
        baud=c.integer(raw, "baud", 9600, "link", low=50),
        data_bits=c.integer(raw, "data_bits", 8, "link", low=5),
        parity=c.choice(raw, "parity", _PARITIES, "none", "link"),
        stop_bits=c.number(raw, "stop_bits", 1.0, "link") or 1.0,
        flow_control=c.choice(raw, "flow_control", _FLOW, "none", "link"),
        remote=c.string(raw, "remote", None, "link"),
        local=tuple(local_raw),
        locks_panel=c.boolean(raw, "locks_panel", False, "link"),
        first=c.string(raw, "first", None, "link"),
        needs_remote=c.boolean(raw, "needs_remote", False, "link"),
        device_clear=c.boolean(raw, "device_clear", True, "link"),
        select=select,
        init=tuple(init_raw),
        single_client=c.boolean(raw, "single_client", False, "link"),
        ack=c.string(raw, "ack", None, "link"),
    )


def _errors(c: _Check, raw: Any) -> Errors | None:
    if raw is None:
        return None
    raw = c.table(raw, "errors")
    c.unknown(raw, ("query", "ok", "drain"), "errors")
    query = c.required(raw, "query", "errors")
    ok_text = c.string(raw, "ok", r"^\s*[+-]?0\b", "errors") or r"^\s*[+-]?0\b"
    try:
        ok = re.compile(ok_text)
    except re.error as e:
        c.fail("errors.ok", f"is not a valid regular expression ({e}).")
        ok = re.compile(r"^\s*[+-]?0\b")
    return Errors(query=query, ok=ok, drain=c.integer(raw, "drain", 20, "errors", low=1))


def _range(c: _Check, raw: Any, where: str) -> Range:
    raw = c.table(raw, where)
    c.unknown(raw, ("min", "max", "digits", "step", "min_query", "max_query"), where)
    low = c.number(raw, "min", 0.0, where)
    high = c.number(raw, "max", None, where)
    if low is not None and high is not None and high <= low:
        c.fail(where, "max must be above min.")
    step = c.number(raw, "step", None, where)
    if step is not None and step <= 0:
        c.fail(f"{where}.step", "must be positive.")
    min_query = c.string(raw, "min_query", None, where)
    max_query = c.string(raw, "max_query", None, where)
    for key, query in (("min_query", min_query), ("max_query", max_query)):
        if query is not None:
            c.template(query, {"ch"}, f"{where}.{key}")
    if high is None and max_query is None:
        c.fail(where, "needs max or max_query.")
    return Range(
        min=None if min_query else low,
        max=high,
        digits=c.integer(raw, "digits", 3, where),
        step=step,
        min_query=min_query,
        max_query=max_query,
    )


def _ranges(c: _Check, raw: dict[str, Any]) -> tuple[dict[str, Range], tuple[RangeOverride, ...]]:
    c.unknown(raw, (*RANGE_KEYS, "models"), "ranges")
    ranges = {key: _range(c, raw[key], f"ranges.{key}") for key in RANGE_KEYS if key in raw}
    overrides: list[RangeOverride] = []
    entries = raw.get("models", [])
    if not isinstance(entries, list):
        c.fail("ranges.models", "must be a list of tables.")
        entries = []
    for i, entry in enumerate(entries):
        where = f"ranges.models[{i}]"
        entry = c.table(entry, where)
        c.unknown(entry, ("model", "channel", *RANGE_KEYS), where)
        channel = entry.get("channel")
        if channel is not None and (
            isinstance(channel, bool) or not isinstance(channel, int) or channel < 1
        ):
            c.fail(f"{where}.channel", "must be a channel number from 1.")
            channel = None
        overrides.append(
            RangeOverride(
                model=c.regex(entry, "model", where),
                channel=channel,
                ranges={
                    key: _range(c, entry[key], f"{where}.{key}")
                    for key in RANGE_KEYS
                    if key in entry
                },
            )
        )
    return ranges, tuple(overrides)


def _parse_options(
    c: _Check, raw: dict[str, Any], where: str, labels: tuple[str, ...] | None
) -> dict[str, Any]:
    """The parser settings shared by readers and flags, checked."""
    parse = c.choice(raw, "parse", PARSES, "float", where)
    mapping = raw.get("map", {})
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    ):
        c.fail(f"{where}.map", "must be a table of text to label.")
        mapping = {}
    if parse == "map" and not mapping:
        c.fail(f"{where}.map", 'missing: parse = "map" needs it.')
    if labels is not None:
        for label in mapping.values():
            if label not in labels:
                c.fail(f"{where}.map", f"'{label}' is not one of {', '.join(labels)}.")
    mask = _per_channel_ints(c, raw, "mask", where)
    if parse == "bits" and not any(mask):
        c.fail(f"{where}.mask", 'missing: parse = "bits" needs it.')
    shift = _per_channel_ints(c, raw, "shift", where)
    if len(mask) > 1 and len(shift) > 1 and len(mask) != len(shift):
        c.fail(where, "mask and shift lists must have one entry per channel.")
    index = raw.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
        c.fail(f"{where}.index", "must be a whole number from 0.")
        index = None
    words: dict[str, str] = {}
    for key, label in mapping.items():
        words.setdefault(label, key.strip())
    scale = c.number(raw, "scale", 1.0, where) or 1.0
    if scale == 0 or (scale != 1.0 and parse not in ("float", "floats")):
        c.fail(f"{where}.scale", 'must be a non-zero number, for parse = "float" or "floats".')
        scale = 1.0
    return {
        "parse": parse,
        "map": {k.strip().strip('"').casefold(): v for k, v in mapping.items()},
        "words": words,
        "mask": mask,
        "shift": shift,
        "hex": c.choice(raw, "format", ("int", "hex"), "int", where) == "hex",
        "index": index,
        "scale": scale,
    }


def _per_channel_ints(c: _Check, raw: dict[str, Any], key: str, where: str) -> tuple[int, ...]:
    """A whole number, or a list with one per channel."""
    value = raw.get(key, 0)
    values = value if isinstance(value, list) else [value]
    if not values or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        c.fail(f"{where}.{key}", "must be a whole number, or a list with one per channel.")
        return (0,)
    return tuple(values)


def _readers(
    c: _Check, raw: dict[str, Any], section: str, units: dict[str, str | None]
) -> tuple[Reader, ...]:
    readers: list[Reader] = []
    for key, entry in raw.items():
        where = f"{section}.{key}"
        entry = c.table(entry, where)
        c.unknown(
            entry,
            (
                "query",
                "parse",
                "fields",
                "index",
                "map",
                "mask",
                "shift",
                "format",
                "flags",
                "optional",
                "channel",
                "scale",
            ),
            where,
        )
        if "flags" in entry:
            if key != "protection":
                c.fail(where, "flags are only for protection.")
            flags = _flags(c, entry.get("flags"), where)
            readers.append(
                Reader(
                    key,
                    None,
                    "map",
                    (key,),
                    None,
                    {},
                    {},
                    (0,),
                    (0,),
                    False,
                    c.boolean(entry, "optional", False, where),
                    c.boolean(entry, "channel", True, where),
                    flags,
                )
            )
            continue
        query = c.required(entry, "query", where)
        c.template(query, {"ch"}, f"{where}.query")
        fields_raw = entry.get("fields")
        labels = ENUM_FIELDS.get(key)
        options = _parse_options(c, entry, where, labels)
        if fields_raw is None:
            fields = (key,)
            if options["parse"] == "floats":
                c.fail(f"{where}.fields", 'missing: parse = "floats" needs it.')
        else:
            if not isinstance(fields_raw, list) or not all(isinstance(f, str) for f in fields_raw):
                c.fail(f"{where}.fields", "must be a list of field names.")
                fields_raw = []
            if options["parse"] != "floats":
                c.fail(f"{where}.fields", 'only parse = "floats" fills several fields.')
            fields = tuple(fields_raw)
        for name in fields:
            if name not in units:
                c.fail(
                    where, f"{name} is not a {section} field of this category: {', '.join(units)}."
                )
            if name in ENUM_FIELDS and options["parse"] not in ("map", "bits"):
                c.fail(where, f'{name} needs parse = "map" or "bits".')
            if name in BOOL_FIELDS and options["parse"] not in ("bool", "bits"):
                c.fail(where, f'{name} needs parse = "bool" or "bits".')
            if (
                name not in ENUM_FIELDS
                and name not in BOOL_FIELDS
                and options["parse"] not in ("float", "floats", "int")
            ):
                c.fail(where, f'{name} is a number; use parse = "float", "floats" or "int".')
        readers.append(
            Reader(
                name=key,
                query=query,
                parse=options["parse"],
                fields=fields,
                index=options["index"],
                map=options["map"],
                words=options["words"],
                mask=options["mask"],
                shift=options["shift"],
                hex=options["hex"],
                optional=c.boolean(entry, "optional", False, where),
                per_channel=c.boolean(entry, "channel", True, where),
                flags=(),
                scale=options["scale"],
            )
        )
    return tuple(readers)


def _flags(c: _Check, raw: Any, where: str) -> tuple[Flag, ...]:
    if not isinstance(raw, list) or not raw:
        c.fail(f"{where}.flags", "must be a non-empty list of tables.")
        return ()
    flags: list[Flag] = []
    for i, entry in enumerate(raw):
        at = f"{where}.flags[{i}]"
        entry = c.table(entry, at)
        c.unknown(entry, ("query", "parse", "value", "map", "mask", "shift", "format", "index"), at)
        query = c.required(entry, "query", at)
        c.template(query, {"ch"}, f"{at}.query")
        value = c.choice(entry, "value", PROTECTION_LABELS[1:], "other", at)
        options = _parse_options(c, entry, at, None)
        if options["parse"] not in ("bool", "bits"):
            c.fail(f"{at}.parse", "must be bool or bits.")
        flags.append(
            Flag(
                query,
                options["parse"],
                value,
                options["map"],
                options["mask"],
                options["shift"],
                options["hex"],
                options["index"],
            )
        )
    return tuple(flags)


def _setters(
    c: _Check,
    raw: dict[str, Any],
    category: str,
    ranges: dict[str, Range],
    overrides: tuple[RangeOverride, ...],
) -> dict[str, Setter]:
    setters: dict[str, Setter] = {}
    allowed = SETTERS[category]
    for key, entry in raw.items():
        where = f"setters.{key}"
        if key not in allowed:
            c.fail(where, f"is not a setter of this category: {', '.join(allowed)}.")
            continue
        entry = c.table(entry, where)
        c.unknown(entry, ("command", "verify", "range", "map", "channel", "scale", "master"), where)
        raw_command = entry.get("command")
        if key == "clear" and isinstance(raw_command, list):
            if not raw_command or not all(isinstance(x, str) and x for x in raw_command):
                c.fail(f"{where}.command", "must be a command or a list of commands.")
                raw_command = []
            commands = tuple(raw_command)
            command = commands[0] if commands else ""
        else:
            command = c.required(entry, "command", where)
            commands = (command,)
        verify = c.string(entry, "verify", None, where)
        if verify is not None:
            c.template(verify, {"ch"}, f"{where}.verify")
        mapping = entry.get("map", {})
        if not isinstance(mapping, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
        ):
            c.fail(f"{where}.map", "must be a table of label to instrument word.")
            mapping = {}
        if key in VALUE_SETTERS:
            c.template(command, {"ch", "value"}, f"{where}.command")
            if "{value}" not in command:
                c.fail(f"{where}.command", "must contain {value}.")
        elif key in SWITCH_SETTERS:
            c.template(command, {"ch", "on", "bit"}, f"{where}.command")
            if "{on}" not in command and "{bit}" not in command:
                c.fail(f"{where}.command", "must contain {on} or {bit}.")
        elif key in LABEL_SETTERS:
            c.template(command, {"ch", "value"}, f"{where}.command")
            if "{value}" not in command:
                c.fail(f"{where}.command", "must contain {value}, the instrument's word.")
            labels = ENUM_FIELDS[VERIFY_FIELD[key]]
            if not mapping:
                c.fail(f"{where}.map", f"missing: {key} needs a map of label to instrument word.")
            for label in mapping:
                if label not in labels:
                    c.fail(f"{where}.map", f"'{label}' is not one of {', '.join(labels)}.")
        elif key == "function":
            c.template(command, {"ch", "function"}, f"{where}.command")
            if "{function}" not in command:
                c.fail(f"{where}.command", "must contain {function}.")
            labels = MODE_LABELS if category == "load" else FUNCTION_LABELS
            if not mapping:
                c.fail(f"{where}.map", "missing: function needs a map of label to instrument word.")
            for label in mapping:
                if label not in labels:
                    c.fail(f"{where}.map", f"'{label}' is not one of {', '.join(labels)}.")
        else:
            for one in commands:
                c.template(one, {"ch"}, f"{where}.command")
        range_key = c.string(entry, "range", None, where)
        if range_key is not None and range_key not in RANGE_KEYS:
            c.fail(f"{where}.range", f"must be one of {', '.join(RANGE_KEYS)}.")
        scale = c.number(entry, "scale", 1.0, where) or 1.0
        if scale == 0 or (scale != 1.0 and key not in VALUE_SETTERS):
            c.fail(f"{where}.scale", "must be a non-zero number, on a value setter.")
            scale = 1.0
        setters[key] = Setter(
            name=key,
            command=command,
            commands=commands,
            verify=verify,
            range=range_key,
            map=mapping,
            per_channel=c.boolean(entry, "channel", True, where),
            scale=scale,
            master=_master(c, entry, key, where),
        )
    return setters


def _master(c: _Check, entry: dict[str, Any], key: str, where: str) -> Master | None:
    raw = entry.get("master")
    if raw is None:
        return None
    where = f"{where}.master"
    if key != "output":
        c.fail(where, "is only for the output setter.")
        return None
    raw = c.table(raw, where)
    c.unknown(raw, ("command", "query", "armed"), where)
    command = c.required(raw, "command", where)
    c.template(command, {"on", "bit"}, f"{where}.command")
    if "{on}" not in command and "{bit}" not in command:
        c.fail(f"{where}.command", "must contain {on} or {bit}.")
    query = c.required(raw, "query", where)
    c.template(query, set(), f"{where}.query")
    armed = c.required(raw, "armed", where)
    c.template(armed, {"ch"}, f"{where}.armed")
    return Master(command, query, armed)


def _dmm(c: _Check, raw: Any, category: str) -> Dmm | None:
    if raw is None:
        if category == "dmm":
            c.fail("dmm", "missing: a DMM profile lists its functions.")
        return None
    if category != "dmm":
        c.fail("dmm", "is only for the dmm category.")
        return None
    raw = c.table(raw, "dmm")
    c.unknown(raw, ("function_query", "function_map", "functions"), "dmm")
    function_query = c.string(raw, "function_query", None, "dmm")
    if function_query is not None:
        c.template(function_query, set(), "dmm.function_query")
    mapping = raw.get("function_map", {})
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    ):
        c.fail("dmm.function_map", "must be a table of reply text to function label.")
        mapping = {}
    functions = raw.get("functions", {})
    if not isinstance(functions, dict) or not functions:
        c.fail("dmm.functions", "must be a non-empty table of function label to unit.")
        functions = {}
    for label in (*mapping.values(), *functions):
        if label not in FUNCTION_LABELS:
            c.fail("dmm", f"'{label}' is not one of {', '.join(FUNCTION_LABELS)}.")
    for label, unit in functions.items():
        if unit is not None and not isinstance(unit, str):
            c.fail(f"dmm.functions.{label}", "must be a unit, as text.")
    if function_query is not None and not mapping:
        c.fail("dmm.function_map", "missing: function_query needs it.")
    words: dict[str, str] = {}
    for key, label in mapping.items():
        words.setdefault(label, key.strip())
    return Dmm(
        function_query=function_query,
        function_map={k.strip().strip('"').casefold(): v for k, v in mapping.items()},
        words=words,
        functions={k: (v or None) for k, v in functions.items()},
    )


def check_paths(paths: Iterable[Path | str]) -> Iterator[tuple[Path, list[str]]]:
    """Each profile file under the paths, with its problems (empty when it is fine)."""
    for given in paths:
        given = Path(given)
        files = sorted(given.rglob("*.toml")) if given.is_dir() else [given]
        for file in files:
            try:
                load_file(file)
            except ProfileError as e:
                yield file, e.problems
            else:
                yield file, []


def main(argv: list[str] | None = None) -> int:
    """`python -m zelos_extension_instruments.profile check <path>...`"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0] != "check":
        print("usage: python -m zelos_extension_instruments.profile check <file or directory>...")
        return 2
    failed = 0
    count = 0
    for file, problems in check_paths(argv[1:]):
        count += 1
        if problems:
            failed += 1
            print(f"{file}:")
            for problem in problems:
                print(f"  {problem}")
    print(f"{count - failed} of {count} profiles OK.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
