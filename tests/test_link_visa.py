import errno
import socket
import time
from typing import Any

import pytest
import pyvisa
from pyvisa import constants, errors
from pyvisa.resources import MessageBasedResource
from test_link_tcp import IDN, FakeInstrument, closed_port, instrument  # noqa: F401

from zelos_extension_instruments.link import LinkError, LinkTimeout, Settings, VisaAddress, visa
from zelos_extension_instruments.link.visa import open_visa

FAST = Settings(timeout_s=0.3)
STATUS = constants.StatusCode


class FakeResource(MessageBasedResource):
    write_termination: Any = None
    timeout: Any = None

    def __init__(self) -> None:
        self._session = None
        self._terminator = ""
        self.written: list[str] = []
        self.replies: list[bytes | Exception] = []
        self.write_error: Exception | None = None
        self.closed = 0
        self.fail_clear = False

    @property
    def read_termination(self) -> str:
        return self._terminator

    @read_termination.setter
    def read_termination(self, value: str) -> None:
        if value[-1:] in value[:-1]:
            raise ValueError("ambiguous ending in termination characters")
        self._terminator = value

    def write(self, message: str, *args: Any, **kwargs: Any) -> int:
        if self.write_error:
            raise self.write_error
        self.written.append(message)
        return len(message)

    def read_raw(self, size: int | None = None) -> bytes:
        outcome = self.replies.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def clear(self) -> None:
        if self.fail_clear:
            raise errors.VisaIOError(STATUS.error_connection_lost)

    def close(self) -> None:
        self.closed += 1


class FakeManager:
    def __init__(self, library: str, resource: FakeResource) -> None:
        self.library = library
        self.resource = resource
        self.opened: list[tuple[str, int]] = []
        self.open_error: Exception | None = None

    def open_resource(self, name: str, open_timeout: int = 0) -> FakeResource:
        self.opened.append((name, open_timeout))
        if self.open_error:
            raise self.open_error
        return self.resource


class FakeVisa:
    """Stands in for `pyvisa.ResourceManager`, recording which library each open asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, system_visa: bool) -> None:
        self.system_visa = system_visa
        self.created: list[str] = []
        self.resource = FakeResource()
        self.managers: dict[str, FakeManager] = {}
        self.open_error: Exception | None = None
        monkeypatch.setattr(pyvisa, "ResourceManager", self)
        monkeypatch.setattr(visa, "_managers", {})

    def __call__(self, library: str = "") -> FakeManager:
        if library == "@ivi" and not self.system_visa:
            raise OSError("Could not open VISA library")
        self.created.append(library)
        manager = self.managers.setdefault(library, FakeManager(library, self.resource))
        manager.open_error = self.open_error
        return manager

    @property
    def opened_with(self) -> list[str]:
        return [library for library, manager in self.managers.items() if manager.opened]


@pytest.fixture
def fake_visa(monkeypatch: pytest.MonkeyPatch) -> FakeVisa:
    return FakeVisa(monkeypatch, system_visa=True)


@pytest.fixture
def no_system_visa(monkeypatch: pytest.MonkeyPatch) -> FakeVisa:
    return FakeVisa(monkeypatch, system_visa=False)


@pytest.mark.parametrize(
    ("backend", "resource", "expected"),
    [
        ("py", "USB0::0x1AB1::0x0E11::DP8::INSTR", "@py"),
        ("py", "GPIB0::5::INSTR", "@py"),
        ("ivi", "TCPIP0::10.0.0.5::INSTR", "@ivi"),
        ("auto", "USB0::0x1AB1::0x0E11::DP8::INSTR", "@ivi"),
        ("auto", "gpib0::5::instr", "@ivi"),
        ("auto", "TCPIP0::10.0.0.5::inst0::INSTR", "@py"),
        ("auto", "TCPIP::10.0.0.5::5025::SOCKET", "@py"),
        ("auto", "ASRL3::INSTR", "@py"),
    ],
)
def test_backend_selection_with_a_system_visa(
    fake_visa: FakeVisa, backend: Any, resource: str, expected: str
) -> None:
    open_visa(VisaAddress(resource, backend), FAST)
    assert fake_visa.opened_with == [expected]
    assert fake_visa.managers[expected].opened == [(resource, 2000)]


@pytest.mark.parametrize("resource", ["USB0::1::2::SN::INSTR", "GPIB0::5::INSTR"])
def test_auto_falls_back_to_pyvisa_py_without_a_system_visa(
    no_system_visa: FakeVisa, resource: str
) -> None:
    open_visa(VisaAddress(resource), FAST)
    assert no_system_visa.opened_with == ["@py"]


def test_auto_never_asks_for_a_system_visa_for_other_resources(no_system_visa: FakeVisa) -> None:
    open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET"), FAST)
    assert "@ivi" not in no_system_visa.created


def test_explicit_system_visa_without_one_installed_is_a_config_error(
    no_system_visa: FakeVisa,
) -> None:
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress("USB0::1::2::SN::INSTR", "ivi"), FAST)
    assert caught.value.kind == "config"
    assert "VISA library" in str(caught.value)


def test_one_manager_per_backend_is_reused(fake_visa: FakeVisa) -> None:
    open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET"), FAST)
    open_visa(VisaAddress("TCPIP::10.0.0.6::5025::SOCKET"), FAST)
    open_visa(VisaAddress("USB0::1::2::SN::INSTR", "py"), FAST)
    assert fake_visa.created == ["@py"]


def test_open_applies_terminators_and_timeout(fake_visa: FakeVisa) -> None:
    settings = Settings(write_terminator="\r\n", read_terminator="\n", timeout_s=1.5)
    open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), settings)
    resource = fake_visa.resource
    assert resource.write_termination == "\r\n"
    assert resource.read_termination == "\n"
    assert resource.timeout == 1500


def test_query_strips_the_terminator_and_replaces_bad_bytes(fake_visa: FakeVisa) -> None:
    link = open_visa(
        VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), Settings(read_terminator="\r\n")
    )
    fake_visa.resource.replies = [b"OK\r\n", b"\xffA\r\n"]
    assert link.query("*IDN?") == "OK"
    assert fake_visa.resource.written == ["*IDN?"]
    assert link.read() == "�A"


def test_delay_spaces_consecutive_writes(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), Settings(delay_ms=200))
    link.write("A")
    started = time.monotonic()
    link.write("B")
    assert time.monotonic() - started >= 0.15


def test_read_timeout_is_a_link_timeout(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), FAST)
    fake_visa.resource.replies = [errors.VisaIOError(STATUS.error_timeout)]
    with pytest.raises(LinkTimeout) as caught:
        link.read()
    assert str(caught.value) == "no reply from TCPIP::10.0.0.5::5025::SOCKET within 0.3 s"


def test_other_read_errors_are_link_errors(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), FAST)
    fake_visa.resource.replies = [errors.VisaIOError(STATUS.error_connection_lost)]
    with pytest.raises(LinkError) as caught:
        link.read()
    assert not isinstance(caught.value, LinkTimeout)
    assert caught.value.kind == "other"
    assert "TCPIP::10.0.0.5::5025::SOCKET" in str(caught.value)


def test_a_write_timeout_is_not_a_missing_reply(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), FAST)
    fake_visa.resource.write_error = errors.VisaIOError(STATUS.error_timeout)
    with pytest.raises(LinkError) as caught:
        link.write("*RST")
    assert not isinstance(caught.value, LinkTimeout)
    assert caught.value.kind == "timeout"


def test_clear_swallows_errors(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), FAST)
    fake_visa.resource.fail_clear = True
    link.clear()


def test_close_is_idempotent_and_ends_the_link(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), FAST)
    link.close()
    link.close()
    fake_visa.resource.replies = [OSError("closed")]
    with pytest.raises(LinkError) as caught:
        link.read()
    assert "was closed" in str(caught.value)


@pytest.mark.parametrize(
    ("resource", "backend", "error", "kind", "mentions"),
    [
        ("junk", "py", errors.VisaIOError(STATUS.error_invalid_resource_name), "config", "valid"),
        ("USB0::1::2::SN::INSTR", "py", ValueError("No device found."), "not_found", "USB0"),
        (
            "GPIB0::5::INSTR",
            "ivi",
            errors.VisaIOError(STATUS.error_resource_not_found),
            "not_found",
            "GPIB0",
        ),
        ("GPIB0::5::INSTR", "ivi", errors.VisaIOError(STATUS.error_resource_busy), "busy", "GPIB0"),
        (
            "GPIB0::5::INSTR",
            "ivi",
            errors.VisaIOError(STATUS.error_resource_locked),
            "busy",
            "GPIB0",
        ),
        ("GPIB0::5::INSTR", "ivi", errors.VisaIOError(STATUS.error_timeout), "timeout", "GPIB0"),
        (
            "GPIB0::5::INSTR",
            "ivi",
            errors.VisaIOError(STATUS.error_connection_lost),
            "other",
            "GPIB0",
        ),
        (
            "GPIB0::5::INSTR",
            "py",
            ValueError("Please install linux-gpib to use this"),
            "config",
            "vendor VISA",
        ),
        (
            "USB0::1::2::SN::INSTR",
            "py",
            ValueError("No class registered for USB"),
            "config",
            "pyusb",
        ),
        ("USB0::1::2::SN::INSTR", "py", ValueError("No backend available"), "config", "libusb"),
        ("ASRL3::INSTR", "py", ValueError("Please install PySerial (>=3.0)"), "config", "pyserial"),
        (
            "TCPIP::10.0.0.5::5025::SOCKET",
            "py",
            Exception("could not connect: -1073807339"),
            "timeout",
            "5025",
        ),
        ("TCPIP::10.0.0.5::5025::SOCKET", "py", ConnectionRefusedError(), "refused", "5025"),
        (
            "TCPIP::psu.local::5025::SOCKET",
            "py",
            socket.gaierror(1, "nope"),
            "unresolved",
            "psu.local",
        ),
        (
            "USB0::1::2::SN::INSTR",
            "py",
            OSError(errno.EACCES, "Access denied"),
            "permission",
            "USB0",
        ),
        ("USB0::1::2::SN::INSTR", "py", OSError(errno.EBUSY, "Resource busy"), "busy", "USB0"),
        ("TCPIP::10.0.0.5::5025::SOCKET", "py", RuntimeError("boom"), "other", "boom"),
    ],
)
def test_open_errors_map_to_kinds(
    fake_visa: FakeVisa, resource: str, backend: Any, error: Exception, kind: str, mentions: str
) -> None:
    fake_visa.open_error = error
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress(resource, backend), FAST)
    assert caught.value.kind == kind
    assert mentions in str(caught.value)
    assert str(caught.value).endswith(".")


def test_an_unusable_terminator_closes_the_resource_and_is_a_config_error(
    fake_visa: FakeVisa,
) -> None:
    with pytest.raises(LinkError) as caught:
        open_visa(
            VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), Settings(read_terminator="\n\n")
        )
    assert caught.value.kind == "config"
    assert fake_visa.resource.closed == 1


def test_real_pyvisa_py_resource_string_that_cannot_be_parsed_is_config() -> None:
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress("not a resource", "py"), FAST)
    assert caught.value.kind == "config"


def test_real_pyvisa_py_socket_against_a_fake_instrument(instrument: FakeInstrument) -> None:  # noqa: F811
    resource = f"TCPIP::127.0.0.1::{instrument.port}::SOCKET"
    link = open_visa(VisaAddress(resource, "py"), Settings(timeout_s=0.5))
    try:
        assert link.where == resource
        assert link.query("*IDN?") == IDN
        sock = link._inst.visalib.sessions[link._inst.session].interface  # type: ignore[attr-defined]
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        started = time.monotonic()
        with pytest.raises(LinkTimeout):
            link.query("NOREPLY?")
        assert 0.4 <= time.monotonic() - started < 1.5
        link.clear()
    finally:
        link.close()


def test_real_pyvisa_py_socket_to_a_closed_port_fails(closed_port: int) -> None:  # noqa: F811
    resource = f"TCPIP::127.0.0.1::{closed_port}::SOCKET"
    with pytest.raises(LinkError):
        link = open_visa(VisaAddress(resource, "py"), FAST)
        link.query("*IDN?")
