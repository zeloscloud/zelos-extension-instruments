"""The byte pipe a worker owns: raw TCP, serial, or a VISA resource.

Every link speaks lines: a command goes out with the write terminator, a reply comes back up to
the read terminator. A link is touched only by its worker's thread, except close(), which stop()
may call from another thread to interrupt a blocked read.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Protocol

from ..waveform import BlockError, block_header

Kind = Literal[
    "not_found",
    "refused",
    "unresolved",
    "busy",
    "held",
    "reply",
    "permission",
    "timeout",
    "config",
    "other",
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
class LinkSettings:
    """How lines are framed and how long a reply may take."""

    write_terminator: str = "\n"
    read_terminator: str = "\n"
    timeout_s: float = 2.0
    # A pause the vendor documents between commands; the link waits it out before each write.
    delay_ms: int = 0
    # The pause after a command that sends no reply, when an instrument needs more than `delay_ms`.
    command_delay_ms: int = 0
    # Whether clear() may send a VISA device clear; some scopes hang on one.
    device_clear: bool = True


@dataclass(frozen=True, slots=True)
class TcpAddress:
    host: str
    port: int

    @property
    def where(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


Parity = Literal["none", "even", "odd", "mark", "space"]
FlowControl = Literal["none", "rts/cts", "xon/xoff", "dtr/dsr"]
VisaBackend = Literal["auto", "py", "ivi"]


@dataclass(frozen=True, slots=True)
class SerialAddress:
    port: str
    baud: int = 9600
    data_bits: int = 8
    parity: Parity = "none"
    stop_bits: float = 1.0
    flow_control: FlowControl = "none"

    @property
    def where(self) -> str:
        return self.port


@dataclass(frozen=True, slots=True)
class VisaAddress:
    resource: str
    # "auto" uses the system VISA for USB and GPIB resources when one is installed, else pyvisa-py.
    backend: VisaBackend = "auto"

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

    def read(self, timeout_s: float | None = None) -> str:
        """One reply without its terminator; raise LinkTimeout when none arrives in time.
        `timeout_s` replaces the settings' timeout for this read."""
        ...

    def query(self, command: str) -> str:
        """Send a command and return its reply."""
        ...

    def read_block(self, timeout_s: float | None = None) -> bytes:
        """One definite-length block reply (`#<n><length><bytes>`) with its payload only; raise
        LinkTimeout when none arrives in time and LinkError when the reply is not a block."""
        ...

    def read_any(self, timeout_s: float | None = None) -> str | bytes:
        """A line, or a block's payload when the reply is a definite-length block."""
        ...

    def clear(self) -> None:
        """Drop pending bytes, and clear the device where the transport can (VISA, USB-TMC)."""
        ...

    def close(self) -> None:
        """Close the link; safe to call twice and from another thread."""
        ...


class Pacer:
    """Keeps the vendor's pause between writes, and a longer one after a command that sends no
    reply: a query is paced by its own reply, a command only by time."""

    def __init__(self, delay_ms: int, command_delay_ms: int = 0) -> None:
        self._delay_s = delay_ms / 1000
        self._after_command_s = max(delay_ms, command_delay_ms) / 1000
        self._last = 0.0
        self._after_command = False

    def wait(self) -> None:
        pause = self._after_command_s if self._after_command else self._delay_s
        if pause <= 0:
            return
        remaining = self._last + pause - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def mark(self, written: str) -> None:
        self._last = time.monotonic()
        self._after_command = "?" not in written


_LINE_ENDS = (b"\n", b"\r")


class Lines:
    """Bytes received so far, handed out one terminated line at a time."""

    def __init__(self, terminator: str) -> None:
        self._terminator = terminator.encode()
        self._buffer = bytearray()
        # After a block, the line endings an instrument sends behind it may still be on the way.
        self._after_block = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk

    def _skip_after_block(self) -> None:
        """Drop the line endings an instrument sent behind a block, however late they came."""
        if self._after_block:
            while self._buffer[:1] in _LINE_ENDS:
                del self._buffer[:1]
            self._after_block = not self._buffer

    def pop(self) -> str | None:
        """The next complete line without its terminator, or None until one has arrived."""
        self._skip_after_block()
        end = self._buffer.find(self._terminator) if self._terminator else len(self._buffer) or -1
        if end < 0:
            return None
        line = bytes(self._buffer[:end])
        del self._buffer[: end + len(self._terminator)]
        return line.decode("utf-8", errors="replace")

    def starts_block(self) -> bool | None:
        """True when the buffered reply is a block, False when a line, None while empty."""
        self._skip_after_block()
        if not self._buffer:
            return None
        if self._buffer[:1] != b"#":
            return False
        if len(self._buffer) < 2:
            return None
        # `#H1F` is a hex number, not a block: only a non-zero digit opens a block.
        return self._buffer[1:2].isdigit() and self._buffer[1:2] != b"0"

    def pop_block(self) -> bytes | None:
        """The payload of the next complete definite-length block, or None until it has arrived;
        raises BlockError when the buffer does not start with a block. The terminator bytes an
        instrument sends after a block are dropped with it."""
        self._skip_after_block()
        header = block_header(bytes(self._buffer[:12]))
        if header is None:
            return None
        start, length = header
        if len(self._buffer) < start + length:
            return None
        payload = bytes(self._buffer[start : start + length])
        del self._buffer[: start + length]
        while self._buffer[:1] in _LINE_ENDS:
            del self._buffer[:1]
        self._after_block = not self._buffer
        return payload

    def clear(self) -> None:
        self._buffer.clear()
        self._after_block = False


def seconds(value: float) -> str:
    """A duration as a message shows it: `2 s`, `0.5 s`."""
    return f"{value:g} s"


class LineLink(ABC):
    """What a raw socket and a serial port share: commands go out with the write terminator, and
    replies are split into lines and #-blocks here. A subclass moves the bytes."""

    # How much one receive asks for while a line, or a block, is awaited.
    line_chunk = 256
    block_chunk = 256

    def __init__(self, where: str, settings: LinkSettings) -> None:
        self.where = where
        self._settings = settings
        self._pacer = Pacer(settings.delay_ms, settings.command_delay_ms)
        self._lines = Lines(settings.read_terminator)
        self._closed = False

    @abstractmethod
    def _send(self, data: bytes) -> None:
        """Write the bytes; raises LinkError."""

    @abstractmethod
    def _receive(self, deadline: float, size: int) -> None:
        """Feed one more chunk of up to `size` bytes into the lines, or raise by `deadline`."""

    def write(self, command: str) -> None:
        self._pacer.wait()
        self._send((command + self._settings.write_terminator).encode())
        self._pacer.mark(command)

    def read(self, timeout_s: float | None = None) -> str:
        deadline = time.monotonic() + (timeout_s or self._settings.timeout_s)
        while True:
            line = self._lines.pop()
            if line is not None:
                return line
            self._receive(deadline, self.line_chunk)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def read_block(self, timeout_s: float | None = None) -> bytes:
        # A block keeps arriving in pieces: each piece restarts the clock.
        wait = timeout_s or self._settings.timeout_s
        while True:
            block = self._pop_block()
            if block is not None:
                return block
            self._receive(time.monotonic() + wait, self.block_chunk)

    def read_any(self, timeout_s: float | None = None) -> str | bytes:
        deadline = time.monotonic() + (timeout_s or self._settings.timeout_s)
        while True:
            kind = self._lines.starts_block()
            if kind is True:
                block = self._pop_block()
                if block is not None:
                    return block
            elif kind is False:
                line = self._lines.pop()
                if line is not None:
                    return line
            self._receive(deadline, self.block_chunk)

    def _pop_block(self) -> bytes | None:
        try:
            return self._lines.pop_block()
        except BlockError as exc:
            self._lines.clear()
            raise LinkError(f"{self.where} did not answer with a block: {exc}", "reply") from exc

    def _no_reply(self) -> LinkTimeout:
        return LinkTimeout(f"no reply from {self.where} within {seconds(self._settings.timeout_s)}")


def open_link(address: Address, settings: LinkSettings) -> Link:
    """Open the link an address describes; raises LinkError."""
    if isinstance(address, TcpAddress):
        from .tcp import open_tcp

        return open_tcp(address, settings)
    if isinstance(address, SerialAddress):
        from .serial import open_serial

        return open_serial(address, settings)
    from .visa import open_visa

    return open_visa(address, settings)
