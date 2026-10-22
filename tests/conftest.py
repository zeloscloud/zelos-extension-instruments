"""Shared by every test: an isolated trace recording, a scripted fake link, and the shipped
profiles."""

import socket
import socketserver
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
from hypothesis import settings

from zelos_extension_instruments import ACTION_PREFIX, config
from zelos_extension_instruments.config import InstrumentConfig
from zelos_extension_instruments.emitter import Emitter
from zelos_extension_instruments.link import Address, Link, LinkError, LinkSettings, LinkTimeout
from zelos_extension_instruments.profile import BOOL_FIELDS, ENUM_FIELDS, Profile
from zelos_extension_instruments.profile_load import from_dict, load_file, shipped
from zelos_extension_instruments.waveform import BlockError, block_payload
from zelos_extension_instruments.worker import InstrumentWorker

# A slow runner must not fail a property test on its deadline, and a failure must reproduce.
settings.register_profile("deterministic", deadline=None, derandomize=True)
settings.load_profile("deterministic")

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
def fast_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the worker's slow round, status and reconnect periods for a test that only waits
    for them; a test that measures the schedule leaves them alone."""
    module = "zelos_extension_instruments.worker"
    monkeypatch.setattr(f"{module}._SLOW_NS", 100_000_000)
    monkeypatch.setattr(f"{module}._STATUS_NS", 100_000_000)
    monkeypatch.setattr(f"{module}._BACKOFF_MIN_S", 0.05)
    monkeypatch.setattr(f"{module}._BACKOFF_MAX_S", 0.4)


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


def assert_field_type(where: str, name: str, value: Any) -> None:
    """A parsed field holds what its name promises: an enumerated field a known label, a flag a
    bool, any other field a number."""
    if name in ENUM_FIELDS:
        assert value in ENUM_FIELDS[name], f"{where}: {name} read {value!r}"
    elif name in BOOL_FIELDS:
        assert isinstance(value, bool), f"{where}: {name} read {value!r}"
    else:
        assert isinstance(value, int | float) and not isinstance(value, bool), (
            f"{where}: {name} read {value!r}"
        )


def instrument_config(**overrides: Any) -> InstrumentConfig:
    """A LAN instrument config with every default applied."""
    item: dict[str, Any] = {"connection": "lan", "host": "127.0.0.1", "name": "psu"}
    item.update(overrides)
    settings = config.from_dict({"instruments": [item]})
    return settings.instruments[0]


def start_blocked_query(
    link: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Thread, list[LinkError]]:
    """Query a silent instrument from a thread; returns once that thread is waiting to receive,
    with the errors it ends on."""
    waiting = threading.Event()
    receive = link._receive
    errors: list[LinkError] = []

    def entering(deadline: float, size: int) -> None:
        waiting.set()
        receive(deadline, size)

    def blocked() -> None:
        try:
            link.query("NOREPLY?")
        except LinkError as exc:
            errors.append(exc)

    monkeypatch.setattr(link, "_receive", entering)
    thread = threading.Thread(target=blocked)
    thread.start()
    assert waiting.wait(5)
    return thread, errors


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


class Dp832:
    """A three-channel supply that answers like a Rigol DP832 and keeps its own state."""

    def __init__(self) -> None:
        self.volt = {1: 5.0, 2: 0.0, 3: 0.0}
        self.curr = {1: 1.0, 2: 0.5, 3: 0.5}
        self.out = {1: True, 2: False, 3: False}
        self.load = {1: 10.0, 2: 10.0, 3: 10.0}
        self.errors: list[str] = []
        self.ovp_tripped = {1: False, 2: False, 3: False}
        self.ovp_level = {1: 33.0, 2: 33.0, 3: 5.5}
        self.ocp_level = {1: 3.3, 2: 3.3, 3: 3.3}
        self.ovp_on = {1: False, 2: False, 3: False}
        self.ocp_on = {1: False, 2: False, 3: False}
        self.silent = False
        # An error the next command (not a query) queues, as a refused setting does.
        self.fail_next_set: str | None = None
        self.calls: list[str] = []

    def measured(self, ch: int) -> tuple[float, float, float]:
        if not self.out[ch]:
            return 0.0, 0.0, 0.0
        v = min(self.volt[ch], self.curr[ch] * self.load[ch])
        i = v / self.load[ch]
        return v, i, v * i

    def mode(self, ch: int) -> str:
        if not self.out[ch]:
            return "UR"
        return "CV" if self.volt[ch] <= self.curr[ch] * self.load[ch] else "CC"

    def __call__(self, command: str) -> str | None:
        self.calls.append(command)
        if self.fail_next_set is not None and not command.endswith("?"):
            self.errors.append(self.fail_next_set)
            self.fail_next_set = None
        if self.silent:
            raise LinkTimeout("no reply from dp832")
        head, _, arg = command.partition(" ")
        if head == "*IDN?":
            return "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
        if head == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if head == ":MEAS:ALL?":
            v, i, p = self.measured(int(arg[2:]))
            return f"{v:.4f},{i:.4f},{p:.3f}"
        if head == ":OUTP?":
            return "ON" if self.out[int(arg[2:])] else "OFF"
        if head == ":OUTP:MODE?":
            return self.mode(int(arg[2:]))
        if head == ":OUTP:OVP:QUES?":
            return "1" if self.ovp_tripped[int(arg[2:])] else "0"
        if head == ":OUTP:OCP:QUES?":
            return "0"
        if head in (":OUTP:OVP:VAL?", ":OUTP:OCP:VAL?"):
            table = self.ovp_level if "OVP" in head else self.ocp_level
            return f"{table[int(arg[2:])]:.3f}"
        if head in (":OUTP:OVP?", ":OUTP:OCP?"):
            table = self.ovp_on if "OVP" in head else self.ocp_on
            return "ON" if table[int(arg[2:])] else "OFF"
        if head == ":OUTP:SENS?":
            return "OFF"
        if head in (":OUTP:OVP:VAL", ":OUTP:OCP:VAL"):
            ch, _, level = arg.partition(",")
            table = self.ovp_level if "OVP" in head else self.ocp_level
            table[int(ch[2:])] = float(level)
            return None
        if head in (":OUTP:OVP", ":OUTP:OCP"):
            ch, _, state = arg.partition(",")
            table = self.ovp_on if "OVP" in head else self.ocp_on
            table[int(ch[2:])] = state == "ON"
            return None
        if head == ":OUTP":
            ch, _, state = arg.partition(",")
            self.out[int(ch[2:])] = state == "ON"
            return None
        if head in (":OUTP:OCP:CLEAR", ":OUTP:OVP:CLEAR"):
            self.ovp_tripped[int(arg[2:])] = False
            return None
        if head in ("SYST:REMOTE", "SYST:LOCAL", "*RST"):
            return None
        if head.startswith(":SOUR"):
            ch = int(head[5])
            field = head.split(":")[2]
            table = self.volt if field.startswith("VOLT") else self.curr
            if field.endswith("?"):
                return f"{table[ch]:.3f}"
            value = float(arg)
            limit = 30.0 if table is self.volt else 3.0
            if value > limit:
                self.errors.append('-222,"Data out of range"')
                return None
            table[ch] = value
            return None
        self.errors.append('-113,"Undefined header"')
        return None


@pytest.fixture
def dp832() -> Dp832:
    return Dp832()


class RecordingEmitter(Emitter):
    """Records what the worker writes instead of a trace."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str, int]] = []
        self.rows: list[tuple[str, int, dict[str, Any], int]] = []
        self.readings: list[tuple[str, str, float, int]] = []
        self.polls: list[tuple[str, str, float, int]] = []
        self.acquisitions: list[tuple[str, dict[str, Any], int]] = []
        self.waveforms: list[tuple[str, int, list[tuple[int, float]]]] = []
        self.statuses: list[dict[str, Any]] = []
        self.notes: list[tuple[str, str, str]] = []
        self._fields: dict[str, dict[str, str | None]] = {}
        self.channel_fields: dict[str, dict[int, set[str]]] = {}
        self.identities: list[tuple[str, dict[str, str]]] = []

    def add_instrument(self, name: str, profile: Profile, channels: int, poll: Any = ()) -> None:
        self.registered.append((name, profile.id, channels))
        self._fields[name] = profile.fields

    def add_channels(self, name: str, fields: dict[int, set[str]]) -> None:
        self.channel_fields[name] = {ch: set(names) for ch, names in fields.items()}

    def identity(self, name: str, values: dict[str, str], time_ns: int) -> None:
        self.identities.append((name, dict(values)))

    def row(self, name: str, ch: int, values: dict[str, Any], time_ns: int) -> None:
        self.rows.append((name, ch, dict(values), time_ns))

    def reading(self, name: str, function: str, value: float, time_ns: int) -> None:
        self.readings.append((name, function, value, time_ns))

    def poll_value(self, name: str, row_name: str, value: float, time_ns: int) -> None:
        self.polls.append((name, row_name, value, time_ns))

    def acquisition_row(self, name: str, values: dict[str, Any], time_ns: int) -> None:
        self.acquisitions.append((name, dict(values), time_ns))

    def waveform(self, name: str, ch: int, time_ns: Any, volts: Any) -> None:
        self.waveforms.append((name, ch, list(zip(time_ns.tolist(), volts.tolist(), strict=True))))

    def status(
        self, name: str, rtt_ms: float, rate_hz: float, errors: int, connected: bool, time_ns: int
    ) -> None:
        self.statuses.append(
            {"rtt_ms": rtt_ms, "rate_hz": rate_hz, "errors": errors, "connected": connected}
        )

    def note(
        self, name: str, level: str, message: str, time_ns: int, source: str = "instruments"
    ) -> None:
        self.notes.append((level, message, source))

    def fields(self, name: str) -> dict[str, str | None]:
        return dict(self._fields.get(name, {}))

    def messages(self) -> list[str]:
        return [m for _, m, _ in self.notes]


def _launch(
    profile: Profile,
    link: ScriptedLink,
    emitter: RecordingEmitter,
    opener: Callable[[Address, LinkSettings], Link] | None,
    overrides: dict[str, Any],
) -> InstrumentWorker:
    overrides.setdefault("rate_hz", 50)
    cfg = instrument_config(**overrides)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener or opener_for(link))
    worker.start()
    return worker


def connected(
    profile: Profile, handler: Callable[[str], str | bytes | None], /, **overrides: Any
) -> tuple[InstrumentWorker, ScriptedLink, RecordingEmitter]:
    """A worker that is connected but not running, for a test that steps its methods by hand."""
    link = ScriptedLink(handler)
    emitter = RecordingEmitter()
    overrides.setdefault("rate_hz", 50)
    worker = InstrumentWorker(
        instrument_config(**overrides), [profile], emitter, open=opener_for(link)
    )
    assert worker._connect(), worker.health
    return worker, link, emitter


def start(
    profile: Profile,
    handler: Callable[[str], str | bytes | None],
    emitter: RecordingEmitter,
    /,
    *,
    opener: Any = None,
    where: str = "127.0.0.1:5555",
    **overrides: Any,
) -> tuple[InstrumentWorker, ScriptedLink]:
    """Start a worker on a scripted link without waiting for it to open."""
    link = ScriptedLink(handler, where)
    return _launch(profile, link, emitter, opener, overrides), link


def start_open(
    profile: Profile, link: ScriptedLink, /, **overrides: Any
) -> tuple[InstrumentWorker, RecordingEmitter]:
    """Start a worker on `link` and wait until it is open."""
    emitter = RecordingEmitter()
    worker = _launch(profile, link, emitter, None, overrides)
    assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
    return worker, emitter


def ask(worker: InstrumentWorker, kind: Any, **args: Any) -> dict[str, Any]:
    return worker.request(kind, **args).result(timeout=5)


@pytest.fixture
def running(dp800: Profile, dp832: Dp832):
    emitter = RecordingEmitter()
    worker, link = start(dp800, dp832, emitter)
    assert wait_until(lambda: worker.state == "open")
    yield worker, link, emitter, dp832
    worker.stop()
    worker.join(2)


def minimal() -> dict[str, Any]:
    """The smallest profile that passes every check."""
    return {
        "schema": 1,
        "id": "acme-psu",
        "name": "Acme PSU",
        "category": "psu",
        "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-20"},
        "match": {"manufacturer": "(?i)^acme", "model": "^P1$"},
        "signals": {"voltage": {"query": "MEAS:VOLT? {ch}", "parse": "float"}},
    }


def profile_from(raw: dict[str, Any]) -> Profile:
    return from_dict(raw, f"{raw['id']}.toml")


# A loopback server that answers the link tests' commands.
IDN = "ACME,PSU-1,SN1,1.0"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, LineServer)
        pending = b""
        while True:
            try:
                data = self.request.recv(1024)
            except OSError:
                return
            if not data:
                return
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = line.decode().rstrip("\r")
                server.received.append(text)
                if not self.answer(text):
                    return

    def answer(self, command: str) -> bool:
        sock = self.request
        if command == "*IDN?":
            sock.sendall(f"{IDN}\n".encode())
        elif command == "SPLIT?":
            sock.sendall(b"HEL")
            time.sleep(0.15)
            sock.sendall(b"LO\n")
        elif command == "TWO?":
            sock.sendall(b"ONE\nTWO\n")
        elif command == "CRLF?":
            sock.sendall(b"OK\r\n")
        elif command == "BAD?":
            sock.sendall(b"\xff\xfeA\n")
        elif command == "DROP?":
            sock.sendall(b"PAR")
            return False
        elif command == "BLOCK?":
            sock.sendall(b"#15HELLO\r\n")
        elif command == "BLOCKSPLIT?":
            sock.sendall(b"#15HE")
            time.sleep(0.15)
            sock.sendall(b"LLO\n")
        elif command == "HEADERSPLIT?":
            sock.sendall(b"#")
            time.sleep(0.15)
            sock.sendall(b"15HELLO\n")
        elif command == "SHORTBLOCK?":
            sock.sendall(b"#15HEL")
        elif command == "NOTBLOCK?":
            sock.sendall(b"OK\n")
        elif command == "INDEFINITE?":
            sock.sendall(b"#0HELLO\n")
        elif command == "BADLENGTH?":
            sock.sendall(b"#2xyHELLO\n")
        elif command.startswith("ECHO "):
            sock.sendall(command.removeprefix("ECHO ").encode() + b"\n")
        return True


class LineServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.received: list[str] = []

    @property
    def port(self) -> int:
        return self.server_address[1]


@pytest.fixture
def instrument() -> Iterator[LineServer]:
    server = LineServer()
    # A short poll interval: shutdown() waits for one, and teardown runs for every test.
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
