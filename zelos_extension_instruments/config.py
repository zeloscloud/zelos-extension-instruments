"""Configuration types and loading."""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "config.schema.json").read_text(encoding="utf-8")
)
# Draft 7, because it is the draft that enforces the per-connection `dependencies`.
_VALIDATOR = Draft7Validator(_SCHEMA)
_ITEM = _SCHEMA["properties"]["instruments"]["items"]
_BRANCHES: list[dict[str, Any]] = _ITEM["dependencies"]["connection"]["oneOf"]
_CONNECTIONS: list[str] = [b["properties"]["connection"]["enum"][0] for b in _BRANCHES]
_ADVANCED: dict[str, Any] = _SCHEMA["definitions"]["advanced"]["properties"]
_SETTINGS: dict[str, Any] = _SCHEMA["properties"]["advanced"]["properties"]
_LINE_ENDINGS = {"LF": "\n", "CRLF": "\r\n", "CR": "\r"}

Connection = Literal["lan", "usb", "serial", "visa", "demo"]
Category = Literal["psu", "load", "dmm", "acsource"]


class ConfigError(Exception):
    """The configuration is invalid; one human sentence per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Limits:
    """The most the extension will ever set, whatever the instrument allows."""

    max_voltage: float | None = None
    max_current: float | None = None
    max_power: float | None = None
    max_frequency: float | None = None

    def for_key(self, key: str) -> float | None:
        return {
            "voltage": self.max_voltage,
            "current": self.max_current,
            "power": self.max_power,
            "frequency": self.max_frequency,
        }.get(key)


@dataclass(frozen=True, slots=True)
class PollRow:
    """One extra reading from the config's poll table."""

    name: str
    query: str
    unit: str | None
    every_s: float


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    """One configured instrument, with every default applied."""

    name: str
    connection: Connection
    host: str | None
    port: int | None
    resource: str | None
    backend: Literal["auto", "py", "ivi"]
    serial_port: str | None
    baud: int | None
    category: Category
    profile: str
    rate_hz: float
    limits: Limits
    keep_panel: bool
    channels: int | None
    timeout_s: float | None
    delay_ms: int | None
    line_ending: str | None
    poll: tuple[PollRow, ...]

    @property
    def where(self) -> str:
        """How the instrument is addressed, for names and messages."""
        if self.connection == "lan":
            assert self.host is not None
            host = f"[{self.host}]" if ":" in self.host else self.host
            return f"{host}:{self.port}" if self.port else host
        if self.connection in ("usb", "visa"):
            assert self.resource is not None
            return self.resource
        if self.connection == "serial":
            assert self.serial_port is not None
            return self.serial_port
        return f"demo {self.category}"


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole configuration."""

    instruments: tuple[InstrumentConfig, ...]
    prefix: str
    log_level: str


def load(path: Path | None = None) -> Settings:
    """Read, validate and default the configuration."""
    path = path or Path(os.environ.get("ZELOS_CONFIG_PATH", "config.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except OSError as e:
        raise ConfigError(
            [f"The configuration file {path} cannot be read: {e.strerror or e}."]
        ) from e
    except UnicodeDecodeError as e:
        raise ConfigError([f"The configuration file {path} is not UTF-8 text."]) from e
    except json.JSONDecodeError as e:
        raise ConfigError([f"The configuration is not valid JSON: {e}."]) from e
    return from_dict(raw)


def from_dict(raw: Any) -> Settings:
    """Validate and default a configuration already parsed from JSON."""
    if not isinstance(raw, dict) or not raw.get("instruments"):
        raise ConfigError(["Add at least one instrument."])
    problems = _problems(raw)
    if problems:
        raise ConfigError(problems)
    instruments: list[InstrumentConfig] = []
    names: dict[str, int] = {}
    for index, item in enumerate(raw["instruments"], 1):
        cfg = _instrument(item)
        # The trace store ignores case: two names that differ only in case leave it empty.
        key = cfg.name.casefold()
        if key and names.setdefault(key, index) != index:
            problems.append(
                f"{_where(index, item)}: name '{cfg.name}' is already used by instrument "
                f"{names[key]}."
            )
        seen: set[str] = set()
        for row in cfg.poll:
            if row.name.casefold() in seen:
                problems.append(
                    f"{_where(index, item)}, Extra readings: '{row.name}' is listed twice."
                )
            seen.add(row.name.casefold())
        instruments.append(cfg)
    if problems:
        raise ConfigError(problems)
    advanced = {name: schema["default"] for name, schema in _SETTINGS.items()}
    advanced.update(raw.get("advanced", {}))
    return Settings(
        instruments=tuple(instruments),
        prefix=advanced["prefix"].strip(" "),
        log_level=advanced["log_level"],
    )


def _instrument(item: dict[str, Any]) -> InstrumentConfig:
    connection = item["connection"]
    adv = item.get("advanced", {}) or {}
    limits_raw = adv.get("limits", {}) or {}
    rows = tuple(
        PollRow(
            name=row["name"].strip(" "),
            query=row["query"],
            unit=(row.get("unit") or None),
            every_s=float(row.get("every_s", _poll_default("every_s"))),
        )
        for row in adv.get("poll", []) or []
    )
    line_ending = adv.get("line_ending")
    return InstrumentConfig(
        # The trace refuses a name that starts with a space.
        name=item.get("name", "").strip(" "),
        connection=connection,
        host=item.get("host"),
        port=int(item["port"]) if connection == "lan" and item.get("port") is not None else None,
        resource=item.get("resource"),
        backend=item.get("backend", "auto"),
        serial_port=item.get("port") if connection == "serial" else None,
        baud=None if item.get("baud") is None else int(item["baud"]),
        category=item.get("category", "psu"),
        profile=(item.get("profile") or "auto").strip(),
        rate_hz=float(item.get("rate_hz", _ITEM["properties"]["rate_hz"]["default"])),
        limits=Limits(
            max_voltage=_optional_float(limits_raw.get("max_voltage")),
            max_current=_optional_float(limits_raw.get("max_current")),
            max_power=_optional_float(limits_raw.get("max_power")),
            max_frequency=_optional_float(limits_raw.get("max_frequency")),
        ),
        keep_panel=bool(adv.get("keep_panel", _ADVANCED["keep_panel"]["default"])),
        channels=None if adv.get("channels") is None else int(adv["channels"]),
        timeout_s=_optional_float(adv.get("timeout_s")),
        delay_ms=None if adv.get("delay_ms") is None else int(adv["delay_ms"]),
        line_ending=_LINE_ENDINGS[line_ending] if line_ending else None,
        poll=rows,
    )


def _poll_default(key: str) -> Any:
    return _ADVANCED["poll"]["items"]["properties"][key]["default"]


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    """The errors worth reporting: for a failed connection branch, those of its own fields."""
    if error.validator != "oneOf":
        yield error
        return
    instance: Any = error.instance
    connection = instance.get("connection")
    if connection in _CONNECTIONS:
        branch = _CONNECTIONS.index(connection)
        yield from (e for e in error.context if e.relative_schema_path[0] == branch)


def _where(index: int, item: Any) -> str:
    connection = item.get("connection") if isinstance(item, dict) else None
    if isinstance(connection, str) and connection:
        return f"Instrument {index} ({connection})"
    return f"Instrument {index}"


def _problems(raw: dict[str, Any]) -> list[str]:
    """One sentence for each field with a mistake."""
    found: dict[tuple[Any, ...], str] = {}
    for error in _VALIDATOR.iter_errors(raw):
        for leaf in _leaves(error):
            for key, problem in _sentences(raw, leaf):
                if key not in found or leaf.validator == "enum":
                    found[key] = problem
    return list(found.values())


def _sentences(
    raw: dict[str, Any], error: ValidationError
) -> Iterator[tuple[tuple[Any, ...], str]]:
    path: list[Any] = list(error.absolute_path)
    if path and path[0] == "instruments" and len(path) > 1:
        where = _where(path[1] + 1, raw["instruments"][path[1]])
    else:
        where = "Settings"
    schema: Any = error.schema
    if error.validator == "required":
        required: Any = error.validator_value
        instance: Any = error.instance
        for name in required:
            if name not in instance:
                title = schema.get("properties", {}).get(name, {}).get("title", name)
                yield (*path, name), f"{where}, {title}: missing."
        return
    title = None if path[:1] == ["instruments"] and len(path) == 2 else schema.get("title")
    yield tuple(path), f"{where}{f', {title}' if title else ''}: {_wording(error, schema)}"


_KINDS = {
    "string": "text",
    "integer": "a whole number",
    "number": "a number",
    "boolean": "true or false",
    "object": "a group of settings",
    "array": "a list",
}


def _wording(error: ValidationError, schema: dict[str, Any]) -> str:
    if error.validator == "enum":
        values = [json.dumps(v) for v in schema["enum"]]
        return f"must be {', '.join(values[:-1])} or {values[-1]}."
    if error.validator == "type":
        return f"must be {_KINDS[schema['type']]}."
    if error.validator in ("minimum", "maximum", "exclusiveMinimum"):
        low = schema.get("minimum", schema.get("exclusiveMinimum"))
        high = schema.get("maximum")
        if high is None:
            return (
                f"must be above {low}."
                if "exclusiveMinimum" in schema
                else f"must be at least {low}."
            )
        return f"must be from {low} to {high}."
    blank = isinstance(error.instance, str) and not error.instance.strip(" ")
    if error.validator == "minLength" or (error.validator == "pattern" and blank):
        return "must not be empty."
    if error.validator == "pattern":
        return "use letters, digits, spaces, _ and - only."
    if error.validator == "maxLength":
        return f"must be at most {schema['maxLength']} characters."
    if error.validator == "additionalProperties":
        instance: Any = error.instance
        unknown = sorted(set(instance) - set(schema.get("properties", {})))
        return f"unknown setting {', '.join(unknown)}."
    return f"{error.message}."
