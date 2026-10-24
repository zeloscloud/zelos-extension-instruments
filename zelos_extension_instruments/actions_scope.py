"""The oscilloscope actions: channels, timebase, trigger, acquisition, measurements and capture."""

from typing import Any

from zelos_sdk.actions import action

from .actions_common import TIMEOUT_S, ask, with_channel, with_instrument
from .profile_scope import (
    ACQUISITION_MODE_LABELS,
    BANDWIDTH_LABELS,
    COUPLING_LABELS,
    MEASUREMENT_FIELDS,
    PULSE_CONDITION_LABELS,
    SCOPE_COMMAND_SETTERS,
    TRIGGER_MODE_LABELS,
    TRIGGER_SLOPE_LABELS,
    TRIGGER_SOURCE_LABELS,
    TRIGGER_TYPE_LABELS,
)

# A capture of millions of samples over VXI-11 takes seconds.
_CAPTURE_TIMEOUT_S = 60.0


@action(
    "Set Channel",
    "Set a scope channel's vertical scale, offset, coupling, probe factor, visibility or "
    "bandwidth limit.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.number("scale", title="Volts per division (V)", required=False)
@action.number("offset", title="Offset (V)", required=False)
@action.select("coupling", title="Coupling", choices=list(COUPLING_LABELS[:-1]), required=False)
@action.number("probe", title="Probe factor", required=False)
@action.boolean("visible", title="Visible", required=False)
@action.select(
    "bandwidth_limit", title="Bandwidth limit", choices=list(BANDWIDTH_LABELS[:-1]), required=False
)
def set_channel(
    instrument: str | None = None,
    channel: int = 1,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    probe: float | None = None,
    visible: bool | None = None,
    bandwidth_limit: str | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        scale=scale,
        offset=offset,
        coupling=coupling or None,
        probe=probe,
        visible=visible,
        bandwidth_limit=bandwidth_limit or None,
    )


@action(
    "Set Timebase",
    "Set a scope's seconds per division and trigger delay.",
    timeout=TIMEOUT_S,
)
@with_instrument
@action.number("timebase", title="Seconds per division (s)", required=False)
@action.number("delay", title="Delay (s)", required=False)
def set_timebase(
    instrument: str | None = None, timebase: float | None = None, delay: float | None = None
) -> dict[str, Any]:
    return ask(instrument, "set", TIMEOUT_S, channel=1, timebase=timebase, delay=delay)


@action(
    "Set Trigger",
    "Set a scope's trigger: source, level, slope, mode, and its type with the pulse width.",
    timeout=TIMEOUT_S,
)
@with_instrument
@action.select("source", title="Source", choices=list(TRIGGER_SOURCE_LABELS[:-1]), required=False)
@action.number("level", title="Level (V)", required=False)
@action.select("slope", title="Slope", choices=list(TRIGGER_SLOPE_LABELS[:-1]), required=False)
@action.select("mode", title="Mode", choices=list(TRIGGER_MODE_LABELS[:-1]), required=False)
@action.select("trigger_type", title="Type", choices=list(TRIGGER_TYPE_LABELS[:-1]), required=False)
@action.number("pulse_width", title="Pulse width (s)", required=False)
@action.select(
    "pulse_condition",
    title="Pulse condition",
    description="Trigger on a pulse longer or shorter than the pulse width.",
    choices=list(PULSE_CONDITION_LABELS[:-1]),
    required=False,
)
def set_trigger(
    instrument: str | None = None,
    source: str | None = None,
    level: float | None = None,
    slope: str | None = None,
    mode: str | None = None,
    trigger_type: str | None = None,
    pulse_width: float | None = None,
    pulse_condition: str | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=1,
        trigger_source=source or None,
        trigger_level=level,
        trigger_slope=slope or None,
        trigger_mode=mode or None,
        trigger_type=trigger_type or None,
        pulse_width=pulse_width,
        pulse_condition=pulse_condition or None,
    )


@action(
    "Set Acquisition",
    "Set a scope's acquisition mode and how many acquisitions it averages.",
    timeout=TIMEOUT_S,
)
@with_instrument
@action.select(
    "acquisition_mode",
    title="Acquisition mode",
    choices=list(ACQUISITION_MODE_LABELS[:-1]),
    required=False,
)
@action.integer("averages", title="Averages", minimum=1, required=False)
def set_acquisition(
    instrument: str | None = None,
    acquisition_mode: str | None = None,
    averages: int | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=1,
        acquisition_mode=acquisition_mode or None,
        averages=averages,
    )


@action(
    "Run Control",
    "Run, stop, arm a single acquisition, autoset a scope, or force a trigger.",
    timeout=TIMEOUT_S,
)
@with_instrument
@action.select("command", title="Command", choices=list(SCOPE_COMMAND_SETTERS), default="run")
def run_control(instrument: str | None = None, *, command: str = "run") -> dict[str, Any]:
    return ask(instrument, "command", TIMEOUT_S, name=command)


@action(
    "Measure",
    "Show a measurement of a scope channel in a measurement slot; it is recorded from then on.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
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
    return ask(instrument, "measure", TIMEOUT_S, channel=channel, type=type, slot=slot, off=off)


@action(
    "Capture Waveform",
    "Fetch the samples of a scope channel on screen into the trace as one burst.",
    timeout=_CAPTURE_TIMEOUT_S,
    read_only=True,
)
@with_instrument
@with_channel
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
    return ask(instrument, "waveform", _CAPTURE_TIMEOUT_S, channel=channel, points=points)
