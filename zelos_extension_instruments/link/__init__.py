"""The byte pipe a worker owns: raw TCP, serial, or a VISA resource.

Every link speaks lines: a command goes out with the write terminator, a reply comes back up to
the read terminator. A link is touched only by its worker's thread, except close(), which stop()
may call from another thread to interrupt a blocked read.
"""

import time
from dataclasses import dataclass
from typing import Literal, Protocol

Kind = Literal[
    "not_found", "refused", "unresolved", "busy", "permission", "timeout", "config", "other"
]

# Short, because a blocked connect holds the worker's thread: Get State and stop wait behind it.
CONNECT_TIMEOUT_S = 2.0


class LinkError(Exception):
    """The link cannot be opened or has failed; `kind` says why, `str()` says it in a sentence."""

    def __init__(self, message: str, kind: Kind = "other") -> None:
        super().__init__(message)
        self.kind: Kind = kind


class LinkTimeout(LinkError):
    """No reply arrived within the timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "timeout")


@dataclass(frozen=True, slots=True)
class Settings:
    """How lines are framed and how long a reply may take."""

    write_terminator: str = "\n"
    read_terminator: str = "\n"
    timeout_s: float = 2.0
    # A pause the vendor documents between commands; the link waits it out before each write.
    delay_ms: int = 0


@dataclass(frozen=True, slots=True)
class TcpAddress:
    host: str
    port: int

    @property
    def where(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class SerialAddress:
    port: str
    baud: int = 9600
    data_bits: int = 8
    parity: Literal["none", "even", "odd", "mark", "space"] = "none"
    stop_bits: float = 1.0
    flow_control: Literal["none", "rts/cts", "xon/xoff"] = "none"

    @property
    def where(self) -> str:
        return self.port


@dataclass(frozen=True, slots=True)
class VisaAddress:
    resource: str
    # "auto" uses the system VISA for USB and GPIB resources when one is installed, else pyvisa-py.
    backend: Literal["auto", "py", "ivi"] = "auto"

    @property
    def where(self) -> str:
        return self.resource


Address = TcpAddress | SerialAddress | VisaAddress


class Link(Protocol):
    """An open line-based connection to one instrument."""

    where: str

    def write(self, command: str) -> None:
        """Send a command with the write terminator; raise LinkError when the link is gone."""
        ...

    def read(self) -> str:
        """One reply without its terminator; raise LinkTimeout when none arrives in time."""
        ...

    def query(self, command: str) -> str:
        """Send a command and return its reply."""
        ...

    def clear(self) -> None:
        """Drop pending bytes, and clear the device where the transport can (VISA, USB-TMC)."""
        ...

    def close(self) -> None:
        """Close the link; safe to call twice and from another thread."""
        ...


class Pacer:
    """Keeps the vendor's inter-command delay between writes."""

    def __init__(self, delay_ms: int) -> None:
        self._delay_s = delay_ms / 1000
        self._last = 0.0

    def wait(self) -> None:
        if self._delay_s <= 0:
            return
        remaining = self._last + self._delay_s - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def mark(self) -> None:
        self._last = time.monotonic()


class Lines:
    """Bytes received so far, handed out one terminated line at a time."""

    def __init__(self, terminator: str) -> None:
        self._terminator = terminator.encode()
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk

    def pop(self) -> str | None:
        """The next complete line without its terminator, or None until one has arrived."""
        end = self._buffer.find(self._terminator) if self._terminator else len(self._buffer) or -1
        if end < 0:
            return None
        line = bytes(self._buffer[:end])
        del self._buffer[: end + len(self._terminator)]
        return line.decode("utf-8", errors="replace")

    def clear(self) -> None:
        self._buffer.clear()


def open_link(address: Address, settings: Settings) -> Link:
    """Open the link an address describes; raises LinkError."""
    if isinstance(address, TcpAddress):
        from .tcp import open_tcp

        return open_tcp(address, settings)
    if isinstance(address, SerialAddress):
        from .serial import open_serial

        return open_serial(address, settings)
    from .visa import open_visa

    return open_visa(address, settings)
