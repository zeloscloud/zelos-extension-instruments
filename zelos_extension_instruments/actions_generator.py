"""The function generator actions: waveform, modulation, burst, sweep and software trigger."""

from typing import Any

from zelos_sdk.actions import action

from .actions_common import TIMEOUT_S, ask, with_channel, with_instrument
from .profile import LOAD_LABELS, MODULATION_LABELS, SHAPE_LABELS


@action(
    "Set Waveform",
    "Set a generator channel's waveform: shape, frequency, levels, timing and output load.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.select("shape", title="Shape", choices=list(SHAPE_LABELS[:-1]), required=False)
@action.number("frequency", title="Frequency (Hz)", required=False)
@action.number("amplitude", title="Amplitude (Vpp)", required=False)
@action.number("offset", title="Offset (V)", required=False)
@action.number("high", title="High level (V)", required=False)
@action.number("low", title="Low level (V)", required=False)
@action.number("phase", title="Phase (deg)", required=False)
@action.number("duty", title="Duty cycle (%)", required=False)
@action.number("symmetry", title="Symmetry (%)", required=False)
@action.number("width", title="Pulse width (s)", required=False)
@action.select(
    "output_load",
    title="Output load",
    description="The load the levels assume: 50 Ohm or high impedance.",
    choices=list(LOAD_LABELS[:-1]),
    required=False,
)
def set_waveform(
    instrument: str | None = None,
    channel: int = 1,
    shape: str | None = None,
    frequency: float | None = None,
    amplitude: float | None = None,
    offset: float | None = None,
    high: float | None = None,
    low: float | None = None,
    phase: float | None = None,
    duty: float | None = None,
    symmetry: float | None = None,
    width: float | None = None,
    output_load: str | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        shape=shape or None,
        frequency=frequency,
        amplitude=amplitude,
        offset=offset,
        high=high,
        low=low,
        phase=phase,
        duty=duty,
        symmetry=symmetry,
        width=width,
        output_load=output_load or None,
    )


@action(
    "Set Modulation",
    "Switch a generator channel's modulation on or off and set its type, depth, deviation and "
    "rate.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.boolean("on", title="On", required=False)
@action.select("type", title="Type", choices=list(MODULATION_LABELS[:-1]), required=False)
@action.number("depth", title="Depth (%)", required=False)
@action.number("deviation", title="Frequency deviation (Hz)", required=False)
@action.number("phase_deviation", title="Phase deviation (deg)", required=False)
@action.number("frequency", title="Modulation frequency (Hz)", required=False)
def set_modulation(
    instrument: str | None = None,
    channel: int = 1,
    on: bool | None = None,
    type: str | None = None,
    depth: float | None = None,
    deviation: float | None = None,
    phase_deviation: float | None = None,
    frequency: float | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        modulation_type=type or None,
        modulation_depth=depth,
        modulation_deviation=deviation,
        phase_deviation=phase_deviation,
        modulation_frequency=frequency,
        modulation=on,
    )


@action(
    "Set Burst",
    "Switch a generator channel's burst mode on or off and set its cycles and period.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.boolean("on", title="On", required=False)
@action.integer("cycles", title="Cycles", minimum=1, required=False)
@action.number("period", title="Period (s)", required=False)
def set_burst(
    instrument: str | None = None,
    channel: int = 1,
    on: bool | None = None,
    cycles: int | None = None,
    period: float | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        burst_cycles=cycles,
        burst_period=period,
        burst=on,
    )


@action(
    "Set Sweep",
    "Switch a generator channel's frequency sweep on or off and set its start, stop and time.",
    timeout=TIMEOUT_S,
)
@with_instrument
@with_channel
@action.boolean("on", title="On", required=False)
@action.number("start", title="Start frequency (Hz)", required=False)
@action.number("stop", title="Stop frequency (Hz)", required=False)
@action.number("time", title="Sweep time (s)", required=False)
def set_sweep(
    instrument: str | None = None,
    channel: int = 1,
    on: bool | None = None,
    start: float | None = None,
    stop: float | None = None,
    time: float | None = None,
) -> dict[str, Any]:
    return ask(
        instrument,
        "set",
        TIMEOUT_S,
        channel=channel,
        sweep_start=start,
        sweep_stop=stop,
        sweep_time=time,
        sweep=on,
    )


@action("Trigger", "Send a software trigger to a generator channel.", timeout=TIMEOUT_S)
@with_instrument
@with_channel
def trigger(instrument: str | None = None, channel: int = 1) -> dict[str, Any]:
    return ask(instrument, "command", TIMEOUT_S, name="trigger", channel=channel)
