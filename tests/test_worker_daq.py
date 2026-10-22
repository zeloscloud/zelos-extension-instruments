"""A worker on a data-acquisition unit: its addresses, the scan it sets up and reads each pass,
and the relays it reads and switches."""

import copy
from typing import Any

import pytest

from tests.conftest import (
    RecordingEmitter,
    ScriptedLink,
    ask,
    connected,
    instrument_config,
    opener_for,
    wait_until,
)
from tests.test_daq_ops import KEYSIGHT_STYLE, SCAN
from tests.test_serve import served
from zelos_extension_instruments import system_ops
from zelos_extension_instruments.link import LinkError
from zelos_extension_instruments.profile import Profile
from zelos_extension_instruments.profile_load import from_dict, shipped
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.sim.fake_daq import FakeDaq
from zelos_extension_instruments.worker import InstrumentWorker

RELAY = 110


def daq_profile(raw: dict[str, Any] = KEYSIGHT_STYLE) -> Profile:
    return from_dict(raw, f"{raw['id']}.toml")


def daq_bench(
    raw: dict[str, Any] = KEYSIGHT_STYLE, relays: tuple[int, ...] = (RELAY,)
) -> tuple[InstrumentWorker, FakeDaq, ScriptedLink, RecordingEmitter]:
    """A worker connected to a fake unit that scans SCAN and has the given relays."""
    profile = daq_profile(raw)
    fake = FakeDaq(profile)
    worker, link, emitter = connected(
        profile,
        fake.handle,
        name="daq",
        profile=profile.id,
        advanced={"scan": SCAN, "relays": list(relays)},
    )
    return worker, fake, link, emitter


def test_the_channels_are_the_configured_addresses_and_the_scan_is_set_up_at_connect() -> None:
    worker, fake, link, emitter = daq_bench()
    assert list(worker._channel) == [101, 102, 103, 104, 105, RELAY]
    assert worker.channels == 6
    assert "ROUT:SCAN (@101,102,103,104,105)" in link.commands
    assert fake.scan_list == (101, 102, 103, 104, 105)
    assert emitter.channel_fields["daq"] == {
        101: {"dc_voltage"},
        102: {"dc_voltage"},
        103: {"temperature"},
        104: {"resistance"},
        105: {"temperature"},
        RELAY: {"closed"},
    }
    assert worker.health.startswith("Connected to ")
    assert "Reading 6 signals on 6 channels at " in worker.health


def test_each_pass_reads_one_scan_and_the_slow_round_reads_the_relays() -> None:
    worker, fake, link, emitter = daq_bench()
    fake.set_reading(101, 1.25)
    fake.set_reading(103, 21.5)
    fake.closed.add(RELAY)
    worker._cycle()
    assert link.commands.count("READ?") == 1
    assert "ROUT:CLOSE? (@110)" in link.commands
    rows = {ch: values for _, ch, values, _ in emitter.rows}
    assert rows[101] == {"dc_voltage": 1.25}
    assert rows[103] == {"temperature": 21.5}
    assert rows[RELAY] == {"closed": True}
    assert worker._channel[RELAY].values == {"closed": True}


def test_a_profile_that_reads_the_relays_itself_is_read_with_its_own_reader() -> None:
    raw = copy.deepcopy(KEYSIGHT_STYLE)
    raw["state"] = {"closed": {"query": "ROUT:CLOSE? (@{ch})", "parse": "bool"}}
    worker, fake, _, emitter = daq_bench(raw)
    assert worker._plan is not None and worker.profile is not None
    assert [step.reader for step in worker._plan.slow] == [worker.profile.state[0]]
    fake.closed.add(RELAY)
    worker._cycle()
    assert ("daq", RELAY, {"closed": True}) in [r[:3] for r in emitter.rows]


def test_a_relay_is_closed_and_opened_with_its_own_commands_and_read_back() -> None:
    worker, fake, link, emitter = daq_bench()
    sent = len(link.commands)
    assert served(worker, "set", channel=RELAY, relay=True) == {"relay": True}
    assert fake.closed == {RELAY}
    assert served(worker, "set", channel=RELAY, relay=False) == {"relay": False}
    assert fake.closed == set()
    writes = [c for c in link.commands[sent:] if "?" not in c]
    assert writes == ["ROUT:CLOSE (@110)", "ROUT:OPEN (@110)"]
    assert [values for _, ch, values, _ in emitter.rows if ch == RELAY] == [
        {"closed": True},
        {"closed": False},
    ]


def test_a_scan_channel_is_not_switched_as_a_relay() -> None:
    worker, fake, link, _ = daq_bench()
    sent = len(link.commands)
    assert served(worker, "set", channel=101, relay=True) == {
        "error": "channel 101 is not a relay; list it under Relays in the config"
    }
    assert link.commands[sent:] == ["SYST:ERR?"]
    assert fake.closed == set()


def test_an_address_the_unit_does_not_have_is_refused_with_the_ones_it_has() -> None:
    worker, _, _, _ = daq_bench()
    assert served(worker, "read", channel=1) == {
        "error": "channel must be one of 101, 102, 103, 104, 105, 110"
    }


def test_a_read_request_scans_again_for_a_scan_channel() -> None:
    worker, fake, _, _ = daq_bench()
    fake.set_reading(104, 470.0)
    assert served(worker, "read", channel=104) == {"resistance": 470.0}


def test_a_scan_reply_that_does_not_parse_is_logged_and_the_link_put_back_in_step() -> None:
    worker, fake, _, emitter = daq_bench()
    worker._cycle()
    fake.scan_list = (101,)
    worker._cycle()
    assert worker._link is not None
    problem = "READ?: expected 5 readings, got 1: '+5.00000000E+00'"
    assert emitter.notes[-2:] == [
        ("warn", problem, "instruments"),
        ("warn", f"replies were out of step ({problem}); back in step", "instruments"),
    ]


def test_a_scan_that_keeps_missing_its_setup_reopens_the_link_which_sets_it_up_again() -> None:
    worker, fake, link, _ = daq_bench()
    worker._cycle()
    fake.scan_list = (101,)
    worker._cycle()
    worker._cycle()
    with pytest.raises(LinkError, match="^3 scans in a row did not read as set up: "):
        worker._cycle()
    worker._lost("a test")
    link.closed = False
    assert worker._connect(), worker.health
    assert fake.scan_list == (101, 102, 103, 104, 105)
    worker._cycle()
    assert worker._bad_scans == 0


def test_a_good_scan_between_bad_ones_starts_the_count_over() -> None:
    worker, fake, _, _ = daq_bench()
    for _ in range(2):
        fake.scan_list = (101,)
        worker._cycle()
    fake.scan_list = (101, 102, 103, 104, 105)
    worker._cycle()
    fake.scan_list = (101,)
    worker._cycle()
    worker._cycle()
    assert worker._link is not None and worker._bad_scans == 2


def recalling_bench() -> tuple[InstrumentWorker, FakeDaq, ScriptedLink]:
    """A unit whose saved state 1 has every relay closed and no scan set up."""
    raw = copy.deepcopy(KEYSIGHT_STYLE)
    raw["system"] = {"save": "*SAV {slot}", "recall": "*RCL {slot}", "slots": {"min": 1, "max": 5}}
    profile = daq_profile(raw)
    fake = FakeDaq(profile)

    def handle(command: str) -> str | bytes | None:
        if command == "*RCL 1":
            fake.scan_list = ()
            fake.functions.clear()
            fake.closed = {RELAY, 111}
            return None
        return fake.handle(command)

    worker, link, _ = connected(
        profile,
        handle,
        name="daq",
        profile=profile.id,
        advanced={"scan": SCAN, "relays": [RELAY, 111]},
    )
    return worker, fake, link


def test_a_reset_sets_the_scan_up_again_and_reports_the_relays_it_opened() -> None:
    worker, fake, link, _ = daq_bench()
    fake.closed.add(RELAY)
    assert system_ops.reset(worker) == {
        "ok": True,
        "outputs_off": [],
        "relays_opened": [RELAY],
        "relays_closed": [],
    }
    assert fake.scan_list == (101, 102, 103, 104, 105)
    assert worker._channel[RELAY].values["closed"] is False
    mark = len(link.commands)
    worker._cycle()
    assert "READ?" in link.commands[mark:] and worker._bad_scans == 0


def test_a_recall_sets_the_scan_up_again_and_reports_the_relays_it_closed() -> None:
    worker, fake, _ = recalling_bench()
    fake.closed.add(111)
    assert system_ops.recall(worker, 1) == {
        "ok": True,
        "slot": 1,
        "outputs_on": [],
        "outputs_off": [],
        "relays_opened": [],
        "relays_closed": [RELAY],
    }
    assert fake.scan_list == (101, 102, 103, 104, 105)


def test_an_address_the_profile_cannot_reach_fails_the_open_as_a_config_mistake() -> None:
    profile = daq_profile()
    fake = FakeDaq(profile)
    link = ScriptedLink(fake.handle)
    cfg = instrument_config(
        name="daq", profile=profile.id, advanced={"scan": SCAN, "relays": [401]}
    )
    worker = InstrumentWorker(cfg, [profile], RecordingEmitter(), open=opener_for(link))
    assert not worker._connect()
    assert worker.state == "failed"
    assert worker.health == (
        "Cannot open 127.0.0.1:5025: channel 401 is not an address this instrument has. "
        "Fix the setting the message names. Retrying."
    )
    assert link.closed
    assert not any(c.startswith("ROUT:SCAN") for c in link.commands)


SHIPPED_DAQ = [p for p in shipped() if p.category == "daq"]


@pytest.mark.parametrize("profile", SHIPPED_DAQ, ids=lambda p: p.id)
def test_every_shipped_daq_profile_scans_and_switches_through_a_worker(profile: Profile) -> None:
    assert profile.daq is not None
    pattern = profile.daq.channel_pattern
    first, second, relay = [n for n in range(1, 10_000) if pattern.fullmatch(str(n))][:3]
    scan: list[dict[str, Any]] = [{"channel": first, "function": "dc_voltage"}]
    if "temperature" in profile.daq.configure:
        scan.append({"channel": second, "function": "temperature", "thermocouple": "K"})
    fake = FakeInstrument(profile)
    link = ScriptedLink(fake.handle, profile.id)
    emitter = RecordingEmitter()
    cfg = instrument_config(
        name="unit",
        profile=profile.id,
        rate_hz=50,
        advanced={"scan": scan, "relays": [relay]},
    )
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    try:
        assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
        expected = {entry["function"] for entry in scan} | {"closed"}
        assert wait_until(
            lambda: expected <= {k for r in emitter.rows for k in r[2]}, timeout_s=10
        ), worker.health
        assert ask(worker, "set", channel=relay, relay=True) == {"relay": True}
        assert ask(worker, "set", channel=relay, relay=False) == {"relay": False}
        counters = ask(worker, "state")["counters"]
        assert counters["timeouts"] == 0
        assert counters["instrument_errors"] == 0, counters["last_error"]
    finally:
        worker.stop()
        worker.join(5)
