"""What the Action panel, notebooks and Zelos AI can do with the instruments."""

import logging
from typing import Any

from zelos_sdk.actions import SelectField, action, register_field_type

from . import profile as profiles_module
from .link import TcpAddress
from .naming import NAME_BYTES, unique_name
from .worker import InstrumentWorker, RequestKind

logger = logging.getLogger(__name__)

#: Instrument name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, InstrumentWorker] = {}

_TIMEOUT_S = 10.0
_SCPI_TIMEOUT_S = 35.0
_ACQUIRE_TIMEOUT_S = 15.0
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
    choices=["lan", "usb", "serial"],
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
    found = discovery.discover(
        profiles_module.shipped(),
        lan=connection == "lan",
        usb=connection == "usb",
        serial=False,
        budget_s=1.0,
        identify_timeout_s=2.0,
        skip=held,
    )
    choices = []
    for f in found:
        value = f.address.host if isinstance(f.address, TcpAddress) else f.address.where
        who = f"{f.idn.manufacturer} {f.idn.model}".strip() if f.idn else ""
        label = f"{who} ({f.address.where})" if who else f.address.where
        detail = f.error or (f.profile_id and f"profile {f.profile_id}") or None
        choices.append({"value": value, "label": label, "detail": detail})
    if not choices:
        where = "on the network" if connection == "lan" else "on USB"
        return {
            "status": "success",
            "choices": [],
            "message": f"No instruments found {where}. Type the address.",
        }
    return {"status": "success", "choices": choices}


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
        if not isinstance(item, dict):
            continue
        for key in ("host", "resource", "port"):
            if isinstance(item.get(key), str):
                known.add(item[key])
    names = {
        i["name"] for i in instruments if isinstance(i, dict) and isinstance(i.get("name"), str)
    }
    held = {w.where for w in workers.values()}
    found = discovery.discover(
        profiles_module.shipped(), lan=True, usb=True, serial=True, budget_s=2.0, skip=held
    )
    added: list[str] = []
    unidentified: list[str] = []
    for f in found:
        if f.error and f.idn is None:
            unidentified.append(f"{f.address.where}: {f.error}")
            continue
        entry = discovery.config_entry(f)
        if any(entry.get(k) in known for k in ("host", "resource", "port")):
            continue
        name = unique_name(discovery.default_name(f), names, max_bytes=NAME_BYTES)
        entry["name"] = name
        instruments.append(entry)
        added.append(f"{name} ({f.address.where})")
    if added:
        message = f"Added {len(added)}: {', '.join(added)}."
    elif instruments:
        message = "No new instruments found."
    else:
        instruments = [
            {"connection": "demo", "category": c} for c in ("psu", "load", "dmm", "acsource")
        ]
        message = "No instruments found; added the demo bench."
    if unidentified:
        message += " Not identified: " + "; ".join(unidentified[:5]) + "."
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
        raise RuntimeError(f"{instrument} did not answer within {wait_s:g} s") from None
    if "error" in result and len(result) == 1:
        raise RuntimeError(result["error"])
    return result


def _check_range(name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
