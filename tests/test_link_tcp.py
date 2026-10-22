import select
import socket
import threading
import time
from typing import Any

import pytest

from tests.conftest import IDN, LineServer, start_blocked_query
from zelos_extension_instruments.link import LinkError, LinkSettings, LinkTimeout, TcpAddress, tcp
from zelos_extension_instruments.link.tcp import open_tcp

FAST = LinkSettings(timeout_s=0.3)


def connect(server: LineServer, settings: LinkSettings = FAST):
    return open_tcp(TcpAddress("127.0.0.1", server.port), settings)


def test_query_returns_the_reply_without_its_terminator(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        assert link.query("*IDN?") == IDN
        assert link.where == f"127.0.0.1:{instrument.port}"
    finally:
        link.close()


def test_write_appends_the_write_terminator(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(write_terminator="\r\n", timeout_s=0.3))
    try:
        assert link.query("ECHO hi") == "hi"
        assert instrument.received == ["ECHO hi"]
    finally:
        link.close()


def test_read_terminator_can_be_longer_than_one_byte(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(read_terminator="\r\n", timeout_s=0.3))
    try:
        assert link.query("CRLF?") == "OK"
    finally:
        link.close()


def test_reply_split_across_two_sends_is_joined(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(timeout_s=2))
    try:
        assert link.query("SPLIT?") == "HELLO"
    finally:
        link.close()


def test_second_reply_in_one_packet_waits_for_the_next_read(
    instrument: LineServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_receive(deadline: float, size: int) -> None:
        raise AssertionError("the buffered reply went back to the socket")

    link = connect(instrument)
    try:
        assert link.query("TWO?") == "ONE"
        monkeypatch.setattr(link, "_receive", no_receive)
        assert link.read() == "TWO"
    finally:
        link.close()


def test_invalid_utf8_is_replaced(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        assert link.query("BAD?") == "��A"
    finally:
        link.close()


def test_silent_instrument_times_out_within_the_timeout(instrument: LineServer) -> None:
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


def test_a_timed_out_link_keeps_working_after_clear(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        with pytest.raises(LinkTimeout):
            link.query("NOREPLY?")
        link.clear()
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def test_connection_closed_mid_reply_is_a_link_error(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(timeout_s=2))
    try:
        with pytest.raises(LinkError) as caught:
            link.query("DROP?")
        assert not isinstance(caught.value, LinkTimeout)
        assert caught.value.kind == "other"
        assert f"127.0.0.1:{instrument.port}" in str(caught.value)
    finally:
        link.close()


def test_write_to_a_dropped_connection_is_a_link_error(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(timeout_s=2))
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


def test_clear_drops_pending_bytes_and_waits_only_for_quiet(instrument: LineServer) -> None:
    """A late reply still arriving is dropped too: clear ends after 0.1 s of silence, not at the
    read timeout."""
    link = connect(instrument, LinkSettings(timeout_s=10))
    try:
        link.write("TWO?")
        sock = link._sock  # type: ignore[attr-defined]
        assert select.select([sock], [], [], 5)[0]
        started = time.monotonic()
        link.clear()
        assert 0.05 < time.monotonic() - started < 5
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def test_delay_spaces_consecutive_writes(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(timeout_s=1, delay_ms=200))
    try:
        link.write("*IDN?")
        started = time.monotonic()
        link.write("*IDN?")
        assert time.monotonic() - started >= 0.15
    finally:
        link.close()


def test_close_is_idempotent(instrument: LineServer) -> None:
    link = connect(instrument)
    link.close()
    link.close()
    with pytest.raises(LinkError):
        link.write("*IDN?")


def test_close_from_another_thread_interrupts_a_blocked_read(
    instrument: LineServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = connect(instrument, LinkSettings(timeout_s=10))
    worker, errors = start_blocked_query(link, monkeypatch)
    started = time.monotonic()
    link.close()
    worker.join(8)
    assert not worker.is_alive()
    # Well before the 10 s read timeout.
    assert time.monotonic() - started < 5
    assert len(errors) == 1
    assert not isinstance(errors[0], LinkTimeout)


def test_close_ends_a_blocked_read_even_where_shutdown_does_not_wake_it(
    instrument: LineServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the macOS runner a shutdown sometimes left the read blocked until its own timeout."""
    monkeypatch.setattr(socket.socket, "shutdown", lambda self, how: None)
    link = connect(instrument, LinkSettings(timeout_s=10))
    worker, errors = start_blocked_query(link, monkeypatch)
    started = time.monotonic()
    link.close()
    worker.join(8)
    assert not worker.is_alive()
    # Well before the 10 s read timeout.
    assert time.monotonic() - started < 5
    assert len(errors) == 1 and not isinstance(errors[0], LinkTimeout)


def test_socket_is_tuned_for_low_latency_and_dead_peer_detection(
    instrument: LineServer,
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
    instrument: LineServer, closed_port: int, monkeypatch: pytest.MonkeyPatch
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


class _Clock:
    """A clock the pacer reads and sleeps on, so the test measures its pauses exactly."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_the_pacer_waits_longer_only_after_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supply dropped a command sent 100 ms after another; a query is paced by its reply."""
    from zelos_extension_instruments import link as link_module

    clock = _Clock()
    monkeypatch.setattr(link_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(link_module.time, "sleep", clock.sleep)
    pacer = link_module.Pacer(100, 300)
    pacer.wait()
    pacer.mark("OUTP CH1,OFF")
    pacer.wait()
    pacer.mark("OUTP CH2,OFF")
    pacer.wait()
    pacer.mark("SYST:STAT?")
    pacer.wait()
    pacer.mark("MEAS:VOLT? CH1")
    assert clock.slept == [0.3, 0.3, 0.1]


def test_the_pacer_keeps_the_plain_delay_without_a_command_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zelos_extension_instruments import link as link_module

    clock = _Clock()
    monkeypatch.setattr(link_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(link_module.time, "sleep", clock.sleep)
    pacer = link_module.Pacer(50)
    for written in ("VOLT 1", "VOLT?", "CURR 2"):
        pacer.wait()
        pacer.mark(written)
    pacer.wait()
    assert clock.slept == [0.05, 0.05, 0.05]
