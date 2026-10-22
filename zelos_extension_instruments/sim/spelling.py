"""The text a regular expression accepts, spelled out: an example of a model or a manufacturer
a profile matches, or of a reply a profile reads."""

import importlib
import re
from typing import Any

# re._parser is the stdlib's regex parser; it ships without type stubs.
_regex_parser: Any = importlib.import_module("re._parser")
_CLASS_CHARS = {"CATEGORY_DIGIT": "0", "CATEGORY_WORD": "A", "CATEGORY_SPACE": " "}


def example(pattern: re.Pattern[str]) -> str | None:
    """The text the pattern's first alternatives and fewest repeats spell, if it matches."""
    try:
        text = _spell(_regex_parser.parse(pattern.pattern, pattern.flags))
    except (ValueError, IndexError, OverflowError):
        return None
    return text if pattern.search(text) else None


def _spell(items: Any) -> str:
    out: list[str] = []
    for op, av in items:
        name = op.name
        if name == "LITERAL":
            out.append(chr(av))
        elif name == "ANY":
            out.append("A")
        elif name == "IN":
            out.append(_spell_class(av))
        elif name == "BRANCH":
            out.append(_spell(av[1][0]))
        elif name == "SUBPATTERN":
            out.append(_spell(av[3]))
        elif name == "ATOMIC_GROUP":
            out.append(_spell(av))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            out.append(_spell(av[2]) * av[0])
        elif name != "AT":
            raise ValueError(f"cannot spell {name}")
    return "".join(out)


def _spell_class(items: Any) -> str:
    op, av = items[0]
    if op.name == "LITERAL":
        return chr(av)
    if op.name == "RANGE":
        return chr(av[0])
    if op.name == "CATEGORY" and av.name in _CLASS_CHARS:
        return _CLASS_CHARS[av.name]
    raise ValueError(f"cannot spell {op.name}")
