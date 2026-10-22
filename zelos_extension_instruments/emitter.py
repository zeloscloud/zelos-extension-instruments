"""Readings to trace events."""

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import zelos_sdk
from zelos_sdk import schemas

from .config import PollRow, poll_key
from .naming import MAX_BYTES, Namer
from .profile import BOOL_FIELDS, ENUM_FIELDS, Profile
from .profile_scope import MEASUREMENT_FIELDS

logger = logging.getLogger(__name__)

STATUS_FIELDS = {"rtt_ms": "ms", "rate_hz": "Hz", "errors": None, "connected": None}


IDENTITY_FIELDS = ("manufacturer", "model", "serial", "firmware", "profile", "address")


@dataclass(slots=True)
class _Instrument:
    units: dict[str, str | None]
    status: zelos_sdk.TraceSourceEvent
    log: zelos_sdk.TraceSourceEvent
    identity: zelos_sdk.TraceSourceEvent
    poll: zelos_sdk.TraceSourceEvent | None
    poll_fields: dict[str, str]
    # One event for settings the whole instrument shares, such as a scope's timebase.
    acquisition: zelos_sdk.TraceSourceEvent | None = None
    acquisition_fields: dict[str, str | None] = field(default_factory=dict)
    # Each channel's event and the fields it records, registered once the readers are known.
    channels: dict[int, zelos_sdk.TraceSourceEvent] = field(default_factory=dict)
    channel_fields: dict[int, dict[str, str | None]] = field(default_factory=dict)
    # A scope's measurement fields: its readers do not list them, its slots do.
    measurements: set[str] = field(default_factory=set)
    # A meter's functions and a channel's waveform get their event at their first value.
    events: dict[str, zelos_sdk.TraceSourceEvent] = field(default_factory=dict)
    dmm_units: dict[str, str | None] = field(default_factory=dict)
    waveform_channels: set[int] = field(default_factory=set)
    warned: set[str] = field(default_factory=set)


class Emitter:
    """Names and writes the trace events of every instrument; the worker decides time.

    An event is registered only once something is written to it, so every signal in the trace
    holds data.
    """

    def __init__(self, source: zelos_sdk.TraceSource) -> None:
        self._source = source
        # Each instrument is touched only by its own worker thread.
        self._instruments: dict[str, _Instrument] = {}

    def add_instrument(
        self, name: str, profile: Profile, channels: int, poll: tuple[PollRow, ...] = ()
    ) -> None:
        """Register the status, log, identity, acquisition and poll events. Channels follow
        through `add_channels`, once the instrument has said which readers it answers."""
        status = self._source.add_event(
            f"{name}/status",
            [
                zelos_sdk.TraceEventFieldMetadata("rtt_ms", zelos_sdk.DataType.Float64, "ms"),
                zelos_sdk.TraceEventFieldMetadata("rate_hz", zelos_sdk.DataType.Float64, "Hz"),
                zelos_sdk.TraceEventFieldMetadata("errors", zelos_sdk.DataType.UInt32),
                zelos_sdk.TraceEventFieldMetadata("connected", zelos_sdk.DataType.Boolean),
            ],
        )
        log = self._source.add_event(f"{name}/log", schemas.Log)
        identity = self._source.add_event(
            f"{name}/identity",
            [
                zelos_sdk.TraceEventFieldMetadata(k, zelos_sdk.DataType.String)
                for k in IDENTITY_FIELDS
            ],
        )
        poll_event = None
        poll_fields: dict[str, str] = {}
        if poll:
            # `time_ns` is the row's time argument and can never be a field.
            namer = Namer(("time_ns",), MAX_BYTES - len(f"{name}/poll".encode()))
            keys = [
                (poll_key(row.name, ch), row.unit)
                for row in poll
                for ch in (range(1, channels + 1) if "{ch}" in row.query else (None,))
            ]
            poll_fields = {key: namer.name(key) for key, _ in keys}
            poll_event = self._source.add_event(
                f"{name}/poll",
                [
                    zelos_sdk.TraceEventFieldMetadata(
                        poll_fields[key], zelos_sdk.DataType.Float64, unit
                    )
                    for key, unit in keys
                ],
            )
        acquisition = None
        acquisition_fields = profile.instrument_fields
        if acquisition_fields:
            event_name = f"{name}/acquisition"
            acquisition = self._source.add_event(
                event_name, [_metadata(k, v) for k, v in acquisition_fields.items()]
            )
            for field_name in acquisition_fields:
                if field_name in ENUM_FIELDS:
                    self._source.add_value_table(
                        event_name, field_name, dict(enumerate(ENUM_FIELDS[field_name]))
                    )
        scope = profile.scope
        self._instruments[name] = _Instrument(
            # A meter's reading lands under its function's event, not on the channel.
            units={k: v for k, v in profile.fields.items() if profile.dmm is None or k != "value"},
            status=status,
            log=log,
            identity=identity,
            poll=poll_event,
            poll_fields=poll_fields,
            acquisition=acquisition,
            acquisition_fields=dict(acquisition_fields),
            measurements=set(MEASUREMENT_FIELDS) if scope is not None else set(),
            dmm_units=dict(profile.dmm.functions) if profile.dmm is not None else {},
            waveform_channels=(
                set(range(1, channels + 1))
                if scope is not None and scope.waveform is not None
                else set()
            ),
        )

    def add_channels(self, name: str, fields: dict[int, set[str]]) -> None:
        """Register `<name>/ch<n>` with the fields its readers produce; a channel that reads
        nothing gets no event."""
        inst = self._instruments[name]
        for ch, names in sorted(fields.items()):
            if ch in inst.channels:
                continue
            kept = {k: u for k, u in inst.units.items() if k in names or k in inst.measurements}
            if not kept:
                continue
            event_name = f"{name}/ch{ch}"
            inst.channels[ch] = self._source.add_event(
                event_name, [_metadata(k, u) for k, u in kept.items()]
            )
            inst.channel_fields[ch] = kept
            for field_name in kept:
                if field_name in ENUM_FIELDS:
                    self._source.add_value_table(
                        event_name, field_name, dict(enumerate(ENUM_FIELDS[field_name]))
                    )

    def _event(
        self, inst: _Instrument, event_name: str, field_name: str, unit: str | None
    ) -> zelos_sdk.TraceSourceEvent:
        event = inst.events.get(event_name)
        if event is None:
            event = self._source.add_event(
                event_name,
                [zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Float64, unit)],
            )
            inst.events[event_name] = event
        return event

    def row(self, name: str, ch: int, values: dict[str, Any], time_ns: int) -> None:
        """Write the fields of one channel read at `time_ns`; labels become their table index."""
        inst = self._instruments[name]
        event = inst.channels.get(ch)
        if event is None:
            return
        row = _typed(values, inst.channel_fields[ch])
        if row:
            event.log_at(time_ns, **row)

    def identity(self, name: str, values: dict[str, str], time_ns: int) -> None:
        """Who the instrument is and how it is reached, written at every connect."""
        self._instruments[name].identity.log_at(
            time_ns, **{k: str(values.get(k) or "") for k in IDENTITY_FIELDS}
        )

    def acquisition_row(self, name: str, values: dict[str, Any], time_ns: int) -> None:
        """Write the instrument-wide fields read at `time_ns`."""
        inst = self._instruments[name]
        if inst.acquisition is None:
            return
        row = _typed(values, inst.acquisition_fields)
        if row:
            inst.acquisition.log_at(time_ns, **row)

    def waveform(
        self,
        name: str,
        ch: int,
        time_ns: npt.NDArray[np.int64],
        volts: npt.NDArray[np.float64],
    ) -> None:
        """One capture of a channel, written as a single Arrow batch."""
        inst = self._instruments[name]
        if ch not in inst.waveform_channels:
            return
        event_name = f"{name}/ch{ch}/waveform"
        self._event(inst, event_name, "voltage", "V")
        batch = pa.record_batch(
            {
                "time_ns": pa.array(time_ns, type=pa.timestamp("ns", tz="UTC")),
                "voltage": pa.array(volts, type=pa.float64()),
            }
        )
        self._source.log_batch(event_name, batch)

    def reading(self, name: str, function: str, value: float, time_ns: int) -> None:
        """A DMM reading, under the active function's event."""
        inst = self._instruments[name]
        if function not in inst.dmm_units:
            if function not in inst.warned:
                inst.warned.add(function)
                logger.warning(
                    "%s: no event for function %s; its readings are dropped", name, function
                )
            return
        event = self._event(inst, f"{name}/{function}", "value", inst.dmm_units[function])
        event.log_at(time_ns, value=value)

    def poll_value(self, name: str, row_name: str, value: float, time_ns: int) -> None:
        inst = self._instruments[name]
        if inst.poll is None:
            return
        inst.poll.log_at(time_ns, **{inst.poll_fields[row_name]: value})

    def status(
        self, name: str, rtt_ms: float, rate_hz: float, errors: int, connected: bool, time_ns: int
    ) -> None:
        self._instruments[name].status.log_at(
            time_ns, rtt_ms=rtt_ms, rate_hz=rate_hz, errors=errors, connected=connected
        )

    def note(
        self, name: str, level: str, message: str, time_ns: int, source: str = "instruments"
    ) -> None:
        """One line in `<name>/log`; `source` is the Log panel's Source column."""
        self._instruments[name].log.log_at(
            time_ns, level=level, message=message, name=source, file="", line=0
        )

    def fields(self, name: str) -> dict[str, str | None]:
        return dict(self._instruments[name].units)


def _typed(values: dict[str, Any], fields: dict[str, str | None]) -> dict[str, Any]:
    """The values of known fields as the event stores them; labels become their table index."""
    row: dict[str, Any] = {}
    for field_name, value in values.items():
        if field_name not in fields:
            continue
        if field_name in ENUM_FIELDS:
            labels = ENUM_FIELDS[field_name]
            row[field_name] = labels.index(value) if value in labels else len(labels) - 1
        elif field_name in BOOL_FIELDS:
            row[field_name] = bool(value)
        else:
            row[field_name] = float(value) if value is not None else math.nan
    return row


def _metadata(field_name: str, unit: str | None) -> zelos_sdk.TraceEventFieldMetadata:
    if field_name in ENUM_FIELDS:
        return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Int8)
    if field_name in BOOL_FIELDS:
        return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Boolean)
    return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Float64, unit)
