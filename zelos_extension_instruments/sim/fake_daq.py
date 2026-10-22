"""A fake data-acquisition unit that answers a daq profile's own commands.

It remembers what each channel was configured to read, scans the list it was given last, and
keeps each relay open or closed. Commands route as FakeInstrument's do: each template of the
profile becomes an anchored, case-insensitive pattern.
"""

import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..profile import Profile
from ..profile_daq import Daq
from .fake import (
    _CAPTURES,
    _NO_ERROR,
    _PLACEHOLDER,
    _QUEUE_DEPTH,
    _QUEUE_OVERFLOW,
    _READINGS,
    _UNDEFINED_HEADER,
    _default_manufacturer,
    _default_model,
    _literal,
)

_DAQ_CAPTURES = {
    **_CAPTURES,
    "channels": r"\d+(?:\s*[,:]\s*\d+)*",
    "tc": "[BEJKNRST]",
    "count": r"\d+",
}
_SETTINGS_CONFLICT = (-221, "Settings conflict")


@dataclass(frozen=True, slots=True)
class _Route:
    pattern: re.Pattern[str]
    act: Callable[[re.Match[str]], str | None]


class FakeDaq:
    """One unit of a daq profile's family. A scan reads each channel's own value, or the
    default for its function. Not thread-safe: FakeServer serializes its clients."""

    def __init__(self, profile: Profile, *, idn: str | None = None) -> None:
        if profile.daq is None:
            raise ValueError(f"{profile.id} is not a daq profile")
        self.profile = profile
        self.daq: Daq = profile.daq
        if idn is None:
            idn = f"{_default_manufacturer(profile)},{_default_model(profile)},SIM00001,1.0"
        self.idn = idn
        self.transcript: list[tuple[str, str | None]] = []
        # Channel to the function, thermocouple type and integration time it was given.
        self.functions: dict[int, str] = {}
        self.thermocouples: dict[int, str] = {}
        self.nplc: dict[int, float] = {}
        self.scan_list: tuple[int, ...] = ()
        self.closed: set[int] = set()
        self._values: dict[int, float] = {}
        self._errors: deque[tuple[int, str]] = deque()
        self._routes = self._routes_for(profile)

    def handle(self, line: str) -> str | bytes | None:
        """The reply to one command, without terminators; None when the command has none."""
        command = line.strip()
        if not command:
            return None
        for route in self._routes:
            match = route.pattern.fullmatch(command)
            if match is not None:
                reply = route.act(match)
                break
        else:
            self._push_error(*_UNDEFINED_HEADER)
            reply = None
        self.transcript.append((command, reply))
        return reply

    def set_reading(self, ch: int, value: float) -> None:
        """What `ch` reads in a scan, whatever its function."""
        self._values[ch] = value

    def reading(self, ch: int) -> float:
        """What `ch` reads in a scan now."""
        return self._values.get(ch, _READINGS[self.functions.get(ch, "dc_voltage")])

    def _routes_for(self, profile: Profile) -> list[_Route]:
        daq = self.daq
        routes: list[_Route] = []

        def add(
            template: str, act: Callable[[re.Match[str]], str | None], words: Iterable[str] = ()
        ) -> None:
            routes.append(_Route(_pattern(template, words), act))

        def configure(function: str) -> Callable[[re.Match[str]], None]:
            return lambda m: self._configure(function, m)

        add(profile.match.idn, lambda _: self.idn)
        if profile.errors is not None:
            for query in (profile.errors.query, *profile.errors.then):
                add(query, lambda _: self._pop_error())
        for function, commands in daq.configure.items():
            for command in commands:
                add(command, configure(function))
        if daq.nplc is not None:
            add(daq.nplc.command, self._set_nplc, daq.nplc.map.values())
        for command in daq.scan:
            add(command, self._set_scan)
        for command in daq.read[:-1]:
            add(command, lambda _: None)
        add(daq.read[-1], self._read)
        relay = profile.setters.get("relay")
        if relay is not None:
            add(relay.command, self._relay)
            if relay.off is not None:
                add(relay.off, lambda m: self._switch(m, False))
            if relay.verify is not None:
                add(relay.verify, lambda m: "1" if int(m.group("ch")) in self.closed else "0")
        clear = profile.setters.get("clear")
        for command in clear.commands if clear is not None else ():
            add(command, lambda _: None)
        for command in (
            profile.link.first,
            profile.link.remote,
            *profile.link.local,
            *profile.link.init,
        ):
            if command is not None:
                add(command, lambda _: None)
        add("*OPC?", lambda _: "1")
        add("*CLS", lambda _: self._errors.clear())
        add("*RST", self._reset)
        return routes

    def _configure(self, function: str, match: re.Match[str]) -> None:
        tc = match.groupdict().get("tc")
        for ch in _addresses(match.group("channels")):
            self.functions[ch] = function
            if tc is not None:
                self.thermocouples[ch] = tc.upper()

    def _set_nplc(self, match: re.Match[str]) -> None:
        assert self.daq.nplc is not None
        groups = match.groupdict()
        addresses = _addresses(groups["channels"])
        if "function" in groups:
            word = groups["function"].casefold()
            words = self.daq.nplc.map
            if any(
                words.get(self.functions.get(ch, ""), "").casefold() != word for ch in addresses
            ):
                self._push_error(*_SETTINGS_CONFLICT)
                return
        for ch in addresses:
            self.nplc[ch] = float(groups["value"])

    def _set_scan(self, match: re.Match[str]) -> None:
        groups = match.groupdict()
        if "channels" not in groups:
            return
        addresses = tuple(sorted(_addresses(groups["channels"])))
        if any(ch not in self.functions for ch in addresses):
            self._push_error(*_SETTINGS_CONFLICT)
            return
        self.scan_list = addresses

    def _read(self, match: re.Match[str]) -> str | None:
        if not self.scan_list:
            self._push_error(*_SETTINGS_CONFLICT)
            return None
        readings = [f"{self.reading(ch):+.8E}" for ch in self.scan_list]
        count = match.groupdict().get("count")
        return ",".join(readings[: int(count)] if count is not None else readings)

    def _relay(self, match: re.Match[str]) -> None:
        groups = match.groupdict()
        if "on" in groups or "bit" in groups:
            on = (groups.get("on") or "").upper() == "ON" or groups.get("bit") == "1"
            self._switch(match, on)
        else:
            self._switch(match, True)

    def _switch(self, match: re.Match[str], closed: bool) -> None:
        ch = int(match.group("ch"))
        if closed:
            self.closed.add(ch)
        else:
            self.closed.discard(ch)

    def _reset(self, _: re.Match[str]) -> None:
        self.functions.clear()
        self.thermocouples.clear()
        self.nplc.clear()
        self.scan_list = ()
        self.closed.clear()

    def _push_error(self, code: int, text: str) -> None:
        if len(self._errors) >= _QUEUE_DEPTH:
            self._errors[-1] = _QUEUE_OVERFLOW
        else:
            self._errors.append((code, text))

    def _pop_error(self) -> str:
        if not self._errors:
            return _NO_ERROR
        code, text = self._errors.popleft()
        return f'{code},"{text}"'


def _pattern(template: str, words: Iterable[str] = ()) -> re.Pattern[str]:
    """The anchored pattern a template's commands match; placeholders become captures."""
    alternatives = sorted(words, key=len, reverse=True)
    captures = {**_DAQ_CAPTURES, "function": "|".join(map(re.escape, alternatives)) or "(?!)"}
    text = template.strip().removeprefix(":")
    out = [":?"]
    seen: set[str] = set()
    position = 0
    for found in _PLACEHOLDER.finditer(text):
        out.append(_literal(text[position : found.start()]))
        name = found.group(1)
        out.append(f"(?P={name})" if name in seen else f"(?P<{name}>{captures[name]})")
        seen.add(name)
        position = found.end()
    out.append(_literal(text[position:]))
    return re.compile("".join(out), re.IGNORECASE)


def _addresses(text: str) -> list[int]:
    """A scan list's addresses, with each `first:last` range spelled out."""
    out: list[int] = []
    for part in text.split(","):
        first, _, last = part.strip().partition(":")
        out.extend(range(int(first), int(last or first) + 1))
    return out
