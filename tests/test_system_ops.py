"""The system functions as a worker runs them on an instrument: reset, reboot, self-test, saved
states, panel lock, beeper, display and screenshots."""

import dataclasses
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    Dp832,
    RecordingEmitter,
    ScriptedLink,
    ask,
    connected,
    instrument_config,
    opener_for,
    start_open,
)
from tests.test_serve import served
from zelos_extension_instruments import scope_ops, serve, system_ops
from zelos_extension_instruments.engine import RangeError
from zelos_extension_instruments.link import LinkError
from zelos_extension_instruments.profile import Profile, System, by_id
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker

SYSTEM = System(
    settle_timeout_s=12.0,
    self_test_timeout_s=45.0,
    save="MEM:SAVE {slot}",
    recall="*RCL {slot}",
    slots=(0, 10),
    beep="SYST:BEEP",
    display="DISP {on}",
    display_text='DISP:TEXT "{text}"',
    display_text_max=8,
    display_clear="DISP:TEXT:CLE",
    lock="SYST:LOCK ON",
    unlock="SYST:LOCK OFF",
    reboot="SYST:REBOOT",
    reboot_s=20.0,
    screenshot=("HCOP:FORM PNG", "*OPC?", "HCOP:DATA?"),
    screenshot_cleanup=("HCOP:CLE",),
    setup_query=":SYST:SET?",
    setup_load=":SYST:SET ",
)
# Every function off.
BARE = System(reset=None, self_test=None, opc=None)


def png(*chunks: tuple[bytes, bytes]) -> bytes:
    """A PNG file made of these (type, data) chunks, then IEND."""
    out = b"\x89PNG\r\n\x1a\n"
    for kind, data in (*chunks, (b"IEND", b"")):
        crc = zlib.crc32(kind + data).to_bytes(4, "big")
        out += len(data).to_bytes(4, "big") + kind + data + crc
    return out


PNG = png((b"IHDR", bytes(13)), (b"IDAT", b"\x00" * 40))
BMP = b"BM" + (30).to_bytes(4, "little") + bytes(24)


class Unit(Dp832):
    """A DP832 that also has the system functions, kept as state."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: dict[int, dict[int, bool]] = {}
        self.locked = False
        self.display_on = True
        self.text = ""
        self.beeps = 0
        self.test_reply = "0"
        self.test_errors: list[str] = []
        self.reset_error: str | None = None
        self.setup = b"SETUP\n\x00\xff"
        self.image: bytes = b"#" + str(len(str(len(PNG)))).encode() + str(len(PNG)).encode() + PNG

    def __call__(self, command: str) -> str | bytes | None:
        head, _, arg = command.partition(" ")
        if head == "*RST":
            self.calls.append(command)
            self.out = dict.fromkeys(self.out, False)
            if self.reset_error:
                self.errors.append(self.reset_error)
            return None
        if head == "MEM:SAVE":
            self.calls.append(command)
            self.saved[int(arg)] = dict(self.out)
            return None
        if head == "*RCL":
            self.calls.append(command)
            if int(arg) not in self.saved:
                self.errors.append('-221,"Settings conflict"')
                return None
            self.out = dict(self.saved[int(arg)])
            return None
        if head == "*OPC?":
            self.calls.append(command)
            return "1"
        if head == "*TST?":
            self.calls.append(command)
            self.errors.extend(self.test_errors)
            return self.test_reply
        if head == "HCOP:DATA?":
            self.calls.append(command)
            return self.image
        if head == ":SYST:SET?":
            self.calls.append(command)
            return b"#1" + str(len(self.setup)).encode() + self.setup
        if head in ("SYST:BEEP", "SYST:LOCK", "DISP", "DISP:TEXT", "DISP:TEXT:CLE", "SYST:REBOOT"):
            self.calls.append(command)
            if head == "SYST:BEEP":
                self.beeps += 1
            elif head == "SYST:LOCK":
                self.locked = arg == "ON"
            elif head == "DISP":
                self.display_on = arg == "ON"
            elif head == "DISP:TEXT":
                self.text = arg.strip('"')
            elif head == "DISP:TEXT:CLE":
                self.text = ""
            return None
        if head in ("HCOP:FORM", "HCOP:CLE", "INIT:CMD"):
            self.calls.append(command)
            return None
        return super().__call__(command)


def profile_with(dp800: Profile, system: System, **link: Any) -> Profile:
    return dataclasses.replace(dp800, system=system, link=dataclasses.replace(dp800.link, **link))


def on_bench(
    profile: Profile, unit: Callable[[str], str | bytes | None]
) -> tuple[InstrumentWorker, ScriptedLink, RecordingEmitter]:
    """A connected worker on a link that moves raw bytes."""
    link = ScriptedLink(unit)
    emitter = RecordingEmitter()
    worker = InstrumentWorker(
        instrument_config(name="psu", rate_hz=50), [profile], emitter, open=opener_for(link)
    )
    assert worker._connect(), worker.health
    return worker, link, emitter


@pytest.fixture
def unit() -> Unit:
    return Unit()


@pytest.fixture
def bench(dp800: Profile, unit: Unit) -> tuple[InstrumentWorker, ScriptedLink, RecordingEmitter]:
    return on_bench(profile_with(dp800, SYSTEM), unit)


@pytest.fixture
def bare(dp800: Profile, unit: Unit) -> InstrumentWorker:
    worker, _, _ = on_bench(profile_with(dp800, BARE), unit)
    return worker


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))
    return tmp_path


def since(link: ScriptedLink, mark: int) -> list[str]:
    return link.commands[mark:]


def test_reset_turns_outputs_off_and_reports_the_ones_that_were_on(bench, unit: Unit) -> None:
    worker, link, emitter = bench
    unit.out[2] = True
    mark = len(link.commands)
    assert system_ops.reset(worker) == {"ok": True, "outputs_off": [1, 2]}
    sent = since(link, mark)
    assert sent.index("*RST") < sent.index("SYST:REMOTE") < sent.index("*OPC?")
    assert unit.out == {1: False, 2: False, 3: False}
    assert ("psu", 1, {"output": False}) in [(n, ch, v) for n, ch, v, _ in emitter.rows]


def test_reset_waits_for_the_operation_to_complete_as_long_as_the_profile_allows(bench) -> None:
    worker, link, _ = bench
    system_ops.reset(worker)
    assert 12.0 in link.timeouts_asked


def test_reset_without_an_operation_complete_query_does_not_ask_one(dp800, unit: Unit) -> None:
    worker, link, _ = on_bench(profile_with(dp800, dataclasses.replace(SYSTEM, opc=None)), unit)
    mark = len(link.commands)
    system_ops.reset(worker)
    assert "*OPC?" not in since(link, mark)


def test_reset_sends_the_connect_commands_again_even_when_the_instrument_reports_an_error(
    dp800, unit: Unit
) -> None:
    worker, link, _ = on_bench(profile_with(dp800, SYSTEM, init=("INIT:CMD",)), unit)
    unit.reset_error = '-200,"Execution error"'
    mark = len(link.commands)
    with pytest.raises(RangeError, match="the instrument reported -200,Execution error"):
        system_ops.reset(worker)
    assert since(link, mark).count("INIT:CMD") == 1


@pytest.mark.parametrize(
    "settle", [system_ops.reset, lambda w: system_ops.recall(w, 1)], ids=["reset", "recall"]
)
def test_reset_and_recall_forget_what_a_later_command_names(bench, settle) -> None:
    worker, _, _ = bench
    system_ops.save(worker, 1)
    worker._function = "dc_voltage"
    worker._acquisition["trigger_source"] = "ch2"
    worker._channel[1].values["modulation_type"] = "fm"
    settle(worker)
    assert worker._function == "other"
    assert "trigger_source" not in worker._acquisition
    assert "modulation_type" not in worker._channel[1].values
    with pytest.raises(RangeError, match="^the trigger source is not known yet$"):
        serve.trigger_source(worker)
    with pytest.raises(RangeError, match="^the modulation type of ch1 is not known$"):
        serve.modulation_word(worker, 1)


def test_reset_forgets_the_selected_channel(bench) -> None:
    worker, _, _ = bench
    forgotten: list[bool] = []
    forget = worker._selector.forget

    def tracking() -> None:
        forgotten.append(True)
        forget()

    worker._selector.forget = tracking
    system_ops.reset(worker)
    assert forgotten


def test_a_scope_scans_its_measurement_slots_again_after_a_reset(profiles) -> None:
    profile = by_id(profiles, "siglent-sds")
    assert profile is not None
    fake = FakeInstrument(profile)
    assert fake.scope is not None

    def handle(command: str) -> str | bytes | None:
        if command == "*RST":
            assert fake.scope is not None
            fake.scope.slots = {
                n: (False, kind, src) for n, (_, kind, src) in fake.scope.slots.items()
            }
            return None
        if command == "*OPC?":
            return "1"
        return fake.handle(command)

    worker, _, _ = connected(profile, handle, name="scope", profile=profile.id)
    scope_ops.measure(worker, 2, {"type": "frequency", "slot": 3})
    assert worker._slots[3] == (2, "frequency")
    assert system_ops.reset(worker) == {"ok": True, "outputs_off": []}
    assert worker._slots == {}


def test_reboot_lets_go_of_the_link_and_says_when_it_reconnects(bench, unit: Unit) -> None:
    worker, link, _ = bench
    assert system_ops.reboot(worker) == {"ok": True, "back_in_s": 20.0}
    assert unit.calls[-1] == "SYST:REBOOT"
    assert link.closed and worker._link is None
    assert worker.health == "Rebooting; reconnecting in about 20 s."
    assert "SYST:LOCAL" not in since(link, 0)


def test_self_test_passes_on_the_reply_the_profile_names(bench) -> None:
    worker, link, _ = bench
    assert system_ops.self_test(worker) == {"passed": True, "reply": "0", "errors": []}
    assert 45.0 in link.timeouts_asked


def test_a_failed_self_test_returns_the_errors_it_queued(bench, unit: Unit) -> None:
    worker, _, _ = bench
    unit.test_reply = "+1"
    unit.test_errors = ['-330,"Self-test failed"', '-314,"Save/recall memory lost"']
    assert system_ops.self_test(worker) == {
        "passed": False,
        "reply": "+1",
        "errors": ["-330,Self-test failed", "-314,Save/recall memory lost"],
    }


def test_save_and_recall_a_slot_report_the_outputs_that_changed(bench, unit: Unit) -> None:
    worker, _, _ = bench
    assert system_ops.save(worker, 4) == {"ok": True, "slot": 4}
    assert unit.saved[4] == {1: True, 2: False, 3: False}
    unit.out = {1: False, 2: True, 3: False}
    assert system_ops.recall(worker, 4) == {
        "ok": True,
        "slot": 4,
        "outputs_on": [1],
        "outputs_off": [2],
    }
    assert unit.out == {1: True, 2: False, 3: False}


def test_recalling_an_empty_slot_reports_the_instruments_error(bench) -> None:
    worker, _, _ = bench
    with pytest.raises(RangeError, match="Settings conflict"):
        system_ops.recall(worker, 7)


@pytest.mark.parametrize("function", [system_ops.save, system_ops.recall])
def test_a_slot_outside_the_profiles_slots_is_refused(bench, unit: Unit, function) -> None:
    worker, link, _ = bench
    mark = len(link.commands)
    with pytest.raises(RangeError, match="^slot must be from 0 to 10$"):
        function(worker, 11)
    assert since(link, mark) == []


def test_save_file_writes_the_setup_block_to_the_file(bench, unit: Unit, tmp_path: Path) -> None:
    worker, _, _ = bench
    target = tmp_path / "setups" / "bench.set"
    assert system_ops.save_file(worker, str(target)) == {
        "ok": True,
        "path": str(target),
        "bytes": len(unit.setup),
    }
    assert target.read_bytes() == unit.setup


@pytest.mark.parametrize(
    "call",
    [
        lambda w, p: system_ops.save_file(w, p),
        lambda w, p: system_ops.screenshot(w, p),
    ],
    ids=["save_file", "screenshot"],
)
def test_an_existing_file_is_never_written_over(bench, tmp_path: Path, call) -> None:
    worker, link, _ = bench
    target = tmp_path / "kept.bin"
    target.write_bytes(b"KEEP")
    mark = len(link.commands)
    with pytest.raises(RangeError, match="already exists; give a new file name$"):
        call(worker, str(target))
    assert target.read_bytes() == b"KEEP"
    assert since(link, mark) == []


def test_a_relative_file_lands_under_the_data_directory(bench, unit: Unit, data_dir: Path) -> None:
    worker, _, _ = bench
    result = system_ops.save_file(worker, "bench.set")
    assert result["path"] == str(data_dir / "bench.set")
    assert (data_dir / "bench.set").read_bytes() == unit.setup


def test_recall_file_sends_the_file_as_a_block_after_the_load_command(
    bench, tmp_path: Path
) -> None:
    worker, link, _ = bench
    source = tmp_path / "bench.set"
    source.write_bytes(b"SETUP\n\x00\xff")
    mark = len(link.commands)
    assert system_ops.recall_file(worker, str(source)) == {
        "ok": True,
        "path": str(source),
        "outputs_on": [],
        "outputs_off": [],
    }
    assert link.blocks == [(":SYST:SET ", b"SETUP\n\x00\xff")]
    assert since(link, mark).index(":SYST:SET ") < since(link, mark).index("*OPC?")


def test_recall_file_of_a_missing_file_sends_nothing(bench, tmp_path: Path) -> None:
    worker, link, _ = bench
    with pytest.raises(RangeError, match="^cannot read "):
        system_ops.recall_file(worker, str(tmp_path / "missing.set"))
    assert link.blocks == []


def test_lock_and_unlock_the_panel(bench, unit: Unit) -> None:
    worker, _, _ = bench
    assert system_ops.lock(worker, True) == {"ok": True, "locked": True}
    assert unit.locked and worker._panel_locked
    assert system_ops.lock(worker, False) == {"ok": True, "locked": False}
    assert not unit.locked and not worker._panel_locked


@pytest.mark.parametrize(
    "remote_again",
    [lambda w: w._resync("a test"), system_ops.reset],
    ids=["resync", "reset"],
)
def test_the_panel_lock_follows_every_return_to_remote(bench, unit: Unit, remote_again) -> None:
    worker, link, _ = bench
    mark = len(link.commands)
    remote_again(worker)
    assert "SYST:LOCK ON" not in since(link, mark)
    system_ops.lock(worker, True)
    mark = len(link.commands)
    remote_again(worker)
    sent = since(link, mark)
    assert sent[sent.index("SYST:REMOTE") + 1] == "SYST:LOCK ON"
    assert unit.locked


def test_local_ends_the_panel_lock_and_get_state_reports_it(bench) -> None:
    worker, link, _ = bench
    assert served(worker, "state")["panel_locked"] is False
    system_ops.lock(worker, True)
    assert served(worker, "state")["panel_locked"] is True
    assert served(worker, "local") == {"ok": True}
    assert served(worker, "state")["panel_locked"] is False
    mark = len(link.commands)
    worker._resync("a test")
    assert "SYST:LOCK ON" not in since(link, mark)


def test_a_reconnect_ends_the_panel_lock(bench) -> None:
    worker, link, _ = bench
    system_ops.lock(worker, True)
    worker._close()
    link.closed = False
    assert worker._connect(), worker.health
    assert not worker._panel_locked


def test_beep(bench, unit: Unit) -> None:
    worker, _, _ = bench
    assert system_ops.beep(worker) == {"ok": True}
    assert unit.beeps == 1


def test_display_switches_and_shows_and_clears_text(bench, unit: Unit) -> None:
    worker, _, _ = bench
    assert system_ops.display(worker, False, None) == {"ok": True, "on": False}
    assert not unit.display_on
    assert system_ops.display(worker, True, "HELLO") == {"ok": True, "on": True, "text": "HELLO"}
    assert unit.display_on and unit.text == "HELLO"
    assert system_ops.display(worker, None, "") == {"ok": True, "text": ""}
    assert unit.calls[-2] == "DISP:TEXT:CLE" and unit.text == ""


def test_empty_text_without_a_clear_command_writes_empty_text(dp800, unit: Unit) -> None:
    profile = profile_with(dp800, dataclasses.replace(SYSTEM, display_clear=None))
    worker, _, _ = on_bench(profile, unit)
    unit.text = "OLD"
    system_ops.display(worker, None, "")
    assert unit.calls[-2] == 'DISP:TEXT ""' and unit.text == ""


@pytest.mark.parametrize(
    ("on", "text", "reason"),
    [
        (None, None, "^give text, or on$"),
        (None, "TOO LONG!", "^text must be at most 8 characters$"),
        (None, 'SAY "HI"', '^text may not contain "$'),
        (None, "HI\nTHERE", "^the text may not contain control characters$"),
        (None, "HI\x00", "^the text may not contain control characters$"),
        (None, "HI\x7f", "^the text may not contain control characters$"),
    ],
)
def test_display_refuses_what_it_cannot_show(bench, unit: Unit, on, text, reason: str) -> None:
    worker, link, _ = bench
    mark = len(link.commands)
    with pytest.raises(RangeError, match=reason):
        system_ops.display(worker, on, text)
    assert since(link, mark) == []


def test_display_text_needs_the_profiles_text_command(dp800, unit: Unit) -> None:
    profile = profile_with(
        dp800, dataclasses.replace(SYSTEM, display_text=None, display_text_max=None)
    )
    worker, _, _ = on_bench(profile, unit)
    with pytest.raises(RangeError, match="^this profile has no display text$"):
        system_ops.display(worker, None, "HI")


def test_a_screenshot_sent_as_a_block_lands_under_the_data_directory(
    bench, unit: Unit, data_dir: Path
) -> None:
    worker, link, _ = bench
    mark = len(link.commands)
    result = system_ops.screenshot(worker, None)
    path = Path(result["path"])
    assert result == {"path": str(path), "bytes": len(PNG), "format": "png"}
    assert path.parent == data_dir / "screenshots"
    assert path.name.startswith("psu-") and path.suffix == ".png"
    assert len(path.stem) == len("psu-yyyymmdd-hhmmss")
    assert path.read_bytes() == PNG
    sent = since(link, mark)
    assert sent[:4] == ["HCOP:FORM PNG", "*OPC?", "HCOP:DATA?", "HCOP:CLE"]


def test_a_screenshot_goes_to_the_path_asked_for(bench, tmp_path: Path) -> None:
    worker, _, _ = bench
    target = tmp_path / "shots" / "screen.png"
    assert system_ops.screenshot(worker, str(target))["path"] == str(target)
    assert target.read_bytes() == PNG


def test_a_bmp_sent_without_a_header_is_read_by_its_own_size(
    dp800, unit: Unit, tmp_path: Path
) -> None:
    profile = profile_with(dp800, dataclasses.replace(SYSTEM, screenshot_framing="bmp"))
    worker, link, emitter = on_bench(profile, unit)
    unit.image = BMP + b"\n"
    result = system_ops.screenshot(worker, str(tmp_path / "screen"))
    assert result["format"] == "bmp" and result["bytes"] == len(BMP)
    assert (tmp_path / "screen").read_bytes() == BMP
    assert not link.raw and not any("out of step" in m for m in emitter.messages())


def test_a_png_sent_without_a_header_is_read_up_to_its_end_chunk(
    dp800, unit: Unit, tmp_path: Path
) -> None:
    profile = profile_with(dp800, dataclasses.replace(SYSTEM, screenshot_framing="png"))
    worker, link, emitter = on_bench(profile, unit)
    unit.image = PNG + b"\n"
    result = system_ops.screenshot(worker, str(tmp_path / "screen.png"))
    assert result == {"path": str(tmp_path / "screen.png"), "bytes": len(PNG), "format": "png"}
    assert not link.raw and not any("out of step" in m for m in emitter.messages())


@pytest.mark.parametrize(("framing", "image"), [("bmp", b"XX" + bytes(30)), ("png", b"GIF89a..")])
def test_raw_bytes_that_are_not_the_framing_named_are_a_reply_error(
    dp800, unit: Unit, tmp_path: Path, framing, image: bytes
) -> None:
    profile = profile_with(dp800, dataclasses.replace(SYSTEM, screenshot_framing=framing))
    worker, _, _ = on_bench(profile, unit)
    unit.image = image
    with pytest.raises(LinkError) as caught:
        system_ops.screenshot(worker, str(tmp_path / "screen"))
    assert caught.value.kind == "reply"


def test_a_jpeg_is_saved_as_jpg(bench, unit: Unit, data_dir: Path) -> None:
    worker, _, _ = bench
    jpeg = b"\xff\xd8\xff\xe0" + bytes(20)
    unit.image = b"#224" + jpeg
    result = system_ops.screenshot(worker, None)
    assert result["format"] == "jpg" and result["path"].endswith(".jpg")


def test_a_screenshot_that_is_not_a_picture_is_refused_and_not_written(
    bench, unit: Unit, data_dir: Path
) -> None:
    worker, _, _ = bench
    unit.image = b"#15hello"
    with pytest.raises(RangeError, match="^the instrument sent 5 bytes that are not a picture$"):
        system_ops.screenshot(worker, None)
    assert not (data_dir / "screenshots").exists()


@pytest.mark.parametrize(
    ("call", "thing"),
    [
        (system_ops.reset, "reset"),
        (system_ops.reboot, "reboot"),
        (system_ops.self_test, "self-test"),
        (lambda w: system_ops.save(w, 1), "saved states"),
        (lambda w: system_ops.recall(w, 1), "saved states"),
        (lambda w: system_ops.save_file(w, "x.set"), "setup transfer"),
        (lambda w: system_ops.recall_file(w, "x.set"), "setup transfer"),
        (lambda w: system_ops.lock(w, True), "panel lock"),
        (lambda w: system_ops.lock(w, False), "panel lock"),
        (system_ops.beep, "beep"),
        (lambda w: system_ops.display(w, True, None), "display"),
        (lambda w: system_ops.display(w, None, "HI"), "display text"),
        (lambda w: system_ops.screenshot(w, None), "screenshot"),
    ],
)
def test_a_function_the_profile_lacks_is_refused(bare: InstrumentWorker, call, thing) -> None:
    link = bare._link
    assert isinstance(link, ScriptedLink)
    mark = len(link.commands)
    with pytest.raises(RangeError, match=f"^this profile has no {thing}$"):
        call(bare)
    assert since(link, mark) == []


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"op": "save"}, "^give a slot or a file$"),
        ({"op": "recall", "slot": 1, "file": "a.set"}, "^give a slot or a file, not both$"),
        ({"op": "format"}, "^unknown system function format$"),
    ],
)
def test_run_refuses_what_is_not_one_system_function(bench, args, reason: str) -> None:
    worker, _, _ = bench
    with pytest.raises(RangeError, match=reason):
        system_ops.run(worker, args)


def test_run_hands_each_function_its_arguments(bench, unit: Unit, tmp_path: Path) -> None:
    worker, _, _ = bench
    assert system_ops.run(worker, {"op": "save", "slot": 2}) == {"ok": True, "slot": 2}
    target = tmp_path / "bench.set"
    assert system_ops.run(worker, {"op": "save", "file": str(target)})["path"] == str(target)
    assert system_ops.run(worker, {"op": "lock", "on": True})["locked"] is True
    assert system_ops.run(worker, {"op": "display", "text": "HI"}) == {"ok": True, "text": "HI"}
    assert system_ops.run(worker, {"op": "beep"}) == {"ok": True}
    assert system_ops.run(worker, {"op": "self_test"})["passed"] is True
    shot = tmp_path / "s.png"
    assert system_ops.run(worker, {"op": "screenshot", "path": str(shot)})["path"] == str(shot)
    assert system_ops.run(worker, {"op": "reset"})["ok"] is True
    assert system_ops.run(worker, {"op": "recall", "slot": 2})["slot"] == 2
    assert system_ops.run(worker, {"op": "reboot"})["back_in_s"] == 20.0


def test_a_running_worker_serves_a_system_request(dp800, unit: Unit) -> None:
    worker, _ = start_open(profile_with(dp800, SYSTEM), ScriptedLink(unit))
    try:
        assert ask(worker, "system", op="reset") == {"ok": True, "outputs_off": [1]}
        assert ask(worker, "system", op="save", slot=11) == {"error": "slot must be from 0 to 10"}
    finally:
        worker.stop()
        worker.join(2)
