"""What a worker does for a request: sets with their readback, reads, raw SCPI, clear and the
state report. Functions of the worker, run on its thread between poll batches."""

from __future__ import annotations

import base64
import dataclasses
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import daq_ops, engine, scope_ops
from .config import poll_key
from .engine import ParseError, RangeError
from .link import LinkError
from .profile import (
    COMMAND_SETTERS,
    INSTRUMENT_FIELDS,
    LABEL_SETTERS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    Master,
    Range,
    Setter,
    System,
)

if TYPE_CHECKING:
    from .worker import InstrumentWorker

# A block reply the SCPI action returns whole, as base64; longer ones return their size.
_BLOCK_RETURN_MAX = 65536
# Set before the rest of a request: the others' commands name what these choose.
_FIRST = ("function", "trigger_source", "modulation_type")
_CHANNEL_SOURCE = re.compile(r"ch(\d+)")


def setter_named(worker: InstrumentWorker, name: str) -> Setter:
    assert worker.profile is not None
    setter = worker.profile.setters.get(name)
    if setter is None:
        raise RangeError(f"the profile {worker.profile.id} cannot set {name}")
    return setter


def set_many(worker: InstrumentWorker, ch: int, args: dict[str, Any]) -> dict[str, Any]:
    wanted = {k: v for k, v in args.items() if v is not None and k != "channel"}
    if not wanted:
        raise RangeError("give at least one value to set")
    for name, value in wanted.items():
        # Every value is checked before any goes out, so a refusal leaves nothing changed.
        check_setting(worker, ch, name, value)
    order = sorted(wanted, key=lambda n: n not in _FIRST)
    remembered: str | None = None
    if "modulation" in wanted and not wanted["modulation"]:
        # Choosing a type switches some generators' modulation on, so it is only remembered.
        order = ["modulation", *(n for n in order if n not in ("modulation", "modulation_type"))]
        if "modulation_type" in wanted:
            assert worker.profile is not None
            remembered = str(wanted["modulation_type"])
            setter = setter_named(worker, "modulation_type")
            mark_own(worker, ch, worker.profile.verify_field(setter), remembered)
    results: dict[str, Any] = {}
    try:
        for name in order:
            value = wanted[name]
            if name in VALUE_SETTERS:
                results[name] = set_value(worker, ch, name, float(value))
            elif name == "function":
                results[name] = set_function(worker, ch, str(value))["function"]
            elif name in SWITCH_SETTERS:
                results[name] = set_switch(worker, ch, name, bool(value))[name]
            else:
                results[name] = set_label(worker, ch, name, str(value))[name]
    except (RangeError, ParseError, LinkError) as e:
        if results:
            raise RangeError(f"{e}; already set: {', '.join(results)}") from e
        raise
    if remembered is not None:
        results["modulation_type"] = remembered
    return results


def check_setting(worker: InstrumentWorker, ch: int, name: str, value: Any) -> None:
    assert worker.profile is not None
    if name in VALUE_SETTERS:
        setter = setter_named(worker, name)
        key = worker.profile.range_key(setter)
        range_ = worker._channel[ch].ranges.get(key) if key else None
        limit = worker.cfg.limits.for_key(key or "")
        if _is_meter_range(worker, name) and range_ is None and limit is None:
            # The meter refuses a range it does not have, and the readback shows what it chose.
            if not math.isfinite(float(value)):
                raise RangeError(f"{name} must be a finite number")
        elif range_ is not None or limit is not None or worker.profile.category != "scope":
            # An unranged scope setting harms nothing, but an output's level must be bounded.
            engine.check_range(float(value), range_, limit, name)
        engine.check_resolution(float(value), range_, name)
    elif name == "function":
        setter = setter_named(worker, "function")
        if str(value) not in setter.map:
            raise RangeError(f"function must be one of {', '.join(setter.map)}")
    elif name in SWITCH_SETTERS:
        setter_named(worker, name)
        if name == "relay" and ch not in worker.cfg.relays:
            raise RangeError(f"channel {ch} is not a relay; list it under Relays in the config")
    elif name in LABEL_SETTERS:
        setter = setter_named(worker, name)
        if str(value) not in setter.map:
            raise RangeError(f"{name} must be one of {', '.join(setter.map)}")
    else:
        raise RangeError(f"{name} is not something that can be set")


def set_label(worker: InstrumentWorker, ch: int, name: str, label: str) -> dict[str, Any]:
    """Set an enumerated setting by its label; the readback must agree."""
    assert worker.profile is not None
    setter = setter_named(worker, name)
    word = setter.map.get(label)
    if word is None:
        raise RangeError(f"{name} must be one of {', '.join(setter.map)}")
    fill = fills(worker, setter, ch)
    worker._select(ch if setter.per_channel else None)
    worker._send(
        engine.render(setter.command, ch=ch if setter.per_channel else None, value=word, **fill)
    )
    worker._after_write(f"set ch{ch} {name} {label}")
    readback = read_back(worker, setter, ch, fill=fill)
    if readback is not None and readback != label:
        raise RangeError(f"{name} reads back {readback}")
    # Without a readback the label sent is the best known: a later command may name it.
    mark_own(worker, ch, worker.profile.verify_field(setter), label)
    if readback is None:
        return {name: label, "unverified": "the profile has no readback"}
    return {name: label}


def run_command(worker: InstrumentWorker, name: str, ch: int) -> dict[str, Any]:
    """Send a setter that is a bare command, such as a scope's run or a generator's trigger;
    a per-channel one goes to `ch`."""
    if name not in COMMAND_SETTERS or name == "clear":
        raise RangeError(f"{name} is not a command of this instrument")
    setter = setter_named(worker, name)
    fill = fills(worker, setter, ch)
    target = ch if setter.per_channel else None
    worker._select(target)
    for command in setter.commands:
        worker._send(engine.render(command, ch=target, **fill))
    worker._after_write(name)
    return {"ok": True, "command": name}


def set_value(worker: InstrumentWorker, ch: int, name: str, value: float) -> dict[str, Any]:
    assert worker.profile is not None
    setter = setter_named(worker, name)
    key = worker.profile.range_key(setter)
    range_ = worker._channel[ch].ranges.get(key) if key else None
    fill = fills(worker, setter, ch)
    worker._select(ch if setter.per_channel else None)
    worker._send(command_of(worker, setter, ch, fill, value=value, range_=range_))
    worker._after_write(f"set ch{ch} {name} {engine.format_value(value, range_)}")
    # A bare verify query answers a number in the instrument's unit.
    readback = read_back(
        worker, setter, ch, lambda reply: engine.parse_float(reply) * setter.scale, fill
    )
    if readback is None:
        return {"set": value, "readback": None, "unverified": "the profile has no readback"}
    readback = float(readback)
    if _is_meter_range(worker, name):
        # The meter picks the smallest range that covers the value asked for.
        agrees = readback >= value * (1 - 1e-9)
    else:
        agrees = engine.verified(value, readback, range_)
    if not agrees:
        raise RangeError(
            f"{name} reads back {readback:g} after setting {value:g}; "
            "the instrument may have clipped it or refused it"
        )
    mark_own(worker, ch, worker.profile.verify_field(setter), readback)
    return {"set": value, "readback": readback}


def set_switch(worker: InstrumentWorker, ch: int, name: str, on: bool) -> dict[str, Any]:
    assert worker.profile is not None
    setter = setter_named(worker, name)
    master = setter.master if on else None
    if master is not None and engine.parse_bool(worker._query(master.query)):
        master = None
    if master is not None:
        # Raising the master switches on every armed channel, not only this one.
        armed = [
            n for n in range(1, worker.channels + 1) if n != ch and is_armed(worker, master, n)
        ]
        if armed:
            names = ", ".join(f"ch{n}" for n in armed)
            raise RangeError(
                f"the master output is off, and raising it would also switch on {names}; "
                "switch those off first"
            )
    fill = fills(worker, setter, ch)
    worker._select(ch if setter.per_channel else None)
    worker._send(command_of(worker, setter, ch, fill, on=on))
    if master is not None:
        worker._send(engine.render(master.command, on=True))
    worker._after_write(f"set ch{ch} {name} {'on' if on else 'off'}")
    readback = read_back(worker, setter, ch, engine.parse_bool, fill)
    if readback is None:
        return {name: on}
    if bool(readback) != on:
        raise RangeError(f"{name} reads back {'on' if readback else 'off'}")
    mark_own(worker, ch, worker.profile.verify_field(setter), bool(readback))
    return {name: bool(readback)}


def is_armed(worker: InstrumentWorker, master: Master, ch: int) -> bool:
    worker._select(ch)
    return engine.parse_bool(worker._query(engine.render(master.armed, ch=ch)))


def set_function(worker: InstrumentWorker, ch: int, function: str) -> dict[str, Any]:
    assert worker.profile is not None
    setter = setter_named(worker, "function")
    worker._select(ch if setter.per_channel else None)
    worker._send(engine.setter_command(setter, ch=ch, function=function))
    worker._after_write(f"set ch{ch} function {function}")
    label = read_back(worker, setter, ch)
    if label is None:
        if worker.profile.category == "dmm":
            worker._function = function
        return {"function": function, "unverified": "the profile has no readback"}
    if label != function:
        raise RangeError(f"function reads back {label}")
    mark_own(worker, ch, worker.profile.verify_field(setter), label)
    if worker.profile.category == "dmm":
        worker._function = label
    return {"function": function}


def read_back(
    worker: InstrumentWorker,
    setter: Setter,
    ch: int,
    bare: Callable[[str], Any] | None = None,
    fill: dict[str, Any] | None = None,
) -> Any:
    """What the instrument reads back after a set, or None when the profile has no readback or
    the reply leaves the field out.

    The state field's reader parses the reply when the verify query is its query, or when no
    `bare` parser is given; otherwise `bare` does.
    """
    profile = worker.profile
    assert profile is not None
    verify = profile.verify_query(setter)
    field_name = profile.verify_field(setter)
    reader = profile.reader(field_name)
    if verify is None or (reader is None and bare is None):
        return None
    reply = worker._query(engine.render(verify, ch=ch, **(fill or {})))
    if reader is not None and (bare is None or reader.query == verify):
        return engine.parse_reply(reader, reply, ch).get(field_name)
    assert bare is not None
    return bare(reply)


def fills(worker: InstrumentWorker, setter: Setter, ch: int) -> dict[str, Any]:
    """What a setter's commands name beyond the channel, value and switch: the scope's trigger
    source, a generator channel's modulation word, or the root word of a meter's function."""
    assert worker.profile is not None
    verify = worker.profile.verify_query(setter) or ""
    text = " ".join((*setter.commands, setter.off or "", verify))
    found: dict[str, Any] = {}
    if "{src}" in text:
        found["src"] = trigger_source(worker)
    if "{modulation}" in text:
        found["modulation"] = modulation_word(worker, ch)
    if "{function}" in text and setter.name != "function":
        found["function"] = function_word(worker, setter)
    return found


def command_of(
    worker: InstrumentWorker,
    setter: Setter,
    ch: int,
    fill: dict[str, Any],
    *,
    value: float | None = None,
    range_: Range | None = None,
    on: bool | None = None,
) -> str:
    """A value or switch setter's command, with what `fills` found in place."""
    word = fill.get("function")
    function = None
    if word is not None:
        # The engine maps a function label to its word; this word was already chosen.
        function = worker._function
        setter = dataclasses.replace(setter, map={function: word})
    return engine.setter_command(
        setter,
        ch=ch,
        value=value,
        range_=range_,
        on=on,
        function=function,
        src=fill.get("src"),
        modulation=fill.get("modulation"),
    )


def trigger_source(worker: InstrumentWorker) -> int:
    """The number of the channel the scope triggers on."""
    label = worker._acquisition.get("trigger_source")
    if label is None:
        raise RangeError("the trigger source is not known yet")
    found = _CHANNEL_SOURCE.fullmatch(str(label))
    if found is None:
        raise RangeError("the trigger source is not a channel")
    return int(found.group(1))


def modulation_word(worker: InstrumentWorker, ch: int) -> str:
    """The instrument's word for the modulation type `ch` has now."""
    assert worker.profile is not None
    label = worker._channel[ch].values.get("modulation_type")
    setter = worker.profile.setters.get("modulation_type")
    word = setter.map.get(label) if setter is not None and label is not None else None
    if word is None:
        raise RangeError(f"the modulation type of ch{ch} is not known")
    return word


def function_word(worker: InstrumentWorker, setter: Setter) -> str:
    """The root word of the meter's current function: the setter's own map, else the meter's."""
    profile = worker.profile
    assert profile is not None
    words = setter.map or (profile.dmm.words if profile.dmm is not None else {})
    word = words.get(worker._function)
    if word is None:
        raise RangeError(f"this profile has no word for the meter's function {worker._function}")
    return word


def _is_meter_range(worker: InstrumentWorker, name: str) -> bool:
    assert worker.profile is not None
    return worker.profile.category == "dmm" and name == "range"


def clear_protection(worker: InstrumentWorker, ch: int) -> dict[str, Any]:
    """Clear a latched protection, and report how many queued errors were drained."""
    assert worker.profile is not None
    cleared = worker._drain_errors(quiet=True)
    setter = worker.profile.setters.get("clear")
    if setter is not None:
        worker._select(ch if setter.per_channel else None)
        for command in engine.clear_commands(setter, ch=ch):
            worker._send(command)
        worker._after_write(f"clear ch{ch} protection")
    if worker.profile.errors is None:
        return {"ok": True}
    return {"ok": True, "errors_cleared": cleared + worker._drain_errors(quiet=True)}


def read_now(worker: InstrumentWorker, ch: int) -> dict[str, Any]:
    """A fresh reading of every fast signal on one channel."""
    assert worker._plan is not None and worker.profile is not None
    values: dict[str, Any] = {}
    for step in worker._plan.fast:
        if step.channel not in (ch, None):
            continue
        worker._select(step.channel)
        values.update(engine.parse_reply(step.reader, worker._query(step.query), step.channel))
    if worker.profile.category == "dmm" and "value" in values:
        values["function"] = worker._function
    if worker._slots:
        rows: dict[int | None, dict[str, Any]] = {}
        scope_ops.read_slots(worker, rows)
        values.update(rows.get(ch, {}))
    if worker.profile.daq is not None and any(row.channel == ch for row in worker.cfg.scan):
        scanned: dict[int | None, dict[str, Any]] = {}
        daq_ops.read(worker, scanned)
        values.update(scanned.get(ch, {}))
    for row in worker.cfg.poll:
        key = poll_key(row.name, ch if "{ch}" in row.query else None)
        if key in worker._poll_values:
            values[key] = worker._poll_values[key]
    return values


def scpi(worker: InstrumentWorker, command: str, timeout_s: float | None) -> dict[str, Any]:
    assert worker._link is not None
    command = command.strip()
    if not command:
        raise RangeError("give a command")
    worker._selector.forget()
    # A query keeps its `?` on the header, with or without a parameter after it.
    is_query = "?" in command
    worker._note("info", f"> {command}", source="tx")
    if is_query:
        worker._queries += 1
        worker._link.write(command)
        answer = worker._link.read_any(timeout_s)
        if isinstance(answer, bytes):
            worker._recent.append((worker._now(), command, f"<block {len(answer)} bytes>"))
            worker._note("info", f"< <block {len(answer)} bytes>", source="rx")
            result: dict[str, Any] = {"block_bytes": len(answer)}
            if len(answer) <= _BLOCK_RETURN_MAX:
                result["base64"] = base64.b64encode(answer).decode()
            return result
        worker._recent.append((worker._now(), command, answer))
        worker._note("info", f"< {answer}", source="rx")
        return {"reply": answer}
    worker._send(command)
    worker._after_write(command, log=False)
    return {"ok": True}


def mark_own(worker: InstrumentWorker, ch: int, field_name: str, value: Any) -> None:
    assert worker.profile is not None
    if field_name in INSTRUMENT_FIELDS[worker.profile.category]:
        # Kept at once, so a later command that names it, such as the trigger source, sees it.
        worker._acquisition[field_name] = value
        worker._emitter.acquisition_row(
            worker.cfg.name, {field_name: value}, worker._clock.epoch_ns(worker._now())
        )
        return
    state = worker._channel[ch]
    state.own_set_ns[field_name] = worker._now()
    state.values[field_name] = value
    worker._emitter.row(
        worker.cfg.name, ch, {field_name: value}, worker._clock.epoch_ns(worker._now())
    )


def state_report(worker: InstrumentWorker) -> dict[str, Any]:
    profile = worker.profile
    channels = {}
    for ch, state in worker._channel.items():
        channels[f"ch{ch}"] = {
            "values": dict(state.values),
            "ranges": {
                k: {"min": r.min, "max": r.max, "digits": r.digits} for k, r in state.ranges.items()
            },
        }
    return {
        "state": worker.state,
        "health": worker.health,
        "panel_locked": worker._panel_locked,
        "acquisition": dict(worker._acquisition) if worker._acquisition else None,
        "slots": {
            str(slot): {"channel": ch, "type": field_name}
            for slot, (ch, field_name) in sorted(worker._slots.items())
        }
        if worker._slots
        else None,
        "where": worker.where,
        "identity": worker.idn.text if worker.idn else None,
        "profile": None
        if profile is None
        else {
            "id": profile.id,
            "name": profile.name,
            "category": profile.category,
            "badge": profile.evidence.badge,
            "notes": profile.notes,
            "setters": sorted(profile.setters),
            "system": system_functions(profile.system),
            "single_client": profile.link.single_client,
        },
        "channels": channels,
        "limits": dataclasses.asdict(worker.cfg.limits),
        "counters": {
            "queries": worker._queries,
            "timeouts": worker._timeouts_total,
            "instrument_errors": worker._errors,
            "last_error": worker._last_error,
            "rtt_ms": None if math.isnan(worker._rtt_ms) else round(worker._rtt_ms, 2),
            "rate_hz": round(worker._rate_hz(), 2),
            "target_hz": worker.cfg.rate_hz,
            "dropped": sorted(worker._dropped),
        },
        "recent": [{"command": c, "reply": r} for _, c, r in list(worker._recent)[-10:]],
    }


def system_functions(system: System) -> list[str]:
    """The system functions a profile gives, by the names the system request takes."""
    given = {
        "reset": system.reset is not None,
        "reboot": system.reboot is not None,
        "self_test": system.self_test is not None,
        "save": system.save is not None,
        "recall": system.recall is not None,
        "lock": system.lock is not None,
        "beep": system.beep is not None,
        "display": system.display is not None or system.display_text is not None,
        "screenshot": bool(system.screenshot),
        "setup": system.setup_query is not None or system.setup_load is not None,
    }
    return sorted(name for name, has in given.items() if has)
