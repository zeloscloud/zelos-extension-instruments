import dataclasses
import logging
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pyvisa
import serialx
import usb.core
import zeroconf

from zelos_extension_instruments import discovery, profile
from zelos_extension_instruments.discovery import Found
from zelos_extension_instruments.link import (
    LinkError,
    LinkTimeout,
    SerialAddress,
    Settings,
    TcpAddress,
    VisaAddress,
)
from zelos_extension_instruments.profile import Idn

real_identify = discovery.identify
PROFILE_PATH = Path(__file__).parent.parent / "profiles" / "psu" / "rigol-dp800.toml"
DP832_REPLY = "RIGOL TECHNOLOGIES,DP832,DP8A123456,00.01.16"


@pytest.fixture(autouse=True)
def _reset_warnings() -> None:
    discovery._warned.clear()


@pytest.fixture(scope="module")
def profiles() -> list[profile.Profile]:
    return [profile.load_file(PROFILE_PATH)]


# --- mDNS and VXI-11 -------------------------------------------------------------------------


class FakeInfo:
    def __init__(self, addresses: list[str], port: int) -> None:
        self._addresses = addresses
        self.port = port

    def parsed_addresses(self) -> list[str]:
        return list(self._addresses)


def _service(name: str, type_: str) -> str:
    return f"{name}.{type_}"


def _fake_zeroconf(
    monkeypatch: pytest.MonkeyPatch, services: dict[tuple[str, str], FakeInfo]
) -> None:
    class FakeZeroconf:
        closed = False

        def get_service_info(self, type_: str, name: str, timeout: int) -> FakeInfo | None:
            return services.get((type_, name))

        def close(self) -> None:
            FakeZeroconf.closed = True

    class FakeBrowser:
        def __init__(self, zc: Any, types: list[str], listener: Any) -> None:
            for type_, name in services:
                if type_ in types:
                    listener.add_service(zc, type_, name)

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(zeroconf, "ServiceBrowser", FakeBrowser)


class FakeManager:
    def __init__(self, resources: list[str] | Exception) -> None:
        self._resources = resources
        self.queries: list[str] = []
        self.closed = False

    def list_resources(self, query: str) -> list[str]:
        self.queries.append(query)
        if isinstance(self._resources, Exception):
            raise self._resources
        return self._resources

    def close(self) -> None:
        self.closed = True


def _fake_visa(monkeypatch: pytest.MonkeyPatch, **managers: FakeManager | Exception) -> None:
    def resource_manager(backend: str = "") -> FakeManager:
        manager = managers[backend.lstrip("@")]
        if isinstance(manager, Exception):
            raise manager
        return manager

    monkeypatch.setattr(pyvisa, "ResourceManager", resource_manager)


def test_list_lan_picks_the_best_address_per_host_and_dedupes_by_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both = "10.0.0.5"
    t_raw, t_hislip, t_vxi, t_lxi = (
        discovery.SERVICE_RAW,
        discovery.SERVICE_HISLIP,
        discovery.SERVICE_VXI11,
        discovery.SERVICE_LXI,
    )
    services = {
        (t_raw, _service("DP832 A", t_raw)): FakeInfo([both], 5555),
        (t_hislip, _service("DP832 A", t_hislip)): FakeInfo([both], 4880),
        (t_hislip, _service("Scope", t_hislip)): FakeInfo(["10.0.0.6"], 4880),
        (t_vxi, _service("Load", t_vxi)): FakeInfo(["10.0.0.7"], 111),
        (t_lxi, _service("Meter", t_lxi)): FakeInfo(["10.0.0.8"], 80),
        (t_raw, _service("V6", t_raw)): FakeInfo(["fe80::1%eth0", "2001:db8::5"], 0),
        (t_vxi, _service("V6 only vxi", t_vxi)): FakeInfo(["2001:db8::6"], 111),
        (t_raw, _service("Unresolved", t_raw)): FakeInfo([], 5025),
    }
    _fake_zeroconf(monkeypatch, services)
    vxi = FakeManager(
        [
            "TCPIP0::10.0.0.7::inst0::INSTR",
            "TCPIP0::10.0.0.9::inst0::INSTR",
            "TCPIP0::10.0.0.5::inst0::INSTR",
        ]
    )
    _fake_visa(monkeypatch, py=vxi)

    found = {f.address.where: f for f in discovery.list_lan(budget_s=0.05)}

    assert found["10.0.0.5:5555"].address == TcpAddress("10.0.0.5", 5555)
    assert found["10.0.0.5:5555"].source == "mdns"
    assert found["10.0.0.5:5555"].detail == "DP832 A"
    assert found["TCPIP::10.0.0.6::hislip0::INSTR"].address == VisaAddress(
        "TCPIP::10.0.0.6::hislip0::INSTR"
    )
    assert found["TCPIP::10.0.0.7::inst0::INSTR"].source == "mdns"
    assert found["10.0.0.8:5025"].source == "mdns"
    assert found["[2001:db8::5]:5025"].address == TcpAddress("2001:db8::5", 5025)
    assert found["TCPIP0::10.0.0.9::inst0::INSTR"].source == "vxi11"
    assert not any("2001:db8::6" in where for where in found)
    assert not any("10.0.0.5" in where and where != "10.0.0.5:5555" for where in found)
    assert len(found) == 6
    assert vxi.queries == ["TCPIP?*INSTR"]
    assert vxi.closed


def test_list_lan_returns_vxi11_when_zeroconf_cannot_open_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse() -> None:
        raise OSError("no socket")

    monkeypatch.setattr(zeroconf, "Zeroconf", refuse)
    _fake_visa(monkeypatch, py=FakeManager(["TCPIP0::10.0.0.9::inst0::INSTR"]))

    with caplog.at_level(logging.WARNING, logger=discovery.log.name):
        first = discovery.list_lan(budget_s=0.05)
        second = discovery.list_lan(budget_s=0.05)

    assert [f.source for f in first] == ["vxi11"]
    assert len(second) == 1
    assert len([r for r in caplog.records if "mDNS is unavailable" in r.message]) == 1


def test_list_lan_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise OSError("no socket")

    monkeypatch.setattr(zeroconf, "Zeroconf", refuse)
    _fake_visa(monkeypatch, py=RuntimeError("no visa"))

    assert discovery.list_lan(budget_s=0.01) == []


# --- USB -------------------------------------------------------------------------------------


class FakeUsbDevice:
    def __init__(
        self,
        vid: int,
        pid: int,
        serial: str | Exception,
        product: str = "Model",
        interface: tuple[int, int] = (0xFE, 3),
    ) -> None:
        self.idVendor = vid
        self.idProduct = pid
        self._serial = serial
        self.product = product
        self._interface = interface

    @property
    def serial_number(self) -> str:
        if isinstance(self._serial, Exception):
            raise self._serial
        return self._serial

    def __iter__(self) -> Iterator[list[SimpleNamespace]]:
        cls, sub = self._interface
        yield [SimpleNamespace(bInterfaceClass=cls, bInterfaceSubClass=sub)]


def _fake_libusb(monkeypatch: pytest.MonkeyPatch, find: Callable[..., Any]) -> None:
    monkeypatch.setattr(discovery.libusb_package, "get_libusb1_backend", lambda: "backend")
    monkeypatch.setattr(usb.core, "find", find)


def test_list_usb_reads_usbtmc_devices_and_skips_unreadable_serials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    devices = [
        FakeUsbDevice(0x1AB1, 0x0E11, "DP8A123456", "DP832"),
        FakeUsbDevice(0x2A8D, 0x1234, ValueError("no string descriptor")),
        FakeUsbDevice(0x0403, 0x6001, "A1", "FT232", interface=(0xFF, 0)),
    ]
    seen: dict[str, Any] = {}

    def find(**kwargs: Any) -> list[FakeUsbDevice]:
        seen.update(kwargs)
        return devices

    _fake_libusb(monkeypatch, find)
    _fake_visa(monkeypatch, ivi=FakeManager(["USB0::0x1::0x2::X::INSTR"]))

    with caplog.at_level(logging.INFO, logger=discovery.log.name):
        found = discovery.list_usb()

    assert seen == {"find_all": True, "backend": "backend"}
    assert found == [
        Found(
            VisaAddress("USB0::0x1AB1::0x0E11::DP8A123456::INSTR"),
            None,
            None,
            None,
            "usb",
            "DP832",
            None,
        )
    ]
    assert len([r for r in caplog.records if "serial cannot be read" in r.message]) == 1


def test_list_usb_falls_back_to_system_visa_when_libusb_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_libusb(monkeypatch, lambda **kwargs: [])
    visa = FakeManager(["USB0::0x1AB1::0x0E11::DP8A123456::INSTR"])
    _fake_visa(monkeypatch, ivi=visa)

    found = discovery.list_usb()

    assert [f.address for f in found] == [VisaAddress("USB0::0x1AB1::0x0E11::DP8A123456::INSTR")]
    assert visa.queries == ["USB?*INSTR"]
    assert visa.closed


def test_list_usb_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def find(**kwargs: Any) -> list[FakeUsbDevice]:
        raise usb.core.NoBackendError("no libusb")

    _fake_libusb(monkeypatch, find)
    _fake_visa(monkeypatch, ivi=OSError("no system VISA"))

    assert discovery.list_usb() == []


# --- serial ----------------------------------------------------------------------------------


def _port(device: str, vid: int | None, product: str | None) -> serialx.SerialPortInfo:
    return serialx.SerialPortInfo(
        device=device,
        resolved_device=device,
        vid=vid,
        pid=1,
        serial_number=None,
        manufacturer=None,
        product=product,
        bcd_device=None,
        interface_description=None,
        interface_num=None,
    )


def test_list_serial_candidates_keeps_only_instrument_vendors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = [
        _port("COM3", 0x1AB1, "DP832"),
        _port("COM4", 0x0403, "FT232R USB UART"),
        _port("COM5", None, None),
        _port("COM6", 0xF4ED, None),
    ]
    monkeypatch.setattr(serialx, "list_serial_ports", lambda: ports)

    found = discovery.list_serial_candidates()

    assert [f.address for f in found] == [SerialAddress("COM3", baud=9600), SerialAddress("COM6")]
    assert [f.detail for f in found] == ["COM3 (DP832)", "COM6"]
    assert {f.source for f in found} == {"serial"}


def test_list_serial_candidates_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list[serialx.SerialPortInfo]:
        raise OSError("enumeration failed")

    monkeypatch.setattr(serialx, "list_serial_ports", fail)

    assert discovery.list_serial_candidates() == []


# --- identify --------------------------------------------------------------------------------


class _IdnHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        reply: str | None = self.server.reply  # type: ignore[attr-defined]
        for line in self.rfile:
            if line.strip() == b"*IDN?" and reply is not None:
                self.wfile.write(reply.encode() + b"\n")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, reply: str | None) -> None:
        super().__init__(("127.0.0.1", 0), _IdnHandler)
        self.reply = reply


@pytest.fixture
def serve() -> Iterator[Callable[[str | None], TcpAddress]]:
    servers: list[_Server] = []

    def start(reply: str | None) -> TcpAddress:
        server = _Server(reply)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return TcpAddress("127.0.0.1", server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _found(address: TcpAddress | SerialAddress | VisaAddress, detail: str = "x") -> Found:
    return Found(address, None, None, None, "mdns", detail, None)


def test_identify_matches_the_profile_over_a_real_socket(
    serve: Callable[[str | None], TcpAddress], profiles: list[profile.Profile]
) -> None:
    address = serve(DP832_REPLY)

    result = discovery.identify(_found(address), profiles, timeout_s=2.0)

    assert result.address == address
    assert result.idn == Idn("RIGOL TECHNOLOGIES", "DP832", "DP8A123456", "00.01.16")
    assert result.profile_id == "rigol-dp800"
    assert result.category == "psu"
    assert result.error is None


def test_identify_reports_an_identity_no_profile_matches(
    serve: Callable[[str | None], TcpAddress], profiles: list[profile.Profile]
) -> None:
    result = discovery.identify(_found(serve("ACME,Widget 9,1,2")), profiles)

    assert result.idn is not None
    assert result.profile_id is None
    assert result.error == "no profile matches ACME Widget 9"


def test_identify_rejects_a_reply_that_is_not_an_identity(
    serve: Callable[[str | None], TcpAddress], profiles: list[profile.Profile]
) -> None:
    result = discovery.identify(_found(serve("hello")), profiles)

    assert result.idn is None
    assert result.error == "did not answer *IDN? as an instrument"


def test_identify_gives_up_on_a_silent_server_within_twice_the_timeout(
    serve: Callable[[str | None], TcpAddress], profiles: list[profile.Profile]
) -> None:
    address = serve(None)
    started = time.monotonic()

    result = discovery.identify(_found(address), profiles, timeout_s=0.4)

    assert time.monotonic() - started < 0.8
    assert result.idn is None
    assert result.error


def test_identify_abandons_a_link_that_hangs_while_opening(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    release = threading.Event()
    closed: list[str] = []

    class Late:
        where = "late"

        def close(self) -> None:
            closed.append("late")

    def hang(address: Any, settings: Settings) -> Late:
        release.wait(5)
        return Late()

    monkeypatch.setattr(discovery, "open_link", hang)
    started = time.monotonic()

    result = discovery.identify(_found(TcpAddress("10.255.255.1", 5025)), profiles, timeout_s=0.3)

    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 0.6
    assert result.error == "did not answer *IDN? within 0.3 s"
    deadline = time.monotonic() + 2
    while not closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closed == ["late"]


def test_identify_turns_a_link_error_into_its_sentence(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    def refuse(address: Any, settings: Settings) -> None:
        raise LinkError("Connection refused by 10.0.0.5:5025.", "refused")

    monkeypatch.setattr(discovery, "open_link", refuse)

    result = discovery.identify(_found(TcpAddress("10.0.0.5", 5025)), profiles)

    assert result.error == "Connection refused by 10.0.0.5:5025."


def test_identify_tries_serial_at_9600_then_115200(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    bauds: list[int] = []

    class Answering:
        where = "COM3"

        def query(self, command: str) -> str:
            return DP832_REPLY

        def close(self) -> None:
            pass

    def open_link(address: Any, settings: Settings) -> Any:
        bauds.append(address.baud)
        if address.baud == 9600:
            raise LinkTimeout("no reply")
        return Answering()

    monkeypatch.setattr(discovery, "open_link", open_link)

    result = discovery.identify(_found(SerialAddress("COM3")), profiles)

    assert bauds == [9600, 115200]
    assert result.address == SerialAddress("COM3", baud=115200)
    assert result.profile_id == "rigol-dp800"


def test_identify_does_not_retry_serial_when_the_port_is_busy(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    bauds: list[int] = []

    def open_link(address: Any, settings: Settings) -> Any:
        bauds.append(address.baud)
        raise LinkError("COM3 is in use by another program.", "busy")

    monkeypatch.setattr(discovery, "open_link", open_link)

    result = discovery.identify(_found(SerialAddress("COM3")), profiles)

    assert bauds == [9600]
    assert result.error == "COM3 is in use by another program."
    assert result.address == SerialAddress("COM3", baud=9600)


# --- discover and default_name ---------------------------------------------------------------


def test_discover_identifies_concurrently_skips_held_addresses_and_sorts(
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[[str | None], TcpAddress],
    profiles: list[profile.Profile],
) -> None:
    live = serve(DP832_REPLY)
    held = TcpAddress("127.0.0.1", 1)
    opened: list[str] = []
    real_open = discovery.open_link

    def counting_open(address: Any, settings: Settings) -> Any:
        opened.append(address.where)
        return real_open(address, settings)

    monkeypatch.setattr(discovery, "open_link", counting_open)
    monkeypatch.setattr(
        discovery,
        "list_lan",
        lambda budget_s, tried=None: [
            Found(live, None, None, None, "mdns", "b", None),
            Found(held, None, None, None, "mdns", "a", None),
        ],
    )
    monkeypatch.setattr(
        discovery,
        "list_usb",
        lambda: [Found(VisaAddress("USB0::1::2::S::INSTR"), None, None, None, "usb", "m", None)],
    )
    monkeypatch.setattr(
        discovery,
        "list_serial_candidates",
        lambda: pytest.fail("serial listing was not asked for"),
    )

    def identify(found: Found, profiles: list[profile.Profile], timeout_s: float = 2.0) -> Found:
        if found.source == "usb":
            return dataclasses.replace(found, error="no usb")
        return real_identify(found, profiles, timeout_s)

    monkeypatch.setattr(discovery, "identify", identify)

    results = discovery.discover(
        profiles, serial=False, budget_s=0.01, identify_timeout_s=1.0, skip={held.where}
    )

    assert [(f.source, f.detail) for f in results] == [("mdns", "a"), ("mdns", "b"), ("usb", "m")]
    assert results[0].error == "in use by this extension"
    assert results[1].profile_id == "rigol-dp800"
    assert results[2].error == "no usb"
    assert held.where not in opened
    assert opened == [live.where]


def test_discover_with_nothing_to_list_returns_nothing(profiles: list[profile.Profile]) -> None:
    assert discovery.discover(profiles, lan=False, usb=False, serial=False) == []


def test_discover_never_raises_when_identify_does(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    monkeypatch.setattr(
        discovery, "list_usb", lambda: [_found(VisaAddress("USB0::1::2::S::INSTR"), "m")]
    )

    def boom(found: Found, profiles: list[profile.Profile], timeout_s: float = 2.0) -> Found:
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "identify", boom)

    results = discovery.discover(profiles, lan=False, usb=True)

    assert [f.error for f in results] == ["boom"]


def test_default_name_uses_model_and_serial_else_detail() -> None:
    idn = Idn("RIGOL TECHNOLOGIES", "DP832", "DP8A123456", "00.01.16")
    named = Found(TcpAddress("h", 1), idn, None, None, "mdns", "DP832 A", None)
    bare = Found(SerialAddress("COM3"), None, None, None, "serial", "COM3 (DP832)", None)

    assert discovery.default_name(named) == "DP832_DP8A123456"
    assert "(" not in discovery.default_name(bare)


def test_default_name_is_at_most_64_bytes() -> None:
    idn = Idn("X", "M" * 100, "S" * 100, "1")
    found = Found(TcpAddress("h", 1), idn, None, None, "mdns", "d", None)

    name = discovery.default_name(found)

    assert 0 < len(name.encode()) <= 64
