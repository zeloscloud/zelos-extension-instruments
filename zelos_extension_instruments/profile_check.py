"""The checker for profile section loaders: it collects every problem instead of stopping."""

import dataclasses
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")


def _at(where: str, key: str) -> str:
    """A key's place in the profile, `match.model`, or the key alone at the top level."""
    return f"{where}.{key}" if where else key


_Choice = TypeVar("_Choice", bound=str)


def section_keys(cls: Any) -> tuple[str, ...]:
    """A section's keys: the fields of the dataclass it becomes."""
    return tuple(f.name for f in dataclasses.fields(cls))


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
        return self.pattern(self.required(raw, key, where), _at(where, key))

    def pattern(self, text: str, where: str, fallback: str = r"(?!)") -> re.Pattern[str]:
        """`text` compiled, or `fallback` after a problem is noted."""
        try:
            return re.compile(text)
        except re.error as e:
            self.fail(where, f"is not a valid regular expression ({e}).")
            return re.compile(fallback)

    def text_table(self, raw: dict[str, Any], key: str, where: str, what: str) -> dict[str, str]:
        """A table of text to text, such as a map of reply words to labels."""
        value = raw.get(key, {})
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            self.fail(_at(where, key), f"must be a table of {what}.")
            return {}
        return value

    def one_of(self, labels: Iterable[str], allowed: Iterable[str], where: str) -> None:
        allowed = tuple(allowed)
        for label in labels:
            if label not in allowed:
                self.fail(where, f"'{label}' is not one of {', '.join(allowed)}.")

    def commands(
        self, raw: dict[str, Any], key: str, where: str, *, required: bool = False
    ) -> tuple[str, ...]:
        """A command, or a list of them, each non-empty text."""
        value = raw.get(key, [])
        if isinstance(value, str) and value:
            value = [value]
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            self.fail(_at(where, key), "must be a command or a list of commands.")
            return ()
        if required and not value:
            self.fail(_at(where, key), "missing: at least one command.")
        return tuple(value)

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
