"""Raw TCP to an instrument's SCPI socket port, on the standard library `socket`."""

import socket
import sys
import threading
import time
from contextlib import suppress
from typing import Any

from . import (
    CONNECT_TIMEOUT_S,
    Lines,
    Link,
    LinkError,
    LinkTimeout,
    Pacer,
    Settings,
    TcpAddress,
)

_CHUNK = 4096
_TCP_USER_TIMEOUT = 18  # Linux; Python only exposes the constant from 3.12
_KEEPALIVE_IDLE_S = 10
_KEEPALIVE_INTERVAL_S = 5
_KEEPALIVE_PROBES = 4


def _seconds(value: float) -> str:
    return f"{value:g} s"


def tune_socket(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == "win32":
        idle_ms, interval_ms = _KEEPALIVE_IDLE_S * 1000, _KEEPALIVE_INTERVAL_S * 1000
        with suppress(OSError):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_ms, interval_ms))
        return
    idle = "TCP_KEEPALIVE" if sys.platform == "darwin" else "TCP_KEEPIDLE"
    options = {
        idle: _KEEPALIVE_IDLE_S,
        "TCP_KEEPINTVL": _KEEPALIVE_INTERVAL_S,
        "TCP_KEEPCNT": _KEEPALIVE_PROBES,
    }
    for name, value in options.items():
        option = getattr(socket, name, None)
        if option is not None:
            with suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    if sys.platform == "linux":
        total_ms = (_KEEPALIVE_IDLE_S + _KEEPALIVE_INTERVAL_S * _KEEPALIVE_PROBES) * 1000
        with suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, _TCP_USER_TIMEOUT, total_ms)


def _resolve(address: TcpAddress, timeout_s: float) -> list[tuple]:
    """Look the host up on a helper thread: a stalled mDNS lookup cannot outlive the timeout."""
    outcome: list[list[tuple] | BaseException] = []
    done = threading.Event()

    def lookup() -> None:
        try:
            outcome.append(socket.getaddrinfo(address.host, address.port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=lookup, name="tcp-resolve", daemon=True).start()
    if not done.wait(timeout_s):
        raise LinkError(
            f"No answer from {address.where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout"
        )
    result = outcome[0]
    if isinstance(result, BaseException):
        if isinstance(result, socket.gaierror):
            raise LinkError(f"Cannot find the host in {address.where}.", "unresolved") from result
        raise LinkError(f"Cannot look up {address.where}: {result}.", "other") from result
    return result


def _open_failure(address: TcpAddress, exc: OSError) -> LinkError:
    if isinstance(exc, ConnectionRefusedError):
        return LinkError(f"{address.where} refused the connection.", "refused")
    if isinstance(exc, TimeoutError):
        return LinkError(
            f"No answer from {address.where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout"
        )
    return LinkError(f"Cannot connect to {address.where}: {exc.strerror or exc}.", "other")


# Ports an instrument that is up answers on even while its SCPI socket is held: the VXI-11
# portmapper, the LXI web page and the telnet console.
_VXI11_PORT = 111
_NEIGHBOR_PORTS = (_VXI11_PORT, 80, 5024)
_NEIGHBOR_TIMEOUT_S = 0.5


def held_failure(address: TcpAddress, infos: list[Any]) -> LinkError:
    """The error for a connect that timed out: a held socket when the host answers on another
    port, else the plain timeout."""
    answered: list[int] = []
    for port in _NEIGHBOR_PORTS:
        for family, kind, proto, _, target in infos:
            try:
                sock = socket.socket(family, kind, proto)
            except OSError:
                continue
            try:
                sock.settimeout(_NEIGHBOR_TIMEOUT_S)
                sock.connect((target[0], port, *target[2:]))
                answered.append(port)
                break
            except OSError:
                continue
            finally:
                sock.close()
    if not answered:
        return LinkError(
            f"No answer from {address.where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout"
        )
    host = f"[{address.host}]" if ":" in address.host else address.host
    text = f"{host} is on the network, but port {address.port} is not accepting connections"
    if _VXI11_PORT in answered:
        text += (
            f"; it answers VXI-11 on port 111, which the VISA connection reaches as "
            f"TCPIP::{address.host}::INSTR"
        )
    return LinkError(text + ".", "held")


def open_tcp(address: TcpAddress, settings: Settings) -> Link:
    """Connect to a host and port; raises LinkError."""
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    infos = _resolve(address, CONNECT_TIMEOUT_S)
    failure: LinkError | None = None
    for index, (family, kind, proto, _, target) in enumerate(infos):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(remaining / (len(infos) - index))
            sock.connect(target)
            tune_socket(sock)
        except OSError as exc:
            sock.close()
            failure = _open_failure(address, exc)
            continue
        return TcpLink(sock, address.where, settings)
    if failure is None or failure.kind == "timeout":
        failure = held_failure(address, infos)
    raise failure


class TcpLink:
    def __init__(self, sock: socket.socket, where: str, settings: Settings) -> None:
        self.where = where
        self._sock = sock
        self._settings = settings
        self._pacer = Pacer(settings.delay_ms)
        self._lines = Lines(settings.read_terminator)
        self._closed = False

    def write(self, command: str) -> None:
        data = (command + self._settings.write_terminator).encode()
        self._pacer.wait()
        try:
            self._sock.settimeout(self._settings.timeout_s)
            self._sock.sendall(data)
        except TimeoutError as exc:
            raise LinkError(
                f"{self.where} did not accept a command within "
                f"{_seconds(self._settings.timeout_s)}.",
                "timeout",
            ) from exc
        except OSError as exc:
            raise self._broken(exc) from exc
        self._pacer.mark()

    def read(self) -> str:
        deadline = time.monotonic() + self._settings.timeout_s
        while True:
            line = self._lines.pop()
            if line is not None:
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._no_reply()
            try:
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(_CHUNK)
            except TimeoutError as exc:
                raise self._no_reply() from exc
            except OSError as exc:
                raise self._broken(exc) from exc
            if not chunk:
                raise self._broken(None)
            self._lines.feed(chunk)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._lines.clear()
        try:
            self._sock.settimeout(0)
            while self._sock.recv(_CHUNK):
                pass
        except OSError:
            pass

    def close(self) -> None:
        self._closed = True
        with suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            self._sock.close()

    def _no_reply(self) -> LinkTimeout:
        return LinkTimeout(
            f"no reply from {self.where} within {_seconds(self._settings.timeout_s)}"
        )

    def _broken(self, exc: OSError | None) -> LinkError:
        if self._closed:
            return LinkError(f"The link to {self.where} was closed.", "other")
        if exc is None:
            return LinkError(f"{self.where} closed the connection.", "other")
        return LinkError(
            f"The connection to {self.where} was lost: {exc.strerror or exc}.", "other"
        )
