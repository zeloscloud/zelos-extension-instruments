"""Profile files: read from TOML, checked, and turned into Profiles.

`python -m zelos_extension_instruments.profile_load check <file or directory>...` checks files.
"""

import dataclasses
import functools
import math
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, get_args

from .config import Connection
from .link import FlowControl, Parity
from .profile import (
    BADGES,
    CATEGORIES,
    FUNCTION_LABELS,
    INSTRUMENT_FIELDS,
    RANGE_KEYS,
    SIGNAL_FIELDS,
    STATE_FIELDS,
    Badge,
    Category,
    Dmm,
    Errors,
    Evidence,
    LinkDefaults,
    Match,
    ModelChannels,
    Profile,
    Range,
    RangeOverride,
)
from .profile_check import Check, section_keys
from .profile_daq import load_daq
from .profile_scope import (
    load_scope,
)
from .profile_signals import load_readers, load_setters
from .profile_system import load_system, system_commands

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
_CONNECTIONS: tuple[Connection, ...] = ("lan", "usb", "serial", "visa")
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


def _head(command: str) -> str:
    """A command's header, as the instrument would read it: the first word, case-folded."""
    return command.strip().split()[0].casefold() if command.strip() else ""


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
            "system",
            "daq",
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
    signals = load_readers(c, c.table(raw.get("signals"), "signals"), "signals", signal_units)
    state = load_readers(
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
    setters = load_setters(c, c.table(raw.get("setters"), "setters"), category, ranges, overrides)
    system = load_system(c, raw.get("system"))
    heads = {
        _head(cmd) for s in setters.values() for cmd in (*s.commands, s.off) if cmd is not None
    }
    system_heads = {_head(cmd) for cmd in system_commands(system)}
    for key, commands in (("init", link.init), ("first", (link.first,))):
        for command in commands:
            if command is None or _UNSAFE_INIT.search(command):
                continue
            if _head(command) in heads:
                c.fail(
                    f"link.{key}", f"'{command}' is a setter's command; it may not run at connect."
                )
            elif _head(command) in system_heads:
                c.fail(
                    f"link.{key}", f"'{command}' is a system command; it may not run at connect."
                )
    dmm = _dmm(c, raw.get("dmm"), category)
    scope = load_scope(c, raw.get("scope"), category)
    daq = load_daq(c, raw.get("daq"), category)
    if category == "dmm" and not any("value" in r.fields for r in signals):
        c.fail("signals.value", "missing: a DMM profile reads value.")
    # A scope's readings come from its slots and a daq's from its scan; a generator reads none.
    if not signals and not generic and category not in ("scope", "daq") and signal_units:
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
        system=system,
        daq=daq,
        path=path,
    )


def _evidence(c: Check, raw: dict[str, Any]) -> Evidence:
    c.unknown(raw, section_keys(Evidence), "evidence")
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
        raw,
        ("manufacturer", "model", "idn", "channels", "channels_query", "models", "connections"),
        "match",
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
        connections=_connections(c, raw),
    )


def _connections(c: Check, raw: dict[str, Any]) -> tuple[Connection, ...]:
    value = raw.get("connections", [])
    if not isinstance(value, list) or not value and "connections" in raw:
        c.fail("match.connections", "must be a non-empty list of lan, usb, serial and visa.")
        return ()
    found: list[Connection] = []
    for item in value:
        if item not in _CONNECTIONS:
            c.fail("match.connections", f"'{item}' is not one of lan, usb, serial and visa.")
        elif item not in found:
            found.append(item)
    return tuple(found)


def _link(c: Check, raw: dict[str, Any]) -> LinkDefaults:
    c.unknown(raw, section_keys(LinkDefaults), "link")
    init_raw = c.commands(raw, "init", "link")
    for command in init_raw:
        if _UNSAFE_INIT.search(command):
            c.fail("link.init", f"'{command}' could change an output; init commands may not.")
    select = c.string(raw, "select", None, "link")
    if select is not None:
        c.template(select, {"ch"}, "link.select")
        if "{ch}" not in select and "{ch0}" not in select:
            c.fail("link.select", "must contain {ch} or {ch0}.")
    local_raw = c.commands(raw, "local", "link")
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
    remote_check: tuple[str, str] | None = None
    if (check_raw := raw.get("remote_check")) is not None:
        where = "link.remote_check"
        check_raw = c.table(check_raw, where)
        c.unknown(check_raw, ("query", "expect"), where)
        query = c.required(check_raw, "query", where)
        c.template(query, set(), f"{where}.query")
        if query and "?" not in query:
            c.fail(f"{where}.query", "must be a query, with ?.")
        remote_check = (query, c.required(check_raw, "expect", where))
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
        remote_check=remote_check,
        skip_blank=c.boolean(raw, "skip_blank", False, "link"),
    )


def _errors(c: Check, raw: Any) -> Errors | None:
    if raw is None:
        return None
    raw = c.table(raw, "errors")
    c.unknown(raw, ("query", "ok", "drain", "bare"), "errors")
    if isinstance(raw.get("query"), list):
        queries = c.commands(raw, "query", "errors", required=True)
    else:
        queries = (c.required(raw, "query", "errors"),)
    for query in queries:
        c.template(query, set(), "errors.query")
    no_error = r"^\s*[+-]?0\b"
    ok = c.pattern(c.string(raw, "ok", no_error, "errors") or no_error, "errors.ok", no_error)
    return Errors(
        query=queries[0] if queries else "",
        ok=ok,
        drain=c.integer(raw, "drain", 20, "errors", low=1),
        then=tuple(queries[1:]),
        bare=c.boolean(raw, "bare", False, "errors"),
    )


def _range(c: Check, raw: Any, where: str) -> Range:
    raw = c.table(raw, where)
    c.unknown(raw, section_keys(Range), where)
    if "choices" in raw:
        return _choices(c, raw, where)
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


def _choices(c: Check, raw: dict[str, Any], where: str) -> Range:
    """A range of fixed values: a setter sends one of them and nothing else is checked."""
    for key in ("min", "max", "step", "min_query", "max_query"):
        if key in raw:
            c.fail(f"{where}.{key}", "does not go with choices, which name every value.")
    value = raw["choices"]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        or not all(math.isfinite(v) for v in value)
    ):
        c.fail(f"{where}.choices", "must be a non-empty list of numbers.")
        value = [0.0]
    choices = tuple(sorted({float(v) for v in value}))
    return Range(
        min=choices[0],
        max=choices[-1],
        digits=c.integer(raw, "digits", 3, where),
        step=None,
        min_query=None,
        max_query=None,
        choices=choices,
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
    mapping = c.text_table(raw, "function_map", "dmm", "reply text to function label")
    functions = raw.get("functions", {})
    if not isinstance(functions, dict) or not functions:
        c.fail("dmm.functions", "must be a non-empty table of function label to unit.")
        functions = {}
    c.one_of((*mapping.values(), *functions), FUNCTION_LABELS, "dmm")
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
