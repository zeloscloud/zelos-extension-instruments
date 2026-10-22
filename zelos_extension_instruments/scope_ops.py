"""What a worker does with an oscilloscope: read its measurement slots, show a measurement in
one, and fetch a channel's samples.

The slots are the instrument's own: which are on, what each measures and from which channel is
read back every slow pass, and only an explicit Measure request changes them. A scope measured
directly has no slots: the worker keeps the (channel, field) pairs Measure added, under numbers of
its own, and asks for each one every fast pass.
"""

from __future__ import annotations

import math
from collections.abc import Generator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from . import engine
from .clock import NS_PER_S
from .engine import ParseError, RangeError
from .link import LinkTimeout
from .profile_scope import MEASUREMENT_FIELDS, Linear, Measure, Part, Scope, Waveform
from .waveform import (
    BlockError,
    LinearScale,
    decode_wavedesc,
    linear_codes,
    linear_offsets_ns,
    linear_samples,
    sample_offsets_ns,
    samples,
)

if TYPE_CHECKING:
    from .worker import InstrumentWorker

# Samples per capture unless the request says otherwise.
DEFAULT_POINTS = 10_000
# A transfer rate every link manages, and the most a capture may wait, under the action's own.
_SLOW_LINK_BPS = 500_000
_CAPTURE_MAX_S = 55.0


Slots = dict[int, tuple[int, str]]


def scan_slots(worker: InstrumentWorker) -> Slots:
    """Every slot that is on and understood, read now in one go; marks the slots in use."""
    worker._slot_scans += 1
    steps = slot_steps(worker)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            live, worker._busy_slots = done.value
            return live


def slot_steps(worker: InstrumentWorker) -> Generator[None, None, tuple[Slots, set[int]]]:
    """The slot scan one query at a time: slot number to (channel, measurement field) for every
    slot that is on and understood, and the slots in use. A scope measured directly keeps the
    pairs it has."""
    profile = worker.profile
    assert profile is not None and profile.scope is not None
    if _direct(profile.scope) is not None:
        return dict(worker._slots), set()
    slots = profile.scope.slots
    if slots is None:
        return {}, set()
    if slots.ready_query is not None:
        try:
            ready = worker._query(slots.ready_query).strip().casefold()
        except LinkTimeout as e:
            worker._recover(f"{slots.ready_query}: {e}")
            return {}, set()
        yield
        if ready != str(slots.ready_value).casefold():
            # Another measurement mode is on: every slot may be in use for all we know.
            return {}, set(range(1, slots.count + 1))
    candidates: Iterable[int] = range(1, slots.count + 1)
    if slots.list_query is not None:
        try:
            reply = worker._query(slots.list_query)
        except LinkTimeout as e:
            worker._recover(f"{slots.list_query}: {e}")
            return {}, set()
        yield
        candidates = slots.listed(reply)
    live: Slots = {}
    busy: set[int] = set()
    for slot in candidates:
        try:
            if slots.enabled is not None:
                on = engine.parse_bool(worker._query(engine.render(slots.enabled, slot=slot)))
                yield
                if not on:
                    continue
            busy.add(slot)
            word = worker._query(engine.render(slots.type, slot=slot)).strip().casefold()
            yield
            source = worker._query(engine.render(slots.source, slot=slot)).strip()
            yield
        except LinkTimeout as e:
            worker._recover(f"slot {slot}: {e}")
            return live, busy
        except ParseError as e:
            worker._log_once(f"slot {slot}", f"slot {slot}: {e}")
            worker._recover(f"slot {slot}: {e}")
            continue
        field_name = slots.types.get(word)
        found = slots.source_pattern.search(source)
        if field_name is None or found is None:
            worker._log_once(
                f"slot {slot} {word} {source}",
                f"slot {slot} measures {word} on {source}, which is not recorded",
            )
            continue
        ch = int(found.group("ch"))
        if ch in worker._channel:
            live[slot] = (ch, field_name)
    return live, busy


def read_slots(worker: InstrumentWorker, rows: dict[int | None, dict[str, Any]]) -> None:
    """One value per live slot into `rows`, under its channel; a bad reply is not-a-number."""
    profile = worker.profile
    assert profile is not None and profile.scope is not None
    direct = _direct(profile.scope)
    if direct is not None:
        for ch, field_name in list(worker._slots.values()):
            try:
                value = _ask_direct(worker, direct, ch, field_name)
            except LinkTimeout as e:
                worker._recover(f"{field_name} on ch{ch}: {e}")
                return
            rows.setdefault(ch, {})[field_name] = value
        return
    slots = profile.scope.slots
    assert slots is not None
    for slot, (ch, field_name) in list(worker._slots.items()):
        try:
            reply = worker._query(engine.render(slots.value, slot=slot))
        except LinkTimeout as e:
            worker._recover(f"slot {slot}: {e}")
            return
        try:
            value = engine.parse_float(reply)
        except ParseError as e:
            worker._log_once(f"slot value {slot}", f"slot {slot}: {e}")
            worker._recover(f"slot {slot}: {e}")
            return
        rows.setdefault(ch, {})[field_name] = value


def measure(worker: InstrumentWorker, ch: int, args: dict[str, Any]) -> dict[str, Any]:
    """Show a measurement of `ch` in a slot, or ask for it directly, and record it from now on."""
    profile = worker.profile
    assert profile is not None
    scope = profile.scope
    direct = _direct(scope) if scope is not None else None
    if direct is None and (scope is None or scope.measure is None or scope.slots is None):
        raise RangeError(f"the profile {profile.id} cannot configure measurements")
    field_name = str(args.get("type") or "")
    if field_name not in MEASUREMENT_FIELDS:
        raise RangeError(f"type must be one of {', '.join(MEASUREMENT_FIELDS)}")
    if direct is not None:
        return _measure_direct(worker, direct, ch, field_name, bool(args.get("off")))
    assert scope is not None and scope.measure is not None and scope.slots is not None
    word = scope.slots.words.get(field_name)
    if word is None:
        raise RangeError(f"the profile {profile.id} has no measurement for {field_name}")
    slot = args.get("slot")
    listed = scope.slots.list_query is not None
    if slot is None or listed:
        # What the slots show right now, not what the last slow pass saw.
        worker._slots = scan_slots(worker)
    if slot is None:
        slot = _free_slot(worker, ch, field_name)
    slot = int(slot)
    if not 1 <= slot <= scope.slots.count:
        raise RangeError(f"slot must be from 1 to {scope.slots.count}")
    if args.get("off"):
        if scope.measure.off is None:
            raise RangeError(f"the profile {profile.id} cannot clear a slot")
        worker._send(engine.render(scope.measure.off, slot=slot))
        worker._after_write(f"slot {slot} off")
        # A slow-round scan already under way read the slot as on; it must not put it back.
        worker._slot_scans += 1
        worker._slots.pop(slot, None)
        worker._busy_slots.discard(slot)
        return {"slot": slot, "off": True}
    if listed and slot in worker._busy_slots:
        if worker._slots.get(slot) != (ch, field_name):
            # A slot that exists is deleted before it is created again.
            if scope.measure.off is None:
                raise RangeError(f"slot {slot} is in use and the profile cannot clear a slot")
            worker._send(engine.render(scope.measure.off, slot=slot))
        else:
            return _shown(worker, scope, slot, ch, field_name)
    for command in scope.measure.commands:
        worker._send(engine.render(command, slot=slot, ch=ch, type=word))
    worker._after_write(f"slot {slot} measures {field_name} on ch{ch}")
    worker._slots = scan_slots(worker)
    return _shown(worker, scope, slot, ch, field_name)


def _shown(
    worker: InstrumentWorker, scope: Scope, slot: int, ch: int, field_name: str
) -> dict[str, Any]:
    """The slot's measurement and its value now, once the slot shows what was asked."""
    assert scope.slots is not None
    if worker._slots.get(slot) != (ch, field_name):
        raise RangeError(f"slot {slot} did not take the measurement")
    value: float | None = None
    with suppress(LinkTimeout, ParseError):
        value = engine.parse_float(worker._query(engine.render(scope.slots.value, slot=slot)))
    return {"slot": slot, "channel": ch, "type": field_name, "value": value}


def _direct(scope: Scope) -> Measure | None:
    """The scope's measurement table when each measurement is asked for directly."""
    measure = scope.measure
    return measure if measure is not None and measure.query is not None else None


def _ask_direct(worker: InstrumentWorker, direct: Measure, ch: int, field_name: str) -> float:
    """One direct measurement now; a reply that holds no number is not-a-number."""
    assert direct.query is not None
    query = engine.render(direct.query, ch=ch, type=direct.words[field_name])
    reply = worker._query(query)
    try:
        if direct.index is not None:
            items = reply.split(",")
            if direct.index >= len(items):
                raise ParseError(f"expected at least {direct.index + 1} values, got {reply!r}")
            reply = items[direct.index]
        return engine.parse_float(reply)
    except ParseError as e:
        worker._log_once(f"measure {query}", f"{query}: {e}")
        return math.nan


def _measure_direct(
    worker: InstrumentWorker, direct: Measure, ch: int, field_name: str, off: bool
) -> dict[str, Any]:
    """Record a measurement asked for directly from now on, or stop recording it."""
    profile = worker.profile
    assert profile is not None
    if field_name not in direct.words:
        raise RangeError(f"the profile {profile.id} has no measurement for {field_name}")
    number = next((n for n, pair in worker._slots.items() if pair == (ch, field_name)), None)
    if off:
        if number is not None:
            del worker._slots[number]
        return {"slot": number, "channel": ch, "type": field_name, "off": True}
    try:
        value = _ask_direct(worker, direct, ch, field_name)
    except LinkTimeout as e:
        worker._recover(f"{field_name} on ch{ch}: {e}")
        raise RangeError(f"the instrument did not measure {field_name} on ch{ch}: {e}") from e
    worker._after_write(f"ch{ch} measures {field_name}")
    if number is None:
        number = max(worker._slots, default=0) + 1
        worker._slots[number] = (ch, field_name)
    return {
        "slot": number,
        "channel": ch,
        "type": field_name,
        "value": None if math.isnan(value) else value,
    }


def _free_slot(worker: InstrumentWorker, ch: int, field_name: str) -> int:
    """The slot already showing this measurement, else the lowest slot that is off."""
    profile = worker.profile
    assert profile is not None and profile.scope is not None and profile.scope.slots is not None
    for slot, assignment in worker._slots.items():
        if assignment == (ch, field_name):
            return slot
    for slot in range(1, profile.scope.slots.count + 1):
        if slot not in worker._slots and slot not in worker._busy_slots:
            return slot
    raise RangeError("every slot shows something; give a slot to replace")


def capture(worker: InstrumentWorker, ch: int, args: dict[str, Any]) -> dict[str, Any]:
    """Fetch the samples of `ch` on screen and record them as one burst.

    The trigger instant is placed at the moment the data was requested; the samples around it keep
    the scope's own spacing.
    """
    profile = worker.profile
    assert profile is not None and worker._link is not None
    scope = profile.scope
    if scope is None or scope.waveform is None:
        raise RangeError(f"the profile {profile.id} cannot fetch waveforms")
    wf = scope.waveform
    points = int(args.get("points") or DEFAULT_POINTS)
    if points < 1:
        raise RangeError("points must be at least 1")
    available: int | None = None
    if wf.points is not None:
        available = int(engine.parse_float(worker._query(wf.points)))
    if wf.max_points is not None:
        points = min(points, int(engine.parse_float(worker._query(wf.max_points))))
    interval = max(1, math.ceil(available / points)) if available else 1
    for command in wf.setup:
        worker._send(engine.render(command, ch=ch, points=points, interval=interval))
    if wf.linear is None:
        fetched = _fetch_wavedesc(worker, wf, scope.grid, ch, points)
    else:
        fetched = _fetch_linear(worker, wf, wf.linear, ch, points, interval)
    done = worker._now()
    values, offsets, sent = fetched.values, fetched.offsets, fetched.sent
    worker._emitter.waveform(worker.cfg.name, ch, offsets + worker._clock.epoch_ns(sent), values)
    worker._after_write(f"captured {len(values)} samples of ch{ch}", log=False)
    per_div = "" if fetched.volts_per_div is None else f", {fetched.volts_per_div:g} V/div"
    worker._note(
        "info",
        f"ch{ch} waveform: {len(values)} samples, {fetched.sample_interval_s:g} s apart{per_div}, "
        f"in {(done - sent) / NS_PER_S:.2f} s",
    )
    return {
        "channel": ch,
        "samples": len(values),
        "sample_interval_s": fetched.sample_interval_s,
        "first_s": float(offsets[0]) / NS_PER_S if len(offsets) else None,
        "last_s": float(offsets[-1]) / NS_PER_S if len(offsets) else None,
        "minimum": float(values.min()) if len(values) else None,
        "maximum": float(values.max()) if len(values) else None,
        "volts_per_div": fetched.volts_per_div,
        "fetched_in_s": round((done - sent) / NS_PER_S, 3),
    }


@dataclass(frozen=True, slots=True)
class _Fetched:
    """A capture's samples, their offsets from the trigger, and when the data was requested."""

    values: npt.NDArray[np.float64]
    offsets: npt.NDArray[np.int64]
    sent: int
    sample_interval_s: float
    volts_per_div: float | None


def _fetch_wavedesc(
    worker: InstrumentWorker, wf: Waveform, grid: int, ch: int, points: int
) -> _Fetched:
    """A WAVEDESC descriptor block, then the data block it scales."""
    profile = worker.profile
    assert profile is not None and worker._link is not None and wf.preamble is not None
    reader = profile.reader("timebase")
    if reader is None or reader.query is None:
        raise RangeError("the profile reads no timebase, which places the samples in time")
    timebase = engine.parse_float(worker._query(reader.query))
    worker._selector.forget()
    try:
        worker._link.write(engine.render(wf.preamble, ch=ch, points=points))
        desc = decode_wavedesc(worker._link.read_block())
        sent = worker._now()
        worker._link.write(engine.render(wf.data, ch=ch, points=points))
        if wf.skip:
            worker._link.read_bytes(wf.skip)
        expected = min(desc.array_bytes or desc.count * desc.bytes_per_sample, points * 2)
        payload = worker._link.read_block(_transfer_s(expected))
    except BlockError as e:
        worker._clear_link()
        raise RangeError(f"the waveform could not be read: {e}") from e
    values = samples(desc, payload)
    return _Fetched(
        values=values,
        offsets=sample_offsets_ns(desc, len(values), timebase, grid),
        sent=sent,
        sample_interval_s=desc.horizontal_interval * desc.step,
        volts_per_div=desc.vertical_gain * desc.probe,
    )


def _fetch_linear(
    worker: InstrumentWorker, wf: Waveform, linear: Linear, ch: int, points: int, interval: int
) -> _Fetched:
    """The preamble line and the queries the scaling terms name, then the data block."""
    assert worker._link is not None
    fields: list[str] = []
    if wf.preamble is not None:
        reply = worker._query(engine.render(wf.preamble, ch=ch, points=points))
        fields = [item.strip().strip('"') for item in reply.split(",")]
    asked: dict[str, float] = {}

    def number(part: Part) -> float:
        if part.value is not None:
            return part.value
        if part.field is not None:
            if part.field >= len(fields):
                raise ParseError(f"the preamble has no field {part.field}: {fields}")
            return engine.parse_float(fields[part.field])
        assert part.query is not None
        query = engine.render(part.query, ch=ch, points=points)
        if query not in asked:
            asked[query] = engine.parse_float(worker._query(query))
        return asked[query]

    try:
        terms = {
            name: sum(part.apply(number(part), interval) for part in parts)
            for name, parts in linear.terms().items()
        }
    except ParseError as e:
        raise RangeError(f"the waveform scale could not be read: {e}") from e
    scale = LinearScale(**terms)
    if not all(map(math.isfinite, terms.values())) or scale.x_inc <= 0 or not scale.y_mult:
        raise RangeError(f"the waveform scale is not usable: {terms}")
    worker._selector.forget()
    sent = worker._now()
    try:
        worker._link.write(engine.render(wf.data, ch=ch, points=points))
        if wf.skip:
            worker._link.read_bytes(wf.skip)
        payload = worker._link.read_block(_transfer_s(points * int(linear.encoding[1])))
    except BlockError as e:
        worker._clear_link()
        raise RangeError(f"the waveform could not be read: {e}") from e
    values = linear_samples(scale, linear_codes(payload, linear.encoding, linear.byte_order))
    return _Fetched(
        values=values,
        offsets=linear_offsets_ns(scale, len(values)),
        sent=sent,
        sample_interval_s=scale.x_inc,
        volts_per_div=None,
    )


def _transfer_s(expected_bytes: int) -> float:
    """How long a data block may take: millions of samples outlast a reply's timeout."""
    return min(_CAPTURE_MAX_S, 5.0 + expected_bytes / _SLOW_LINK_BPS)
