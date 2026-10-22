"""The function generator part of the fake instrument: every channel's settings, answered in the
profile's own formats, keyed replies included."""

from collections.abc import Callable
from typing import Any

from ..profile import INSTRUMENT_FIELDS, SIGNAL_FIELDS, STATE_FIELDS, Profile, Reader, Setter

_DEFAULTS: dict[str, Any] = {
    "output": False,
    "shape": "sine",
    "frequency": 1000.0,
    "amplitude": 1.0,
    "offset": 0.0,
    "phase": 0.0,
    "duty": 50.0,
    "symmetry": 50.0,
    "width": 1e-4,
    "output_load": "high_z",
    "modulation": False,
    "modulation_type": "am",
    "burst": False,
    "burst_cycles": 1.0,
    "burst_period": 0.01,
    "sweep": False,
    "sweep_start": 100.0,
    "sweep_stop": 1000.0,
    "sweep_time": 1.0,
}
_LABELS = ("shape", "output_load", "modulation_type")
# What each modulation type remembers for itself.
_MODULATION_DEFAULTS = {
    "modulation_depth": 100.0,
    "modulation_deviation": 100.0,
    "phase_deviation": 90.0,
    "modulation_frequency": 10.0,
}
# The unit a keyed reply writes after a number, in capitals.
_KEYED_SUFFIX = {"Hz": "HZ", "V": "V", "Vpp": "V", "s": "S", "A": "A", "W": "W", "Ohm": "OHM"}


def default_label(profile: Profile, name: str, preferred: str) -> str:
    """`preferred` when the profile can spell it, else the first label it can."""
    reader = profile.reader(name)
    setter = profile.setters.get(name)
    known = [*reader.words] if reader is not None and reader.words else []
    if not known and setter is not None:
        known = [*setter.map]
    spelled = [label for label in known if label not in ("other", "unknown")] or known
    return preferred if not known or preferred in known else spelled[0]


def _deviation(name: str, modulation: str | None) -> str:
    """The deviation a modulation type means when a command names the type: a phase modulation's
    is a phase, any other's a frequency."""
    if modulation is None or name not in ("modulation_deviation", "phase_deviation"):
        return name
    return "phase_deviation" if modulation == "pm" else "modulation_deviation"


class FakeFgen:
    """A generator's settings per channel, each kept in its own unit.

    High and low follow amplitude and offset, and the other way round. A modulation type keeps
    its own depth, deviation, phase deviation and frequency. Not thread-safe: FakeServer
    serializes its clients.
    """

    names = frozenset(STATE_FIELDS["fgen"])

    def __init__(self, profile: Profile, channels: int) -> None:
        self.profile = profile
        self.channels = channels
        self._defaults = {
            **_DEFAULTS,
            **{n: default_label(profile, n, _DEFAULTS[n]) for n in _LABELS},
        }
        modulation = profile.setters.get("modulation_type")
        self._types = (
            {word.casefold(): label for label, word in modulation.map.items()}
            if modulation is not None
            else {}
        )
        # How many times each channel was triggered.
        self.triggers = dict.fromkeys(range(1, channels + 1), 0)
        self.state: dict[int, dict[str, Any]] = {}
        self.reset()

    def reset(self) -> None:
        self.state = {ch: {**self._defaults, "types": {}} for ch in range(1, self.channels + 1)}

    def value(self, name: str, ch: int, modulation: str | None = None) -> float | bool | str:
        """A field of the channel; `modulation` is a type's label when the query names one."""
        key = _deviation(name.removesuffix("_set"), modulation)
        state = self.state[ch]
        if key == "high":
            return state["offset"] + state["amplitude"] / 2
        if key == "low":
            return state["offset"] - state["amplitude"] / 2
        if key in _MODULATION_DEFAULTS:
            return self._type(state, modulation)[key]
        return state[key]

    def label_of(self, word: str | None) -> str | None:
        """The modulation type label an instrument word names."""
        return None if word is None else self._types.get(word.casefold())

    def apply(
        self,
        setter: Setter,
        targets: list[int],
        *,
        value: float | None,
        word: str | None,
        on: bool | None,
        modulation: str | None,
    ) -> None:
        """Run a setter on `targets`: `value` in the unit the profile states, `word` the
        instrument's word for a label, `on` a switch, `modulation` a type word the command names."""
        name = setter.name
        label = self.label_of(modulation)
        for ch in targets:
            state = self.state[ch]
            if name == "trigger":
                self.triggers[ch] += 1
            elif value is not None:
                self._set_number(state, name, value, label)
            elif word is not None:
                state[name] = next(
                    k for k, v in setter.map.items() if v.casefold() == word.casefold()
                )
            elif on is not None:
                state[name] = on
                if name == "modulation" and on and label is not None:
                    state["modulation_type"] = label

    def snapshot(self) -> dict[str, Any]:
        return {str(ch): state for ch, state in self.state.items()}

    def restore(self, data: dict[str, Any]) -> None:
        self.state = {int(ch): state for ch, state in data.items()}

    def _type(self, state: dict[str, Any], label: str | None) -> dict[str, float]:
        types: dict[str, dict[str, float]] = state["types"]
        return types.setdefault(label or state["modulation_type"], dict(_MODULATION_DEFAULTS))

    def _set_number(
        self, state: dict[str, Any], name: str, value: float, modulation: str | None
    ) -> None:
        name = _deviation(name, modulation)
        if name in ("high", "low"):
            high = value if name == "high" else state["offset"] + state["amplitude"] / 2
            low = value if name == "low" else state["offset"] - state["amplitude"] / 2
            state["amplitude"], state["offset"] = high - low, (high + low) / 2
        elif name in _MODULATION_DEFAULTS:
            self._type(state, modulation)[name] = value
        else:
            state[name] = value


def keyed_reply(
    profile: Profile, command: str, readers: list[Reader], value_of: Callable[[str], Any]
) -> str:
    """The reply to a query whose readers each take one key of a comma list: a header named after
    the query, then each reader's key and its value, as `C1:BSWV WVTP,SINE,FRQ,100HZ` reads."""
    units = {
        **SIGNAL_FIELDS[profile.category],
        **STATE_FIELDS[profile.category],
        **INSTRUMENT_FIELDS[profile.category],
    }
    header = command.partition(" ")[0].removesuffix("?").lstrip(":")
    tokens: list[str] = []
    for reader in readers:
        assert reader.key is not None
        name = reader.fields[0]
        value = value_of(name)
        if isinstance(value, bool):
            word = "on" if value else "off"
            text = reader.words.get(word, word.upper())
        elif isinstance(value, str):
            text = reader.words.get(value) or value.upper()
        else:
            suffix = _KEYED_SUFFIX.get(units.get(name) or "", "")
            text = f"{float(value) / reader.scale:.10g}{suffix}"
        tokens += [reader.key, text]
    body = ",".join(tokens)
    return f"{header} {body}" if ":" in header else body
