import errno
import socket
import time
from types import SimpleNamespace
from typing import Any

import pytest
import pyvisa
from pyvisa import constants, errors
from pyvisa.resources import MessageBasedResource

from tests.conftest import IDN, LineServer
from zelos_extension_instruments.link import LinkError, LinkSettings, LinkTimeout, VisaAddress, visa
from zelos_extension_instruments.link.visa import open_visa

FAST = LinkSettings(timeout_s=0.3)
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
        self.cleared = 0
        self.flushed: list[Any] = []
        self.read_timeouts: list[Any] = []
        self.chunk_sizes: list[int | None] = []

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

    # Bytes a byte-count read left behind for the next read.
    pending: bytes = b""

    def _next(self) -> bytes:
        self.read_timeouts.append(self.timeout)
        outcome = self.replies.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def read_raw(self, size: int | None = None) -> bytes:
        if self.pending:
            data, self.pending = self.pending, b""
            return data
        return self._next()

    def read_bytes(
        self, count: int, chunk_size: int | None = None, break_on_termchar: bool = False
    ) -> bytes:
        """Exactly `count` bytes, drawn from the replies in order, as pyvisa reads by count."""
        self.chunk_sizes.append(chunk_size)
        while len(self.pending) < count:
            self.pending += self._next()
        data, self.pending = self.pending[:count], self.pending[count:]
        return data

    def clear(self) -> None:
        self.cleared += 1
        if self.fail_clear:
            raise errors.VisaIOError(STATUS.error_connection_lost)

    def flush(self, mask: Any) -> None:
        self.flushed.append(mask)

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


def test_every_open_asks_pyvisa_for_the_backends_manager(fake_visa: FakeVisa) -> None:
    """pyvisa keeps one open manager per library and replaces a closed one, so nothing is
    cached here: a cached manager closed elsewhere failed every open after it."""
    open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET"), FAST)
    open_visa(VisaAddress("TCPIP::10.0.0.6::5025::SOCKET"), FAST)
    open_visa(VisaAddress("USB0::1::2::SN::INSTR", "py"), FAST)
    assert fake_visa.created == ["@py", "@py", "@py"]


def test_open_applies_terminators_and_timeout(fake_visa: FakeVisa) -> None:
    settings = LinkSettings(write_terminator="\r\n", read_terminator="\n", timeout_s=1.5)
    open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), settings)
    resource = fake_visa.resource
    assert resource.write_termination == "\r\n"
    assert resource.read_termination == "\n"
    assert resource.timeout == 1500


def test_query_strips_the_terminator_and_replaces_bad_bytes(fake_visa: FakeVisa) -> None:
    link = open_visa(
        VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), LinkSettings(read_terminator="\r\n")
    )
    fake_visa.resource.replies = [b"OK\r\n", b"\xffA\r\n"]
    assert link.query("*IDN?") == "OK"
    assert fake_visa.resource.written == ["*IDN?"]
    assert link.read() == "�A"


def test_a_block_payload_is_one_read_of_its_whole_length(fake_visa: FakeVisa) -> None:
    """pyvisa's 20 KB default made a VXI-11 round trip per piece: 1.8 times slower on a scope."""
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), LinkSettings())
    payload = bytes(range(256)) * 400
    fake_visa.resource.replies = [b"#6102400" + payload + b"\n"]
    assert link.read_block() == payload
    assert fake_visa.resource.chunk_sizes[-1] == len(payload)


def test_delay_spaces_consecutive_writes(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), LinkSettings(delay_ms=200))
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
        ("TCPIP::10.0.0.5::5025::SOCKET", "py", TimeoutError("timed out"), "timeout", "5025"),
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
            VisaAddress("TCPIP::10.0.0.5::5025::SOCKET", "py"), LinkSettings(read_terminator="\n\n")
        )
    assert caught.value.kind == "config"
    assert fake_visa.resource.closed == 1


def test_real_pyvisa_py_resource_string_that_cannot_be_parsed_is_config() -> None:
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress("not a resource", "py"), FAST)
    assert caught.value.kind == "config"


def test_real_pyvisa_py_socket_against_a_fake_instrument(instrument: LineServer) -> None:
    resource = f"TCPIP::127.0.0.1::{instrument.port}::SOCKET"
    link = open_visa(VisaAddress(resource, "py"), LinkSettings(timeout_s=0.5))
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


def test_a_one_character_reply_does_not_wait_for_more(instrument: LineServer) -> None:
    """`OUTP?` answers `1` and a newline: two bytes that are already the whole reply."""
    resource = f"TCPIP::127.0.0.1::{instrument.port}::SOCKET"
    link = open_visa(VisaAddress(resource, "py"), LinkSettings(timeout_s=1.0))
    try:
        for reply in ("1", "", "#H1F"):
            link.write(f"ECHO {reply}")
            started = time.monotonic()
            assert link.read_any() == reply
            assert time.monotonic() - started < 0.5
    finally:
        link.close()


def test_real_pyvisa_py_socket_to_a_closed_port_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows takes about 2 s to report a refused loopback connect, so the refusal is simulated.
    def refuse(self: socket.socket, target: object) -> int:
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    # pyvisa-py connects without blocking, through connect_ex.
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    with pytest.raises(LinkError) as caught:
        link = open_visa(VisaAddress("TCPIP::127.0.0.1::9::SOCKET", "py"), FAST)
        link.query("*IDN?")
    assert "127.0.0.1" in str(caught.value)


def test_read_any_returns_a_block_as_its_payload(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), LinkSettings())
    fake_visa.resource.replies = [b"#15hello\n", b"1\n"]
    assert link.read_any() == b"hello"
    # The line ending behind the block was read with it, not left as the next reply.
    assert link.read_any() == "1"


@pytest.mark.parametrize(
    ("reply", "complaint"),
    [(b"OK\nrest\n", "expected a #-block, got b'OK'"), (b"#0abc\n", "expected a #-block")],
)
def test_a_reply_that_is_not_a_block_is_refused_and_what_followed_it_is_dropped(
    fake_visa: FakeVisa, reply: bytes, complaint: str
) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), LinkSettings())
    fake_visa.resource.replies = [reply, b"NEXT\n"]
    with pytest.raises(LinkError, match=complaint) as caught:
        link.read_block()
    assert caught.value.kind == "reply"
    assert link.read() == "NEXT"


def test_a_block_length_that_is_not_a_number_is_refused(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), LinkSettings())
    fake_visa.resource.replies = [b"#2ab5hello\n"]
    with pytest.raises(LinkError, match="expected a block length, got b'ab'") as caught:
        link.read_block()
    assert caught.value.kind == "reply"


def test_a_block_read_waits_its_own_timeout_and_restores_the_link_timeout(
    fake_visa: FakeVisa,
) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), FAST)
    fake_visa.resource.replies = [b"#15hello\n"]
    assert link.read_block(7) == b"hello"
    assert fake_visa.resource.read_timeouts == [7000]
    assert fake_visa.resource.timeout == 300


def test_a_block_read_gives_up_after_its_own_timeout_and_restores_the_link_timeout(
    fake_visa: FakeVisa,
) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), FAST)
    fake_visa.resource.replies = [errors.VisaIOError(STATUS.error_timeout)]
    with pytest.raises(LinkTimeout, match="within 7 s"):
        link.read_block(7)
    assert fake_visa.resource.timeout == 300


def test_a_failure_while_reading_a_block_is_a_link_error(fake_visa: FakeVisa) -> None:
    link = open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), FAST)
    fake_visa.resource.replies = [OSError("pipe broke")]
    with pytest.raises(LinkError) as caught:
        link.read_block()
    assert not isinstance(caught.value, LinkTimeout)
    assert str(caught.value) == "The connection to TCPIP::10.0.0.5::INSTR failed: pipe broke."


def test_a_resource_that_is_not_message_based_is_closed_and_refused(fake_visa: FakeVisa) -> None:
    class Meter:
        closed = 0

        def close(self) -> None:
            self.closed += 1

    meter = Meter()
    fake_visa.resource = SimpleNamespace(close=meter.close)  # type: ignore[assignment]
    with pytest.raises(LinkError, match="is not a message-based instrument") as caught:
        open_visa(VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), FAST)
    assert caught.value.kind == "config"
    assert meter.closed == 1


@pytest.mark.parametrize(("device_clear", "cleared", "flushed"), [(True, 1, 0), (False, 0, 1)])
def test_clear_flushes_the_read_buffer_where_a_device_clear_would_hang_the_instrument(
    fake_visa: FakeVisa, device_clear: bool, cleared: int, flushed: int
) -> None:
    link = open_visa(
        VisaAddress("TCPIP::10.0.0.5::INSTR", "py"), LinkSettings(device_clear=device_clear)
    )
    link.clear()
    assert fake_visa.resource.cleared == cleared
    assert len(fake_visa.resource.flushed) == flushed
    if flushed:
        assert fake_visa.resource.flushed == [constants.BufferOperation.discard_read_buffer]


@pytest.mark.parametrize(
    ("platform", "advice"),
    [("win32", "Zadig"), ("linux", "udev rule"), ("darwin", None)],
)
def test_a_missing_usb_device_comes_with_advice_for_the_platform(
    fake_visa: FakeVisa, monkeypatch: pytest.MonkeyPatch, platform: str, advice: str | None
) -> None:
    monkeypatch.setattr(visa, "sys", SimpleNamespace(platform=platform))
    fake_visa.open_error = ValueError("No device found.")
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress("USB0::1::2::SN::INSTR", "py"), FAST)
    message = str(caught.value)
    assert message.startswith("No instrument answers at USB0::1::2::SN::INSTR.")
    assert (advice in message) if advice else message.endswith("INSTR.")


def test_a_missing_device_that_is_not_usb_comes_with_no_usb_advice(fake_visa: FakeVisa) -> None:
    fake_visa.open_error = ValueError("No device found.")
    with pytest.raises(LinkError) as caught:
        open_visa(VisaAddress("GPIB0::5::INSTR", "py"), FAST)
    assert str(caught.value) == "No instrument answers at GPIB0::5::INSTR."
