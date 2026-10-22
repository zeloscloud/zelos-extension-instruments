"""What the Action panel, notebooks and Zelos AI can do with the instruments."""

import logging
from typing import Any

from zelos_sdk.actions import action

# Imported to register their actions.
from . import actions_generator, actions_scope  # noqa: F401
from .actions_common import TIMEOUT_S, ask, with_channel, with_instrument, workers
from .link import Address, TcpAddress
from .naming import NAME_BYTES, unique_name
from .profile import CURRENT_RANGE_LABELS, FUNCTION_LABELS, TRACKING_LABELS
from .profile_load import shipped
from .worker import InstrumentWorker

logger = logging.getLogger(__name__)

_SCPI_TIMEOUT_S = 35.0
_ACQUIRE_TIMEOUT_S = 15.0
# A recall that settles slower than this reports a timeout, but the instrument still finishes it.
_SYSTEM_TIMEOUT_S = 30.0
_SELF_TEST_TIMEOUT_S = 120.0
_SCREENSHOT_TIMEOUT_S = 60.0
# The app gives up on a list after 10 s and on Auto-configure after 30 s; a search ends well inside.
_LIST_TIMEOUT_S = 10
_AUTOCONFIG_TIMEOUT_S = 30
_LIST_SEARCH_S = 5.0
_AUTOCONFIG_SEARCH_S = 15.0
_MIN_SCPI_S, _MAX_SCPI_S = 0.1, 30.0
_FUNCTIONS = ("cc", "cv", "cp", "cr", *FUNCTION_LABELS[:-1])


@action(
    "List Instruments",
    "Instruments reachable from this machine, as choices for an address field.",
    timeout=_LIST_TIMEOUT_S,
    standalone=True,
    read_only=True,
)
@action.select(
    "connection",
    title="Connection",
    choices=["lan", "usb", "serial", "visa"],
    required=False,
    default="lan",
)
def list_instruments(connection: str = "lan") -> dict[str, Any]:
    from . import discovery

    if connection == "serial":
        choices = [
            {"value": p.choice, "label": p.label, "detail": p.detail}
            for p in discovery.list_all_serial_ports()
        ]
        if not choices:
            return {
                "status": "success",
                "choices": [],
                "message": "No serial ports found. Type a path such as /dev/ttyUSB0 or COM7.",
            }
        return {"status": "success", "choices": choices}
    held = {w.where for w in workers.values()}
    tried: list[str] = []
    found = discovery.discover(
        shipped(),
        lan=connection in ("lan", "visa"),
        usb=connection == "usb",
        serial=False,
        budget_s=1.0,
        identify_timeout_s=2.0,
        deadline_s=_LIST_SEARCH_S,
        skip=held,
        tried=tried,
        prefer="visa" if connection == "visa" else "socket",
    )
    choices = []
    visa_only: list[str] = []
    for f in found:
        holder = _holder(f.address)
        idn = f.idn or (holder.idn if holder else None)
        who = f"{idn.manufacturer} {idn.model}".strip() if idn else ""
        label = f"{who} ({f.address.where})" if who else f.address.where
        if connection == "visa":
            value = _visa_value(f.address)
        elif isinstance(f.address, TcpAddress):
            value = _lan_value(f.address)
        elif connection == "lan":
            # A resource string is not a host: the instrument belongs under VISA resource.
            visa_only.append(label)
            continue
        else:
            value = f.address.where
        detail = f.error or (f.profile_id and f"profile {f.profile_id}") or None
        choices.append({"value": value, "label": label, "detail": detail})
    result: dict[str, Any] = {"status": "success", "choices": choices}
    if visa_only:
        result["message"] = (
            f"Reachable only as a VISA resource: {', '.join(visa_only)}. "
            "Set Connection to VISA resource to use it."
        )
    elif not choices:
        result["message"] = _nothing_found(tried)
    return result


@action(
    "List Profiles",
    "The shipped profiles, as choices for the Profile field.",
    timeout=_LIST_TIMEOUT_S,
    standalone=True,
    read_only=True,
)
def list_profiles() -> dict[str, Any]:
    choices: list[dict[str, Any]] = [
        {"value": "auto", "label": "Automatic", "detail": "From the instrument's identity"}
    ]
    for p in shipped():
        choices.append(
            {
                "value": p.id,
                "label": p.name,
                "detail": f"{p.category}, {p.evidence.badge}",
            }
        )
    return {"status": "success", "choices": choices}


@action(
    "Auto-configure",
    "Keep the configured instruments and add one for each instrument found on the network, "
    "on USB and on the serial ports of known instrument vendors, or a demo bench when there "
    "are none.",
    timeout=_AUTOCONFIG_TIMEOUT_S,
    standalone=True,
    read_only=True,
)
@action.object("config", title="Configuration", properties={}, required=False)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    from . import discovery

    instruments: list[Any] = list((config or {}).get("instruments") or [])
    known: set[str] = set()
    for item in instruments:
        if isinstance(item, dict):
            known |= _hosts(item)
    known |= discovery.resolve(known)
    names = {
        i["name"] for i in instruments if isinstance(i, dict) and isinstance(i.get("name"), str)
    }
    held = {w.where for w in workers.values()}
    tried: list[str] = []
    found = discovery.discover(
        shipped(),
        lan=True,
        usb=True,
        serial=True,
        budget_s=2.0,
        deadline_s=_AUTOCONFIG_SEARCH_S,
        skip=held,
        tried=tried,
    )
    added: list[str] = []
    in_form: list[str] = []
    running: list[str] = []
    unidentified: list[str] = []
    for f in found:
        if f.error == discovery.IN_USE:
            holder = _holder(f.address)
            if holder is None:
                running.append(f.address.where)
                continue
            entry = holder.cfg.entry
            if holder.cfg.name not in names and not _hosts(entry) & known:
                instruments.append(entry)
                names.add(holder.cfg.name)
            label = f"{holder.cfg.name} ({holder.where})"
            if label not in running:
                running.append(label)
            continue
        if f.error and f.idn is None:
            unidentified.append(f"{f.address.where}: {f.error}")
            continue
        if discovery.host_of(f.address) in known:
            in_form.append(f.address.where)
            continue
        entry = discovery.config_entry(f)
        name = unique_name(discovery.default_name(f), names, max_bytes=NAME_BYTES)
        entry["name"] = name
        instruments.append(entry)
        added.append(f"{name} ({f.address.where})")
    parts: list[str] = []
    if added:
        parts.append(f"Added {len(added)}: {', '.join(added)}.")
    if running:
        parts.append(f"Already recording: {', '.join(running)}.")
    if in_form and not added:
        parts.append(f"Already in the form: {', '.join(in_form)}.")
    if not (added or running or in_form or unidentified):
        parts.append(_nothing_found(tried))
        if not instruments:
            instruments = [
                {"connection": "demo", "category": c} for c in ("psu", "load", "dmm", "acsource")
            ]
            parts.append("Added the demo bench.")
    if unidentified:
        parts.append("Found but not identified: " + "; ".join(unidentified[:5]) + ".")
    message = " ".join(parts)
    return {"status": "success", "config": {"instruments": instruments}, "message": message}


@action(
    "Set",
    "Set one or more values on a channel; the readback confirms each one.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.number("voltage", title="Voltage (V)", required=False)
@action.number("current", title="Current (A)", required=False)
@action.number("power", title="Power (W)", required=False)
@action.number("resistance", title="Resistance (Ohm)", required=False)
@action.number("frequency", title="Frequency (Hz)", required=False)
@action.number("ovp", title="Over-voltage protection (V)", required=False)
@action.number("ocp", title="Over-current protection (A)", required=False)
@action.number("ovp_delay", title="Over-voltage protection delay (s)", required=False)
@action.boolean("ovp_on", title="Over-voltage protection on", required=False)
@action.boolean("ocp_on", title="Over-current protection on", required=False)
@action.boolean("remote_sense", title="Remote sense", required=False)
@action.select("tracking", title="Tracking", choices=list(TRACKING_LABELS[:-1]), required=False)
@action.number("slew_rise", title="Rising slew rate (A/s)", required=False)
@action.number("slew_fall", title="Falling slew rate (A/s)", required=False)
@action.select(
    "current_range", title="Current range", choices=list(CURRENT_RANGE_LABELS[:-1]), required=False
)
@action.select(
    "voltage_range", title="Voltage range", choices=list(CURRENT_RANGE_LABELS[:-1]), required=False
)
@action.select(
    "function",
    title="Function",
    description="A load's mode (cc, cv, cp, cr) or a multimeter's measurement.",
    choices=list(_FUNCTIONS),
    required=False,
)
def set_values(
    instrument: str | None = None,
    channel: int = 1,
    voltage: float | None = None,
    current: float | None = None,
    power: float | None = None,
    resistance: float | None = None,
    frequency: float | None = None,
    ovp: float | None = None,
    ocp: float | None = None,
    ovp_delay: float | None = None,
    ovp_on: bool | None = None,
    ocp_on: bool | None = None,
    remote_sense: bool | None = None,
    tracking: str | None = None,
    slew_rise: float | None = None,
    slew_fall: float | None = None,
    current_range: str | None = None,
    voltage_range: str | None = None,
    function: str | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        voltage=voltage,
        current=current,
        power=power,
        resistance=resistance,
        frequency=frequency,
        ovp=ovp,
        ocp=ocp,
        ovp_delay=ovp_delay,
        ovp_on=ovp_on,
        ocp_on=ocp_on,
        remote_sense=remote_sense,
        tracking=tracking or None,
        slew_rise=slew_rise,
        slew_fall=slew_fall,
        current_range=current_range or None,
        voltage_range=voltage_range or None,
        function=function or None,
    )


@action("Output", "Switch a channel's output (a load's input) on or off.", timeout=TIMEOUT_S)
@with_instrument
@with_channel
@action.boolean("on", title="On", default=True)
def output(instrument: str | None = None, channel: int = 1, *, on: bool = True) -> dict[str, Any]:
    return ask(instrument, "set", TIMEOUT_S, channel=channel, output=on)


@action(
    "Read",
    "A fresh measurement of every signal on a channel, now.",
    timeout=TIMEOUT_S,
    read_only=True,
)
@with_instrument
@with_channel
def read(instrument: str | None = None, channel: int = 1) -> dict[str, Any]:
    return ask(instrument, "read", TIMEOUT_S, channel=channel)


@action(
    "Get State",
    "Health, identity, profile, channels with their ranges and last values, counters and the "
    "last commands of an instrument.",
    timeout=TIMEOUT_S,
    read_only=True,
)
@with_instrument
def get_state(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "state", TIMEOUT_S)


@action(
    "SCPI",
    "Send any command to an instrument and return the reply for a query. Nothing is checked.",
    timeout=_SCPI_TIMEOUT_S,
)
@with_instrument
@action.text("command", title="Command")
@action.number(
    "timeout_s",
    title="Timeout (s)",
    minimum=_MIN_SCPI_S,
    maximum=_MAX_SCPI_S,
    required=False,
    default=2.0,
)
def scpi(instrument: str | None = None, *, command: str, timeout_s: float = 2.0) -> dict[str, Any]:
    _check_range("timeout_s", timeout_s, _MIN_SCPI_S, _MAX_SCPI_S)
    return ask(instrument, "scpi", _SCPI_TIMEOUT_S, command=command, timeout_s=timeout_s)


@action(
    "Set Meter",
    "Set a multimeter's measurement, range, autorange and integration time.",
    timeout=TIMEOUT_S,
)
@with_instrument
@action.select("function", title="Function", choices=list(FUNCTION_LABELS[:-1]), required=False)
@action.number(
    "range",
    title="Range",
    description="The largest value to measure, in the function's unit; the meter picks the "
    "range that covers it.",
    required=False,
)
@action.boolean("autorange", title="Autorange", required=False)
@action.number("nplc", title="Integration time (power line cycles)", required=False)
def set_meter(
    instrument: str | None = None,
    function: str | None = None,
    range: float | None = None,
    autorange: bool | None = None,
    nplc: float | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=1,
        function=function or None,
        range=range,
        autorange=autorange,
        nplc=nplc,
    )


@action("Short", "Short a load's input, or end the short.", timeout=TIMEOUT_S)
@with_instrument
@with_channel
@action.boolean("on", title="On", default=True)
def short(instrument: str | None = None, channel: int = 1, *, on: bool = True) -> dict[str, Any]:
    return ask(instrument, "set", TIMEOUT_S, channel=channel, short=on)


@action("Relay", "Close or open a relay channel of a switch unit.", timeout=TIMEOUT_S)
@with_instrument
@with_channel
@action.boolean("closed", title="Closed", default=True)
def relay(
    instrument: str | None = None, channel: int = 1, *, closed: bool = True
) -> dict[str, Any]:
    return ask(instrument, "set", TIMEOUT_S, channel=channel, relay=closed)


@action(
    "Reset",
    "Return the instrument to its reset state: outputs off, settings to defaults.",
    timeout=_SYSTEM_TIMEOUT_S,
)
@with_instrument
def reset(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="reset")


@action(
    "Reboot",
    "Restart the instrument; the extension reconnects when it is back.",
    timeout=TIMEOUT_S,
)
@with_instrument
def reboot(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "system", TIMEOUT_S, op="reboot")


@action(
    "Self-Test",
    "Run the instrument's self-test and report pass or the failure.",
    timeout=_SELF_TEST_TIMEOUT_S,
)
@with_instrument
def self_test(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "system", _SELF_TEST_TIMEOUT_S, op="self_test")


_slot = action.integer("slot", title="Slot", minimum=0, required=False)
_file = action.text(
    "file",
    title="File",
    description="A path on this computer; a relative one is under the Zelos data folder.",
    required=False,
)


@action(
    "Save State",
    "Save the instrument's settings to one of its memory slots, or to a file.",
    timeout=_SYSTEM_TIMEOUT_S,
)
@with_instrument
@_slot
@_file
def save_state(
    instrument: str | None = None, slot: int | None = None, file: str | None = None
) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="save", **_slot_or_file(slot, file))


@action(
    "Recall State",
    "Restore the instrument's settings from one of its memory slots, or from a file.",
    timeout=_SYSTEM_TIMEOUT_S,
)
@with_instrument
@_slot
@_file
def recall_state(
    instrument: str | None = None, slot: int | None = None, file: str | None = None
) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="recall", **_slot_or_file(slot, file))


@action("Lock Panel", "Lock or unlock the instrument's front panel.", timeout=_SYSTEM_TIMEOUT_S)
@with_instrument
@action.boolean("on", title="Locked", default=True)
def lock_panel(instrument: str | None = None, *, on: bool = True) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="lock", on=on)


@action("Beep", "Sound the instrument's beeper.", timeout=_SYSTEM_TIMEOUT_S)
@with_instrument
def beep(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="beep")


@action(
    "Display",
    "Show text on the instrument's display, clear it, or switch the display on or off.",
    timeout=_SYSTEM_TIMEOUT_S,
)
@with_instrument
@action.text("text", title="Text", description="Empty clears the text.", required=False)
@action.boolean("on", title="On", required=False)
def display(
    instrument: str | None = None, text: str | None = None, on: bool | None = None
) -> dict[str, Any]:
    return ask(instrument, "system", _SYSTEM_TIMEOUT_S, op="display", text=text, on=on)


@action(
    "Screenshot",
    "Save a picture of the instrument's screen to a file.",
    timeout=_SCREENSHOT_TIMEOUT_S,
)
@with_instrument
@action.text(
    "path",
    title="File",
    description="Empty saves it under screenshots in the Zelos data folder; a relative path is "
    "under that folder.",
    required=False,
)
def screenshot(instrument: str | None = None, path: str | None = None) -> dict[str, Any]:
    return ask(instrument, "system", _SCREENSHOT_TIMEOUT_S, op="screenshot", path=path or None)


@action("Clear", "Clear a channel's latched protection and the error queue.", timeout=TIMEOUT_S)
@with_instrument
@with_channel
def clear(instrument: str | None = None, channel: int = 1) -> dict[str, Any]:
    return ask(instrument, "clear", TIMEOUT_S, channel=channel)


@action("Local", "Give the front panel back to the person at the bench.", timeout=TIMEOUT_S)
@with_instrument
def local(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "local", TIMEOUT_S)


@action("Release", "Close the link so another program can use the instrument.", timeout=TIMEOUT_S)
@with_instrument
def release(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "release", TIMEOUT_S)


@action("Acquire", "Take a released instrument back.", timeout=_ACQUIRE_TIMEOUT_S)
@with_instrument
def acquire(instrument: str | None = None) -> dict[str, Any]:
    return ask(instrument, "acquire", _ACQUIRE_TIMEOUT_S)


def _nothing_found(tried: list[str]) -> str:
    """What was searched, so an instrument on another subnet is a known gap, not a mystery."""
    searched = "; ".join(tried) if tried else "nothing"
    return (
        f"No instruments found. Searched: {searched}. An instrument on another subnet does not "
        "answer these searches: type its address."
    )


def _holder(address: Address) -> InstrumentWorker | None:
    """The worker that holds this instrument, under this address or its other one."""
    from . import discovery

    host = discovery.host_of(address)
    return next(
        (
            w
            for w in workers.values()
            if w.where == address.where or discovery.host_of_where(w.where) == host
        ),
        None,
    )


def _hosts(item: dict[str, Any]) -> set[str]:
    """The instruments a config entry addresses, by host as discovery tells them apart."""
    from . import discovery

    return {
        discovery.host_of_where(item[k])
        for k in ("host", "resource", "port")
        if isinstance(item.get(k), str)
    }


def _visa_value(address: Address) -> str:
    """A found address as a VISA resource: a raw socket becomes its SOCKET form."""
    if isinstance(address, TcpAddress):
        return f"TCPIP::{address.host}::{address.port}::SOCKET"
    return address.where


def _lan_value(address: TcpAddress) -> str:
    """What the Host field stores: the host alone on the usual port, else with the port."""
    return address.host if address.port == 5025 else address.where


def _slot_or_file(slot: int | None, file: str | None) -> dict[str, Any]:
    """Exactly one of a memory slot and a file."""
    if slot is None and not file:
        raise ValueError("give a slot or a file")
    if slot is not None and file:
        raise ValueError("give a slot or a file, not both")
    return {"slot": slot} if slot is not None else {"file": file}


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
