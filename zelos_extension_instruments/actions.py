"""What the Action panel, notebooks and Zelos AI can do with the instruments."""

import logging
from typing import Any

from zelos_sdk.actions import SelectField, action, register_field_type

from . import profile as profiles_module
from .link import Address, TcpAddress
from .naming import NAME_BYTES, unique_name
from .profile_scope import (
    COUPLING_LABELS,
    MEASUREMENT_FIELDS,
    SCOPE_COMMAND_SETTERS,
    TRIGGER_MODE_LABELS,
    TRIGGER_SLOPE_LABELS,
    TRIGGER_SOURCE_LABELS,
)
from .worker import InstrumentWorker, RequestKind

logger = logging.getLogger(__name__)

#: Instrument name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, InstrumentWorker] = {}

_TIMEOUT_S = 10.0
_SCPI_TIMEOUT_S = 35.0
_ACQUIRE_TIMEOUT_S = 15.0
# A capture of millions of samples over VXI-11 takes seconds.
_CAPTURE_TIMEOUT_S = 60.0
# A wait ends this much before the action's own timeout, so the caller gets the reason.
_MARGIN_S = 0.5
# The app's choices list gives up after 10 s; Auto-configure waits longer.
_LIST_TIMEOUT_S = 10
_AUTOCONFIG_TIMEOUT_S = 30
_MIN_SCPI_S, _MAX_SCPI_S = 0.1, 30.0
_VALUES = ("voltage", "current", "power", "resistance", "frequency", "ovp", "ocp")
_FUNCTIONS = ("cc", "cv", "cp", "cr", *profiles_module.FUNCTION_LABELS[:-1])


def _first_instrument() -> str | None:
    return next(iter(workers), None)


@register_field_type("instrument")
class _InstrumentField(SelectField):
    """An instrument name field whose form default is the first configured instrument."""

    def to_json_schema(self, current_values: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = super().to_json_schema(current_values)
        schema["default"] = _first_instrument()
        return schema


_instrument = action.input(
    "instrument",
    type="instrument",
    title="Instrument",
    choices=lambda *_: sorted(workers),
    required=False,
)
_channel = action.integer(
    "channel", title="Channel", minimum=1, maximum=64, required=False, default=1
)


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
        profiles_module.shipped(),
        lan=connection in ("lan", "visa"),
        usb=connection == "usb",
        serial=False,
        budget_s=1.0,
        identify_timeout_s=2.0,
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
    for p in profiles_module.shipped():
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
    names = {
        i["name"] for i in instruments if isinstance(i, dict) and isinstance(i.get("name"), str)
    }
    held = {w.where for w in workers.values()}
    tried: list[str] = []
    found = discovery.discover(
        profiles_module.shipped(),
        lan=True,
        usb=True,
        serial=True,
        budget_s=2.0,
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
    if not (added or running or in_form):
        parts.append(_nothing_found(tried))
        if not instruments:
            instruments = [
                {"connection": "demo", "category": c} for c in ("psu", "load", "dmm", "acsource")
            ]
            parts.append("Added the demo bench.")
    if unidentified:
        parts.append("Not identified: " + "; ".join(unidentified[:5]) + ".")
    message = " ".join(parts)
    return {"status": "success", "config": {"instruments": instruments}, "message": message}


@action(
    "Set",
    "Set one or more values on a channel; the readback confirms each one.",
    timeout=_TIMEOUT_S,
)
@_instrument
@_channel
@action.number("voltage", title="Voltage (V)", required=False)
@action.number("current", title="Current (A)", required=False)
@action.number("power", title="Power (W)", required=False)
@action.number("resistance", title="Resistance (Ohm)", required=False)
@action.number("frequency", title="Frequency (Hz)", required=False)
@action.number("ovp", title="Over-voltage protection (V)", required=False)
@action.number("ocp", title="Over-current protection (A)", required=False)
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
    function: str | None = None,
) -> dict[str, Any]:
    return _ask(
        instrument,
        "set",
        _TIMEOUT_S,
        channel=channel,
        voltage=voltage,
        current=current,
        power=power,
        resistance=resistance,
        frequency=frequency,
        ovp=ovp,
        ocp=ocp,
        function=function or None,
    )


@action("Output", "Switch a channel's output (a load's input) on or off.", timeout=_TIMEOUT_S)
@_instrument
@_channel
@action.boolean("on", title="On", default=True)
def output(instrument: str | None = None, channel: int = 1, *, on: bool = True) -> dict[str, Any]:
    return _ask(instrument, "output", _TIMEOUT_S, channel=channel, on=on)


@action(
    "Read",
    "A fresh measurement of every signal on a channel, now.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_instrument
@_channel
def read(instrument: str | None = None, channel: int = 1) -> dict[str, Any]:
    return _ask(instrument, "read", _TIMEOUT_S, channel=channel)


@action(
    "Get State",
    "Health, identity, profile, channels with their ranges and last values, counters and the "
    "last commands of an instrument.",
    timeout=_TIMEOUT_S,
    read_only=True,
)
@_instrument
def get_state(instrument: str | None = None) -> dict[str, Any]:
    return _ask(instrument, "state", _TIMEOUT_S)


@action(
    "SCPI",
    "Send any command to an instrument and return the reply for a query. Nothing is checked.",
    timeout=_SCPI_TIMEOUT_S,
)
@_instrument
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
    return _ask(instrument, "scpi", _SCPI_TIMEOUT_S, command=command, timeout_s=timeout_s)


@action(
    "Set Channel",
    "Set a scope channel's vertical scale, offset, coupling, probe factor or visibility.",
    timeout=_TIMEOUT_S,
)
@_instrument
@_channel
@action.number("scale", title="Volts per division (V)", required=False)
@action.number("offset", title="Offset (V)", required=False)
@action.select("coupling", title="Coupling", choices=list(COUPLING_LABELS[:-1]), required=False)
@action.number("probe", title="Probe factor", required=False)
@action.boolean("visible", title="Visible", required=False)
def set_channel(
    instrument: str | None = None,
    channel: int = 1,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
    visible: bool | None = None,
) -> dict[str, Any]:
    return _ask(
        instrument,
        "set",
        _TIMEOUT_S,
        channel=channel,
        scale=scale,
        offset=offset,
        coupling=coupling or None,
        probe=probe,
        visible=visible,
    )


@action(
    "Set Timebase",
    "Set a scope's seconds per division and trigger delay.",
    timeout=_TIMEOUT_S,
)
@_instrument
@action.number("timebase", title="Seconds per division (s)", required=False)
@action.number("delay", title="Delay (s)", required=False)
def set_timebase(
    instrument: str | None = None, timebase: float | None = None, delay: float | None = None
) -> dict[str, Any]:
    return _ask(instrument, "set", _TIMEOUT_S, channel=1, timebase=timebase, delay=delay)


@action(
    "Set Trigger",
    "Set a scope's edge trigger: source, level, slope and mode.",
    timeout=_TIMEOUT_S,
)
@_instrument
@action.select("source", title="Source", choices=list(TRIGGER_SOURCE_LABELS[:-1]), required=False)
@action.number("level", title="Level (V)", required=False)
@action.select("slope", title="Slope", choices=list(TRIGGER_SLOPE_LABELS[:-1]), required=False)
@action.select("mode", title="Mode", choices=list(TRIGGER_MODE_LABELS[:-1]), required=False)
def set_trigger(
    instrument: str | None = None,
    source: str | None = None,
    level: float | None = None,
    slope: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    return _ask(
        instrument,
        "set",
        _TIMEOUT_S,
        channel=1,
        trigger_source=source or None,
        trigger_level=level,
        trigger_slope=slope or None,
        trigger_mode=mode or None,
    )


@action(
    "Run Control", "Run, stop, arm a single acquisition, or autoset a scope.", timeout=_TIMEOUT_S
)
@_instrument
@action.select("command", title="Command", choices=list(SCOPE_COMMAND_SETTERS), default="run")
def run_control(instrument: str | None = None, *, command: str = "run") -> dict[str, Any]:
    return _ask(instrument, "command", _TIMEOUT_S, name=command)


@action(
    "Measure",
    "Show a measurement of a scope channel in a measurement slot; it is recorded from then on.",
    timeout=_TIMEOUT_S,
)
@_instrument
@_channel
@action.select("type", title="Measurement", choices=list(MEASUREMENT_FIELDS), default="pkpk")
@action.integer(
    "slot",
    title="Slot",
    description="Empty reuses the slot that already shows it, or the lowest free one.",
    minimum=1,
    maximum=64,
    required=False,
)
@action.boolean("off", title="Turn the slot off instead", default=False, required=False)
def measure(
    instrument: str | None = None,
    channel: int = 1,
    *,
    type: str = "pkpk",
    slot: int | None = None,
    off: bool = False,
) -> dict[str, Any]:
    return _ask(instrument, "measure", _TIMEOUT_S, channel=channel, type=type, slot=slot, off=off)


@action(
    "Capture Waveform",
    "Fetch the samples of a scope channel on screen into the trace as one burst.",
    timeout=_CAPTURE_TIMEOUT_S,
)
@_instrument
@_channel
@action.integer(
    "points",
    title="Points",
    description="At most this many samples, spread over the screen.",
    minimum=1,
    maximum=100_000_000,
    default=10_000,
    required=False,
)
def capture_waveform(
    instrument: str | None = None, channel: int = 1, points: int = 10_000
) -> dict[str, Any]:
    return _ask(instrument, "waveform", _CAPTURE_TIMEOUT_S, channel=channel, points=points)


@action("Clear", "Clear a channel's latched protection and the error queue.", timeout=_TIMEOUT_S)
@_instrument
@_channel
def clear(instrument: str | None = None, channel: int = 1) -> dict[str, Any]:
    return _ask(instrument, "clear", _TIMEOUT_S, channel=channel)


@action("Local", "Give the front panel back to the person at the bench.", timeout=_TIMEOUT_S)
@_instrument
def local(instrument: str | None = None) -> dict[str, Any]:
    return _ask(instrument, "local", _TIMEOUT_S)


@action("Release", "Close the link so another program can use the instrument.", timeout=_TIMEOUT_S)
@_instrument
def release(instrument: str | None = None) -> dict[str, Any]:
    return _ask(instrument, "release", _TIMEOUT_S)


@action("Acquire", "Take a released instrument back.", timeout=_ACQUIRE_TIMEOUT_S)
@_instrument
def acquire(instrument: str | None = None) -> dict[str, Any]:
    return _ask(instrument, "acquire", _ACQUIRE_TIMEOUT_S)


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


def _ask(
    instrument: str | None, kind: RequestKind, action_timeout_s: float, **args: Any
) -> dict[str, Any]:
    """The worker's reply; its error is raised as it is."""
    if instrument is None:
        instrument = _first_instrument()
        if instrument is None:
            raise ValueError("no instrument is configured")
    worker = workers.get(instrument)
    if worker is None:
        raise ValueError(f"no instrument is named {instrument}; configured: {', '.join(workers)}")
    future = worker.request(kind, **args)
    wait_s = action_timeout_s - _MARGIN_S
    try:
        result = future.result(timeout=wait_s)
    except TimeoutError:
        # The worker must not run the request later, after the person was told it failed.
        future.cancel()
        raise RuntimeError(f"{instrument} did not answer within {wait_s:g} s") from None
    if "error" in result and len(result) == 1:
        raise RuntimeError(result["error"])
    return result


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
