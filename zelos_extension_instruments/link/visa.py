"""A VISA resource through pyvisa: pyvisa-py by default, a system VISA library when one is there."""

import socket
import sys
from contextlib import suppress
from typing import Any, cast

import pyvisa
from pyvisa import constants, errors
from pyvisa.resources import MessageBasedResource

from ..waveform import BlockError, block_payload
from . import CONNECT_TIMEOUT_S, Link, LinkError, LinkTimeout, Pacer, Settings, VisaAddress
from .tcp import tune_socket

_MISSING_DRIVER = {
    "USB": "USB needs the pyusb package and a libusb library",
    "GPIB": "GPIB needs a vendor VISA library such as NI-VISA or Keysight VISA",
    "ASRL": "Serial ports through VISA need the pyserial package",
}


def _seconds(value: float) -> str:
    return f"{value:g} s"


def _manager(backend: str) -> pyvisa.ResourceManager:
    # pyvisa keeps one open manager per library and makes a new one after a close, so this is
    # never cached here: a cached manager that was closed elsewhere fails every open.
    # A bare ResourceManager() falls back to pyvisa-py; "@ivi" fails when no VISA is installed.
    return pyvisa.ResourceManager("@py" if backend == "py" else "@ivi")


def _backend(address: VisaAddress) -> str:
    if address.backend != "auto":
        return address.backend
    if address.resource.upper().startswith(("USB", "GPIB")):
        try:
            _manager("ivi")
        except (OSError, ValueError):
            return "py"
        return "ivi"
    return "py"


def _usb_advice(where: str) -> str:
    """How to make a USB-TMC device openable through the built-in library, by platform."""
    if not where.upper().startswith("USB"):
        return ""
    if sys.platform == "win32":
        return (
            " On Windows the device keeps its vendor driver: install NI-VISA or Keysight IO "
            "Libraries, which the extension then uses, or bind the device to WinUSB with Zadig."
        )
    if sys.platform == "linux":
        return (
            " On Linux give this user access to the device: a udev rule for its vendor id "
            '(MODE="0666" or a group this user is in) for /dev/bus/usb and /dev/usbtmc*.'
        )
    return ""


def _open_failure(address: VisaAddress, backend: str, exc: Exception) -> LinkError:
    where = address.resource
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, errors.VisaIOError):
        code = exc.error_code
        if code == constants.StatusCode.error_invalid_resource_name:
            return LinkError(f"{where} is not a valid VISA resource string.", "config")
        if code == constants.StatusCode.error_resource_not_found:
            return LinkError(f"No instrument answers at {where}.", "not_found")
        if code in (
            constants.StatusCode.error_resource_busy,
            constants.StatusCode.error_resource_locked,
        ):
            return LinkError(f"{where} is in use by another program.", "busy")
        if code == constants.StatusCode.error_timeout:
            return LinkError(
                f"No answer from {where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout"
            )
        return LinkError(f"Cannot open {where}: {exc.description}.", "other")
    if "no device found" in lowered:
        return LinkError(f"No instrument answers at {where}.{_usb_advice(where)}", "not_found")
    if "could not connect" in lowered:
        return LinkError(f"No answer from {where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout")
    if isinstance(exc, ConnectionRefusedError):
        return LinkError(f"{where} refused the connection.", "refused")
    if isinstance(exc, socket.gaierror):
        return LinkError(f"Cannot find the host in {where}.", "unresolved")
    if isinstance(exc, TimeoutError):
        return LinkError(f"No answer from {where} within {_seconds(CONNECT_TIMEOUT_S)}.", "timeout")
    if backend == "ivi" and isinstance(exc, OSError) and "visa library" in lowered:
        return LinkError(
            "No system VISA library is installed; install NI-VISA or Keysight VISA, "
            "or switch the connection to pyvisa-py.",
            "config",
        )
    if any(s in lowered for s in ("no class registered", "please install", "no backend available")):
        interface = where.split("::")[0].rstrip("0123456789").upper()
        need = _MISSING_DRIVER.get(interface, f"{interface or 'This'} resources need a driver")
        return LinkError(f"{need}, and it is missing: {where} cannot be opened.", "config")
    code = getattr(exc, "errno", None)
    if code == 13:
        return LinkError(f"This account may not open {where}.{_usb_advice(where)}", "permission")
    if code == 16:
        return LinkError(f"{where} is in use by another program.", "busy")
    if isinstance(exc, ValueError):
        return LinkError(f"{where} cannot be set up: {message}.", "config")
    return LinkError(f"Cannot open {where}: {message or type(exc).__name__}.", "other")


def open_visa(address: VisaAddress, settings: Settings) -> Link:
    """Open a VISA resource; raises LinkError."""
    backend = _backend(address)
    inst = None
    try:
        inst = _manager(backend).open_resource(
            address.resource, open_timeout=int(CONNECT_TIMEOUT_S * 1000)
        )
        if not isinstance(inst, MessageBasedResource):
            raise LinkError(f"{address.resource} is not a message-based instrument.", "config")
        inst.read_termination = settings.read_terminator
        inst.write_termination = settings.write_terminator
        inst.timeout = max(1, round(settings.timeout_s * 1000))
        if backend == "py" and address.resource.upper().endswith("::SOCKET"):
            with suppress(Exception):
                tune_socket(cast(Any, inst.visalib).sessions[inst.session].interface)
    except LinkError:
        if inst is not None:
            with suppress(Exception):
                inst.close()
        raise
    except Exception as exc:
        if inst is not None:
            with suppress(Exception):
                inst.close()
        raise _open_failure(address, backend, exc) from exc
    return VisaLink(inst, address.where, settings)


class VisaLink:
    def __init__(self, inst: MessageBasedResource, where: str, settings: Settings) -> None:
        self.where = where
        self._inst = inst
        self._settings = settings
        self._pacer = Pacer(settings.delay_ms)
        self._closed = False

    def write(self, command: str) -> None:
        self._pacer.wait()
        try:
            self._inst.write(command)
        except Exception as exc:
            raise self._failure(exc, reading=False) from exc
        self._pacer.mark()

    def read(self, timeout_s: float | None = None) -> str:
        return self._text(self._read_raw(timeout_s, terminated=True), timeout_s)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def read_block(self, timeout_s: float | None = None) -> bytes:
        # A block holds every byte value, so the termination character must not end the read;
        # VISA then reads to the message's END.
        try:
            return block_payload(self._read_raw(timeout_s, terminated=False))
        except BlockError as exc:
            raise LinkError(f"{self.where} did not answer with a block: {exc}", "other") from exc

    def read_any(self, timeout_s: float | None = None) -> str | bytes:
        raw = self._read_raw(timeout_s, terminated=True)
        if raw[:1] != b"#":
            return self._text(raw, timeout_s)
        # The terminator may have ended the read inside the block: read on until it is whole.
        buffer = bytearray(raw)
        while True:
            try:
                return block_payload(bytes(buffer))
            except BlockError as exc:
                if "holds" not in str(exc) and "incomplete" not in str(exc):
                    return self._text(bytes(buffer), timeout_s)
            buffer += self._read_raw(timeout_s, terminated=False)

    def _read_raw(self, timeout_s: float | None, *, terminated: bool) -> bytes:
        inst = self._inst
        termination = inst.read_termination
        timeout = inst.timeout
        if not terminated:
            inst.read_termination = None
        if timeout_s is not None:
            inst.timeout = max(1, round(timeout_s * 1000))
        try:
            return inst.read_raw()
        except Exception as exc:
            raise self._failure(exc, reading=True, timeout_s=timeout_s) from exc
        finally:
            inst.read_termination = termination
            inst.timeout = timeout

    def _text(self, raw: bytes, timeout_s: float | None) -> str:
        text = raw.decode("utf-8", errors="replace")
        terminator = self._settings.read_terminator
        return text.removesuffix(terminator) if terminator else text

    def clear(self) -> None:
        """A device clear where the profile allows one, else only the read buffer."""
        with suppress(Exception):
            if self._settings.device_clear:
                self._inst.clear()
            else:
                self._inst.flush(constants.BufferOperation.discard_read_buffer)

    def close(self) -> None:
        self._closed = True
        with suppress(Exception):
            self._inst.close()

    def _failure(
        self, exc: Exception, *, reading: bool, timeout_s: float | None = None
    ) -> LinkError:
        if self._closed:
            return LinkError(f"The link to {self.where} was closed.", "other")
        if isinstance(exc, errors.VisaIOError):
            if exc.error_code == constants.StatusCode.error_timeout:
                if reading:
                    waited = timeout_s or self._settings.timeout_s
                    return LinkTimeout(f"no reply from {self.where} within {_seconds(waited)}")
                return LinkError(f"{self.where} did not accept a command in time.", "timeout")
            return LinkError(f"The connection to {self.where} failed: {exc.description}.", "other")
        return LinkError(f"The connection to {self.where} failed: {exc}.", "other")
