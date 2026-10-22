import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from zelos_extension_instruments.profile import Profile
from zelos_extension_instruments.profile_load import SHIPPED_DIR, from_dict, load_file
from zelos_extension_instruments.record import load_transcript
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.sim.faults import Faults
from zelos_extension_instruments.sim.server import FakeServer

RIGOL = SHIPPED_DIR / "psu" / "rigol-dp800.toml"


class Client:
    def __init__(self, port: int, command_end: bytes = b"\n", reply_end: bytes = b"\n") -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.command_end = command_end
        self.reply_end = reply_end
        self.buffer = b""

    def write(self, command: str) -> None:
        self.sock.sendall(command.encode() + self.command_end)

    def read(self) -> bytes:
        while self.reply_end not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError(self.buffer)
            self.buffer += chunk
        reply, self.buffer = self.buffer.split(self.reply_end, 1)
        return reply

    def query(self, command: str) -> str:
        self.write(command)
        return self.read().decode()

    def rest(self) -> bytes:
        """Everything the server sends until it closes the connection."""
        data = self.buffer
        while chunk := self.sock.recv(4096):
            data += chunk
        return data

    def closed(self) -> bool:
        """Whether the server closes the connection within the timeout."""
        try:
            self.rest()
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        except TimeoutError:
            return False
        return True

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def rigol() -> Profile:
    return load_file(RIGOL)


@pytest.fixture
def instrument(rigol: Profile) -> FakeInstrument:
    return FakeInstrument(rigol, "DP832")


@pytest.fixture
def connect() -> Iterator[Callable[..., Client]]:
    clients: list[Client] = []

    def open_client(port: int, command_end: bytes = b"\n", reply_end: bytes = b"\n") -> Client:
        client = Client(port, command_end, reply_end)
        clients.append(client)
        return client

    yield open_client
    for client in clients:
        client.close()


def eventually(check: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_query_round_trip(instrument, connect):
    with FakeServer(instrument) as server:
        client = connect(server.port)
        assert client.query("*IDN?") == instrument.idn
        client.write(":SOUR1:VOLT 5.000")
        assert float(client.query(":SOUR1:VOLT?")) == 5.0
        assert client.query("SYST:ERR?") == '0,"No error"'
    assert (":SOUR1:VOLT 5.000", None) in instrument.transcript


def test_framing_follows_the_profile_terminators(connect):
    raw = {
        "schema": 1,
        "id": "crlf-psu",
        "name": "CRLF supply",
        "category": "psu",
        "evidence": {"badge": "manual", "source": "test", "date": "2026-10-20"},
        "match": {"manufacturer": "(?i)^acme", "model": "^PS1"},
        "link": {"write_terminator": "\r\n", "read_terminator": "\r"},
        "signals": {"voltage": {"query": "MEAS:VOLT?", "parse": "float"}},
    }
    instrument = FakeInstrument(from_dict(raw, "crlf-psu.toml"))
    with FakeServer(instrument) as server:
        client = connect(server.port, b"\r\n", b"\r")
        client.sock.sendall(b"*IDN?\r\nMEAS:VOLT?\r\n")
        assert client.read().decode() == instrument.idn
        assert client.read() == b"0"


def test_two_clients_share_one_instrument(instrument, connect):
    with FakeServer(instrument) as server:
        first, second = connect(server.port), connect(server.port)
        first.write(":SOUR2:VOLT 7.500")
        # A round trip on the first client proves the server has taken its set before the
        # second client reads; the two sockets are served in whatever order they arrive.
        assert first.query("*OPC?") == "1"
        assert float(second.query(":SOUR2:VOLT?")) == 7.5
        assert first.query("*IDN?") == second.query("*IDN?") == instrument.idn


def test_refuse_second_client(instrument, connect):
    faults = Faults()
    faults.refuse_second_client = True
    with FakeServer(instrument, faults=faults) as server:
        first = connect(server.port)
        assert first.query("*OPC?") == "1"
        second = connect(server.port)
        assert second.closed()
        assert first.query("*OPC?") == "1"
        first.close()

        def third_answers() -> bool:
            try:
                return connect(server.port).query("*OPC?") == "1"
            except (EOFError, OSError):
                return False

        assert eventually(third_answers)


def test_drop_after_closes_once(instrument, connect):
    faults = Faults()
    faults.drop_after(2)
    with FakeServer(instrument, faults=faults) as server:
        client = connect(server.port)
        assert client.query("*OPC?") == "1"
        assert client.query("*IDN?") == instrument.idn
        assert client.closed()
        again = connect(server.port)
        for _ in range(3):
            assert again.query("*OPC?") == "1"


def test_stall_delays_every_reply(instrument, connect):
    faults = Faults()
    with FakeServer(instrument, faults=faults) as server:
        client = connect(server.port)
        faults.stall(0.3)
        # Windows timers tick every 15.6 ms, so a wait can measure a tick short.
        for _ in range(2):
            started = time.monotonic()
            assert client.query("*OPC?") == "1"
            assert 0.27 <= time.monotonic() - started < 1.5
        faults.stall(0)
        started = time.monotonic()
        assert client.query("*OPC?") == "1"
        assert time.monotonic() - started < 0.3


def test_garbage_once(instrument, connect):
    faults = Faults()
    with FakeServer(instrument, faults=faults) as server:
        client = connect(server.port)
        faults.garbage_once()
        client.write("*IDN?")
        assert client.read() == b"\xff\xfe??"
        assert client.query("*IDN?") == instrument.idn


def test_half_reply_once_then_close(instrument, connect):
    faults = Faults()
    with FakeServer(instrument, faults=faults) as server:
        client = connect(server.port)
        faults.half_reply_once()
        client.write("*IDN?")
        idn = instrument.idn.encode()
        assert client.rest() == idn[: len(idn) // 2]
        assert connect(server.port).query("*IDN?") == instrument.idn


def test_restart_closes_clients_and_keeps_state(instrument, connect):
    faults = Faults()
    with FakeServer(instrument, faults=faults) as server:
        client = connect(server.port)
        client.write(":SOUR1:VOLT 3.300")
        assert client.query("*OPC?") == "1"
        faults.restart()
        assert client.closed()
        assert float(connect(server.port).query(":SOUR1:VOLT?")) == 3.3


def test_reset_clears_faults(instrument, connect):
    faults = Faults()
    faults.stall(5)
    faults.drop_after(0)
    faults.garbage_once()
    faults.refuse_second_client = True
    faults.reset()
    with FakeServer(instrument, faults=faults) as server:
        first, second = connect(server.port), connect(server.port)
        started = time.monotonic()
        assert first.query("*OPC?") == second.query("*OPC?") == "1"
        assert time.monotonic() - started < 1


TRANSCRIPT = """
# recorded from a unit
> *IDN?
< RIGOL TECHNOLOGIES,DP832,DP8C000001,00.01.16

> :SOUR1:VOLT 5.000
> :SOUR1:VOLT?
< 5.000
> :OUTP? CH1
<
"""


def test_load_transcript(tmp_path: Path):
    path = tmp_path / "dp832.txt"
    path.write_text(TRANSCRIPT, encoding="utf-8")
    assert load_transcript(path) == [
        ("*IDN?", "RIGOL TECHNOLOGIES,DP832,DP8C000001,00.01.16"),
        (":SOUR1:VOLT 5.000", None),
        (":SOUR1:VOLT?", "5.000"),
        (":OUTP? CH1", ""),
    ]
    path.write_text("< 5.000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dp832.txt:1"):
        load_transcript(path)


def test_strict_replay_answers_the_transcript(tmp_path: Path, connect):
    path = tmp_path / "dp832.txt"
    path.write_text(TRANSCRIPT, encoding="utf-8")
    with FakeServer(replay=load_transcript(path)) as server:
        client = connect(server.port)
        assert client.query("*IDN?") == "RIGOL TECHNOLOGIES,DP832,DP8C000001,00.01.16"
        client.write(":SOUR1:VOLT 5.000")
        assert client.query(":SOUR1:VOLT?") == "5.000"
        assert client.query(":OUTP? CH1") == ""
        assert server.remaining == []
        assert server.mismatch is None


def test_strict_replay_records_a_mismatch(connect):
    pairs = [("*IDN?", "ACME,PS1,1,1"), (":SOUR1:VOLT?", "5.000")]
    with FakeServer(replay=pairs) as server:
        client = connect(server.port)
        assert client.query("*IDN?") == "ACME,PS1,1,1"
        client.write(":SOUR2:VOLT?")
        assert client.closed()
        assert server.mismatch == "expected ':SOUR1:VOLT?', got ':SOUR2:VOLT?'"
        assert server.remaining == [(":SOUR1:VOLT?", "5.000")]


def test_strict_replay_records_commands_past_the_end(connect):
    with FakeServer(replay=[("*IDN?", "ACME,PS1,1,1")]) as server:
        client = connect(server.port)
        assert client.query("*IDN?") == "ACME,PS1,1,1"
        client.write("*OPC?")
        assert client.closed()
        assert server.mismatch == "got '*OPC?' after the transcript ended"


def test_server_needs_something_to_serve():
    with pytest.raises(ValueError):
        FakeServer()
    with pytest.raises(RuntimeError):
        _ = FakeServer(replay=[]).port
