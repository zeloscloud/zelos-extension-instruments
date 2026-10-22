"""Executes a profile: renders commands, reads replies, plans the polling.

The engine knows no instrument. Everything it sends and everything it expects back comes from
the profile, so one engine serves every family.
"""

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from .profile import (
    BOOL_FIELDS,
    ENUM_FIELDS,
    MAP_ANY,
    Errors,
    Flag,
    Profile,
    Range,
    Reader,
    Setter,
)

Cadence = Literal["fast", "slow"]

# SCPI's "not a number" and "infinity" sentinels: 9.9E37, 9.91E37, and anything at or past them.
_NAN_FROM = 9.9e37
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_INTEGER = re.compile(r"[+-]?\d+")
_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"0", "off", "false", "no"}


class ParseError(ValueError):
    """A reply the reader cannot read; the message says what was expected and what came."""


class RangeError(ValueError):
    """A value a setter may not send."""


@dataclass(frozen=True, slots=True)
class Step:
    """One query of the poll plan."""

    cadence: Cadence
    channel: int | None
    reader: Reader
    flag: Flag | None = None

    @property
    def query(self) -> str:
        template = self.flag.query if self.flag is not None else self.reader.query
        assert template is not None
        return render(template, ch=self.channel)

    @property
    def key(self) -> str:
        """Identifies the step in a dropped-set: reader name, flag index and channel."""
        flag = "" if self.flag is None else f"#{self.reader.flags.index(self.flag)}"
        return f"{self.reader.name}{flag}@{self.channel}"


@dataclass(frozen=True, slots=True)
class Plan:
    """The queries a worker runs, by cadence, in order."""

    fast: tuple[Step, ...]
    slow: tuple[Step, ...]

    def __iter__(self):
        yield from self.fast
        yield from self.slow


def compile_plan(
    profile: Profile, channels: int, dropped: frozenset[str] | set[str] = frozenset()
) -> Plan:
    """Every step for `channels` channels, minus the keys in `dropped`."""
    # Read with every reading, so a knob turned at the bench never mislabels one.
    fast_state = tuple(
        r for r in profile.state if profile.category == "dmm" and "function" in r.fields
    )
    slow_state = tuple(r for r in profile.state if r not in fast_state)
    fast = _steps("fast", (*fast_state, *profile.signals), channels, dropped)
    slow = _steps("slow", slow_state, channels, dropped)
    return Plan(tuple(fast), tuple(slow))


def _steps(
    cadence: Cadence, readers: tuple[Reader, ...], channels: int, dropped: frozenset[str] | set[str]
) -> list[Step]:
    steps: list[Step] = []
    for reader in readers:
        for channel in range(1, channels + 1) if reader.per_channel else [None]:
            if reader.flags:
                for flag in reader.flags:
                    step = Step(cadence, channel, reader, flag)
                    if step.key not in dropped:
                        steps.append(step)
            else:
                step = Step(cadence, channel, reader)
                if step.key not in dropped:
                    steps.append(step)
    return steps


def render(
    template: str,
    *,
    ch: int | None = None,
    value: str | None = None,
    on: bool | None = None,
    function: str | None = None,
    slot: int | None = None,
    type: str | None = None,
    points: int | None = None,
    interval: int | None = None,
    text: str | None = None,
    src: int | None = None,
    modulation: str | None = None,
    channels: str | None = None,
    tc: str | None = None,
    count: int | None = None,
) -> str:
    """Fill a command template's placeholders."""
    values: dict[str, Any] = {}
    if ch is not None:
        values["ch"] = ch
        values["ch0"] = ch - 1
    for key, extra in (
        ("slot", slot),
        ("type", type),
        ("points", points),
        ("interval", interval),
        ("text", text),
        ("src", src),
        ("modulation", modulation),
        ("channels", channels),
        ("tc", tc),
        ("count", count),
    ):
        if extra is not None:
            values[key] = extra
    if value is not None:
        values["value"] = value
    if on is not None:
        values["on"] = "ON" if on else "OFF"
        values["bit"] = "1" if on else "0"
    if function is not None:
        values["function"] = function
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"cannot render '{template}': missing {e}") from None


_METRIC = {"k": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12}
_UNITS = frozenset(
    {"V", "A", "W", "Hz", "Sa/s", "S", "s", "Ohm", "F", "H", "V/div", "s/div", "A/div"}
)
_UNIT = re.compile(r"[A-Za-z%/]+")


def parse_float(text: str) -> float:
    """A number in any SCPI form; the overflow sentinels read as not-a-number."""
    cleaned = text.strip().strip('"')
    found = _NUMBER.search(cleaned)
    if found is None or cleaned.lower().startswith(("0x", "#h")):
        raise ParseError(f"expected a number, got {text.strip()!r}")
    if found.start() != 0:
        # Text before the number is a header (`C1:VDIV 5.00E-01V`), never part of the value.
        raise ParseError(f"expected a number, got {text.strip()!r}")
    number = float(found.group())
    suffix = cleaned[found.end() :].strip()
    if suffix and not _UNIT.fullmatch(suffix):
        raise ParseError(f"expected a number, got {text.strip()!r}")
    if suffix in _METRIC or (suffix[:1] in _METRIC and suffix[1:] in _UNITS):
        # A metric prefix, as in 10M, 1.00GSa/s or 100ps; any other suffix is a unit.
        number *= _METRIC[suffix[0]]
    if abs(number) >= _NAN_FROM:
        return math.nan
    return number


def parse_int(text: str, hex: bool = False) -> int:
    cleaned = text.strip().strip('"')
    try:
        if hex or cleaned.lower().startswith(("0x", "#h")):
            return int(cleaned.lower().removeprefix("0x").removeprefix("#h"), 16)
        try:
            return int(cleaned)
        except ValueError:
            number = float(cleaned)
            if not math.isfinite(number):
                raise ValueError from None
            return int(number)
    except (ValueError, OverflowError):
        raise ParseError(f"expected a whole number, got {text.strip()!r}") from None


def parse_bool(text: str, words: dict[str, str] | None = None) -> bool:
    cleaned = text.strip().strip('"').casefold()
    if words:
        cleaned = words.get(cleaned, cleaned)
    if cleaned in _TRUE:
        return True
    if cleaned in _FALSE:
        return False
    raise ParseError(f"expected ON/OFF or 1/0, got {text.strip()!r}")


def _pick(text: str, index: int | None) -> str:
    if index is None:
        return text
    stripped = text.strip()
    if len(stripped) > 1 and stripped[0] == stripped[-1] == '"':
        # A quoted list, such as `"SIN,+5.0E+03,+3.0E+00"`.
        stripped = stripped[1:-1]
    parts = stripped.split(",")
    if index >= len(parts):
        raise ParseError(f"expected at least {index + 1} values, got {text.strip()!r}")
    return parts[index]


# Keyed replies write units in capitals, so `M` is mega before HZ and milli before the rest.
_KEYED_UNITS = ("VPP", "VRMS", "HZ", "DEG", "OHM", "V", "S", "A", "W", "%")
_KEYED_PREFIX = {"": 1.0, "G": 1e9, "K": 1e3, "U": 1e-6, "N": 1e-9, "P": 1e-12}


def _keyed_number(text: str) -> float:
    """A keyed reply's number with its unit: `100HZ`, `5KHZ`, `1MHZ`, `500MV`, `10US`, `-1V`."""
    cleaned = text.strip().strip('"')
    found = _NUMBER.match(cleaned)
    if found is None:
        raise ParseError(f"expected a number, got {text.strip()!r}")
    suffix = cleaned[found.end() :].strip().upper()
    unit = next((u for u in _KEYED_UNITS if suffix.endswith(u)), "")
    prefix = suffix[: len(suffix) - len(unit)]
    if prefix == "M":
        factor = 1e6 if unit == "HZ" else 1e-3
    elif prefix in _KEYED_PREFIX:
        factor = _KEYED_PREFIX[prefix]
    else:
        raise ParseError(f"expected a number, got {text.strip()!r}")
    number = float(found.group())
    if abs(number) >= _NAN_FROM:
        return math.nan
    return number * factor


def _keyed_value(text: str, key: str, until: str | None = None) -> str | None:
    """The token after the first token equal to `key`, ignoring case, in a reply such as
    `C1:BSWV WVTP,SINE,FRQ,100HZ`; a header (up to the first space, when the first token holds a
    colon) is skipped, and so is everything from the first token equal to `until`. None when the
    key is not there."""
    head, space, rest = text.partition(" ")
    body = rest if space and ":" in text.split(",")[0] else text
    tokens = [t.strip().strip('"') for t in body.split(",")]
    if until is not None:
        stop = until.casefold()
        tokens = tokens[: next((i for i, t in enumerate(tokens) if t.casefold() == stop), None)]
    wanted = key.casefold()
    for i, token in enumerate(tokens[:-1]):
        if token.casefold() == wanted:
            return tokens[i + 1]
    return None


def _keyed(reader: Reader, text: str) -> dict[str, Any]:
    """A keyed reply's field. A key the reply lacks (an instrument drops the keys of a mode that
    is off) leaves the field unknown for this read: a number reads as not-a-number, a label as
    its table's last label, and a switch is left out."""
    name = reader.fields[0]
    assert reader.key is not None
    token = _keyed_value(text, reader.key, reader.until)
    if token is None:
        if name in ENUM_FIELDS:
            return {name: unknown_label(name)}
        if name in BOOL_FIELDS:
            return {}
        return {name: math.nan}
    if name in ENUM_FIELDS:
        label = _mapped(reader, token.casefold())
        if label is None:
            raise ParseError(f"'{token}' is not in the map of {reader.name}")
        return {name: label}
    if name in BOOL_FIELDS:
        return {name: parse_bool(token, reader.map)}
    return {name: _keyed_number(token) * reader.scale}


def _per_channel(values: tuple[int, ...], channel: int | None) -> int:
    if len(values) == 1 or channel is None:
        return values[0]
    if channel > len(values):
        raise ParseError(f"no mask or shift for channel {channel}")
    return values[channel - 1]


def _bits(
    text: str, mask: tuple[int, ...], shift: tuple[int, ...], hex: bool, channel: int | None
) -> int:
    return (parse_int(text, hex) & _per_channel(mask, channel)) >> _per_channel(shift, channel)


def parse_reply(reader: Reader, reply: str, channel: int | None = None) -> dict[str, Any]:
    """The fields a reply fills. Enumerated fields hold their label as text. A keyed reply
    that lacks its key leaves the field unknown for this read: see `_keyed`.

    `channel` picks the mask and shift of a `bits` reader that lists one per channel.
    """
    text = reply.strip()
    parse = reader.parse
    if parse == "floats":
        parts = text.split(",")
        if len(parts) < len(reader.fields):
            raise ParseError(f"expected {len(reader.fields)} values, got {text!r}")
        return {
            name: parse_float(part) * reader.scale
            for name, part in zip(reader.fields, parts, strict=False)
        }
    name = reader.fields[0]
    if parse == "float":
        return {name: parse_float(_pick(text, reader.index)) * reader.scale}
    if parse == "int":
        return {name: parse_int(_pick(text, reader.index), reader.hex)}
    if parse == "bool":
        return {name: parse_bool(_pick(text, reader.index), reader.map)}
    if parse == "str":
        return {name: text if reader.index is None else _pick(text, reader.index).strip()}
    if parse == "keyed":
        return _keyed(reader, text)
    if parse == "map":
        key = _pick(text, reader.index).strip().strip('"').casefold()
        label = _mapped(reader, key)
        if label is None:
            raise ParseError(f"'{text}' is not in the map of {reader.name}")
        return {name: label}
    if parse == "bits":
        number = _bits(_pick(text, reader.index), reader.mask, reader.shift, reader.hex, channel)
        if not reader.map:
            return {name: bool(number)}
        label = _mapped(reader, str(number))
        if label is None:
            raise ParseError(f"bit value {number} of {reader.name} is not in its map")
        return {name: label}
    raise ParseError(f"unknown parser {parse}")


def _mapped(reader: Reader, word: str) -> str | None:
    """The label a reader's map gives a reply word: its own entry, else the `*` entry. Without
    a `*` entry an unmapped word is None, which is how a late reply shows."""
    return reader.map.get(word, reader.map.get(MAP_ANY))


def flag_is_set(flag: Flag, reply: str, channel: int | None = None) -> bool:
    """Whether a protection flag's reply reads true."""
    text = _pick(reply.strip(), flag.index)
    if flag.parse == "bool":
        return parse_bool(text, flag.map)
    number = _bits(text, flag.mask, flag.shift, flag.hex, channel)
    if flag.map:
        return flag.map.get(str(number), "") not in ("", "none", "false", "0")
    return bool(number)


def unknown_label(field: str) -> str:
    """What an enumerated field reads as when the reply is not in its map: its table's last
    label, `unknown` or `other`."""
    return ENUM_FIELDS[field][-1]


def format_value(value: float, range_: Range | None) -> str:
    """The number as the command carries it: the range's digits, or every digit it has."""
    if range_ is None:
        return f"{value:.12g}"
    return f"{value:.{range_.digits}f}"


def check_resolution(value: float, range_: Range | None, name: str) -> None:
    """Refuse a value the instrument's digits would round away."""
    if range_ is None or range_.step is not None or range_.choices:
        return
    sent = float(format_value(value, range_))
    if abs(sent - value) > 1e-9 * max(1.0, abs(value)):
        raise RangeError(
            f"{name} {value:g} is finer than the instrument's {range_.digits} decimals; "
            f"it would be sent as {sent:g}"
        )


def check_range(value: float, range_: Range | None, limit: float | None, name: str) -> None:
    """Refuse a value outside the profile's range or the configured limit. A range of fixed
    choices takes exactly one of them and nothing else."""
    if not math.isfinite(value):
        raise RangeError(f"{name} must be a finite number")
    if range_ is not None and range_.choices:
        if not any(abs(value - c) <= 1e-9 * max(1.0, abs(c)) for c in range_.choices):
            raise RangeError(f"{name} must be one of {', '.join(f'{c:g}' for c in range_.choices)}")
        return
    if range_ is None or range_.max is None:
        if limit is None:
            raise RangeError(
                f"no range is known for {name} on this profile; set a limit for it in the config"
            )
        if value < -limit:
            raise RangeError(f"{name} must be from {-limit:g} to {limit:g}, the configured limit")
    else:
        low = range_.min if range_.min is not None else 0.0
        if not low <= value <= range_.max:
            raise RangeError(f"{name} must be from {low:g} to {range_.max:g}")
    if limit is not None and value > limit:
        raise RangeError(f"{name} must be at most {limit:g}, the configured limit")


def clear_commands(setter: Setter, *, ch: int | None, src: int | None = None) -> list[str]:
    """Every command a command setter, such as clear, sends."""
    return [render(one, ch=ch if setter.per_channel else None, src=src) for one in setter.commands]


def setter_command(
    setter: Setter,
    *,
    ch: int | None,
    value: float | None = None,
    range_: Range | None = None,
    on: bool | None = None,
    function: str | None = None,
    src: int | None = None,
    modulation: str | None = None,
) -> str:
    """The command a setter sends; a switch with its own off command sends that for off."""
    text = None if value is None else format_value(value / setter.scale, _scaled(range_, setter))
    word = None
    if function is not None:
        word = setter.map.get(function)
        if word is None:
            raise RangeError(f"{function} is not a function this profile can set")
    command = setter.off if on is False and setter.off is not None else setter.command
    return render(
        command,
        ch=ch if setter.per_channel else None,
        value=text,
        on=on,
        function=word,
        src=src,
        modulation=modulation,
    )


def _scaled(range_: Range | None, setter: Setter) -> Range | None:
    """The range in the instrument's own unit, so its digits apply to what is sent."""
    if range_ is None or setter.scale == 1.0:
        return range_
    return dataclasses.replace(
        range_,
        min=None if range_.min is None else range_.min / setter.scale,
        max=None if range_.max is None else range_.max / setter.scale,
        step=None if range_.step is None else range_.step / setter.scale,
    )


def verified(expected: float, readback: float, range_: Range | None) -> bool:
    """Whether a readback agrees with the value sent, within one step of the range."""
    if not math.isfinite(readback):
        return False
    if range_ is None:
        # Without digits, allow a part in a thousand of rounding; a tiny value never passes as 0.
        return abs(readback - expected) <= 1e-3 * max(abs(expected), abs(readback))
    tolerance = range_.step if range_.step is not None else 10.0**-range_.digits
    return abs(readback - expected) <= tolerance * 1.5


@dataclass(frozen=True, slots=True)
class InstrumentError:
    code: int | None
    text: str


def read_error(errors: Errors, reply: str) -> InstrumentError | None:
    """An error-queue reply as an error, or None when it says there is none. Where the profile
    says its queries answer a bare number, that number is the error code; elsewhere a reply
    without a code is out of step."""
    text = reply.strip()
    if errors.ok.search(text):
        return None
    code: int | None = None
    message = text
    head, sep, tail = text.partition(",")
    if errors.bare and _INTEGER.fullmatch(text):
        code = int(text)
    elif sep:
        try:
            code = int(head.strip().lstrip("+"))
            message = tail.strip().strip('"')
        except ValueError:
            pass
    return InstrumentError(code, message or text)


class Selector:
    """Tracks the channel an instrument has selected, for the select-then-command style."""

    def __init__(self, select: str | None) -> None:
        self._select = select
        self._selected: int | None = None

    def command_for(self, ch: int | None) -> str | None:
        """The select command to send before touching `ch`, or None when none is needed."""
        if self._select is None or ch is None or ch == self._selected:
            return None
        return render(self._select, ch=ch)

    def mark(self, ch: int | None) -> None:
        """The select command went out and was accepted."""
        if ch is not None:
            self._selected = ch

    def forget(self) -> None:
        """After a reconnect or an outside command, the selection is unknown."""
        self._selected = None
