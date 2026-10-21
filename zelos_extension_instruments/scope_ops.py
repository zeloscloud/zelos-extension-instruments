"""What a worker does with an oscilloscope: read its measurement slots, show a measurement in
one, and fetch a channel's samples.

The slots are the instrument's own: which are on, what each measures and from which channel is
read back every slow pass, and only an explicit Measure request changes them.
"""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from . import engine
from .engine import ParseError, RangeError
from .link import LinkTimeout
from .profile_scope import MEASUREMENT_FIELDS
from .waveform import BlockError, decode_wavedesc, sample_times_s, samples

if TYPE_CHECKING:
    from .worker import InstrumentWorker

# Samples per capture unless the request says otherwise.
DEFAULT_POINTS = 10_000
# A transfer rate every link manages, and the most a capture may wait, under the action's own.
_SLOW_LINK_BPS = 500_000
_CAPTURE_MAX_S = 55.0
_S = 1_000_000_000


def scan_slots(worker: InstrumentWorker) -> dict[int, tuple[int, str]]:
    """Slot number to (channel, measurement field) for every slot that is on and understood."""
    profile = worker.profile
    assert profile is not None and profile.scope is not None
    slots = profile.scope.slots
    if slots is None:
        return {}
    worker._busy_slots = set()
    if slots.ready_query is not None:
        try:
            ready = worker._query(slots.ready_query).strip().casefold()
        except LinkTimeout as e:
            worker._recover(f"{slots.ready_query}: {e}")
            return {}
        if ready != str(slots.ready_value).casefold():
            # Another measurement mode is on: every slot may be in use for all we know.
            worker._busy_slots = set(range(1, slots.count + 1))
            return {}
    live: dict[int, tuple[int, str]] = {}
    for slot in range(1, slots.count + 1):
        try:
            if not engine.parse_bool(worker._query(engine.render(slots.enabled, slot=slot))):
                continue
            worker._busy_slots.add(slot)
            word = worker._query(engine.render(slots.type, slot=slot)).strip().casefold()
            source = worker._query(engine.render(slots.source, slot=slot)).strip()
        except LinkTimeout as e:
            worker._recover(f"slot {slot}: {e}")
            return live
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
    return live


def read_slots(worker: InstrumentWorker, rows: dict[int | None, dict[str, Any]]) -> None:
    """One value per live slot into `rows`, under its channel; a bad reply is not-a-number."""
    profile = worker.profile
    assert profile is not None and profile.scope is not None and profile.scope.slots is not None
    slots = profile.scope.slots
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
    """Show a measurement of `ch` in a slot and record it from now on."""
    profile = worker.profile
    assert profile is not None
    scope = profile.scope
    if scope is None or scope.measure is None or scope.slots is None:
        raise RangeError(f"the profile {profile.id} cannot configure measurements")
    field_name = str(args.get("type") or "")
    if field_name not in MEASUREMENT_FIELDS:
        raise RangeError(f"type must be one of {', '.join(MEASUREMENT_FIELDS)}")
    word = scope.slots.words.get(field_name)
    if word is None:
        raise RangeError(f"the profile {profile.id} has no measurement for {field_name}")
    slot = args.get("slot")
    if slot is None:
        # What the slots show right now, not what the last slow pass saw.
        worker._slots = scan_slots(worker)
        slot = _free_slot(worker, ch, field_name)
    slot = int(slot)
    if not 1 <= slot <= scope.slots.count:
        raise RangeError(f"slot must be from 1 to {scope.slots.count}")
    if args.get("off"):
        if scope.measure.off is None:
            raise RangeError(f"the profile {profile.id} cannot clear a slot")
        worker._send(engine.render(scope.measure.off, slot=slot))
        worker._after_write(f"slot {slot} off")
        worker._slots.pop(slot, None)
        return {"slot": slot, "off": True}
    for command in scope.measure.commands:
        worker._send(engine.render(command, slot=slot, ch=ch, type=word))
    worker._after_write(f"slot {slot} measures {field_name} on ch{ch}")
    worker._slots = scan_slots(worker)
    if worker._slots.get(slot) != (ch, field_name):
        raise RangeError(f"slot {slot} did not take the measurement")
    value: float | None = None
    with suppress(LinkTimeout, ParseError):
        value = engine.parse_float(worker._query(engine.render(scope.slots.value, slot=slot)))
    return {"slot": slot, "channel": ch, "type": field_name, "value": value}


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
    reader = profile.reader("timebase")
    if reader is None or reader.query is None:
        raise RangeError("the profile reads no timebase, which places the samples in time")
    timebase = engine.parse_float(worker._query(reader.query))
    worker._selector.forget()
    try:
        worker._link.write(wf.preamble)
        desc = decode_wavedesc(worker._link.read_block())
        sent = worker._now()
        worker._link.write(wf.data)
        # A capture of millions of samples needs more than a reply's timeout: allow a slow
        # link's worth of transfer time, within what the action waits.
        expected = min(desc.array_bytes or desc.count * desc.bytes_per_sample, points * 2)
        payload = worker._link.read_block(min(_CAPTURE_MAX_S, 5.0 + expected / _SLOW_LINK_BPS))
    except BlockError as e:
        worker._clear_link()
        raise RangeError(f"the waveform could not be read: {e}") from e
    done = worker._now()
    values = samples(desc, payload)
    times = sample_times_s(desc, len(values), timebase, scope.grid)
    anchor = worker._clock.epoch_ns(sent)
    burst = [(anchor + round(t * _S), v) for t, v in zip(times, values, strict=True)]
    worker._emitter.waveform(worker.cfg.name, ch, burst)
    worker._after_write(f"captured {len(values)} samples of ch{ch}", log=False)
    worker._note(
        "info",
        f"ch{ch} waveform: {len(values)} samples, "
        f"{desc.horizontal_interval * desc.step:g} s apart, "
        f"{desc.vertical_gain * desc.probe:g} V/div, in {(done - sent) / _S:.2f} s",
    )
    return {
        "channel": ch,
        "samples": len(values),
        "sample_interval_s": desc.horizontal_interval * desc.step,
        "first_s": times[0] if times else None,
        "last_s": times[-1] if times else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "volts_per_div": desc.vertical_gain * desc.probe,
        "fetched_in_s": round((done - sent) / _S, 3),
    }
