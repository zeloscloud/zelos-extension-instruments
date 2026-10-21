import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from zelos_extension_instruments.link import LinkError, LinkTimeout, Settings, TcpAddress, tcp
from zelos_extension_instruments.link.tcp import open_tcp

IDN = "ACME,PSU-1,SN1,1.0"
FAST = Settings(timeout_s=0.3)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, FakeInstrument)
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
        elif command.startswith("ECHO "):
            sock.sendall(command.removeprefix("ECHO ").encode() + b"\n")
        return True


class FakeInstrument(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.received: list[str] = []

    @property
    def port(self) -> int:
        return self.server_address[1]


@pytest.fixture
def instrument() -> Iterator[FakeInstrument]:
    server = FakeInstrument()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connect(server: FakeInstrument, settings: Settings = FAST):
    return open_tcp(TcpAddress("127.0.0.1", server.port), settings)


def test_query_returns_the_reply_without_its_terminator(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    try:
        assert link.query("*IDN?") == IDN
        assert link.where == f"127.0.0.1:{instrument.port}"
    finally:
        link.close()


def test_write_appends_the_write_terminator(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(write_terminator="\r\n", timeout_s=0.3))
    try:
        assert link.query("ECHO hi") == "hi"
        assert instrument.received == ["ECHO hi"]
    finally:
        link.close()


def test_read_terminator_can_be_longer_than_one_byte(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(read_terminator="\r\n", timeout_s=0.3))
    try:
        assert link.query("CRLF?") == "OK"
    finally:
        link.close()


def test_reply_split_across_two_sends_is_joined(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(timeout_s=2))
    try:
        assert link.query("SPLIT?") == "HELLO"
    finally:
        link.close()


def test_second_reply_in_one_packet_waits_for_the_next_read(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    try:
        assert link.query("TWO?") == "ONE"
        started = time.monotonic()
        assert link.read() == "TWO"
        assert time.monotonic() - started < 0.1
    finally:
        link.close()


def test_invalid_utf8_is_replaced(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    try:
        assert link.query("BAD?") == "��A"
    finally:
        link.close()


def test_silent_instrument_times_out_within_the_timeout(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    try:
        started = time.monotonic()
        with pytest.raises(LinkTimeout) as caught:
            link.query("NOREPLY?")
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 1.0
        assert str(caught.value) == f"no reply from 127.0.0.1:{instrument.port} within 0.3 s"
        assert caught.value.kind == "timeout"
    finally:
        link.close()


def test_a_timed_out_link_keeps_working_after_clear(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    try:
        with pytest.raises(LinkTimeout):
            link.query("NOREPLY?")
        link.clear()
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def test_connection_closed_mid_reply_is_a_link_error(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(timeout_s=2))
    try:
        with pytest.raises(LinkError) as caught:
            link.query("DROP?")
        assert not isinstance(caught.value, LinkTimeout)
        assert caught.value.kind == "other"
        assert f"127.0.0.1:{instrument.port}" in str(caught.value)
    finally:
        link.close()


def test_write_to_a_dropped_connection_is_a_link_error(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(timeout_s=2))
    try:
        with pytest.raises(LinkError):
            link.query("DROP?")
        with pytest.raises(LinkError) as caught:
            for _ in range(10):
                link.write("*IDN?")
                time.sleep(0.05)
        assert caught.value.kind == "other"
    finally:
        link.close()


def test_clear_drops_pending_bytes_and_waits_only_for_quiet(instrument: FakeInstrument) -> None:
    """A late reply still arriving is dropped too: clear ends after 0.1 s of silence."""
    link = connect(instrument)
    try:
        link.write("TWO?")
        time.sleep(0.2)
        started = time.monotonic()
        link.clear()
        assert 0.05 < time.monotonic() - started < 0.5
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def test_delay_spaces_consecutive_writes(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(timeout_s=1, delay_ms=200))
    try:
        link.write("*IDN?")
        started = time.monotonic()
        link.write("*IDN?")
        assert time.monotonic() - started >= 0.15
    finally:
        link.close()


def test_close_is_idempotent(instrument: FakeInstrument) -> None:
    link = connect(instrument)
    link.close()
    link.close()
    with pytest.raises(LinkError):
        link.write("*IDN?")


def test_close_from_another_thread_interrupts_a_blocked_read(instrument: FakeInstrument) -> None:
    link = connect(instrument, Settings(timeout_s=10))
    errors: list[LinkError] = []

    def blocked() -> None:
        try:
            link.query("NOREPLY?")
        except LinkError as exc:
            errors.append(exc)

    worker = threading.Thread(target=blocked)
    worker.start()
    time.sleep(0.2)
    started = time.monotonic()
    link.close()
    worker.join(2)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1.5
    assert len(errors) == 1
    assert not isinstance(errors[0], LinkTimeout)


def test_socket_is_tuned_for_low_latency_and_dead_peer_detection(
    instrument: FakeInstrument,
) -> None:
    link = connect(instrument)
    try:
        sock = link._sock  # type: ignore[attr-defined]
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    finally:
        link.close()


def test_refused_port_is_kind_refused(closed_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows reports a refused loopback connect after about 2 s, so the default deadline races it.
    monkeypatch.setattr(tcp, "CONNECT_TIMEOUT_S", 5.0)
    with pytest.raises(LinkError) as caught:
        open_tcp(TcpAddress("127.0.0.1", closed_port), FAST)
    assert caught.value.kind == "refused"
    assert f"127.0.0.1:{closed_port}" in str(caught.value)


def test_unresolvable_host_is_kind_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> list:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(LinkError) as caught:
        open_tcp(TcpAddress("psu.local", 5025), FAST)
    assert caught.value.kind == "unresolved"
    assert "psu.local:5025" in str(caught.value)


def test_a_stalled_name_lookup_cannot_outlive_the_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def stall(*args: object, **kwargs: object) -> list:
        release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", stall)
    monkeypatch.setattr(tcp, "CONNECT_TIMEOUT_S", 0.3)
    started = time.monotonic()
    try:
        with pytest.raises(LinkError) as caught:
            open_tcp(TcpAddress("psu.local", 5025), FAST)
        assert time.monotonic() - started < 1.5
        assert caught.value.kind == "timeout"
    finally:
        release.set()


def test_open_tries_the_next_address_when_one_fails(
    instrument: FakeInstrument, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two_addresses(*args: object, **kwargs: object) -> list:
        stream = socket.SOCK_STREAM
        return [
            (socket.AF_INET, stream, 6, "", ("127.0.0.1", closed_port)),
            (socket.AF_INET, stream, 6, "", ("127.0.0.1", instrument.port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", two_addresses)
    link = open_tcp(TcpAddress("psu.local", instrument.port), FAST)
    try:
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def _listener() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    return sock


def test_a_connect_timeout_on_a_host_that_answers_elsewhere_is_a_held_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SCPI socket with its one client slot taken stops completing handshakes while the
    instrument's other ports still answer."""
    other = _listener()
    try:
        port = other.getsockname()[1]
        address = TcpAddress("127.0.0.1", 1)
        infos = socket.getaddrinfo("127.0.0.1", 1, type=socket.SOCK_STREAM)

        monkeypatch.setattr(tcp, "_NEIGHBOR_PORTS", (port,))
        error = tcp.held_failure(address, infos)
        assert error.kind == "held"
        assert str(error) == "127.0.0.1 is on the network, but port 1 is not accepting connections."

        monkeypatch.setattr(tcp, "_VXI11_PORT", port)
        error = tcp.held_failure(address, infos)
        assert str(error) == (
            "127.0.0.1 is on the network, but port 1 is not accepting connections; it answers "
            "VXI-11 on port 111, which the VISA connection reaches as TCPIP::127.0.0.1::INSTR."
        )

        monkeypatch.setattr(tcp, "_NEIGHBOR_PORTS", (1,))
        error = tcp.held_failure(address, infos)
        assert error.kind == "timeout"
        assert str(error) == "No answer from 127.0.0.1:1 within 2 s."
    finally:
        other.close()


def test_open_tcp_classifies_a_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(self: socket.socket, target: Any) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", never)
    consulted: list[TcpAddress] = []

    def classify(address: TcpAddress, infos: list[Any]) -> LinkError:
        consulted.append(address)
        return LinkError("held", "held")

    monkeypatch.setattr(tcp, "held_failure", classify)
    with pytest.raises(LinkError) as caught:
        open_tcp(TcpAddress("127.0.0.1", 1), FAST)
    assert caught.value.kind == "held"
    assert consulted == [TcpAddress("127.0.0.1", 1)]
