"""What the fresh-eyes review of the worker, engine, links and discovery found, each pinned."""

import json
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import ScriptedLink, instrument_config, opener_for, wait_until
from tests.test_worker import Dp832, RecordingEmitter, ask
from zelos_extension_instruments import discovery, waveform
from zelos_extension_instruments.engine import format_value, verified
from zelos_extension_instruments.link import Lines, LinkError, LinkTimeout, TcpAddress, VisaAddress
from zelos_extension_instruments.profile import Profile, Range, by_id
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.worker import InstrumentWorker

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def dp832() -> Dp832:
    return Dp832()


def _worker(
    profile: Profile, link: ScriptedLink, **config: Any
) -> tuple[InstrumentWorker, RecordingEmitter]:
    emitter = RecordingEmitter()
    cfg = instrument_config(rate_hz=50, **config)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    assert wait_until(lambda: worker.state == "open", timeout_s=5), worker.health
    return worker, emitter


# ---------------------------------------------------------------- values without a range


def test_an_unranged_value_keeps_its_digits_and_verifies_relatively() -> None:
    assert format_value(1e-6, None) == "1e-06"
    assert format_value(0.2834, None) == "0.2834"
    assert format_value(12.3456, Range(0, 30, 3, None, None, None)) == "12.346"
    assert verified(1e-6, 1e-6, None)
    assert verified(0.2834, 0.28341, None)
    assert not verified(1e-6, 0.0, None), "a tiny value must not pass as zero"
    assert not verified(0.2834, 0.28, None), "a snapped value is a refusal"


# ---------------------------------------------------------------- requests


def test_a_request_the_action_gave_up_on_never_reaches_the_instrument(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, emitter = _worker(dp800, link)
    try:
        future = worker.request("set", channel=1, voltage=4.2)
        future.cancel()
        future2 = worker.request("read", channel=1)
        assert "voltage" in future2.result(timeout=5)
        assert ":SOUR1:VOLT 4.200" not in link.commands
        assert any("dropped" in m for m in emitter.messages())
    finally:
        worker.stop()
        worker.join(2)


def test_a_silent_query_in_a_request_fails_the_request_and_keeps_the_link(
    dp800: Profile, dp832: Dp832
) -> None:
    original = dp832.__call__

    def mostly(command: str) -> str | None:
        if command == "NOPE?":
            raise LinkTimeout("no reply")
        return original(command)

    link = ScriptedLink(mostly)
    worker, emitter = _worker(dp800, link)
    try:
        result = ask(worker, "scpi", command="NOPE?")
        assert "no reply" in result["error"]
        time.sleep(0.3)
        assert worker.state == "open"
        assert not any("disconnected" in m for m in emitter.messages())
        assert ask(worker, "scpi", command="*IDN?")["reply"].startswith("RIGOL")
    finally:
        worker.stop()
        worker.join(2)


def test_a_multi_value_set_checks_everything_before_sending_anything(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link)
    try:
        result = ask(worker, "set", channel=1, voltage=12.0, current=50.0)
        assert result == {"error": "current must be from 0 to 3"}
        assert ":SOUR1:VOLT 12.000" not in link.commands
    finally:
        worker.stop()
        worker.join(2)


def test_a_set_that_fails_after_an_earlier_write_names_what_went_out(
    dp800: Profile, dp832: Dp832
) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link)
    try:
        dp832.fail_next_set = None
        original = dp832.__call__

        def refuse_current(command: str) -> str | None:
            if command.startswith(":SOUR1:CURR ") and "?" not in command:
                dp832.errors.append('-222,"Data out of range"')
                return None
            return original(command)

        link._handler = refuse_current
        result = ask(worker, "set", channel=1, voltage=12.0, current=2.0)
        assert result == {
            "error": "the instrument reported -222,Data out of range; already set: voltage"
        }
        assert dp832.volt[1] == 12.0
    finally:
        worker.stop()
        worker.join(2)


def test_clear_reports_how_many_errors_it_drained(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link)
    try:
        dp832.errors.extend(['-113,"Undefined header"', '-222,"Data out of range"'])
        assert ask(worker, "clear", channel=1) == {"ok": True, "errors_cleared": 2}
    finally:
        worker.stop()
        worker.join(2)


# ---------------------------------------------------------------- the link and the loop


def test_a_lost_cycle_waits_the_backoff_before_reconnecting(dp800: Profile) -> None:
    opened: list[float] = []

    def opener(address: Any, settings: Any) -> ScriptedLink:
        opened.append(time.monotonic())
        dp832 = Dp832()
        original = dp832.__call__

        def broken(command: str) -> str | None:
            if command.startswith(":MEAS"):
                raise LinkError("the connection was lost", "other")
            return original(command)

        return ScriptedLink(broken)

    cfg = instrument_config(rate_hz=50)
    worker = InstrumentWorker(cfg, [dp800], RecordingEmitter(), open=opener)
    worker.start()
    try:
        time.sleep(2.0)
        assert 2 <= len(opened) <= 4, opened
    finally:
        worker.stop()
        worker.join(2)


def test_readings_are_stamped_at_their_own_query(dp800: Profile, dp832: Dp832) -> None:
    """A slow request between two channels' queries must not move either reading in time."""
    original = dp832.__call__

    def slow_set(command: str) -> str | None:
        reply = original(command)
        if command.startswith(":SOUR2:VOLT ") and "?" not in command:
            time.sleep(0.4)
        return reply

    link = ScriptedLink(slow_set)
    worker, emitter = _worker(dp800, link)
    try:
        before = len(emitter.rows)
        ask(worker, "set", channel=2, voltage=1.0)
        assert wait_until(lambda: len(emitter.rows) > before + 6, timeout_s=5)
        rows = [r for r in emitter.rows[before:] if "voltage" in r[2]]
        stamps = sorted(r[3] for r in rows)
        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        # Cycles are 20 ms apart at 50 Hz; the 400 ms set shows once as a gap between cycles,
        # never inside one.
        assert max(gaps) < 600_000_000
    finally:
        worker.stop()
        worker.join(2)


def test_the_panel_is_handed_back_on_the_worker_thread(dp800: Profile, dp832: Dp832) -> None:
    writers: list[str] = []
    original = dp832.__call__

    def noting(command: str) -> str | None:
        if command == "SYST:LOCAL":
            writers.append(threading.current_thread().name)
        return original(command)

    link = ScriptedLink(noting)
    worker, _ = _worker(dp800, link, advanced={"keep_panel": False})
    worker.stop()
    worker.join(3)
    assert writers and all(w == worker.name for w in writers)


def test_keep_panel_hands_the_panel_back_once_a_second(dp800: Profile, dp832: Dp832) -> None:
    link = ScriptedLink(dp832)
    worker, _ = _worker(dp800, link)
    try:
        time.sleep(1.3)
        assert link.commands.count("SYST:LOCAL") >= 1
    finally:
        worker.stop()
        worker.join(2)


def test_a_hex_reply_is_a_number_not_a_block() -> None:
    lines = Lines("\n")
    lines.feed(b"#H1F\n")
    assert lines.starts_block() is False
    assert lines.pop() == "#H1F"
    lines.feed(b"#")
    assert lines.starts_block() is None
    lines.feed(b"3005hello")
    assert lines.starts_block() is True
    assert lines.pop_block() == b"hello"


def test_a_line_ending_that_arrives_after_a_block_is_not_an_empty_reply() -> None:
    lines = Lines("\n")
    lines.feed(b"#3005hello")
    assert lines.pop_block() == b"hello"
    lines.feed(b"\n")
    assert lines.pop() is None
    lines.feed(b"\nNEXT\n")
    assert lines.pop() == "NEXT"


def test_a_line_ending_that_arrives_after_a_block_does_not_hide_the_next_block() -> None:
    lines = Lines("\n")
    lines.feed(b"#13abc")
    assert lines.pop_block() == b"abc"
    lines.feed(b"\n#14wxyz\n")
    assert lines.starts_block() is True
    assert lines.pop_block() == b"wxyz"


# ---------------------------------------------------------------- the real scope's waveform


def test_the_recorded_sds824x_descriptor_places_the_trigger_at_zero() -> None:
    """A preamble and a whole data block recorded from the SDS824X HD: edge trigger on channel
    1, rising, at 0.4 V, delay 1 ms, 1000 samples 2500 acquisitions apart. The formulas must put
    the first sample at the left edge of the screen and the trigger at t = 0, where the recorded
    signal must cross the trigger level on its way up."""
    meta = json.loads((FIXTURES / "sds824x_waveform.json").read_text(encoding="utf-8"))
    preamble = waveform.block_payload((FIXTURES / "sds824x_preamble.bin").read_bytes())
    desc = waveform.decode_wavedesc(preamble)
    timebase = float(meta["timebase"])
    delay = float(meta["delay"])
    # The descriptor describes the whole acquisition; only the data block says how many
    # samples the transfer carried.
    assert desc.count == 2_500_000 and desc.array_bytes == 5_000_000
    assert desc.bytes_per_sample == 2 and desc.step == 2500
    assert desc.horizontal_interval == pytest.approx(1 / float(meta["sample_rate"]))
    assert desc.horizontal_offset == pytest.approx(delay)
    assert desc.probe == pytest.approx(float(meta["probe"]))
    assert desc.vertical_gain * desc.probe == pytest.approx(float(meta["scale"]))
    raw = (FIXTURES / "sds824x_data.bin").read_bytes()
    head = waveform.block_header(raw)
    # The guide's "0A 0A" trailer follows the block: two bytes the block reader drops.
    assert head is not None and head[1] == 2000 and meta["data_len"] == head[0] + head[1] + 2
    payload = waveform.block_payload(raw)
    values = waveform.samples(desc, payload)
    codes = struct.unpack(f"<{len(payload) // 2}h", payload)
    assert len(values) == len(codes) == 1000
    times = (waveform.sample_offsets_ns(desc, len(values), timebase, 10) / 1e9).tolist()
    pitch = desc.horizontal_interval * desc.step
    assert pitch == pytest.approx(5e-6)
    assert times[0] == pytest.approx(delay - timebase * 5)
    assert times[-1] - times[0] == pytest.approx(pitch * 999)
    assert pitch * 1000 == pytest.approx(timebase * 10, rel=1e-6)
    # A 12-bit code left-aligned in 16 bits: 7680 codes per division at 0.5 V/div with a 10x
    # probe, so every sample sits within the screen's eight divisions.
    assert all(abs(v) <= 4.5 for v in values)
    assert values[0] == pytest.approx(
        codes[0] * desc.vertical_gain / desc.code_per_div * desc.probe
        - desc.vertical_offset * desc.probe
    )
    # The sign of the delay: the sample at t = 0 is the trigger, where the rising edge crosses
    # the trigger level. A delay added instead of subtracted would put t = 0 two edges away.
    level = float(meta["trigger_level"])
    at_trigger = round(-times[0] / pitch)
    assert abs(times[at_trigger]) < pitch / 10
    assert values[at_trigger - 1] < level <= values[at_trigger + 1]
    assert values[at_trigger] == pytest.approx(level, abs=0.05)
    crossings = [i for i in range(1, len(values)) if values[i - 1] < level <= values[i]]
    assert at_trigger in crossings


# ---------------------------------------------------------------- discovery


def test_a_held_instrument_is_held_under_its_other_address_too(
    monkeypatch: pytest.MonkeyPatch, profiles: list[Profile]
) -> None:
    held = discovery.Found(
        VisaAddress("TCPIP::10.0.0.5::INSTR"), None, None, None, "vxi11", "v", None
    )
    sock = discovery.Found(TcpAddress("10.0.0.5", 5025), None, None, None, "scan", "s", None)
    monkeypatch.setattr(
        discovery, "list_lan", lambda budget_s, tried=None, deadline_s=10.0: [held, sock]
    )
    monkeypatch.setattr(
        discovery, "identify", lambda f, p, t=2.0: pytest.fail("opened a held host")
    )
    found = discovery.discover(profiles, usb=False, budget_s=0.01, skip={"10.0.0.5:5025"})
    assert [(f.address.where, f.error) for f in found] == [
        ("10.0.0.5:5025", discovery.IN_USE),
    ]


def test_a_scope_profile_without_slots_is_refused() -> None:
    from zelos_extension_instruments.profile_load import ProfileError, from_dict

    raw = {
        "schema": 1,
        "id": "acme-scope",
        "name": "Acme scope",
        "category": "scope",
        "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-21"},
        "match": {"manufacturer": "(?i)^acme", "model": "^S1$"},
        "state": {"timebase": {"query": ":TIM:SCAL?", "parse": "float"}},
    }
    with pytest.raises(ProfileError, match="scope: missing"):
        from_dict(raw, "acme-scope.toml")
    raw["scope"] = {
        "slots": {
            "count": 2,
            "enabled": "P{slot}?",
            "type": "T{slot}?",
            "source": "S{slot}?",
            "source_pattern": "C(?P<ch>\\d)",
            "value": "V{slot}?",
            "types": {"PKPK": "nope"},
        }
    }
    with pytest.raises(ProfileError, match="'nope' is not one of"):
        from_dict(raw, "acme-scope.toml")


def test_measure_reuses_the_slot_that_already_shows_the_measurement() -> None:
    from zelos_extension_instruments.sim.fake import FakeInstrument

    profile = by_id(shipped(), "siglent-sds")
    assert profile is not None
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(name="unit", profile=profile.id, rate_hz=50)
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        first = ask(worker, "measure", channel=1, type="pkpk")
        again = ask(worker, "measure", channel=1, type="pkpk")
        assert first["slot"] == again["slot"] == 1
        # A slot that shows something the profile does not record is never taken over.
        assert fake.scope is not None
        fake.scope.slots[2] = (True, "DELAY", "C1")
        other = ask(worker, "measure", channel=2, type="rms")
        assert other["slot"] == 3
    finally:
        worker.stop()
        worker.join(5)
