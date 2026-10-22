"""Finds the instruments on this machine's network and ports and names the ones it knows.

Listing never opens an instrument and identifying never raises: a failure becomes `Found.error`.
Libraries do the protocol work: zeroconf (mDNS), pyvisa-py (VXI-11 portmapper broadcast), pyusb
with libusb-package (USB-TMC) and serialx (serial ports).
"""

import asyncio
import dataclasses
import ipaddress
import logging
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import libusb_package
import psutil
import pyvisa
import serialx
import usb.core
import zeroconf
from pyvisa_py.tcpip import TCPIPInstrVxi11

from . import connect, naming
from .link import (
    CONNECT_TIMEOUT_S,
    Address,
    Link,
    LinkError,
    LinkSettings,
    LinkTimeout,
    SerialAddress,
    TcpAddress,
    VisaAddress,
    open_link,
)
from .profile import Idn, Profile, find, is_identity, parse_idn

log = logging.getLogger(__name__)

RAW_PORT = 5025
SERVICE_RAW = "_scpi-raw._tcp.local."
SERVICE_HISLIP = "_hislip._tcp.local."
SERVICE_VXI11 = "_vxi-11._tcp.local."
SERVICE_LXI = "_lxi._tcp.local."
SERVICE_TYPES = [SERVICE_RAW, SERVICE_HISLIP, SERVICE_VXI11, SERVICE_LXI]

USBTMC_CLASS = 0xFE
USBTMC_SUBCLASS = 3

# USB vendor ids of bench-instrument makers; generic USB-serial bridges are not on this list.
INSTRUMENT_VENDORS = frozenset(
    {
        0x2A8D,  # Keysight
        0x0957,  # Agilent
        0x1AB1,  # Rigol
        0xF4EC,  # Siglent
        0xF4ED,  # Siglent
        0x05E6,  # Keithley
        0x0AAD,  # Rohde & Schwarz
        0x0699,  # Tektronix
        0x2E3C,  # B&K Precision
        0x2184,  # GW Instek
        0x5345,  # Owon
    }
)
SERIAL_BAUDS = (9600, 115200)

IN_USE = "in use by this extension"
USB_UNREADABLE = (
    "a USB-TMC device this user cannot read: on Windows install NI-VISA or bind it to WinUSB "
    "with Zadig; on Linux add a udev rule for it"
)
NOT_AN_INSTRUMENT = "did not answer *IDN? as an instrument"
OUT_OF_TIME = "did not answer before the search ended"
RESOLVE_TIMEOUT_MS = 1000

_warned: set[str] = set()


@dataclass(frozen=True)
class Found:
    """One candidate instrument: where it is, what it said, and which profile fits."""

    address: Address
    idn: Idn | None
    profile_id: str | None
    category: str | None
    source: str
    detail: str
    error: str | None


def _candidate(address: Address, source: str, detail: str) -> Found:
    return Found(address, None, None, None, source, detail, None)


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


class _Background:
    """A call on a daemon thread: a caller past its deadline neither waits for it nor keeps the
    process alive for it."""

    _UNSET: Any = object()

    def __init__(self, call: Callable[[], Any], name: str) -> None:
        self._done = threading.Event()
        self._value: Any = self._UNSET
        threading.Thread(target=self._run, args=(call,), name=name, daemon=True).start()

    def _run(self, call: Callable[[], Any]) -> None:
        try:
            self._value = call()
        except Exception as e:
            log.warning("%s failed: %s", threading.current_thread().name, e)
        finally:
            self._done.set()

    def result(self, deadline: float, default: Any) -> Any:
        self._done.wait(max(0.0, deadline - time.monotonic()))
        return default if self._value is self._UNSET else self._value


class _Browser(zeroconf.ServiceListener):
    """Collects what the mDNS browse resolves, per host, off the browser's thread."""

    def __init__(self, zc: Any) -> None:
        self._zc = zc
        self._lock = threading.Lock()
        self.hosts: dict[str, dict[str, tuple[str, int]]] = {}

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        # A resolve still running when the browse ends is dropped, never waited for.
        threading.Thread(target=self._resolve, args=(type_, name), daemon=True).start()

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    def _resolve(self, type_: str, name: str) -> None:
        try:
            info = self._zc.get_service_info(type_, name, timeout=RESOLVE_TIMEOUT_MS)
            ip = _pick_ip(info.parsed_addresses()) if info else None
            if ip is None:
                return
            with self._lock:
                host = self.hosts.setdefault(ip, {})
                host[type_] = (name.removesuffix("." + type_), info.port)
        except Exception as e:
            log.debug("Could not resolve %s: %s", name, e)


def _pick_ip(addresses: list[str]) -> str | None:
    v4 = [a for a in addresses if ":" not in a]
    if v4:
        return v4[0]
    v6 = [a.partition("%")[0] for a in addresses if not a.lower().startswith("fe80")]
    return v6[0] if v6 else None


def _lan_address(ip: str, services: dict[str, tuple[str, int]]) -> Found | None:
    """The best address for one host: raw socket, else HiSLIP, else VXI-11."""
    if SERVICE_RAW in services:
        name, port = services[SERVICE_RAW]
        return _candidate(TcpAddress(ip, port or RAW_PORT), "mdns", name)
    visa_ok = ":" not in ip
    if SERVICE_HISLIP in services and visa_ok:
        resource = f"TCPIP::{ip}::hislip0::INSTR"
        return _candidate(VisaAddress(resource), "mdns", services[SERVICE_HISLIP][0])
    if SERVICE_VXI11 in services and visa_ok:
        resource = f"TCPIP::{ip}::inst0::INSTR"
        return _candidate(VisaAddress(resource), "mdns", services[SERVICE_VXI11][0])
    if SERVICE_LXI in services:
        return _candidate(TcpAddress(ip, RAW_PORT), "mdns", services[SERVICE_LXI][0])
    return None


def _browse(budget_s: float) -> dict[str, Found]:
    try:
        zc = zeroconf.Zeroconf()
    except Exception as e:
        _warn_once("zeroconf", "mDNS is unavailable, listing only VXI-11 instruments: %s", e)
        return {}
    try:
        listener = _Browser(zc)
        browser = zeroconf.ServiceBrowser(zc, SERVICE_TYPES, listener=listener)
        threading.Event().wait(budget_s)
        browser.cancel()
        with listener._lock:
            hosts = dict(listener.hosts)
        found = {ip: _lan_address(ip, services) for ip, services in hosts.items()}
        return {ip: f for ip, f in found.items() if f is not None}
    except Exception as e:
        log.warning("mDNS browse failed: %s", e)
        return {}
    finally:
        try:
            zc.close()
        except Exception as e:
            log.debug("Closing mDNS failed: %s", e)


def _vxi11() -> dict[str, Found]:
    found: dict[str, Found] = {}
    try:
        # The broadcast alone: a resource manager's listing also walks USB and browses mDNS again.
        for resource in TCPIPInstrVxi11.list_resources():
            parts = resource.split("::")
            if len(parts) > 1:
                found.setdefault(parts[1], _candidate(VisaAddress(resource), "vxi11", resource))
    except Exception as e:
        log.warning("VXI-11 broadcast failed: %s", e)
    return found


# Raw-socket ports the shipped profiles use, probed on every local subnet.
SCAN_PORTS = (5025, 5555, 2268, 3490)
# The largest subnet the scan covers; a /22 at 0.3 s per host, 512 at a time, takes about 3 s.
SCAN_MAX_HOSTS = 1024
_SCAN_TIMEOUT_S = 0.3
# Handshakes in flight at once; one batch covers a /22 at every port in one timeout.
_SCAN_PARALLEL = 2048


def local_subnets() -> list[ipaddress.IPv4Network]:
    """The IPv4 subnets this machine sits on, loopback and link-local left out."""
    subnets: list[ipaddress.IPv4Network] = []
    try:
        for addresses in psutil.net_if_addrs().values():
            for entry in addresses:
                if entry.family != socket.AF_INET or not entry.netmask:
                    continue
                network = ipaddress.ip_network(f"{entry.address}/{entry.netmask}", strict=False)
                if not isinstance(network, ipaddress.IPv4Network):
                    continue
                if network.is_loopback or network.is_link_local or network.prefixlen >= 32:
                    continue
                if network not in subnets:
                    subnets.append(network)
    except Exception as e:
        log.warning("cannot list the network interfaces: %s", e)
    return subnets


def _scan_parallel() -> int:
    """Handshakes in flight at once, within the process's open-file limit where there is one."""
    if sys.platform == "win32":
        return _SCAN_PARALLEL
    try:
        import resource

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return _SCAN_PARALLEL
    # Leave room for the links, the log and the agent's own files.
    return max(16, min(_SCAN_PARALLEL, soft - 64))


async def _open_ports(targets: list[tuple[str, int]], deadline: float) -> list[tuple[str, int]]:
    """The targets that complete a TCP handshake within the scan timeout, a batch at a time; the
    batches stop at the deadline."""
    loop = asyncio.get_running_loop()
    hits: list[tuple[str, int]] = []
    batch = _scan_parallel()
    for start in range(0, len(targets), batch):
        if time.monotonic() >= deadline:
            break
        # One timer per batch: a timeout per probe costs a quarter more CPU, shared with recording.
        probes: dict[asyncio.Future[None], tuple[str, int, socket.socket]] = {}
        for ip, port in targets[start : start + batch]:
            sock = socket.socket()
            sock.setblocking(False)
            probes[asyncio.ensure_future(loop.sock_connect(sock, (ip, port)))] = (ip, port, sock)
        wait_s = min(_SCAN_TIMEOUT_S, deadline - time.monotonic())
        done, pending = await asyncio.wait(probes, timeout=max(0.0, wait_s))
        for probe in pending:
            probe.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for probe, (ip, port, sock) in probes.items():
            if probe in done and probe.exception() is None:
                hits.append((ip, port))
            sock.close()
    return hits


def scan_lan(
    subnets: list[ipaddress.IPv4Network] | None = None, deadline_s: float = 10.0
) -> list[Found]:
    """Hosts on the local subnets that accept a connection on a SCPI port, one per host."""
    deadline = time.monotonic() + deadline_s
    return _scan_hosts(_scan_open(local_subnets() if subnets is None else subnets, deadline))


def _scanned(subnets: list[ipaddress.IPv4Network]) -> list[ipaddress.IPv4Network]:
    return [n for n in subnets if n.num_addresses <= SCAN_MAX_HOSTS]


def _scan_open(subnets: list[ipaddress.IPv4Network], deadline: float) -> set[tuple[str, int]]:
    targets = [
        (str(host), port) for n in _scanned(subnets) for host in n.hosts() for port in SCAN_PORTS
    ]
    return set(asyncio.run(_open_ports(targets, deadline))) if targets else set()


def _scan_hosts(open_ports: set[tuple[str, int]]) -> list[Found]:
    found: dict[str, Found] = {}
    for ip, port in sorted(open_ports, key=lambda t: (SCAN_PORTS.index(t[1]), t[0])):
        if ip not in found:
            found[ip] = _candidate(TcpAddress(ip, port), "scan", f"{ip}:{port}")
    return list(found.values())


def _drop_closed_sockets(
    found: dict[str, Found], subnets: list[ipaddress.IPv4Network], open_ports: set[tuple[str, int]]
) -> None:
    """Drop a raw socket the scan found closed when its host has another address: a held socket
    only waits out the connect timeout, and the other address reaches the instrument."""
    scanned = _scanned(subnets)
    for key, candidate in list(found.items()):
        address = candidate.address
        if not isinstance(address, TcpAddress) or address.port not in SCAN_PORTS:
            continue
        if (address.host, address.port) in open_ports:
            continue
        try:
            ip = ipaddress.ip_address(address.host)
        except ValueError:
            continue
        if not any(ip in n for n in scanned):
            continue
        if any(k != key and host_of(c.address) == address.host for k, c in found.items()):
            del found[key]


def list_lan(
    budget_s: float = 2.0, tried: list[str] | None = None, deadline_s: float = 10.0
) -> list[Found]:
    """Instruments on the LAN, one per host: mDNS for `budget_s`, a VXI-11 broadcast, and a
    connection probe of every local subnet's SCPI ports, all ending by `deadline_s`. `tried`
    collects what ran."""
    deadline = time.monotonic() + deadline_s
    try:
        subnets = local_subnets()
        broadcast = _Background(_vxi11, "VXI-11 broadcast")
        scan = _Background(lambda: _scan_open(subnets, deadline), "port scan")
        found = _browse(max(0.0, min(budget_s, deadline - time.monotonic())))
        vxi11: dict[str, Found] | None = broadcast.result(deadline, None)
        for ip, candidate in (vxi11 or {}).items():
            # Keep both: `discover` keeps whichever identifies, and a held socket does not.
            if ip in found and not isinstance(found[ip].address, VisaAddress):
                found[f"{ip} vxi11"] = candidate
            else:
                found.setdefault(ip, candidate)
        open_ports: set[tuple[str, int]] | None = scan.result(deadline, None)
        for candidate in _scan_hosts(open_ports or set()):
            ip = candidate.address.where.rsplit(":", 1)[0]
            # Keep both addresses: `discover` picks the one that identifies.
            if ip not in found:
                found[ip] = candidate
            elif found[ip].source == "vxi11":
                found[f"{ip} socket"] = candidate
        if open_ports is not None:
            _drop_closed_sockets(found, subnets, open_ports)
        if tried is not None:
            late = " (cut short)"
            tried.append(f"mDNS for {budget_s:g} s")
            tried.append("VXI-11 broadcast" + ("" if vxi11 is not None else late))
            scanned = [str(n) for n in _scanned(subnets)]
            ports = ", ".join(str(p) for p in SCAN_PORTS)
            tried.append(
                f"ports {ports} on {', '.join(scanned)}" + ("" if open_ports is not None else late)
                if scanned
                else "no local subnet to scan"
            )
        return list(found.values())
    except Exception as e:
        log.warning("LAN listing failed: %s", e)
        return []


def _usb_serial(dev: Any) -> str | None:
    try:
        return dev.serial_number or None
    except Exception as e:
        log.info(
            "Skipping USB device %04x:%04x, its serial cannot be read: %s",
            dev.idVendor,
            dev.idProduct,
            e,
        )
        return None


def _is_usbtmc(dev: Any) -> bool:
    return any(
        intf.bInterfaceClass == USBTMC_CLASS and intf.bInterfaceSubClass == USBTMC_SUBCLASS
        for cfg in dev
        for intf in cfg
    )


def _libusb_devices() -> list[Found]:
    found: list[Found] = []
    backend = libusb_package.get_libusb1_backend()
    devices: Any = usb.core.find(find_all=True, backend=backend)
    for dev in devices or ():
        try:
            if not _is_usbtmc(dev):
                continue
            serial = _usb_serial(dev)
            vid, pid = dev.idVendor, dev.idProduct
            try:
                product = dev.product
            except Exception:
                product = None
            resource = f"USB0::0x{vid:04X}::0x{pid:04X}::{serial or '?'}::INSTR"
            detail = product or f"{vid:04X}:{pid:04X}"
            candidate = _candidate(VisaAddress(resource), "usb", detail)
            if serial is None:
                # Unreadable descriptors mean a driver or a permission no open gets past either.
                candidate = dataclasses.replace(candidate, error=USB_UNREADABLE)
            found.append(candidate)
        except Exception as e:
            log.info("Skipping a USB device that cannot be read: %s", e)
    return found


def _system_visa_devices() -> list[Found]:
    try:
        rm = pyvisa.ResourceManager("@ivi")
        return [
            _candidate(VisaAddress(resource), "usb", resource)
            for resource in rm.list_resources("USB?*INSTR")
        ]
    except Exception as e:
        log.debug("No system VISA for USB: %s", e)
        return []


def list_usb() -> list[Found]:
    """USB-TMC instruments, through libusb and, when that finds none, a system VISA."""
    try:
        found = _libusb_devices()
    except Exception as e:
        log.warning("USB listing failed: %s", e)
        found = []
    if any(f.error is None for f in found):
        return found
    # Nothing readable through libusb: a system VISA may hold the driver that can.
    return found + _system_visa_devices()


def list_serial_candidates() -> list[Found]:
    """Serial ports of instrument makers; a generic USB-serial bridge is never a candidate."""
    try:
        found: list[Found] = []
        for port in serialx.list_serial_ports():
            if port.vid not in INSTRUMENT_VENDORS:
                continue
            detail = f"{port.device} ({port.product})" if port.product else port.device
            address = SerialAddress(port.device, baud=SERIAL_BAUDS[0])
            found.append(_candidate(address, "serial", detail))
        return found
    except Exception as e:
        log.warning("Serial listing failed: %s", e)
        return []


class _Attempt:
    """One `*IDN?` exchange on a thread, abandoned when it outlives its deadline."""

    def __init__(self, address: Address, timeout_s: float) -> None:
        self._address = address
        # A device clear resets what another client is asking: a read-buffer flush is enough.
        self._settings = LinkSettings(
            write_terminator="\n", read_terminator="\n", timeout_s=timeout_s, device_clear=False
        )
        self._lock = threading.Lock()
        self._link: Link | None = None
        self._abandoned = False
        self._done = threading.Event()
        self._reply = ""
        self._error: LinkError | None = None

    def _close(self, link: Link) -> None:
        try:
            link.close()
        except Exception as e:
            log.debug("Closing %s failed: %s", self._address.where, e)

    def _run(self) -> None:
        link = None
        try:
            link = open_link(self._address, self._settings)
            with self._lock:
                if self._abandoned:
                    return
                self._link = link
            self._reply = connect.ask_identity(link, "*IDN?")
        except LinkError as e:
            self._error = e
        except Exception as e:
            self._error = LinkError(str(e) or type(e).__name__)
        finally:
            if link is not None:
                self._close(link)
            self._done.set()

    def run(self, deadline: float) -> str:
        threading.Thread(target=self._run, name="identify", daemon=True).start()
        # The connect, the query and the second query it may need, and never past the deadline.
        budget_s = min(
            CONNECT_TIMEOUT_S + 2 * self._settings.timeout_s, deadline - time.monotonic()
        )
        started = time.monotonic()
        if not self._done.wait(max(0.0, budget_s)):
            with self._lock:
                self._abandoned = True
                link = self._link
            if link is not None:
                # Closing a VISA session can take seconds; nobody waits for it.
                threading.Thread(target=self._close, args=(link,), daemon=True).start()
            waited = time.monotonic() - started
            raise LinkTimeout(f"did not answer *IDN? within {waited:.1f} s")
        if self._error is not None:
            raise self._error
        return self._reply


def _identify_at(address: Address, timeout_s: float, deadline: float) -> Idn:
    reply = _Attempt(address, timeout_s).run(deadline)
    if not is_identity(reply):
        raise LinkError(NOT_AN_INSTRUMENT)
    return parse_idn(reply)


def identify(
    found: Found, profiles: list[Profile], timeout_s: float = 2.0, deadline_s: float = 10.0
) -> Found:
    """Ask the instrument `*IDN?` and fill in its identity and matching profile, by the deadline.

    A serial port is tried at 9600 then 115200 baud; the result carries the baud that answered.
    """
    return _identify(found, profiles, timeout_s, time.monotonic() + deadline_s)


def _identify(found: Found, profiles: list[Profile], timeout_s: float, deadline: float) -> Found:
    address = found.address
    if isinstance(address, SerialAddress):
        candidates: list[Address] = [dataclasses.replace(address, baud=b) for b in SERIAL_BAUDS]
    else:
        candidates = [address]
    error: LinkError | None = None
    for candidate in candidates:
        if time.monotonic() >= deadline:
            return dataclasses.replace(found, error=OUT_OF_TIME)
        try:
            idn = _identify_at(candidate, timeout_s, deadline)
        except LinkError as e:
            error = e
            if e.kind not in ("timeout", "other"):
                break
            continue
        profile = find(profiles, idn)
        return dataclasses.replace(
            found,
            address=candidate,
            idn=idn,
            profile_id=profile.id if profile else None,
            category=profile.category if profile else None,
            error=None if profile else f"no profile matches {idn.manufacturer} {idn.model}",
        )
    return dataclasses.replace(found, error=str(error))


def _identify_safely(
    found: Found, profiles: list[Profile], timeout_s: float, deadline: float
) -> Found:
    try:
        return _identify(found, profiles, timeout_s, deadline)
    except Exception as e:
        return dataclasses.replace(found, error=str(e) or type(e).__name__)


def _identify_host(
    candidates: list[Found], profiles: list[Profile], timeout_s: float, deadline: float
) -> list[Found]:
    """One host's addresses, one after another until one identifies: two clients at once on an
    instrument's single parser garble each other's replies."""
    results: list[Found] = []
    for candidate in candidates:
        if any(r.idn is not None and _same_instrument(r, candidate) for r in results):
            continue
        results.append(_identify_safely(candidate, profiles, timeout_s, deadline))
    return results


@dataclass(frozen=True, slots=True)
class _Search:
    """What one discovery looks at."""

    listings: frozenset[str]
    budget_s: float
    identify_timeout_s: float
    skip: frozenset[str]
    prefer: Literal["socket", "visa"]

    def covers(self, other: "_Search") -> bool:
        return (
            self.listings >= other.listings
            and self.budget_s >= other.budget_s
            and self.identify_timeout_s >= other.identify_timeout_s
            and self.skip == other.skip
            and self.prefer == other.prefer
        )


@dataclass(frozen=True, slots=True)
class _Searched:
    search: _Search
    finished: float
    # (listing, found) and (listing, line), so a narrower search takes only its own.
    found: list[tuple[str, Found]]
    tried: list[tuple[str, str]]

    def answer(self, asked: _Search) -> tuple[list[Found], list[str]]:
        found = [f for listing, f in self.found if listing in asked.listings]
        return found, [line for listing, line in self.tried if listing in asked.listings]


class _OneAtATime:
    """Discovery runs one at a time; a caller that waited through a search covering its own
    takes that result. Two at once would scan every subnet twice and put two clients on a
    single-client socket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: _Searched | None = None

    def run(
        self,
        asked: _Search,
        search: Callable[[list[tuple[str, str]]], list[tuple[str, Found]]],
        deadline: float,
    ) -> tuple[list[Found], list[str]]:
        arrived = time.perf_counter()
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return [], ["nothing: another search was still running"]
        try:
            last = self._last
            if last is not None and last.finished > arrived and last.search.covers(asked):
                return last.answer(asked)
            tried: list[tuple[str, str]] = []
            found = search(tried)
            self._last = _Searched(asked, time.perf_counter(), found, tried)
            return self._last.answer(asked)
        finally:
            self._lock.release()


_searches = _OneAtATime()


def discover(
    profiles: list[Profile],
    *,
    lan: bool = True,
    usb: bool = True,
    serial: bool = False,
    budget_s: float = 2.0,
    identify_timeout_s: float = 2.0,
    deadline_s: float = 10.0,
    skip: set[str] | None = None,
    tried: list[str] | None = None,
    prefer: Literal["socket", "visa"] = "socket",
) -> list[Found]:
    """Every instrument the chosen listings find by `deadline_s`, identified, one per host,
    sorted by source then detail.

    A host reached two ways keeps the address that identified, and among those the `prefer`
    kind. Addresses in `skip` are listed but not opened: a running worker holds them. `tried`
    collects one line per search that ran, for a message that says what was looked at.
    """
    chosen = {name for name, on in (("lan", lan), ("usb", usb), ("serial", serial)) if on}
    if not chosen:
        return []
    deadline = time.monotonic() + deadline_s
    asked = _Search(frozenset(chosen), budget_s, identify_timeout_s, frozenset(skip or ()), prefer)
    found, lines = _searches.run(asked, lambda t: _search(profiles, asked, t, deadline), deadline)
    if tried is not None:
        tried.extend(lines)
    return sorted(_one_per_host(found, prefer), key=lambda f: (f.source, f.detail))


def _search(
    profiles: list[Profile], asked: _Search, tried: list[tuple[str, str]], deadline: float
) -> list[tuple[str, Found]]:
    """Every candidate the asked listings find, with its listing, identified unless held."""
    lan_lines: list[str] = []
    listings: dict[str, _Background] = {}
    if "lan" in asked.listings:
        listings["lan"] = _Background(
            lambda: list_lan(asked.budget_s, lan_lines, deadline - time.monotonic()), "LAN listing"
        )
    if "usb" in asked.listings:
        listings["usb"] = _Background(list_usb, "USB listing")
    if "serial" in asked.listings:
        listings["serial"] = _Background(list_serial_candidates, "serial listing")
    candidates: list[tuple[str, Found]] = []
    for name, listing in listings.items():
        batch: list[Found] | None = listing.result(deadline, None)
        if batch is None:
            tried.append((name, f"the {_LISTING_NAMES[name]} (cut short)"))
        candidates += [(name, f) for f in batch or ()]
    tried.extend(("lan", line) for line in list(lan_lines))
    if "usb" in asked.listings:
        tried.append(("usb", "USB-TMC devices"))
    if "serial" in asked.listings:
        tried.append(("serial", "serial ports of known instrument vendors"))
    # A worker holds an instrument: its other address and its host name's address are held too.
    held_hosts = {host_of_where(where) for where in asked.skip}
    held_hosts |= resolve(held_hosts, min(1.0, max(0.0, deadline - time.monotonic())))

    def is_held(f: Found) -> bool:
        return f.address.where in asked.skip or host_of(f.address) in held_hosts

    rest = [
        (n, dataclasses.replace(f, error=IN_USE) if is_held(f) else f)
        for n, f in candidates
        if is_held(f) or f.error is not None
    ]
    hosts: dict[str, list[tuple[str, Found]]] = {}
    for n, f in candidates:
        if not is_held(f) and f.error is None:
            hosts.setdefault(host_of(f.address), []).append((n, f))
    jobs: dict[str, tuple[list[tuple[str, Found]], _Background]] = {}
    for host, group in hosts.items():
        # The preferred kind of address first: the first that identifies ends the host's turn.
        group.sort(
            key=lambda nf: isinstance(nf[1].address, TcpAddress) != (asked.prefer == "socket")
        )
        addresses = [f for _, f in group]
        jobs[host] = (
            group,
            _Background(
                lambda a=addresses: _identify_host(a, profiles, asked.identify_timeout_s, deadline),
                f"identify {host}",
            ),
        )
    identified: list[tuple[str, Found]] = []
    for group, job in jobs.values():
        results: list[Found] | None = job.result(deadline, None)
        if results is None:
            identified += [(n, dataclasses.replace(f, error=OUT_OF_TIME)) for n, f in group]
        else:
            listing = {f.address.where: n for n, f in group}
            identified += [(listing.get(r.address.where, group[0][0]), r) for r in results]
    return identified + rest


_LISTING_NAMES = {"lan": "LAN search", "usb": "USB search", "serial": "serial port search"}


def host_of_where(where: str) -> str:
    """The host of a worker's `where`: `host:port`, `[v6]:port`, or a TCPIP VISA resource."""
    if "::" in where:
        parts = where.split("::")
        return parts[1] if len(parts) > 1 and parts[0].upper().startswith("TCPIP") else where
    host = where.rsplit(":", 1)[0] if ":" in where else where
    return host.strip("[]")


def host_of(address: Address) -> str:
    """What two addresses of one instrument share: the host of a LAN address, else the whole."""
    if isinstance(address, TcpAddress):
        return address.host
    if isinstance(address, VisaAddress):
        parts = address.resource.split("::")
        if len(parts) > 1 and parts[0].upper().startswith("TCPIP"):
            return parts[1]
    return address.where


def _same_instrument(a: Found, b: Found) -> bool:
    """A host reached by raw socket and by VXI-11 is one instrument; two sockets are two."""
    return host_of(a.address) == host_of(b.address) and not (
        isinstance(a.address, TcpAddress) and isinstance(b.address, TcpAddress)
    )


def _one_per_host(found: list[Found], prefer: str) -> list[Found]:
    """A host reached by raw socket and by VXI-11 is one instrument; two sockets are two."""

    def rank(f: Found) -> tuple[bool, bool, bool, str]:
        wanted = isinstance(f.address, TcpAddress) == (prefer == "socket")
        # A held instrument is listed as held, never as free under its other address.
        return (f.error != IN_USE, f.idn is None, not wanted, f.source)

    chosen: list[Found] = []
    for f in sorted(found, key=rank):
        if not any(_same_instrument(f, c) for c in chosen):
            chosen.append(f)
    return chosen


def default_name(found: Found) -> str:
    idn = found.idn
    raw = idn.model if idn is not None and idn.model else found.detail
    # The config's Name takes ASCII only: a model such as Prüfgerät is saved as Prufgerat.
    return naming.safe_name(naming.ascii_name(raw) or "instrument", naming.NAME_BYTES)


def resolve(hosts: set[str], deadline_s: float = 1.0) -> set[str]:
    """The addresses the host names among `hosts` resolve to by the deadline, so a name typed
    in the form matches the address a search finds."""
    deadline = time.monotonic() + deadline_s
    jobs = [
        _Background(lambda n=name: {str(i[4][0]) for i in socket.getaddrinfo(n, None)}, name)
        for name in hosts
        if _is_host_name(name)
    ]
    resolved: set[str] = set()
    for job in jobs:
        resolved |= job.result(deadline, set())
    return resolved


def _is_host_name(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        # Not an address, a VISA resource or a serial port.
        return "::" not in text and "/" not in text and not text.upper().startswith("COM")
    return False


@dataclass(frozen=True)
class SerialPortChoice:
    """One serial port for the Port field, instrument or not."""

    choice: str
    label: str
    detail: str | None


def list_all_serial_ports() -> list[SerialPortChoice]:
    """Every serial port on this machine, for the Port field; never raises."""
    try:
        ports = list(serialx.list_serial_ports())
    except Exception as e:
        log.warning("cannot list serial ports: %s", e)
        return []
    choices: list[SerialPortChoice] = []
    for p in sorted(ports, key=lambda p: p.device):
        usb = f"{p.vid:04x}:{p.pid:04x}" if p.vid is not None and p.pid is not None else None
        label = f"{p.product} ({p.device})" if p.product else p.device
        detail = " ".join(filter(None, (p.manufacturer, usb, p.serial_number))) or None
        choices.append(SerialPortChoice(p.device, label, detail))
    return choices


def config_entry(found: Found) -> dict[str, Any]:
    """The config entry for a found instrument, without a name."""
    profile = found.profile_id or "auto"
    address = found.address
    if isinstance(address, TcpAddress):
        return {
            "connection": "lan",
            "host": address.host,
            "port": address.port,
            "profile": profile,
        }
    if isinstance(address, SerialAddress):
        return {
            "connection": "serial",
            "port": address.port,
            "baud": address.baud,
            "profile": profile,
        }
    connection = "usb" if address.resource.upper().startswith("USB") else "visa"
    return {"connection": connection, "resource": address.resource, "profile": profile}
