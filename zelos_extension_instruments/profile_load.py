"""Profile files: read from TOML, checked, and turned into Profiles.

`python -m zelos_extension_instruments.profile_load check <file or directory>...` checks files.
"""

import dataclasses
import functools
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar, get_args

from .link import FlowControl, Parity
from .profile import (
    BADGES,
    BOOL_FIELDS,
    CATEGORIES,
    ENUM_FIELDS,
    FUNCTION_LABELS,
    INSTRUMENT_FIELDS,
    LABEL_SETTERS,
    MODE_LABELS,
    PARSES,
    PROTECTION_LABELS,
    RANGE_KEYS,
    SETTERS,
    SIGNAL_FIELDS,
    STATE_FIELDS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    VERIFY_FIELD,
    Badge,
    Category,
    Dmm,
    Errors,
    Evidence,
    Flag,
    LinkDefaults,
    Master,
    Match,
    ModelChannels,
    Profile,
    Range,
    RangeOverride,
    Reader,
    Setter,
)
from .profile_scope import (
    load_scope,
)

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "profiles"


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


class ProfileError(Exception):
    """A profile file that cannot be used; one sentence per problem, each naming the key."""

    def __init__(self, path: Path | str, problems: list[str]) -> None:
        self.path = Path(path)
        self.problems = problems
        super().__init__(f"{self.path.name}: " + " ".join(problems))


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


@functools.cache
def shipped() -> list[Profile]:
    """The profiles that ship with the extension, read once: every search and list asks."""
    return load_dir(SHIPPED_DIR)


def _at(where: str, key: str) -> str:
    """A key's place in the profile, `match.model`, or the key alone at the top level."""
    return f"{where}.{key}" if where else key


_Choice = TypeVar("_Choice", bound=str)


class Check:
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
            self.fail(_at(where, key), "must be non-empty text.")
            return default
        return value

    def required(self, raw: dict[str, Any], key: str, where: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            self.fail(_at(where, key), "missing or empty.")
            return ""
        return value

    def number(
        self, raw: dict[str, Any], key: str, default: float | None, where: str
    ) -> float | None:
        value = raw.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.fail(_at(where, key), "must be a number.")
            return default
        return float(value)

    def integer(self, raw: dict[str, Any], key: str, default: int, where: str, low: int = 0) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            self.fail(_at(where, key), f"must be a whole number of at least {low}.")
            return default
        return value

    def boolean(self, raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
        value = raw.get(key, default)
        if not isinstance(value, bool):
            self.fail(_at(where, key), "must be true or false.")
            return default
        return value

    def choice(
        self,
        raw: dict[str, Any],
        key: str,
        choices: tuple[_Choice, ...],
        default: _Choice,
        where: str,
    ) -> _Choice:
        value = raw.get(key, default)
        if value not in choices:
            self.fail(_at(where, key), f"must be one of {', '.join(choices)}.")
            return default
        return value

    def regex(self, raw: dict[str, Any], key: str, where: str) -> re.Pattern[str]:
        text = self.required(raw, key, where)
        try:
            return re.compile(text)
        except re.error as e:
            self.fail(_at(where, key), f"is not a valid regular expression ({e}).")
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
    c = Check(path)
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
    category: Category = c.choice(raw, "category", CATEGORIES, "psu", "")
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
        category=category,
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


def _evidence(c: Check, raw: dict[str, Any]) -> Evidence:
    c.unknown(raw, ("badge", "source", "url", "firmware", "date"), "evidence")
    badge: Badge = c.choice(raw, "badge", BADGES, "manual", "evidence")
    source = c.required(raw, "source", "evidence")
    date = c.required(raw, "date", "evidence")
    if date and not _DATE.fullmatch(date):
        c.fail("evidence.date", "must be YYYY-MM-DD.")
    return Evidence(
        badge=badge,
        source=source,
        url=c.string(raw, "url", None, "evidence"),
        firmware=c.string(raw, "firmware", None, "evidence"),
        date=date,
    )


def _match(c: Check, raw: dict[str, Any], generic: bool) -> Match:
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


def _link(c: Check, raw: dict[str, Any]) -> LinkDefaults:
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
        parity=c.choice(raw, "parity", get_args(Parity), "none", "link"),
        stop_bits=c.number(raw, "stop_bits", 1.0, "link") or 1.0,
        flow_control=c.choice(raw, "flow_control", get_args(FlowControl), "none", "link"),
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


def _errors(c: Check, raw: Any) -> Errors | None:
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


def _range(c: Check, raw: Any, where: str) -> Range:
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


def _ranges(c: Check, raw: dict[str, Any]) -> tuple[dict[str, Range], tuple[RangeOverride, ...]]:
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
    c: Check, raw: dict[str, Any], where: str, labels: tuple[str, ...] | None
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
    if parse == "bool" and any(v not in ("on", "off") for v in mapping.values()):
        c.fail(f"{where}.map", 'must map each word to "on" or "off" for parse = "bool".')
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


def _per_channel_ints(c: Check, raw: dict[str, Any], key: str, where: str) -> tuple[int, ...]:
    """A whole number, or a list with one per channel."""
    value = raw.get(key, 0)
    values = value if isinstance(value, list) else [value]
    if not values or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        c.fail(f"{where}.{key}", "must be a whole number, or a list with one per channel.")
        return (0,)
    return tuple(values)


def _readers(
    c: Check, raw: dict[str, Any], section: str, units: dict[str, str | None]
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


def _flags(c: Check, raw: Any, where: str) -> tuple[Flag, ...]:
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
    c: Check,
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


def _master(c: Check, entry: dict[str, Any], key: str, where: str) -> Master | None:
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


def _dmm(c: Check, raw: Any, category: str) -> Dmm | None:
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
    """`python -m zelos_extension_instruments.profile_load check <path>...`"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0] != "check":
        print(
            "usage: python -m zelos_extension_instruments.profile_load check <file or directory>..."
        )
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
