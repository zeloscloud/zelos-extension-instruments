"""Finding instruments on the LAN, USB and serial ports and identifying what answers."""

import asyncio
import dataclasses
import ipaddress
import logging
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import pyvisa
import serialx
import usb.core
import zeroconf
from pyvisa_py.tcpip import TCPIPInstrVxi11

from tests.conftest import LineServer, wait_until
from zelos_extension_instruments import discovery, profile
from zelos_extension_instruments.discovery import Found
from zelos_extension_instruments.link import (
    LinkError,
    LinkSettings,
    LinkTimeout,
    SerialAddress,
    TcpAddress,
    VisaAddress,
)
from zelos_extension_instruments.profile import Idn

real_identify = discovery._identify
real_local_subnets = discovery.local_subnets
real_scan_open = discovery._scan_open
DP832_REPLY = "RIGOL TECHNOLOGIES,DP832,DP8A123456,00.01.16"


@pytest.fixture(autouse=True)
def _reset_warnings() -> None:
    discovery._warned.clear()


@pytest.fixture(scope="module")
def dp800_only(dp800: profile.Profile) -> list[profile.Profile]:
    return [dp800]


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


def _fake_vxi11(monkeypatch: pytest.MonkeyPatch, resources: list[str] | Exception) -> list[int]:
    """The VXI-11 broadcast's answers; the returned list counts the broadcasts."""
    calls: list[int] = []

    def listing() -> list[str]:
        calls.append(1)
        if isinstance(resources, Exception):
            raise resources
        return resources

    monkeypatch.setattr(TCPIPInstrVxi11, "list_resources", staticmethod(listing))
    return calls


def _fake_visa(monkeypatch: pytest.MonkeyPatch, **managers: FakeManager | Exception) -> None:
    def resource_manager(backend: str = "") -> FakeManager:
        manager = managers[backend.lstrip("@")]
        if isinstance(manager, Exception):
            raise manager
        return manager

    monkeypatch.setattr(pyvisa, "ResourceManager", resource_manager)


@pytest.fixture(autouse=True)
def _no_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing tests never probe the real network."""
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets, deadline: set())
    monkeypatch.setattr(discovery, "local_subnets", list)


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
    broadcasts = _fake_vxi11(
        monkeypatch,
        [
            "TCPIP0::10.0.0.7::inst0::INSTR",
            "TCPIP0::10.0.0.9::inst0::INSTR",
            "TCPIP0::10.0.0.5::inst0::INSTR",
        ],
    )

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
    # The host mDNS names by raw socket keeps its VXI-11 address too: a held socket must not
    # hide the instrument.
    assert found["TCPIP0::10.0.0.5::inst0::INSTR"].source == "vxi11"
    assert len(found) == 7
    assert broadcasts == [1]


def test_list_lan_returns_vxi11_when_zeroconf_cannot_open_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse() -> None:
        raise OSError("no socket")

    monkeypatch.setattr(zeroconf, "Zeroconf", refuse)
    _fake_vxi11(monkeypatch, ["TCPIP0::10.0.0.9::inst0::INSTR"])

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
    _fake_vxi11(monkeypatch, RuntimeError("no visa"))

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
        ),
        # A USB-TMC device whose serial cannot be read is listed with the fix, never hidden.
        Found(
            VisaAddress("USB0::0x2A8D::0x1234::?::INSTR"),
            None,
            None,
            None,
            "usb",
            "Model",
            discovery.USB_UNREADABLE,
        ),
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
    assert not visa.closed


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
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        return TcpAddress("127.0.0.1", server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _found(address: TcpAddress | SerialAddress | VisaAddress, detail: str = "x") -> Found:
    return Found(address, None, None, None, "mdns", detail, None)


def test_identify_matches_the_profile_over_a_real_socket(
    serve: Callable[[str | None], TcpAddress], dp800_only: list[profile.Profile]
) -> None:
    address = serve(DP832_REPLY)

    result = discovery.identify(_found(address), dp800_only, timeout_s=2.0)

    assert result.address == address
    assert result.idn == Idn("RIGOL TECHNOLOGIES", "DP832", "DP8A123456", "00.01.16")
    assert result.profile_id == "rigol-dp800"
    assert result.category == "psu"
    assert result.error is None


def test_identify_reports_an_identity_no_profile_matches(
    serve: Callable[[str | None], TcpAddress], dp800_only: list[profile.Profile]
) -> None:
    result = discovery.identify(_found(serve("ACME,Widget 9,1,2")), dp800_only)

    assert result.idn is not None
    assert result.profile_id is None
    assert result.error == "no profile matches ACME Widget 9"


def test_identify_rejects_a_reply_that_is_not_an_identity(
    serve: Callable[[str | None], TcpAddress], dp800_only: list[profile.Profile]
) -> None:
    result = discovery.identify(_found(serve("hello")), dp800_only)

    assert result.idn is None
    assert result.error == "did not answer *IDN? as an instrument"


def test_identify_gives_up_on_a_silent_server_after_asking_twice(
    serve: Callable[[str | None], TcpAddress], dp800_only: list[profile.Profile]
) -> None:
    address = serve(None)
    started = time.monotonic()

    result = discovery.identify(_found(address), dp800_only, timeout_s=0.4)

    # Two queries of 0.4 s each, with room for a slow CI runner.
    assert 0.75 < time.monotonic() - started < 1.5
    assert result.idn is None
    assert result.error


def test_identify_abandons_a_link_that_hangs_while_opening(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    release = threading.Event()
    closed: list[str] = []

    class Late:
        where = "late"

        def close(self) -> None:
            closed.append("late")

    def hang(address: Any, settings: LinkSettings) -> Late:
        release.wait(10)
        return Late()

    monkeypatch.setattr(discovery, "open_link", hang)
    started = time.monotonic()

    result = discovery.identify(_found(TcpAddress("10.255.255.1", 5025)), dp800_only, timeout_s=0.3)

    elapsed = time.monotonic() - started
    release.set()
    # The attempt's budget, the connect timeout and two queries, well short of the 10 s hang.
    assert elapsed < 4.0
    assert (result.error or "").startswith("did not answer *IDN? within")
    deadline = time.monotonic() + 2
    while not closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closed == ["late"]


def test_identify_turns_a_link_error_into_its_sentence(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    def refuse(address: Any, settings: LinkSettings) -> None:
        raise LinkError("Connection refused by 10.0.0.5:5025.", "refused")

    monkeypatch.setattr(discovery, "open_link", refuse)

    result = discovery.identify(_found(TcpAddress("10.0.0.5", 5025)), dp800_only)

    assert result.error == "Connection refused by 10.0.0.5:5025."


def test_identify_tries_serial_at_9600_then_115200(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    bauds: list[int] = []

    class Answering:
        where = "COM3"

        def query(self, command: str) -> str:
            return DP832_REPLY

        def close(self) -> None:
            pass

    def open_link(address: Any, settings: LinkSettings) -> Any:
        bauds.append(address.baud)
        if address.baud == 9600:
            raise LinkTimeout("no reply")
        return Answering()

    monkeypatch.setattr(discovery, "open_link", open_link)

    result = discovery.identify(_found(SerialAddress("COM3")), dp800_only)

    assert bauds == [9600, 115200]
    assert result.address == SerialAddress("COM3", baud=115200)
    assert result.profile_id == "rigol-dp800"


def test_identify_does_not_retry_serial_when_the_port_is_busy(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    bauds: list[int] = []

    def open_link(address: Any, settings: LinkSettings) -> Any:
        bauds.append(address.baud)
        raise LinkError("COM3 is in use by another program.", "busy")

    monkeypatch.setattr(discovery, "open_link", open_link)

    result = discovery.identify(_found(SerialAddress("COM3")), dp800_only)

    assert bauds == [9600]
    assert result.error == "COM3 is in use by another program."
    assert result.address == SerialAddress("COM3", baud=9600)


# --- discover and default_name ---------------------------------------------------------------


def test_discover_identifies_concurrently_skips_held_addresses_and_sorts(
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[[str | None], TcpAddress],
    dp800_only: list[profile.Profile],
) -> None:
    live = serve(DP832_REPLY)
    # Another host: a held address holds its whole host, and the live one shares localhost.
    held = TcpAddress("10.255.255.1", 1)
    opened: list[str] = []
    real_open = discovery.open_link

    def counting_open(address: Any, settings: LinkSettings) -> Any:
        opened.append(address.where)
        return real_open(address, settings)

    monkeypatch.setattr(discovery, "open_link", counting_open)
    monkeypatch.setattr(
        discovery,
        "list_lan",
        lambda budget_s, tried=None, deadline_s=10.0: [
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

    def identify(
        found: Found, profiles: list[profile.Profile], timeout_s: float, deadline: float
    ) -> Found:
        if found.source == "usb":
            return dataclasses.replace(found, error="no usb")
        return real_identify(found, profiles, timeout_s, deadline)

    monkeypatch.setattr(discovery, "_identify", identify)

    results = discovery.discover(
        dp800_only, serial=False, budget_s=0.01, identify_timeout_s=1.0, skip={held.where}
    )

    assert [(f.source, f.detail) for f in results] == [("mdns", "a"), ("mdns", "b"), ("usb", "m")]
    assert results[0].error == "in use by this extension"
    assert results[1].profile_id == "rigol-dp800"
    assert results[2].error == "no usb"
    assert held.where not in opened
    assert opened == [live.where]


def test_discover_with_nothing_to_list_returns_nothing(dp800_only: list[profile.Profile]) -> None:
    assert discovery.discover(dp800_only, lan=False, usb=False, serial=False) == []


def test_discover_never_raises_when_identify_does(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    monkeypatch.setattr(
        discovery, "list_usb", lambda: [_found(VisaAddress("USB0::1::2::S::INSTR"), "m")]
    )

    def boom(
        found: Found, profiles: list[profile.Profile], timeout_s: float, deadline: float
    ) -> Found:
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "_identify", boom)

    results = discovery.discover(dp800_only, lan=False, usb=True)

    assert [f.error for f in results] == ["boom"]


def test_default_name_is_the_model_else_the_detail() -> None:
    idn = Idn("RIGOL TECHNOLOGIES", "DP832", "DP8A123456", "00.01.16")
    named = Found(TcpAddress("h", 1), idn, None, None, "mdns", "DP832 A", None)
    bare = Found(SerialAddress("COM3"), None, None, None, "serial", "COM3 (DP832)", None)

    assert discovery.default_name(named) == "DP832"
    assert "(" not in discovery.default_name(bare)


def test_default_name_is_at_most_64_bytes() -> None:
    idn = Idn("X", "M" * 100, "S" * 100, "1")
    found = Found(TcpAddress("h", 1), idn, None, None, "mdns", "d", None)

    name = discovery.default_name(found)

    assert 0 < len(name.encode()) <= 64


def test_discover_keeps_one_address_per_instrument(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    """A host reached by VXI-11 and by raw socket is one instrument: the address that
    identified wins, then the kind the caller prefers. Two sockets on one host stay two."""
    sock = Found(TcpAddress("10.0.0.5", 5025), None, None, None, "scan", "5025", None)
    vxi = Found(VisaAddress("TCPIP::10.0.0.5::INSTR"), None, None, None, "vxi11", "v", None)
    other = Found(TcpAddress("10.0.0.5", 5555), None, None, None, "scan", "5555", None)
    monkeypatch.setattr(
        discovery, "list_lan", lambda budget_s, tried=None, deadline_s=10.0: [sock, vxi, other]
    )
    answers: dict[str, bool] = {}

    def identify(
        found: Found, profiles: list[profile.Profile], timeout_s: float, deadline: float
    ) -> Found:
        if answers.get(found.address.where, True):
            return dataclasses.replace(found, idn=profile.parse_idn(DP832_REPLY))
        return dataclasses.replace(found, error="no reply")

    monkeypatch.setattr(discovery, "_identify", identify)

    def run(prefer: str = "socket") -> list[str]:
        found = discovery.discover(dp800_only, usb=False, budget_s=0.01, prefer=prefer)  # type: ignore[arg-type]
        return [f.address.where for f in found]

    assert run() == ["10.0.0.5:5025", "10.0.0.5:5555"]
    assert run("visa") == ["TCPIP::10.0.0.5::INSTR"]
    answers["10.0.0.5:5025"] = False
    answers["10.0.0.5:5555"] = False
    assert run() == ["TCPIP::10.0.0.5::INSTR"]


def test_the_vxi11_broadcast_opens_no_resource_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """pyvisa shares a library's manager with every open link, so a closed one fails them."""
    _fake_vxi11(monkeypatch, ["TCPIP0::10.0.0.9::inst0::INSTR"])
    _fake_visa(monkeypatch, py=RuntimeError("no manager may be opened"))
    found = discovery.list_lan(budget_s=0.01)
    assert [f.address.where for f in found] == ["TCPIP0::10.0.0.9::inst0::INSTR"]


def _held_search(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[float], threading.Event, threading.Event]:
    """A LAN listing that blocks until released, counting the searches that ran."""
    calls: list[float] = []
    started = threading.Event()
    release = threading.Event()

    def list_lan(
        budget_s: float, tried: list[str] | None = None, deadline_s: float = 10.0
    ) -> list[Found]:
        calls.append(budget_s)
        started.set()
        release.wait(5)
        if tried is not None:
            tried.append("the LAN")
        return [Found(TcpAddress("10.0.0.5", 5025), None, None, None, "scan", "s", None)]

    monkeypatch.setattr(discovery, "list_lan", list_lan)
    monkeypatch.setattr(
        discovery,
        "list_usb",
        lambda: [Found(VisaAddress("USB0::1::2::S::INSTR"), None, None, None, "usb", "u", None)],
    )
    monkeypatch.setattr(
        discovery,
        "_identify",
        lambda found, profiles, timeout_s, deadline: dataclasses.replace(
            found, idn=profile.parse_idn(DP832_REPLY)
        ),
    )
    return calls, started, release


class _SpyLock:
    """The single-flight lock, signalling once a second caller reaches for it."""

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self._attempts = 0
        self.contended = threading.Event()

    def acquire(self, timeout: float = -1) -> bool:
        self._attempts += 1
        if self._attempts > 1:
            self.contended.set()
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()


def _spy_on_the_lock(monkeypatch: pytest.MonkeyPatch) -> _SpyLock:
    spy = _SpyLock(discovery._searches._lock)
    monkeypatch.setattr(discovery._searches, "_lock", spy)
    return spy


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    return thread, out


def test_a_caller_that_arrives_during_a_search_takes_its_result(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    """Two searches at once scanned every subnet twice and walked libusb from two threads."""
    calls, started, release = _held_search(monkeypatch)
    spy = _spy_on_the_lock(monkeypatch)
    first_tried: list[str] = []
    second_tried: list[str] = []
    first, first_out = _in_thread(
        lambda: discovery.discover(dp800_only, usb=True, budget_s=2.0, tried=first_tried)
    )
    assert started.wait(5)
    second, second_out = _in_thread(
        lambda: discovery.discover(dp800_only, usb=False, budget_s=1.0, tried=second_tried)
    )
    assert spy.contended.wait(5)
    release.set()
    first.join(5)
    second.join(5)

    assert calls == [2.0]
    assert [f.address.where for f in first_out[0]] == ["10.0.0.5:5025", "USB0::1::2::S::INSTR"]
    assert [f.address.where for f in second_out[0]] == ["10.0.0.5:5025"]
    assert first_tried == ["the LAN", "USB-TMC devices"]
    assert second_tried == ["the LAN"]


def test_a_caller_asking_for_more_waits_and_searches_again(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    calls, started, release = _held_search(monkeypatch)
    spy = _spy_on_the_lock(monkeypatch)
    first, _ = _in_thread(lambda: discovery.discover(dp800_only, usb=False, budget_s=1.0))
    assert started.wait(5)
    second, second_out = _in_thread(lambda: discovery.discover(dp800_only, usb=True, budget_s=1.0))
    assert spy.contended.wait(5)
    # The second caller stays behind the held search, which a narrower result cannot answer.
    second.join(0.2)
    assert calls == [1.0]

    release.set()
    first.join(5)
    second.join(5)

    assert calls == [1.0, 1.0]
    assert [f.source for f in second_out[0]] == ["scan", "usb"]


def test_a_caller_after_a_search_searches_again(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    calls, _, release = _held_search(monkeypatch)
    release.set()
    discovery.discover(dp800_only, usb=False)
    discovery.discover(dp800_only, usb=False)
    assert calls == [2.0, 2.0]


def _held_scope_lan(
    monkeypatch: pytest.MonkeyPatch, vxi: list[str], open_ports: set[tuple[str, int]]
) -> dict[str, Found]:
    """mDNS names 10.0.0.5 by its raw socket on 5025; the scan saw `open_ports` open."""
    raw = discovery.SERVICE_RAW
    _fake_zeroconf(monkeypatch, {(raw, _service("Scope", raw)): FakeInfo(["10.0.0.5"], 5025)})
    _fake_vxi11(monkeypatch, vxi)
    monkeypatch.setattr(discovery, "local_subnets", lambda: [ipaddress.ip_network("10.0.0.0/24")])
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets, deadline: open_ports)
    return {f.address.where: f for f in discovery.list_lan(budget_s=0.05)}


def test_a_socket_the_scan_found_closed_is_dropped_when_the_host_has_another_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held socket cost a whole connect timeout in every Auto-configure."""
    found = _held_scope_lan(monkeypatch, ["TCPIP0::10.0.0.5::inst0::INSTR"], set())
    assert list(found) == ["TCPIP0::10.0.0.5::inst0::INSTR"]


def test_a_closed_socket_stays_when_it_is_the_only_address(monkeypatch: pytest.MonkeyPatch) -> None:
    found = _held_scope_lan(monkeypatch, [], set())
    assert list(found) == ["10.0.0.5:5025"]


def test_an_open_socket_stays_beside_the_other_address(monkeypatch: pytest.MonkeyPatch) -> None:
    found = _held_scope_lan(monkeypatch, ["TCPIP0::10.0.0.5::inst0::INSTR"], {("10.0.0.5", 5025)})
    assert sorted(found) == ["10.0.0.5:5025", "TCPIP0::10.0.0.5::inst0::INSTR"]


class _SleepyHandler(socketserver.StreamRequestHandler):
    """Ignores the first *IDN? on each connection, as an HMP does after a connect."""

    def handle(self) -> None:
        seen = 0
        for line in self.rfile:
            if line.strip() == b"*IDN?":
                seen += 1
                if seen > 1:
                    self.wfile.write(DP832_REPLY.encode() + b"\n")


def test_identify_asks_again_on_the_same_connection(dp800_only: list[profile.Profile]) -> None:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SleepyHandler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        address = TcpAddress("127.0.0.1", server.server_address[1])
        result = discovery.identify(_found(address), dp800_only, timeout_s=0.5)
        assert result.error is None
        assert result.idn is not None and result.idn.model == "DP832"
    finally:
        server.shutdown()
        server.server_close()


# --- the deadline ----------------------------------------------------------------------------


def test_a_listing_that_hangs_ends_at_the_deadline_and_keeps_no_thread_alive(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    release = threading.Event()

    def stalled(budget_s: float, tried: list[str] | None = None, deadline_s: float = 10.0):
        release.wait(30)
        return []

    monkeypatch.setattr(discovery, "list_lan", stalled)
    monkeypatch.setattr(
        discovery, "list_usb", lambda: [_found(VisaAddress("USB0::1::2::S::INSTR"), "m")]
    )
    monkeypatch.setattr(
        discovery,
        "_identify",
        lambda found, profiles, timeout_s, deadline: dataclasses.replace(found, error="no usb"),
    )
    tried: list[str] = []
    started = time.monotonic()
    try:
        results = discovery.discover(dp800_only, deadline_s=0.5, tried=tried)
        elapsed = time.monotonic() - started
        stuck = [
            t
            for t in threading.enumerate()
            if not t.daemon
            and t is not threading.main_thread()
            # The pytest-timeout watchdog is a non-daemon timer.
            and not t.name.startswith("pytest_timeout")
        ]

    finally:
        release.set()
    assert elapsed < 1.0
    assert [f.detail for f in results] == ["m"]
    assert "the LAN search (cut short)" in tried
    assert stuck == []


def test_an_instrument_still_answering_at_the_deadline_is_reported_as_out_of_time(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    monkeypatch.setattr(
        discovery,
        "list_lan",
        lambda budget_s, tried=None, deadline_s=10.0: [_found(TcpAddress("10.0.0.5", 5025), "a")],
    )
    release = threading.Event()

    def slow(found: Found, profiles: list[profile.Profile], timeout_s: float, deadline: float):
        release.wait(30)
        return found

    monkeypatch.setattr(discovery, "_identify", slow)
    started = time.monotonic()
    try:
        results = discovery.discover(dp800_only, usb=False, deadline_s=0.5)
    finally:
        release.set()
    assert time.monotonic() - started < 1.0
    assert [f.error for f in results] == [discovery.OUT_OF_TIME]


def test_one_instruments_addresses_are_asked_one_after_another(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    """Two clients at once on one instrument's parser garble each other's replies."""
    addresses = [
        _found(TcpAddress("10.0.0.5", 5025), "a"),
        _found(VisaAddress("TCPIP::10.0.0.5::INSTR"), "a vxi"),
        _found(TcpAddress("10.0.0.6", 5025), "b"),
    ]
    monkeypatch.setattr(
        discovery, "list_lan", lambda budget_s, tried=None, deadline_s=10.0: addresses
    )
    spans: dict[str, tuple[float, float]] = {}

    def timed(found: Found, profiles: list[profile.Profile], timeout_s: float, deadline: float):
        began = time.monotonic()
        time.sleep(0.2)
        spans[found.address.where] = (began, time.monotonic())
        return dataclasses.replace(found, error="silent")

    monkeypatch.setattr(discovery, "_identify", timed)
    discovery.discover(dp800_only, usb=False, deadline_s=5.0)

    def overlap(a: str, b: str) -> bool:
        return spans[a][0] < spans[b][1] and spans[b][0] < spans[a][1]

    assert not overlap("10.0.0.5:5025", "TCPIP::10.0.0.5::INSTR")
    assert overlap("10.0.0.5:5025", "10.0.0.6:5025")


def test_the_port_scan_starts_no_batch_past_its_deadline() -> None:
    import asyncio

    started = time.monotonic()
    hits = asyncio.run(discovery._open_ports([("10.255.255.1", 5025)] * 5000, started - 1))
    assert hits == []
    assert time.monotonic() - started < 0.2


def test_an_instrument_held_under_its_host_name_is_held_at_its_address(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    monkeypatch.setattr(
        discovery,
        "list_lan",
        lambda budget_s, tried=None, deadline_s=10.0: [_found(TcpAddress("127.0.0.1", 5025), "a")],
    )
    monkeypatch.setattr(
        discovery,
        "_identify",
        lambda *a: pytest.fail("a held instrument was opened"),
    )
    results = discovery.discover(dp800_only, usb=False, skip={"localhost:5025"}, deadline_s=5.0)
    assert [f.error for f in results] == [discovery.IN_USE]


def test_a_held_instrument_is_held_under_its_other_address_too(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    held = discovery.Found(
        VisaAddress("TCPIP::10.0.0.5::INSTR"), None, None, None, "vxi11", "v", None
    )
    sock = discovery.Found(TcpAddress("10.0.0.5", 5025), None, None, None, "scan", "s", None)
    monkeypatch.setattr(
        discovery, "list_lan", lambda budget_s, tried=None, deadline_s=10.0: [held, sock]
    )
    monkeypatch.setattr(
        discovery, "identify", lambda f, p, t=2.0: pytest.fail("opened a held host")
    )
    found = discovery.discover(profiles, usb=False, budget_s=0.01, skip={"10.0.0.5:5025"})
    assert [(f.address.where, f.error) for f in found] == [
        ("10.0.0.5:5025", discovery.IN_USE),
    ]


# --- the port scan and the interfaces it covers ----------------------------------------------


def test_the_port_scan_reports_the_listening_port_and_not_the_closed_one(
    instrument: LineServer, closed_port: int
) -> None:
    targets = [("127.0.0.1", instrument.port), ("127.0.0.1", closed_port)]
    hits = asyncio.run(discovery._open_ports(targets, time.monotonic() + 5))
    assert hits == [("127.0.0.1", instrument.port)]


def test_the_port_scan_does_not_count_a_connection_that_failed(
    instrument: LineServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused connection finishes at once, with an error, as it does on most systems."""
    refused = 9
    loop = asyncio.new_event_loop()

    class Refusing:
        def __getattr__(self, name: str) -> Any:
            return getattr(loop, name)

        async def sock_connect(self, sock: socket.socket, address: tuple[str, int]) -> None:
            if address[1] == refused:
                raise ConnectionRefusedError
            await loop.sock_connect(sock, address)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Refusing())
    targets = [("127.0.0.1", refused), ("127.0.0.1", instrument.port)]
    try:
        hits = loop.run_until_complete(discovery._open_ports(targets, time.monotonic() + 5))
    finally:
        loop.close()
    assert hits == [("127.0.0.1", instrument.port)]


def test_scan_lan_lists_each_host_that_accepts_a_connection_on_a_scpi_port(
    instrument: LineServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "SCAN_PORTS", (instrument.port,))
    monkeypatch.setattr(discovery, "_scan_open", real_scan_open)

    found = discovery.scan_lan([ipaddress.IPv4Network("127.0.0.1/32")], deadline_s=5)

    assert found == [
        Found(
            TcpAddress("127.0.0.1", instrument.port),
            None,
            None,
            None,
            "scan",
            f"127.0.0.1:{instrument.port}",
            None,
        )
    ]


def test_scan_lan_skips_a_subnet_too_big_to_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_scan_open", real_scan_open)
    monkeypatch.setattr(
        discovery,
        "_open_ports",
        lambda targets, deadline: pytest.fail("a /16 was scanned"),
    )
    assert discovery.scan_lan([ipaddress.IPv4Network("10.1.0.0/16")], deadline_s=5) == []


def test_a_host_open_on_two_ports_is_one_candidate_on_the_first_port_in_scan_order() -> None:
    found = discovery._scan_hosts({("10.0.0.5", 5555), ("10.0.0.5", 5025), ("10.0.0.6", 5555)})
    assert [f.address for f in found] == [
        TcpAddress("10.0.0.5", 5025),
        TcpAddress("10.0.0.6", 5555),
    ]


def _interface(family: int, address: str, netmask: str | None) -> SimpleNamespace:
    return SimpleNamespace(family=family, address=address, netmask=netmask)


def test_local_subnets_keeps_the_ipv4_networks_the_machine_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inet = socket.AF_INET
    interfaces = {
        "eth0": [
            _interface(inet, "192.168.1.20", "255.255.255.0"),
            _interface(socket.AF_INET6, "2001:db8::5", "ffff:ffff:ffff:ffff::"),
        ],
        "eth1": [
            _interface(inet, "192.168.1.30", "255.255.255.0"),
            _interface(inet, "10.0.4.9", "255.255.252.0"),
        ],
        "lo": [_interface(inet, "127.0.0.1", "255.0.0.0")],
        "autoconf": [_interface(inet, "169.254.3.4", "255.255.0.0")],
        "tunnel": [_interface(inet, "10.9.9.9", "255.255.255.255")],
        "down": [_interface(inet, "172.16.0.5", None)],
    }
    monkeypatch.setattr(discovery.psutil, "net_if_addrs", lambda: interfaces)

    assert real_local_subnets() == [
        ipaddress.ip_network("192.168.1.0/24"),
        ipaddress.ip_network("10.0.4.0/22"),
    ]


def test_local_subnets_is_empty_when_the_interfaces_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail() -> dict[str, list[Any]]:
        raise RuntimeError("no permission")

    monkeypatch.setattr(discovery.psutil, "net_if_addrs", fail)

    with caplog.at_level(logging.WARNING, logger=discovery.log.name):
        assert real_local_subnets() == []

    assert "cannot list the network interfaces: no permission" in caplog.text


class _Limits:
    """Stands in for the `resource` module with a fixed open-file limit."""

    RLIMIT_NOFILE = 7

    def __init__(self, soft: int | Exception) -> None:
        self._soft = soft

    def getrlimit(self, which: int) -> tuple[int, int]:
        if isinstance(self._soft, Exception):
            raise self._soft
        return self._soft, 1 << 20


@pytest.mark.parametrize(
    ("platform", "limit", "expected"),
    [
        ("linux", _Limits(256), 192),
        ("linux", _Limits(50), 16),
        ("linux", _Limits(1 << 20), 2048),
        ("linux", _Limits(OSError("no limit")), 2048),
        ("linux", None, 2048),
        ("win32", _Limits(256), 2048),
    ],
    ids=["limited", "floor", "unlimited", "unreadable", "no resource module", "windows"],
)
def test_the_scan_runs_as_many_handshakes_as_the_open_file_limit_allows(
    monkeypatch: pytest.MonkeyPatch, platform: str, limit: _Limits | None, expected: int
) -> None:
    monkeypatch.setattr(discovery, "sys", SimpleNamespace(platform=platform))
    # A None entry makes `import resource` fail, as on a platform without the module.
    monkeypatch.setitem(sys.modules, "resource", limit)

    assert discovery._scan_parallel() == expected


# --- what list_lan survives and reports -------------------------------------------------------


def test_list_lan_skips_a_service_that_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Failing(FakeInfo):
        def parsed_addresses(self) -> list[str]:
            raise RuntimeError("the resolver hung up")

    raw = discovery.SERVICE_RAW
    _fake_zeroconf(
        monkeypatch,
        {
            (raw, _service("Broken", raw)): Failing([], 5025),
            (raw, _service("Fine", raw)): FakeInfo(["10.0.0.6"], 5025),
        },
    )
    _fake_vxi11(monkeypatch, [])

    with caplog.at_level(logging.DEBUG, logger=discovery.log.name):
        found = discovery.list_lan(budget_s=0.05)

    assert [f.address.where for f in found] == ["10.0.0.6:5025"]
    assert "Could not resolve Broken._scpi-raw._tcp.local.: the resolver hung up" in caplog.text


def test_list_lan_keeps_the_broadcast_results_when_the_mdns_browse_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Zeroconf:
        def close(self) -> None:
            raise OSError("close failed")

    def browse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("browse failed")

    monkeypatch.setattr(zeroconf, "Zeroconf", Zeroconf)
    monkeypatch.setattr(zeroconf, "ServiceBrowser", browse)
    _fake_vxi11(monkeypatch, ["TCPIP0::10.0.0.9::inst0::INSTR"])

    with caplog.at_level(logging.DEBUG, logger=discovery.log.name):
        found = discovery.list_lan(budget_s=0.01)

    assert [f.source for f in found] == ["vxi11"]
    assert "mDNS browse failed: browse failed" in caplog.text
    assert "Closing mDNS failed: close failed" in caplog.text


def test_list_lan_is_empty_when_the_interfaces_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail() -> list[ipaddress.IPv4Network]:
        raise RuntimeError("no interfaces")

    _fake_zeroconf(monkeypatch, {})
    monkeypatch.setattr(discovery, "local_subnets", fail)

    with caplog.at_level(logging.WARNING, logger=discovery.log.name):
        assert discovery.list_lan(budget_s=0.01) == []

    assert "LAN listing failed: no interfaces" in caplog.text


def test_list_lan_says_what_it_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zeroconf(monkeypatch, {})
    _fake_vxi11(monkeypatch, [])
    monkeypatch.setattr(
        discovery,
        "local_subnets",
        lambda: [ipaddress.ip_network("10.0.0.0/24"), ipaddress.ip_network("10.1.0.0/16")],
    )
    tried: list[str] = []

    discovery.list_lan(budget_s=0.05, tried=tried)

    assert tried == [
        "mDNS for 0.05 s",
        "VXI-11 broadcast",
        "ports 5025, 5555, 2268, 3490 on 10.0.0.0/24",
    ]


def test_list_lan_says_when_there_is_no_subnet_to_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zeroconf(monkeypatch, {})
    _fake_vxi11(monkeypatch, [])
    tried: list[str] = []

    discovery.list_lan(budget_s=0.05, tried=tried)

    assert tried[-1] == "no local subnet to scan"


def test_list_lan_marks_the_searches_it_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def stalled(*args: Any) -> Any:
        release.wait(30)
        return {}

    _fake_zeroconf(monkeypatch, {})
    monkeypatch.setattr(discovery, "_vxi11", stalled)
    monkeypatch.setattr(discovery, "_scan_open", stalled)
    monkeypatch.setattr(discovery, "local_subnets", lambda: [ipaddress.ip_network("10.0.0.0/24")])
    tried: list[str] = []
    try:
        found = discovery.list_lan(budget_s=0.01, tried=tried, deadline_s=0.3)
    finally:
        release.set()

    assert found == []
    assert tried == [
        "mDNS for 0.01 s",
        "VXI-11 broadcast (cut short)",
        "ports 5025, 5555, 2268, 3490 on 10.0.0.0/24 (cut short)",
    ]


def test_a_host_the_broadcast_lists_keeps_its_raw_socket_when_the_scan_finds_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_zeroconf(monkeypatch, {})
    _fake_vxi11(monkeypatch, ["TCPIP0::10.0.0.5::inst0::INSTR"])
    monkeypatch.setattr(discovery, "local_subnets", lambda: [ipaddress.ip_network("10.0.0.0/24")])
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets, deadline: {("10.0.0.5", 5025)})

    found = discovery.list_lan(budget_s=0.05)

    assert sorted(f.address.where for f in found) == [
        "10.0.0.5:5025",
        "TCPIP0::10.0.0.5::inst0::INSTR",
    ]


class _UnnamedUsbDevice(FakeUsbDevice):
    @property
    def product(self) -> str:
        raise ValueError("no product string")

    @product.setter
    def product(self, value: str) -> None:
        pass


def test_list_usb_names_a_device_without_a_product_string_by_its_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_libusb(monkeypatch, lambda **kwargs: [_UnnamedUsbDevice(0x1AB1, 0x0E11, "DP8A123456")])

    [found] = discovery.list_usb()

    assert found.detail == "1AB1:0E11"
    assert found.error is None


# --- serial ports ----------------------------------------------------------------------------


def test_list_all_serial_ports_lists_every_port_by_device_with_what_identifies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = dataclasses.replace(
        _port("COM4", 0x0403, "FT232R USB UART"), manufacturer="FTDI", serial_number="A1"
    )
    ports = [bridge, _port("COM3", None, None)]
    monkeypatch.setattr(serialx, "list_serial_ports", lambda: ports)

    assert discovery.list_all_serial_ports() == [
        discovery.SerialPortChoice("COM3", "COM3", None),
        discovery.SerialPortChoice("COM4", "FT232R USB UART (COM4)", "FTDI 0403:0001 A1"),
    ]


def test_list_all_serial_ports_is_empty_when_the_ports_cannot_be_listed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail() -> list[serialx.SerialPortInfo]:
        raise RuntimeError("enumeration failed")

    monkeypatch.setattr(serialx, "list_serial_ports", fail)

    with caplog.at_level(logging.WARNING, logger=discovery.log.name):
        assert discovery.list_all_serial_ports() == []

    assert "cannot list serial ports: enumeration failed" in caplog.text


def test_a_serial_port_becomes_a_config_entry_with_its_baud() -> None:
    found = Found(
        SerialAddress("COM3", baud=115200), None, "rigol-dp800", None, "serial", "COM3", None
    )
    assert discovery.config_entry(found) == {
        "connection": "serial",
        "port": "COM3",
        "baud": 115200,
        "profile": "rigol-dp800",
    }


def test_discover_searches_the_serial_ports_when_asked(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    monkeypatch.setattr(
        discovery, "list_serial_candidates", lambda: [_found(SerialAddress("COM3"), "COM3")]
    )
    monkeypatch.setattr(
        discovery,
        "_identify",
        lambda found, profiles, timeout_s, deadline: dataclasses.replace(found, error="silent"),
    )
    tried: list[str] = []

    results = discovery.discover(dp800_only, lan=False, usb=False, serial=True, tried=tried)

    assert [(f.address, f.error) for f in results] == [(SerialAddress("COM3"), "silent")]
    assert tried == ["serial ports of known instrument vendors"]


def test_a_search_that_cannot_start_before_its_deadline_says_another_is_running(
    dp800_only: list[profile.Profile],
) -> None:
    lock = discovery._searches._lock
    assert lock.acquire(timeout=5)
    tried: list[str] = []
    try:
        results = discovery.discover(dp800_only, lan=False, usb=True, deadline_s=0.1, tried=tried)
    finally:
        lock.release()

    assert results == []
    assert tried == ["nothing: another search was still running"]


# --- an identify attempt that fails oddly ----------------------------------------------------


@pytest.mark.parametrize(
    ("error", "sentence"),
    [(RuntimeError("driver crashed"), "driver crashed"), (RuntimeError(), "RuntimeError")],
    ids=["with a message", "without one"],
)
def test_identify_turns_any_failure_of_the_attempt_into_its_sentence(
    monkeypatch: pytest.MonkeyPatch,
    dp800_only: list[profile.Profile],
    error: Exception,
    sentence: str,
) -> None:
    def crash(address: Any, settings: LinkSettings) -> None:
        raise error

    monkeypatch.setattr(discovery, "open_link", crash)

    result = discovery.identify(_found(TcpAddress("10.0.0.5", 5025)), dp800_only)

    assert result.error == sentence


def test_identify_ignores_a_link_that_fails_to_close(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    class Quirky:
        where = "quirky"

        def clear(self) -> None:
            pass

        def query(self, command: str) -> str:
            return DP832_REPLY

        def close(self) -> None:
            raise OSError("close failed")

    monkeypatch.setattr(discovery, "open_link", lambda address, settings: Quirky())

    result = discovery.identify(_found(TcpAddress("10.0.0.5", 5025)), dp800_only)

    assert result.profile_id == "rigol-dp800" and result.error is None


def test_identify_closes_a_link_whose_query_outlives_the_deadline(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    release = threading.Event()
    closed: list[str] = []

    class Stuck:
        where = "stuck"

        def clear(self) -> None:
            pass

        def query(self, command: str) -> str:
            release.wait(10)
            return ""

        def close(self) -> None:
            closed.append("stuck")

    monkeypatch.setattr(discovery, "open_link", lambda address, settings: Stuck())
    try:
        result = discovery.identify(
            _found(TcpAddress("10.0.0.5", 5025)), dp800_only, timeout_s=2.0, deadline_s=0.3
        )
        assert (result.error or "").startswith("did not answer *IDN? within")
        assert wait_until(lambda: closed == ["stuck"], timeout_s=2)
    finally:
        release.set()


def test_identify_does_not_try_the_next_baud_rate_once_the_deadline_has_passed(
    monkeypatch: pytest.MonkeyPatch, dp800_only: list[profile.Profile]
) -> None:
    clock = {"now": 0.0}
    bauds: list[int] = []

    def open_late(address: SerialAddress, settings: LinkSettings) -> None:
        bauds.append(address.baud)
        # The first attempt fails after the deadline.
        clock["now"] = 10.0
        raise LinkError("closed", "other")

    monkeypatch.setattr(discovery, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setattr(discovery, "open_link", open_late)

    result = discovery.identify(_found(SerialAddress("COM3")), dp800_only, deadline_s=5.0)

    assert bauds == [9600]
    assert result.error == discovery.OUT_OF_TIME


def test_list_lan_lists_a_host_only_the_port_scan_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_zeroconf(monkeypatch, {})
    _fake_vxi11(monkeypatch, [])
    monkeypatch.setattr(discovery, "local_subnets", lambda: [ipaddress.ip_network("10.0.0.0/24")])
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets, deadline: {("10.0.0.7", 5555)})

    found = discovery.list_lan(budget_s=0.05)

    assert [(f.source, f.address) for f in found] == [("scan", TcpAddress("10.0.0.7", 5555))]


def test_a_closed_socket_on_a_host_name_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name is not an address the scan could have covered, so its socket is not known closed."""
    socket_candidate = _found(TcpAddress("psu.local", 5025))
    vxi_candidate = _found(VisaAddress("TCPIP::psu.local::inst0::INSTR"))
    found = {"psu.local": socket_candidate, "psu.local vxi11": vxi_candidate}

    discovery._drop_closed_sockets(found, [ipaddress.IPv4Network("10.0.0.0/24")], set())

    assert list(found.values()) == [socket_candidate, vxi_candidate]


def test_a_listing_that_raises_yields_its_default_and_logs_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = discovery._Background(lambda: 1 // 0, "broken listing")

    with caplog.at_level(logging.WARNING, logger=discovery.log.name):
        assert job.result(time.monotonic() + 5, "default") == "default"

    assert "broken listing failed: integer division or modulo by zero" in caplog.text


class _BrokenUsbDevice(FakeUsbDevice):
    def __iter__(self) -> Iterator[list[SimpleNamespace]]:
        raise OSError("descriptor read failed")


def test_list_usb_skips_a_device_whose_descriptors_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    devices = [
        _BrokenUsbDevice(0x2A8D, 0x1234, "X1"),
        FakeUsbDevice(0x1AB1, 0x0E11, "DP8A123456", "DP832"),
    ]
    _fake_libusb(monkeypatch, lambda **kwargs: devices)

    with caplog.at_level(logging.INFO, logger=discovery.log.name):
        found = discovery.list_usb()

    assert [f.detail for f in found] == ["DP832"]
    assert "Skipping a USB device that cannot be read: descriptor read failed" in caplog.text


def test_each_address_maps_to_the_connection_that_reaches_it() -> None:
    assert discovery.connection_of(TcpAddress("10.0.0.5", 5025)) == "lan"
    assert discovery.connection_of(SerialAddress("COM3")) == "serial"
    assert discovery.connection_of(VisaAddress("USB0::0x2A8D::0x1602::MY1::INSTR")) == "usb"
    assert discovery.connection_of(VisaAddress("TCPIP0::10.0.0.5::inst0::INSTR")) == "visa"
