"""Readings to trace events."""

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import zelos_sdk
from zelos_sdk import schemas

from .config import PollRow, poll_key
from .naming import MAX_BYTES, Namer
from .profile import BOOL_FIELDS, ENUM_FIELDS, Profile

logger = logging.getLogger(__name__)

STATUS_FIELDS = {"rtt_ms": "ms", "rate_hz": "Hz", "errors": None, "connected": None}


@dataclass(slots=True)
class _Instrument:
    channels: dict[int, zelos_sdk.TraceSourceEvent]
    functions: dict[str, zelos_sdk.TraceSourceEvent]
    status: zelos_sdk.TraceSourceEvent
    log: zelos_sdk.TraceSourceEvent
    poll: zelos_sdk.TraceSourceEvent | None
    poll_fields: dict[str, str]
    fields: dict[str, str | None]
    warned: set[str] = field(default_factory=set)


class Emitter:
    """Names and writes the trace events of every instrument; the worker decides time."""

    def __init__(self, source: zelos_sdk.TraceSource) -> None:
        self._source = source
        # Each instrument is touched only by its own worker thread.
        self._instruments: dict[str, _Instrument] = {}

    def add_instrument(
        self, name: str, profile: Profile, channels: int, poll: tuple[PollRow, ...] = ()
    ) -> None:
        """Register `<name>/ch<n>`, the DMM function events, status, log and the poll table."""
        # A meter's reading lands under its function's event, not on the channel.
        units = {k: v for k, v in profile.fields.items() if profile.dmm is None or k != "value"}
        channel_events: dict[int, zelos_sdk.TraceSourceEvent] = {}
        fields = [_metadata(field_name, unit) for field_name, unit in units.items()]
        for ch in range(1, channels + 1):
            event_name = f"{name}/ch{ch}"
            if fields:
                channel_events[ch] = self._source.add_event(event_name, fields)
                for field_name in units:
                    if field_name in ENUM_FIELDS:
                        self._source.add_value_table(
                            event_name, field_name, dict(enumerate(ENUM_FIELDS[field_name]))
                        )
        functions: dict[str, zelos_sdk.TraceSourceEvent] = {}
        if profile.dmm is not None:
            for label, unit in profile.dmm.functions.items():
                functions[label] = self._source.add_event(
                    f"{name}/{label}",
                    [zelos_sdk.TraceEventFieldMetadata("value", zelos_sdk.DataType.Float64, unit)],
                )
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
        self._instruments[name] = _Instrument(
            channel_events, functions, status, log, poll_event, poll_fields, dict(units)
        )

    def row(self, name: str, ch: int, values: dict[str, Any], time_ns: int) -> None:
        """Write the fields of one channel read at `time_ns`; labels become their table index."""
        inst = self._instruments[name]
        event = inst.channels.get(ch)
        if event is None or not values:
            return
        row: dict[str, Any] = {}
        for field_name, value in values.items():
            if field_name not in inst.fields:
                continue
            if field_name in ENUM_FIELDS:
                labels = ENUM_FIELDS[field_name]
                row[field_name] = labels.index(value) if value in labels else len(labels) - 1
            elif field_name in BOOL_FIELDS:
                row[field_name] = bool(value)
            else:
                row[field_name] = float(value) if value is not None else math.nan
        if row:
            event.log_at(time_ns, **row)

    def reading(self, name: str, function: str, value: float, time_ns: int) -> None:
        """A DMM reading, under the active function's event."""
        inst = self._instruments[name]
        event = inst.functions.get(function)
        if event is None:
            if function not in inst.warned:
                inst.warned.add(function)
                logger.warning(
                    "%s: no event for function %s; its readings are dropped", name, function
                )
            return
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
        return dict(self._instruments[name].fields)


def _metadata(field_name: str, unit: str | None) -> zelos_sdk.TraceEventFieldMetadata:
    if field_name in ENUM_FIELDS:
        return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Int8)
    if field_name in BOOL_FIELDS:
        return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Boolean)
    return zelos_sdk.TraceEventFieldMetadata(field_name, zelos_sdk.DataType.Float64, unit)
