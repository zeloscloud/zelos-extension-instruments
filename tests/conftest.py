"""Shared by every test: an isolated trace recording, a scripted fake link, and the shipped
profiles."""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import zelos_sdk

from zelos_extension_instruments import ACTION_PREFIX, config
from zelos_extension_instruments.config import InstrumentConfig, Settings
from zelos_extension_instruments.link import Address, Link, LinkError, LinkTimeout
from zelos_extension_instruments.link import Settings as LinkSettings
from zelos_extension_instruments.profile import Profile, load_file, shipped
from zelos_extension_instruments.waveform import BlockError, block_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
# Not reader.time_range(): it raises on a trace that holds no rows.
ALL_TIME = ("1970-01-01T00:00:00Z", "2200-01-01T00:00:00Z")
# No field can be named this: sanitising turns `@` into `_`.
TIME = "@time_s"


@dataclass
class Recording:
    """A trace source in its own namespace, written to `path`."""

    source: zelos_sdk.TraceSource
    path: Path
    writer: zelos_sdk.TraceWriter

    def events(self) -> dict[str, list[dict[str, Any]]]:
        """Close the recording and read it back."""
        self.source.flush()
        self.writer.close()
        return read_events(self.path)


def read_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Event name (`psu/ch1`) to its rows, each `{TIME: float, field: value, ...}`."""
    events: dict[str, list[dict[str, Any]]] = {}
    with zelos_sdk.TraceReader(str(path)) as reader:
        segments = [segment.id for segment in reader.list_data_segments()]
        for source in reader.list_fields():
            for event in source.events:
                fields = [field.path for field in event.fields]
                data = reader.query(segments, fields, *ALL_TIME).to_arrow()
                prefix = f"{source.name}/{event.name}."
                rows = pa.ipc.open_stream(data).read_all().to_pylist() if data else []
                events[event.name] = [
                    {
                        key.removeprefix(prefix) if key.startswith(prefix) else TIME: value
                        for key, value in row.items()
                    }
                    for row in rows
                ]
    return events


@pytest.fixture
def trace_file(tmp_path: Path) -> Iterator[Recording]:
    """A recording that never touches the global source or an agent."""
    namespace = zelos_sdk.TraceNamespace(f"test-{uuid.uuid4().hex}")
    source = zelos_sdk.TraceSource(ACTION_PREFIX, namespace=namespace)
    path = tmp_path / "trace.trz"
    writer = zelos_sdk.TraceWriter(str(path), namespace=namespace)
    writer.open()
    yield Recording(source, path, writer)
    writer.close()


@pytest.fixture(scope="session")
def profiles() -> list[Profile]:
    return shipped()


@pytest.fixture(scope="session")
def dp800() -> Profile:
    return load_file(REPO_ROOT / "profiles" / "psu" / "rigol-dp800.toml")


class ScriptedLink:
    """A Link whose replies come from a handler; records every command.

    `handler(command)` returns the reply text, None for a command with no reply, or raises
    LinkTimeout/LinkError to fake a silent or broken instrument.
    """

    def __init__(self, handler: Callable[[str], str | bytes | None], where: str = "fake") -> None:
        self._handler = handler
        self.where = where
        self.commands: list[str] = []
        self.closed = False
        self.cleared = 0
        self._pending: list[str | bytes] = []
        self.timeouts_asked: list[float | None] = []
        # Re-entrant: a handler may push a reply while the lock is held for its own write.
        self.lock = threading.RLock()

    def write(self, command: str) -> None:
        with self.lock:
            if self.closed:
                raise LinkError(f"{self.where} is closed")
            self.commands.append(command)
            reply = self._handler(command)
            if reply is not None:
                self._pending.append(reply)

    def read(self, timeout_s: float | None = None) -> str:
        with self.lock:
            if self.closed:
                raise LinkError(f"{self.where} is closed")
            if not self._pending:
                raise LinkTimeout(f"no reply from {self.where}")
            self.timeouts_asked.append(timeout_s)
            reply = self._pending.pop(0)
            return reply.decode("utf-8", errors="replace") if isinstance(reply, bytes) else reply

    def push(self, reply: str | bytes) -> None:
        """Queue a reply as if the instrument sent it unasked, as a late reply arrives."""
        with self.lock:
            self._pending.append(reply)

    def read_any(self, timeout_s: float | None = None) -> str | bytes:
        with self.lock:
            if not self._pending:
                raise LinkTimeout(f"no reply from {self.where}")
            block = isinstance(self._pending[0], bytes) and self._pending[0][:1] == b"#"
        return self.read_block(timeout_s) if block else self.read(timeout_s)

    def read_block(self, timeout_s: float | None = None) -> bytes:
        with self.lock:
            if self.closed:
                raise LinkError(f"{self.where} is closed")
            if not self._pending:
                raise LinkTimeout(f"no reply from {self.where}")
            reply = self._pending.pop(0)
        raw = reply if isinstance(reply, bytes) else reply.encode()
        try:
            return block_payload(raw)
        except BlockError as exc:
            raise LinkError(f"{self.where} did not answer with a block: {exc}") from exc

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self.cleared += 1
        self._pending.clear()

    def close(self) -> None:
        self.closed = True


def opener_for(link: Link) -> Callable[[Address, LinkSettings], Link]:
    """An opener that hands out one link, then raises as a vanished instrument would."""

    def open(address: Address, settings: LinkSettings) -> Link:
        if getattr(link, "closed", False):
            raise LinkError(f"{address.where} is gone", "refused")
        return link

    return open


def instrument_config(**overrides: Any) -> InstrumentConfig:
    """A LAN instrument config with every default applied."""
    item: dict[str, Any] = {"connection": "lan", "host": "127.0.0.1", "name": "psu"}
    item.update(overrides)
    settings = config.from_dict({"instruments": [item]})
    return settings.instruments[0]


def settings_for(*instruments: InstrumentConfig) -> Settings:
    return Settings(instruments=tuple(instruments), prefix="Instruments", log_level="INFO")


def wait_until(
    condition: Callable[[], bool], timeout_s: float = 5.0, every_s: float = 0.01
) -> bool:
    """Poll a condition; True when it held before the timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(every_s)
    return condition()
