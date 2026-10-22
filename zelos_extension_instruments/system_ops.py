"""What a worker does for the instrument's system functions: reset, reboot, self-test, saved
states, panel lock, beeper, display and screenshots. Functions of the worker, run on its thread
between poll batches."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import daq_ops, engine, scope_ops, serve
from .engine import ParseError, RangeError
from .link import Link, LinkError, LinkTimeout
from .profile import Profile, System

if TYPE_CHECKING:
    from .worker import InstrumentWorker

# How long the first bytes of an image may take while the instrument draws it.
_IMAGE_WAIT_S = 30.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BMP_HEADER = 6


def run(worker: InstrumentWorker, args: dict[str, Any]) -> dict[str, Any]:
    """The system function `args["op"]` names, with that function's arguments."""
    op = args.get("op")
    if op == "reset":
        return reset(worker)
    if op == "reboot":
        return reboot(worker)
    if op == "self_test":
        return self_test(worker)
    if op in ("save", "recall"):
        file, slot = args.get("file"), args.get("slot")
        if file is not None and slot is not None:
            raise RangeError("give a slot or a file, not both")
        if file is not None:
            return (save_file if op == "save" else recall_file)(worker, str(file))
        if slot is None:
            raise RangeError("give a slot or a file")
        return (save if op == "save" else recall)(worker, int(slot))
    if op == "lock":
        return lock(worker, bool(args.get("on", True)))
    if op == "beep":
        return beep(worker)
    if op == "display":
        on = args.get("on")
        text = args.get("text")
        return display(
            worker, None if on is None else bool(on), None if text is None else str(text)
        )
    if op == "screenshot":
        path = args.get("path")
        return screenshot(worker, str(path) if path else None)
    raise RangeError(f"unknown system function {op}")


def reset(worker: InstrumentWorker) -> dict[str, Any]:
    """Return the instrument to its reset state; reports the outputs that went off."""
    system = _system(worker)
    if system.reset is None:
        raise RangeError("this profile has no reset")
    before, relays = _outputs(worker), _relays(worker)
    worker._send(system.reset)
    _settle(worker, "reset")
    after = _outputs(worker, own=True)
    return {"ok": True, "outputs_off": _went_off(before, after), **_relay_changes(worker, relays)}


def reboot(worker: InstrumentWorker) -> dict[str, Any]:
    """Restart the instrument; the worker reconnects once it has had time to come back."""
    system = _system(worker)
    if system.reboot is None:
        raise RangeError("this profile has no reboot")
    worker._send(system.reboot)
    worker._note("info", "reboot", source="action")
    worker.disconnect_for(
        system.reboot_s, f"Rebooting; reconnecting in about {system.reboot_s:g} s."
    )
    return {"ok": True, "back_in_s": system.reboot_s}


def self_test(worker: InstrumentWorker) -> dict[str, Any]:
    """Run the self-test; the errors it queued come back with the verdict."""
    system = _system(worker)
    if system.self_test is None:
        raise RangeError("this profile has no self-test")
    reply = _ask(worker, system.self_test, system.self_test_timeout_s).strip()
    passed = system.self_test_ok.search(reply) is not None
    worker._note(
        "info" if passed else "warn",
        f"self-test {'passed' if passed else 'failed'}: {reply}",
        source="action",
    )
    return {"passed": passed, "reply": reply, "errors": _error_texts(worker)}


def save(worker: InstrumentWorker, slot: int) -> dict[str, Any]:
    """Save the settings to a memory slot."""
    system = _system(worker)
    if system.save is None:
        raise RangeError("this profile has no saved states")
    _check_slot(system, slot)
    worker._send(engine.render(system.save, slot=slot))
    worker._after_write(f"save state {slot}")
    return {"ok": True, "slot": slot}


def recall(worker: InstrumentWorker, slot: int) -> dict[str, Any]:
    """Restore the settings from a memory slot; reports the outputs that changed."""
    system = _system(worker)
    if system.recall is None:
        raise RangeError("this profile has no saved states")
    _check_slot(system, slot)
    before, relays = _outputs(worker), _relays(worker)
    worker._send(engine.render(system.recall, slot=slot))
    _settle(worker, f"recall state {slot}")
    after = _outputs(worker, own=True)
    return {
        "ok": True,
        "slot": slot,
        "outputs_on": _went_on(before, after),
        "outputs_off": _went_off(before, after),
        **_relay_changes(worker, relays),
    }


def save_file(worker: InstrumentWorker, path: str) -> dict[str, Any]:
    """Save the instrument's setup to a file."""
    system = _system(worker)
    if system.setup_query is None:
        raise RangeError("this profile has no setup transfer")
    assert worker._link is not None
    target = _new_file(path)
    worker._selector.forget()
    worker._queries += 1
    worker._link.write(system.setup_query)
    setup = worker._link.read_block(system.settle_timeout_s)
    worker._recent.append((worker._now(), system.setup_query, f"<block {len(setup)} bytes>"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(setup)
    worker._note("info", f"saved the setup to {target}", source="action")
    return {"ok": True, "path": str(target), "bytes": len(setup)}


def recall_file(worker: InstrumentWorker, path: str) -> dict[str, Any]:
    """Send a setup file back to the instrument; reports the outputs that changed."""
    system = _system(worker)
    if system.setup_load is None:
        raise RangeError("this profile has no setup transfer")
    link = worker._link
    assert link is not None
    source = _resolve(path)
    try:
        setup = source.read_bytes()
    except OSError as e:
        raise RangeError(f"cannot read {source}: {e.strerror or e}") from e
    before, relays = _outputs(worker), _relays(worker)
    worker._selector.forget()
    link.write_block(system.setup_load, setup)
    worker._recent.append((worker._now(), system.setup_load, f"<block {len(setup)} bytes>"))
    ack = _profile(worker).link.ack
    if ack is not None and (reply := link.read().strip()) != ack:
        raise RangeError(f"{worker.where} answered {reply!r} to {system.setup_load}")
    _settle(worker, f"recall the setup from {source}")
    after = _outputs(worker, own=True)
    return {
        "ok": True,
        "path": str(source),
        "outputs_on": _went_on(before, after),
        "outputs_off": _went_off(before, after),
        **_relay_changes(worker, relays),
    }


def lock(worker: InstrumentWorker, on: bool) -> dict[str, Any]:
    """Lock or unlock the front panel; while locked, the worker never hands it back."""
    system = _system(worker)
    command = system.lock if on else system.unlock
    if command is None:
        raise RangeError("this profile has no panel lock")
    worker._send(command)
    worker._after_write("lock the panel" if on else "unlock the panel")
    worker._panel_locked = on
    return {"ok": True, "locked": on}


def beep(worker: InstrumentWorker) -> dict[str, Any]:
    system = _system(worker)
    if system.beep is None:
        raise RangeError("this profile has no beep")
    worker._send(system.beep)
    worker._after_write("beep")
    return {"ok": True}


def display(worker: InstrumentWorker, on: bool | None, text: str | None) -> dict[str, Any]:
    """Switch the display, and show text on it; empty text clears it."""
    system = _system(worker)
    if on is None and text is None:
        raise RangeError("give text, or on")
    if on is not None and system.display is None:
        raise RangeError("this profile has no display")
    commands: list[str] = []
    if on is not None:
        assert system.display is not None
        commands.append(engine.render(system.display, on=on))
    if text == "" and system.display_clear is not None:
        commands.append(system.display_clear)
    elif text is not None:
        if system.display_text is None or system.display_text_max is None:
            raise RangeError("this profile has no display text")
        if len(text) > system.display_text_max:
            raise RangeError(f"text must be at most {system.display_text_max} characters")
        if '"' in text:
            raise RangeError('text may not contain "')
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise RangeError("the text may not contain control characters")
        commands.append(engine.render(system.display_text, text=text))
    for command in commands:
        worker._send(command)
    worker._after_write("display")
    result: dict[str, Any] = {"ok": True}
    if on is not None:
        result["on"] = on
    if text is not None:
        result["text"] = text
    return result


def screenshot(worker: InstrumentWorker, path: str | None) -> dict[str, Any]:
    """Fetch a picture of the screen and write it to `path`, or under the data directory."""
    system = _system(worker)
    if not system.screenshot:
        raise RangeError("this profile has no screenshot")
    chosen = _new_file(path) if path else None
    *setup, query = system.screenshot
    _run_commands(worker, setup)
    worker._selector.forget()
    image = _read_image(worker, query, system)
    if system.screenshot_framing != "block":
        # A picture sent with no header leaves its line ending behind: drop it, not a reply.
        worker._clear_link()
    _run_commands(worker, system.screenshot_cleanup)
    worker._after_write("screenshot", log=False)
    kind = image_format(image)
    if kind is None:
        raise RangeError(f"the instrument sent {len(image)} bytes that are not a picture")
    if chosen is not None:
        target = chosen
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = _data_dir() / "screenshots" / f"{worker.cfg.name}-{stamp}.{kind}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image)
    worker._note("info", f"screenshot saved to {target}", source="action")
    return {"path": str(target), "bytes": len(image), "format": kind}


def image_format(image: bytes) -> str | None:
    """The file extension an image's first bytes call for: png, bmp or jpg."""
    if image.startswith(_PNG_SIGNATURE):
        return "png"
    if image.startswith(b"BM"):
        return "bmp"
    if image.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return None


def _read_image(worker: InstrumentWorker, query: str, system: System) -> bytes:
    """Ask for the image and read it in the framing the profile names."""
    link = worker._link
    assert link is not None
    worker._queries += 1
    link.write(query)
    if system.screenshot_framing == "block":
        image = link.read_block(_IMAGE_WAIT_S)
    elif system.screenshot_framing == "bmp":
        image = _read_bmp(link)
    else:
        image = _read_png(link)
    worker._recent.append((worker._now(), query, f"<image {len(image)} bytes>"))
    return image


def _read_bmp(link: Link) -> bytes:
    """A BMP file sent as it is: its header gives its size."""
    head = link.read_bytes(_BMP_HEADER, _IMAGE_WAIT_S)
    size = int.from_bytes(head[2:6], "little")
    if head[:2] != b"BM" or size < _BMP_HEADER:
        raise LinkError(f"{link.where} did not send a BMP image: {head!r}", "reply")
    return head + link.read_bytes(size - _BMP_HEADER, _IMAGE_WAIT_S)


def _read_png(link: Link) -> bytes:
    """A PNG file sent as it is: chunks up to the IEND chunk and its CRC."""
    image = bytearray(link.read_bytes(len(_PNG_SIGNATURE), _IMAGE_WAIT_S))
    if image != _PNG_SIGNATURE:
        raise LinkError(f"{link.where} did not send a PNG image: {bytes(image)!r}", "reply")
    while True:
        head = link.read_bytes(8, _IMAGE_WAIT_S)
        length = int.from_bytes(head[:4], "big")
        # The chunk's data and its CRC.
        image += head + link.read_bytes(length + 4, _IMAGE_WAIT_S)
        if head[4:8] == b"IEND":
            return bytes(image)


def _settle(worker: InstrumentWorker, what: str) -> None:
    """After a reset or recall: wait for it to finish, then put the link back the way the
    connect left it."""
    profile = _profile(worker)
    system = profile.system
    worker._selector.forget()
    # What a later command names is read again, never assumed to have survived.
    worker._function = engine.unknown_label("function")
    worker._acquisition.pop("trigger_source", None)
    for state in worker._channel.values():
        state.values.pop("modulation_type", None)
    if profile.link.remote:
        # A reset may drop the instrument out of remote mode, where it answers nothing.
        worker._send(profile.link.remote)
        if worker._panel_locked and system.lock is not None:
            worker._send(system.lock)
    if system.opc is not None:
        _ask(worker, system.opc, system.settle_timeout_s)
    try:
        worker._after_write(what)
    finally:
        for command in profile.link.init:
            worker._send(command)
        if profile.daq is not None:
            daq_ops.setup(worker)
        worker._drain_errors(quiet=True)
        if profile.scope is not None:
            worker._slots = scope_ops.scan_slots(worker)


def _outputs(worker: InstrumentWorker, *, own: bool = False) -> dict[int, bool]:
    """Each channel's output read now, for the channels the profile can read it on; `own`
    records them as this extension's change."""
    return _switches(worker, "output", own=own)


def _relays(worker: InstrumentWorker, *, own: bool = False) -> dict[int, bool]:
    """Each relay's state read now, as the slow round reads it."""
    return _switches(worker, "closed", own=own)


def _switches(worker: InstrumentWorker, field_name: str, *, own: bool) -> dict[int, bool]:
    plan = worker._plan
    assert plan is not None
    found: dict[int, bool] = {}
    for step in plan:
        if step.flag is not None or field_name not in step.reader.fields:
            continue
        try:
            worker._select(step.channel)
            value = engine.parse_reply(step.reader, worker._query(step.query), step.channel)
        except (LinkTimeout, ParseError) as e:
            worker._recover(f"{step.query}: {e}")
            continue
        on = value.get(field_name)
        if not isinstance(on, bool):
            continue
        for ch in [step.channel] if step.channel is not None else list(worker._channel):
            found[ch] = on
            if own:
                serve.mark_own(worker, ch, field_name, on)
    return found


def _relay_changes(worker: InstrumentWorker, before: dict[int, bool]) -> dict[str, list[int]]:
    """The relays that opened and closed since `before`, for a unit that has relays."""
    if _profile(worker).daq is None:
        return {}
    after = _relays(worker, own=True)
    return {"relays_opened": _went_off(before, after), "relays_closed": _went_on(before, after)}


def _went_off(before: dict[int, bool], after: dict[int, bool]) -> list[int]:
    return sorted(ch for ch, on in after.items() if not on and before.get(ch) is True)


def _went_on(before: dict[int, bool], after: dict[int, bool]) -> list[int]:
    return sorted(ch for ch, on in after.items() if on and before.get(ch) is not True)


def _error_texts(worker: InstrumentWorker) -> list[str]:
    """Every error the queue holds now, as `code,text`."""
    errors = _profile(worker).errors
    if errors is None:
        return []
    texts: list[str] = []
    for query in (errors.query, *errors.then):
        for _ in range(errors.drain):
            try:
                reply = worker._query(query)
            except LinkTimeout as e:
                worker._recover(f"{query}: {e}")
                break
            error = engine.read_error(errors, reply)
            if error is None:
                break
            if error.code is None:
                worker._resync(f"unexpected reply {reply.strip()!r} to {query}")
                break
            text = f"{error.code},{error.text}"
            worker._errors += 1
            worker._last_error = text
            texts.append(text)
    return texts


def _ask(worker: InstrumentWorker, query: str, timeout_s: float) -> str:
    """A query whose reply may take longer than the link's own timeout."""
    link = worker._link
    assert link is not None
    worker._queries += 1
    link.write(query)
    reply = link.read(timeout_s)
    worker._recent.append((worker._now(), query, reply))
    return reply


def _run_commands(worker: InstrumentWorker, commands: tuple[str, ...] | list[str]) -> None:
    """Send each command; a query among them is asked and its reply dropped."""
    for command in commands:
        if "?" in command:
            worker._query(command)
        else:
            worker._send(command)


def _check_slot(system: System, slot: int) -> None:
    assert system.slots is not None
    low, high = system.slots
    if not low <= slot <= high:
        raise RangeError(f"slot must be from {low} to {high}")


def _resolve(path: str) -> Path:
    """A path as given, with a relative one under the data directory."""
    target = Path(path).expanduser()
    return target if target.is_absolute() else _data_dir() / target


def _new_file(path: str) -> Path:
    """`path` resolved, refused when a file is already there."""
    target = _resolve(path)
    if target.exists():
        raise RangeError(f"{target} already exists; give a new file name")
    return target


def _data_dir() -> Path:
    data_dir = os.environ.get("ZELOS_DATA_DIR")
    return Path(data_dir) if data_dir else Path.cwd()


def _system(worker: InstrumentWorker) -> System:
    return _profile(worker).system


def _profile(worker: InstrumentWorker) -> Profile:
    assert worker.profile is not None
    return worker.profile
