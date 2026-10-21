"""A serial port, or any URL serialx opens (`socket://host:port` runs the same code over TCP)."""

import errno
import socket
import time
from contextlib import suppress

import serialx

from . import Lines, Link, LinkError, LinkTimeout, Pacer, SerialAddress, Settings

_WRITE_TIMEOUT_S = 1.0
_CHUNK = 256
_ERROR_ACCESS_DENIED = 5  # Windows reports a COM port held by another program this way


def _seconds(value: float) -> str:
    return f"{value:g} s"


def _open_failure(port: str, exc: Exception) -> LinkError:
    code = getattr(exc, "errno", None)
    if isinstance(exc, FileNotFoundError) or code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        return LinkError(f"Serial port {port} does not exist.", "not_found")
    if isinstance(exc, PermissionError) and getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED:
        return LinkError(f"{port} is in use by another program.", "busy")
    if isinstance(exc, PermissionError) or code == errno.EACCES:
        return LinkError(f"This account may not open {port}.", "permission")
    if code == errno.EBUSY:
        return LinkError(f"{port} is in use by another program.", "busy")
    if isinstance(exc, serialx.SerialException | ValueError) or code == errno.EINVAL:
        return LinkError(f"The serial settings for {port} are not valid: {exc}.", "config")
    if isinstance(exc, ConnectionRefusedError):
        return LinkError(f"{port} refused the connection.", "refused")
    if isinstance(exc, socket.gaierror):
        return LinkError(f"Cannot find the host in {port}.", "unresolved")
    if isinstance(exc, TimeoutError):
        return LinkError(f"No answer from {port}.", "timeout")
    return LinkError(f"Cannot open {port}: {getattr(exc, 'strerror', None) or exc}.", "other")


def open_serial(address: SerialAddress, settings: Settings) -> Link:
    """Open a serial port or serialx URL; raises LinkError."""
    try:
        port = serialx.serial_for_url(
            address.port,
            baudrate=address.baud,
            byte_size=address.data_bits,
            parity=serialx.Parity[address.parity.upper()],
            stopbits=address.stop_bits,
            rtscts=address.flow_control == "rts/cts",
            xonxoff=address.flow_control == "xon/xoff",
            exclusive=True,
            write_timeout=_WRITE_TIMEOUT_S,
        )
        port.open()
    except Exception as exc:
        raise _open_failure(address.port, exc) from exc
    return SerialLink(port, address.where, settings)


class SerialLink:
    def __init__(self, port: serialx.BaseSerial, where: str, settings: Settings) -> None:
        self.where = where
        self._port = port
        self._settings = settings
        self._pacer = Pacer(settings.delay_ms)
        self._lines = Lines(settings.read_terminator)
        self._closed = False

    def write(self, command: str) -> None:
        data = (command + self._settings.write_terminator).encode()
        self._pacer.wait()
        try:
            self._port.write(data)
        except TimeoutError as exc:
            raise LinkError(
                f"{self.where} did not accept a command within {_seconds(_WRITE_TIMEOUT_S)}.",
                "timeout",
            ) from exc
        except Exception as exc:
            raise self._broken(exc) from exc
        self._pacer.mark()

    def read(self) -> str:
        deadline = time.monotonic() + self._settings.timeout_s
        chunk = bytearray(_CHUNK)
        while True:
            line = self._lines.pop()
            if line is not None:
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._no_reply()
            try:
                count = self._port.readinto(chunk, timeout=remaining)
            except Exception as exc:
                raise self._broken(exc) from exc
            if self._closed:
                raise self._broken(None)
            self._lines.feed(bytes(chunk[:count]))

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._lines.clear()
        with suppress(Exception):
            self._port.reset_read_buffer()

    def close(self) -> None:
        self._closed = True
        with suppress(Exception):
            self._port.close()

    def _no_reply(self) -> LinkTimeout:
        return LinkTimeout(
            f"no reply from {self.where} within {_seconds(self._settings.timeout_s)}"
        )

    def _broken(self, exc: Exception | None) -> LinkError:
        if self._closed:
            return LinkError(f"The link to {self.where} was closed.", "other")
        reason = getattr(exc, "strerror", None) or exc
        return LinkError(f"The connection to {self.where} was lost: {reason}.", "other")
