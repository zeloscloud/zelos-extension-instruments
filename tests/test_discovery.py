import dataclasses
import ipaddress
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


@pytest.fixture(autouse=True)
def _no_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing tests never probe the real network."""
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets: set())
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
    # The host mDNS names by raw socket keeps its VXI-11 address too: a held socket must not
    # hide the instrument.
    assert found["TCPIP0::10.0.0.5::inst0::INSTR"].source == "vxi11"
    assert len(found) == 7
    assert vxi.queries == ["TCPIP?*INSTR"]
    assert not vxi.closed


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
    # Well short of the 5 s hang, with room for a slow CI runner.
    assert elapsed < 2.0
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
    # Another host: a held address holds its whole host, and the live one shares localhost.
    held = TcpAddress("10.255.255.1", 1)
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
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    """A host reached by VXI-11 and by raw socket is one instrument: the address that
    identified wins, then the kind the caller prefers. Two sockets on one host stay two."""
    sock = Found(TcpAddress("10.0.0.5", 5025), None, None, None, "scan", "5025", None)
    vxi = Found(VisaAddress("TCPIP::10.0.0.5::INSTR"), None, None, None, "vxi11", "v", None)
    other = Found(TcpAddress("10.0.0.5", 5555), None, None, None, "scan", "5555", None)
    monkeypatch.setattr(discovery, "list_lan", lambda budget_s, tried=None: [sock, vxi, other])
    answers: dict[str, bool] = {}

    def identify(found: Found, profiles: list[profile.Profile], timeout_s: float = 2.0) -> Found:
        if answers.get(found.address.where, True):
            return dataclasses.replace(found, idn=profile.parse_idn(DP832_REPLY))
        return dataclasses.replace(found, error="no reply")

    monkeypatch.setattr(discovery, "identify", identify)

    def run(prefer: str = "socket") -> list[str]:
        found = discovery.discover(profiles, usb=False, budget_s=0.01, prefer=prefer)  # type: ignore[arg-type]
        return [f.address.where for f in found]

    assert run() == ["10.0.0.5:5025", "10.0.0.5:5555"]
    assert run("visa") == ["TCPIP::10.0.0.5::INSTR"]
    answers["10.0.0.5:5025"] = False
    answers["10.0.0.5:5555"] = False
    assert run() == ["TCPIP::10.0.0.5::INSTR"]


def test_vxi11_broadcast_leaves_the_shared_manager_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """pyvisa shares one manager per library with every open link: closing it here closed the
    worker's live session."""
    vxi = FakeManager(["TCPIP0::10.0.0.9::inst0::INSTR"])
    _fake_visa(monkeypatch, py=vxi)
    found = discovery.list_lan(budget_s=0.01)
    assert [f.address.where for f in found] == ["TCPIP0::10.0.0.9::inst0::INSTR"]
    assert not vxi.closed


def _held_search(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[float], threading.Event, threading.Event]:
    """A LAN listing that blocks until released, counting the searches that ran."""
    calls: list[float] = []
    started = threading.Event()
    release = threading.Event()

    def list_lan(budget_s: float, tried: list[str] | None = None) -> list[Found]:
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
        "identify",
        lambda found, profiles, timeout_s=2.0: dataclasses.replace(
            found, idn=profile.parse_idn(DP832_REPLY)
        ),
    )
    return calls, started, release


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    return thread, out


def test_a_caller_that_arrives_during_a_search_takes_its_result(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    """Two searches at once scanned every subnet twice and walked libusb from two threads."""
    calls, started, release = _held_search(monkeypatch)
    first_tried: list[str] = []
    second_tried: list[str] = []
    first, first_out = _in_thread(
        lambda: discovery.discover(profiles, usb=True, budget_s=2.0, tried=first_tried)
    )
    assert started.wait(5)
    second, second_out = _in_thread(
        lambda: discovery.discover(profiles, usb=False, budget_s=1.0, tried=second_tried)
    )
    time.sleep(0.2)
    release.set()
    first.join(5)
    second.join(5)

    assert calls == [2.0]
    assert [f.address.where for f in first_out[0]] == ["10.0.0.5:5025", "USB0::1::2::S::INSTR"]
    assert [f.address.where for f in second_out[0]] == ["10.0.0.5:5025"]
    assert first_tried == ["the LAN", "USB-TMC devices"]
    assert second_tried == ["the LAN"]


def test_a_caller_asking_for_more_waits_and_searches_again(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    calls, started, release = _held_search(monkeypatch)
    first, _ = _in_thread(lambda: discovery.discover(profiles, usb=False, budget_s=1.0))
    assert started.wait(5)
    second, second_out = _in_thread(lambda: discovery.discover(profiles, usb=True, budget_s=1.0))
    time.sleep(0.2)
    assert calls == [1.0]
    release.set()
    first.join(5)
    second.join(5)

    assert calls == [1.0, 1.0]
    assert [f.source for f in second_out[0]] == ["scan", "usb"]


def test_a_caller_after_a_search_searches_again(
    monkeypatch: pytest.MonkeyPatch, profiles: list[profile.Profile]
) -> None:
    calls, _, release = _held_search(monkeypatch)
    release.set()
    discovery.discover(profiles, usb=False)
    discovery.discover(profiles, usb=False)
    assert calls == [2.0, 2.0]


def _held_scope_lan(
    monkeypatch: pytest.MonkeyPatch, vxi: list[str], open_ports: set[tuple[str, int]]
) -> dict[str, Found]:
    """mDNS names 10.0.0.5 by its raw socket on 5025; the scan saw `open_ports` open."""
    raw = discovery.SERVICE_RAW
    _fake_zeroconf(monkeypatch, {(raw, _service("Scope", raw)): FakeInfo(["10.0.0.5"], 5025)})
    _fake_visa(monkeypatch, py=FakeManager(vxi))
    monkeypatch.setattr(discovery, "local_subnets", lambda: [ipaddress.ip_network("10.0.0.0/24")])
    monkeypatch.setattr(discovery, "_scan_open", lambda subnets: open_ports)
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


def test_identify_asks_again_on_the_same_connection(profiles: list[profile.Profile]) -> None:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SleepyHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        address = TcpAddress("127.0.0.1", server.server_address[1])
        result = discovery.identify(_found(address), profiles, timeout_s=0.5)
        assert result.error is None
        assert result.idn is not None and result.idn.model == "DP832"
    finally:
        server.shutdown()
        server.server_close()
