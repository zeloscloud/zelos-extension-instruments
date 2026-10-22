"""The data-acquisition part of the profile vocabulary and format.

A daq unit scans the channels the configuration names: each scan channel reads one function, and
a relay channel is open or closed.
"""

import dataclasses
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .profile_check import Check

# A scan channel's reading, named after its function.
DAQ_SIGNAL_FIELDS: dict[str, str | None] = {
    "dc_voltage": "V",
    "ac_voltage": "V",
    "dc_current": "A",
    "ac_current": "A",
    "resistance": "Ohm",
    "resistance_4w": "Ohm",
    "frequency": "Hz",
    "period": "s",
    "temperature": "degC",
}
# A relay channel's state.
DAQ_STATE_FIELDS: dict[str, str | None] = {"closed": None}


@dataclass(frozen=True, slots=True)
class Nplc:
    """The command that sets a group of channels' integration time, and the functions that
    take one."""

    command: str
    # Function label to the instrument's word for it, which fills {function}.
    map: dict[str, str]


@dataclass(frozen=True, slots=True)
class Daq:
    """How the unit is told what to scan, and how one scan is read."""

    # Function label to the commands that set a group of channels to it.
    configure: dict[str, tuple[str, ...]]
    nplc: Nplc | None
    scan: tuple[str, ...]
    # Commands, the last a query whose reply is the readings in scan order.
    read: tuple[str, ...]
    channel_pattern: re.Pattern[str]


def load_daq(c: "Check", raw: Any, category: str) -> Daq | None:
    """The `[daq]` table of a profile, checked."""
    if raw is None:
        if category == "daq":
            c.fail("daq", "missing: a daq profile describes how a scan is set up and read.")
        return None
    if category != "daq":
        c.fail("daq", "is only for the daq category.")
        return None
    raw = c.table(raw, "daq")
    c.unknown(raw, [f.name for f in dataclasses.fields(Daq)], "daq")
    configure = _configure(c, raw.get("configure"))
    nplc = _nplc(c, raw.get("nplc"), configure)
    scan = c.commands(raw, "scan", "daq", required=True)
    for command in scan:
        c.template(command, {"channels", "count"}, "daq.scan")
    if scan and not any("{channels}" in x for x in scan):
        c.fail("daq.scan", "must contain {channels} in one command.")
    read = c.commands(raw, "read", "daq", required=True)
    for command in read:
        c.template(command, {"count"}, "daq.read")
    if read and not _is_query(read[-1]):
        c.fail("daq.read", "must end with the query that returns the readings.")
    if any(_is_query(x) for x in read[:-1]):
        c.fail("daq.read", "may hold only one query, the last.")
    channel_pattern = c.regex(raw, "channel_pattern", "daq")
    return Daq(
        configure=configure,
        nplc=nplc,
        scan=scan,
        read=read,
        channel_pattern=channel_pattern,
    )


def _configure(c: "Check", raw: Any) -> dict[str, tuple[str, ...]]:
    where = "daq.configure"
    if raw is None:
        c.fail(where, "missing: name the commands for each function the unit reads.")
        return {}
    if not isinstance(raw, dict) or not raw:
        c.fail(where, "must be a table that names at least one function.")
        return {}
    c.one_of(raw, DAQ_SIGNAL_FIELDS, where)
    configure: dict[str, tuple[str, ...]] = {}
    for function in raw:
        if function not in DAQ_SIGNAL_FIELDS:
            continue
        at = f"{where}.{function}"
        commands = c.commands(raw, function, where, required=True)
        thermocouple = function == "temperature"
        for command in commands:
            c.template(command, {"channels", "tc"} if thermocouple else {"channels"}, at)
            if "{channels}" not in command:
                c.fail(at, "must contain {channels} in every command.")
        if thermocouple and commands and not any("{tc}" in x for x in commands):
            c.fail(at, "must contain {tc}, the thermocouple type, in one command.")
        configure[function] = commands
    return configure


def _nplc(c: "Check", raw: Any, configure: dict[str, tuple[str, ...]]) -> Nplc | None:
    if raw is None:
        return None
    where = "daq.nplc"
    raw = c.table(raw, where)
    c.unknown(raw, [f.name for f in dataclasses.fields(Nplc)], where)
    command = c.required(raw, "command", where)
    if command:
        c.template(command, {"channels", "value", "function"}, f"{where}.command")
        if "{channels}" not in command or "{value}" not in command:
            c.fail(f"{where}.command", "must contain {channels} and {value}.")
    words = c.text_table(raw, "map", where, "function to instrument word")
    if not words:
        c.fail(f"{where}.map", "must name each function that takes an integration time.")
    for function in words:
        if function not in configure:
            c.fail(f"{where}.map", f"'{function}' is not a function daq.configure sets.")
    return Nplc(command=command, map=words)


def _is_query(command: str) -> bool:
    words = command.split()
    return bool(words) and words[0].endswith("?")
