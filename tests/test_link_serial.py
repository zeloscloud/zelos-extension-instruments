import errno
import socket
import sys
import threading
import time

import pytest
import serialx

from tests.conftest import IDN, LineServer
from zelos_extension_instruments.link import LinkError, LinkSettings, LinkTimeout, SerialAddress
from zelos_extension_instruments.link.serial import open_serial

FAST = LinkSettings(timeout_s=0.3)


def connect(server: LineServer, settings: LinkSettings = FAST):
    return open_serial(SerialAddress(f"socket://127.0.0.1:{server.port}"), settings)


def test_query_over_a_serialx_url(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        assert link.query("*IDN?") == IDN
        assert link.where == f"socket://127.0.0.1:{instrument.port}"
    finally:
        link.close()


def test_write_appends_the_write_terminator_and_echoes(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(write_terminator="\r\n", timeout_s=0.3))
    try:
        assert link.query("ECHO hi") == "hi"
        assert instrument.received == ["ECHO hi"]
    finally:
        link.close()


def test_reply_split_across_two_sends_is_joined(instrument: LineServer) -> None:
    link = connect(instrument, LinkSettings(timeout_s=2))
    try:
        assert link.query("SPLIT?") == "HELLO"
    finally:
        link.close()


def test_second_reply_in_one_packet_waits_for_the_next_read(
    instrument: LineServer,
) -> None:
    link = connect(instrument)
    try:
        assert link.query("TWO?") == "ONE"
        started = time.monotonic()
        assert link.read() == "TWO"
        assert time.monotonic() - started < 0.1
    finally:
        link.close()


def test_invalid_utf8_is_replaced(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        assert link.query("BAD?") == "��A"
    finally:
        link.close()


def test_silent_instrument_times_out_within_the_timeout(
    instrument: LineServer,
) -> None:
    link = connect(instrument)
    try:
        started = time.monotonic()
        with pytest.raises(LinkTimeout) as caught:
            link.query("NOREPLY?")
        assert 0.25 <= time.monotonic() - started < 1.0
        assert str(caught.value) == (
            f"no reply from socket://127.0.0.1:{instrument.port} within 0.3 s"
        )
    finally:
        link.close()


def test_clear_drops_pending_bytes(instrument: LineServer) -> None:
    link = connect(instrument)
    try:
        link.write("TWO?")
        time.sleep(0.2)
        link.clear()
        assert link.query("*IDN?") == IDN
    finally:
        link.close()


def test_connection_closed_mid_reply_is_a_link_error(
    instrument: LineServer,
) -> None:
    link = connect(instrument, LinkSettings(timeout_s=2))
    try:
        with pytest.raises(LinkError) as caught:
            link.query("DROP?")
        assert not isinstance(caught.value, LinkTimeout)
        assert caught.value.kind == "other"
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


def test_close_is_idempotent_and_ends_the_link(instrument: LineServer) -> None:
    link = connect(instrument)
    link.close()
    link.close()
    with pytest.raises(LinkError):
        link.query("*IDN?")


def test_close_from_another_thread_ends_a_blocked_read(
    instrument: LineServer,
) -> None:
    link = connect(instrument, LinkSettings(timeout_s=1))
    errors: list[LinkError] = []

    def blocked() -> None:
        try:
            link.query("NOREPLY?")
        except LinkError as exc:
            errors.append(exc)

    worker = threading.Thread(target=blocked)
    worker.start()
    time.sleep(0.2)
    link.close()
    worker.join(3)
    assert not worker.is_alive()
    assert len(errors) == 1


def test_refused_connection_is_kind_refused(closed_port: int) -> None:
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress(f"socket://127.0.0.1:{closed_port}"), FAST)
    assert caught.value.kind == "refused"
    assert f"socket://127.0.0.1:{closed_port}" in str(caught.value)


def test_unresolvable_host_is_kind_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> list:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress("socket://psu.local:5025"), FAST)
    assert caught.value.kind == "unresolved"


def test_a_missing_port_is_not_found() -> None:
    port = "COM99" if sys.platform == "win32" else "/dev/ttyZELOSNONE0"
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress(port), FAST)
    assert caught.value.kind == "not_found"
    assert port in str(caught.value)


def test_an_unknown_url_scheme_is_a_config_error() -> None:
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress("nosuch://device"), FAST)
    assert caught.value.kind == "config"


class _Recorder:
    def __init__(self) -> None:
        self.url = ""
        self.kwargs: dict[str, object] = {}
        self.opened = False

    def __call__(self, url: str, **kwargs: object) -> "_Recorder":
        self.url, self.kwargs = url, kwargs
        return self

    def open(self) -> None:
        self.opened = True


def test_address_becomes_serialx_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serialx, "serial_for_url", recorder)
    open_serial(
        SerialAddress(
            "COM7", baud=115200, data_bits=7, parity="even", stop_bits=2, flow_control="rts/cts"
        ),
        FAST,
    )
    assert recorder.opened
    assert recorder.url == "COM7"
    assert recorder.kwargs == {
        "baudrate": 115200,
        "byte_size": 7,
        "parity": serialx.Parity.EVEN,
        "stopbits": 2,
        "rtscts": True,
        "xonxoff": False,
        "dsrdtr": False,
        "exclusive": True,
        "write_timeout": 1.0,
    }


def test_xonxoff_flow_control(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serialx, "serial_for_url", recorder)
    open_serial(SerialAddress("COM7", flow_control="xon/xoff"), FAST)
    assert recorder.kwargs["xonxoff"] is True
    assert recorder.kwargs["rtscts"] is False


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (FileNotFoundError(errno.ENOENT, "No such file"), "not_found"),
        (OSError(errno.ENODEV, "No such device"), "not_found"),
        (PermissionError(errno.EACCES, "Permission denied"), "permission"),
        (OSError(errno.EBUSY, "Device or resource busy"), "busy"),
        (serialx.UnsupportedSetting("no 5 data bits"), "config"),
        (ValueError("3 is not a valid StopBits"), "config"),
        (OSError(errno.EINVAL, "Invalid argument"), "config"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "refused"),
        (OSError(errno.EIO, "Input/output error"), "other"),
    ],
)
def test_open_errors_map_to_kinds(
    monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str
) -> None:
    def fail(url: str, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(serialx, "serial_for_url", fail)
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress("COM7"), FAST)
    assert caught.value.kind == kind
    assert "COM7" in str(caught.value)
    assert str(caught.value).endswith(".")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access-denied code")
def test_windows_access_denied_means_the_port_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, **kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "Access is denied", None, 5)

    monkeypatch.setattr(serialx, "serial_for_url", fail)
    with pytest.raises(LinkError) as caught:
        open_serial(SerialAddress("COM7"), FAST)
    assert caught.value.kind == "busy"
