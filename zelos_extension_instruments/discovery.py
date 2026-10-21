"""Finds the instruments on this machine's network and ports and names the ones it knows.

Listing never opens an instrument and identifying never raises: a failure becomes `Found.error`.
Libraries do the protocol work: zeroconf (mDNS), pyvisa-py (VXI-11 portmapper broadcast), pyusb
with libusb-package (USB-TMC) and serialx (serial ports).
"""

import dataclasses
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import libusb_package
import pyvisa
import serialx
import usb.core
import zeroconf

from . import naming
from .link import (
    Address,
    Link,
    LinkError,
    LinkTimeout,
    SerialAddress,
    Settings,
    TcpAddress,
    VisaAddress,
    open_link,
)
from .profile import Idn, Profile, find, parse_idn

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
NOT_AN_INSTRUMENT = "did not answer *IDN? as an instrument"
# An attempt that outlives this multiple of its timeout is abandoned, whatever the link is doing.
DEADLINE_FACTOR = 1.5
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


class _Browser(zeroconf.ServiceListener):
    """Collects what the mDNS browse resolves, per host, off the browser's thread."""

    def __init__(self, zc: Any, pool: ThreadPoolExecutor) -> None:
        self._zc = zc
        self._pool = pool
        self._lock = threading.Lock()
        self.hosts: dict[str, dict[str, tuple[str, int]]] = {}

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        self._pool.submit(self._resolve, type_, name)

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
        with ThreadPoolExecutor(max_workers=8) as pool:
            listener = _Browser(zc, pool)
            browser = zeroconf.ServiceBrowser(zc, SERVICE_TYPES, listener=listener)
            threading.Event().wait(budget_s)
            browser.cancel()
        found = {ip: _lan_address(ip, services) for ip, services in listener.hosts.items()}
        return {ip: f for ip, f in found.items() if f is not None}
    except Exception as e:
        log.warning("mDNS browse failed: %s", e)
        return {}
    finally:
        try:
            zc.close()
        except Exception as e:
            log.debug("Closing mDNS failed: %s", e)


def _close_manager(rm: Any) -> None:
    try:
        rm.close()
    except Exception as e:
        log.debug("Closing the VISA manager failed: %s", e)


def _vxi11() -> dict[str, Found]:
    found: dict[str, Found] = {}
    rm = None
    try:
        rm = pyvisa.ResourceManager("@py")
        for resource in rm.list_resources("TCPIP?*INSTR"):
            parts = resource.split("::")
            if len(parts) > 1:
                found.setdefault(parts[1], _candidate(VisaAddress(resource), "vxi11", resource))
    except Exception as e:
        log.warning("VXI-11 broadcast failed: %s", e)
    finally:
        if rm is not None:
            _close_manager(rm)
    return found


def list_lan(budget_s: float = 2.0) -> list[Found]:
    """Instruments on the LAN, one per host: mDNS for `budget_s`, plus a VXI-11 broadcast."""
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            broadcast = pool.submit(_vxi11)
            found = _browse(budget_s)
            for ip, candidate in broadcast.result().items():
                found.setdefault(ip, candidate)
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
            if serial is None:
                continue
            vid, pid = dev.idVendor, dev.idProduct
            try:
                product = dev.product
            except Exception:
                product = None
            resource = f"USB0::0x{vid:04X}::0x{pid:04X}::{serial}::INSTR"
            detail = product or f"{vid:04X}:{pid:04X}"
            found.append(_candidate(VisaAddress(resource), "usb", detail))
        except Exception as e:
            log.info("Skipping a USB device that cannot be read: %s", e)
    return found


def _system_visa_devices() -> list[Found]:
    rm = None
    try:
        rm = pyvisa.ResourceManager("@ivi")
        return [
            _candidate(VisaAddress(resource), "usb", resource)
            for resource in rm.list_resources("USB?*INSTR")
        ]
    except Exception as e:
        log.debug("No system VISA for USB: %s", e)
        return []
    finally:
        if rm is not None:
            _close_manager(rm)


def list_usb() -> list[Found]:
    """USB-TMC instruments, through libusb and, when that finds none, a system VISA."""
    try:
        found = _libusb_devices()
    except Exception as e:
        log.warning("USB listing failed: %s", e)
        found = []
    return found or _system_visa_devices()


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
        self._settings = Settings(write_terminator="\n", read_terminator="\n", timeout_s=timeout_s)
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
            self._reply = link.query("*IDN?")
        except LinkError as e:
            self._error = e
        except Exception as e:
            self._error = LinkError(str(e) or type(e).__name__)
        finally:
            if link is not None:
                self._close(link)
            self._done.set()

    def run(self) -> str:
        threading.Thread(target=self._run, name="identify", daemon=True).start()
        timeout_s = self._settings.timeout_s
        if not self._done.wait(timeout_s * DEADLINE_FACTOR):
            with self._lock:
                self._abandoned = True
                link = self._link
            if link is not None:
                self._close(link)
            raise LinkTimeout(f"did not answer *IDN? within {timeout_s:g} s")
        if self._error is not None:
            raise self._error
        return self._reply


def _identify_at(address: Address, timeout_s: float) -> Idn:
    reply = _Attempt(address, timeout_s).run()
    if "," not in reply:
        raise LinkError(NOT_AN_INSTRUMENT)
    return parse_idn(reply)


def identify(found: Found, profiles: list[Profile], timeout_s: float = 2.0) -> Found:
    """Ask the instrument `*IDN?` and fill in its identity and matching profile.

    A serial port is tried at 9600 then 115200 baud; the result carries the baud that answered.
    """
    address = found.address
    if isinstance(address, SerialAddress):
        candidates: list[Address] = [dataclasses.replace(address, baud=b) for b in SERIAL_BAUDS]
    else:
        candidates = [address]
    error: LinkError | None = None
    for candidate in candidates:
        try:
            idn = _identify_at(candidate, timeout_s)
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


def _identify_safely(found: Found, profiles: list[Profile], timeout_s: float) -> Found:
    try:
        return identify(found, profiles, timeout_s)
    except Exception as e:
        return dataclasses.replace(found, error=str(e) or type(e).__name__)


def discover(
    profiles: list[Profile],
    *,
    lan: bool = True,
    usb: bool = True,
    serial: bool = False,
    budget_s: float = 2.0,
    identify_timeout_s: float = 2.0,
    skip: set[str] | None = None,
) -> list[Found]:
    """Every candidate the chosen listings find, identified, sorted by source then detail.

    Addresses in `skip` are listed but not opened: a running worker holds them.
    """
    listings: list[Callable[[], list[Found]]] = []
    if lan:
        listings.append(lambda: list_lan(budget_s))
    if usb:
        listings.append(list_usb)
    if serial:
        listings.append(list_serial_candidates)
    if not listings:
        return []
    skip = skip or set()
    with ThreadPoolExecutor(max_workers=len(listings)) as pool:
        candidates = [f for batch in pool.map(lambda listing: listing(), listings) for f in batch]
    free = [f for f in candidates if f.address.where not in skip]
    held = [dataclasses.replace(f, error=IN_USE) for f in candidates if f.address.where in skip]
    with ThreadPoolExecutor(max_workers=8) as pool:
        identified = list(
            pool.map(lambda f: _identify_safely(f, profiles, identify_timeout_s), free)
        )
    return sorted(identified + held, key=lambda f: (f.source, f.detail))


def default_name(found: Found) -> str:
    """`<model>_<serial>` as a trace-safe name, else the list detail."""
    idn = found.idn
    if idn is not None and idn.model:
        raw = f"{idn.model}_{idn.serial}" if idn.serial else idn.model
        return naming.safe_name(raw, naming.NAME_BYTES)
    return naming.safe_name(found.detail, naming.NAME_BYTES)


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
