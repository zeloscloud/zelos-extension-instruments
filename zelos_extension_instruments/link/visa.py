"""A VISA resource through pyvisa: pyvisa-py by default, a system VISA library when one is there."""

import socket
from contextlib import suppress
from typing import Any, cast

import pyvisa
from pyvisa import constants, errors
from pyvisa.resources import MessageBasedResource

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
        return LinkError(f"No instrument answers at {where}.", "not_found")
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
        return LinkError(f"This account may not open {where}.", "permission")
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

    def read(self) -> str:
        try:
            raw = self._inst.read_raw()
        except Exception as exc:
            raise self._failure(exc, reading=True) from exc
        text = raw.decode("utf-8", errors="replace")
        terminator = self._settings.read_terminator
        return text.removesuffix(terminator) if terminator else text

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        with suppress(Exception):
            self._inst.clear()

    def close(self) -> None:
        self._closed = True
        with suppress(Exception):
            self._inst.close()

    def _failure(self, exc: Exception, *, reading: bool) -> LinkError:
        if self._closed:
            return LinkError(f"The link to {self.where} was closed.", "other")
        if isinstance(exc, errors.VisaIOError):
            if exc.error_code == constants.StatusCode.error_timeout:
                if reading:
                    return LinkTimeout(
                        f"no reply from {self.where} within {_seconds(self._settings.timeout_s)}"
                    )
                return LinkError(f"{self.where} did not accept a command in time.", "timeout")
            return LinkError(f"The connection to {self.where} failed: {exc.description}.", "other")
        return LinkError(f"The connection to {self.where} failed: {exc}.", "other")
