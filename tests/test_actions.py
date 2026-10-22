"""The actions as the agent runs them: through the SDK registry, against workers on scripted
links."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from zelos_sdk.actions import ActionExecutionError, ValidationError

from tests.conftest import (
    Dp832,
    RecordingEmitter,
    ScriptedLink,
    instrument_config,
    opener_for,
    wait_until,
)
from zelos_extension_instruments import actions, discovery
from zelos_extension_instruments.link import TcpAddress, VisaAddress
from zelos_extension_instruments.profile import Profile, by_id, parse_idn
from zelos_extension_instruments.sim.fake import FakeInstrument
from zelos_extension_instruments.worker import InstrumentWorker

ACTIONS = {
    name: getattr(actions, name)._action
    for name in dir(actions)
    if hasattr(getattr(actions, name), "_action")
}


def execute(path: str, **params: Any) -> Any:
    """What the agent gets back when it runs the action with these parameters."""
    return ACTIONS[path].execute(**params)


def run(path: str, **params: Any) -> Any:
    """The action's value, as the agent runs it with validated parameters."""
    return execute(path, **params).value


@pytest.fixture
def bench(dp800: Profile) -> Iterator[tuple[Dp832, RecordingEmitter]]:
    """Two running instruments, `psu` and `aux`, both scripted supplies."""
    units = {"psu": Dp832(), "aux": Dp832()}
    emitter = RecordingEmitter()
    actions.workers.clear()
    for name, unit in units.items():
        cfg = instrument_config(name=name, rate_hz=50)

        def opener(address: Any, settings: Any, unit: Dp832 = unit, name: str = name) -> Any:
            return ScriptedLink(unit, name)

        worker = InstrumentWorker(cfg, [dp800], emitter, open=opener)
        worker.start()
        actions.workers[name] = worker
    assert wait_until(lambda: all(w.state == "open" for w in actions.workers.values()))
    yield units["psu"], emitter
    for worker in actions.workers.values():
        worker.stop()
    for worker in actions.workers.values():
        worker.join(2)
    actions.workers.clear()


def test_the_registry_holds_every_action_with_its_read_only_flag() -> None:
    flags = {name: action.read_only for name, action in ACTIONS.items()}
    assert flags == {
        "list_instruments": True,
        "list_profiles": True,
        "auto_config": True,
        "set_values": False,
        "output": False,
        "read": True,
        "get_state": True,
        "scpi": False,
        "clear": False,
        "local": False,
        "release": False,
        "acquire": False,
        "set_channel": False,
        "set_timebase": False,
        "set_trigger": False,
        "run_control": False,
        "measure": False,
        "capture_waveform": False,
    }


def test_instrument_field_defaults_to_the_first_configured(bench) -> None:
    schema = ACTIONS["read"].to_schema()
    assert schema["properties"]["instrument"]["default"] == "psu"
    assert schema["properties"]["instrument"]["enum"] == ["aux", "psu"]


def test_set_and_output_through_the_registry(bench) -> None:
    psu, _ = bench
    assert run("set_values", instrument="psu", channel=2, voltage=9.5, current=0.2) == {
        "voltage": {"set": 9.5, "readback": 9.5},
        "current": {"set": 0.2, "readback": 0.2},
    }
    assert psu.volt[2] == 9.5
    assert run("output", instrument="psu", channel=2, on=True) == {"output": True}
    assert psu.out[2] is True


def test_a_failed_set_returns_the_reason_as_an_error(bench) -> None:
    with pytest.raises(ActionExecutionError, match="voltage must be from 0 to 30"):
        execute("set_values", instrument="psu", channel=1, voltage=99.0)


def test_set_with_nothing_to_set_is_refused(bench) -> None:
    with pytest.raises(ActionExecutionError, match="give at least one value to set"):
        execute("set_values", instrument="psu", channel=1)


def test_read_state_scpi_and_clear(bench) -> None:
    assert run("read", instrument="psu", channel=1) == {
        "voltage": 5.0,
        "current": 0.5,
        "power": 2.5,
    }
    state = run("get_state", instrument="psu")
    assert state["state"] == "open" and state["profile"]["id"] == "rigol-dp800"
    assert run("scpi", instrument="aux", command="*IDN?")["reply"].startswith("RIGOL")
    assert run("clear", instrument="psu", channel=1) == {"ok": True, "errors_cleared": 0}


def test_the_first_instrument_is_the_default_target(bench) -> None:
    assert run("get_state")["identity"] == "RIGOL TECHNOLOGIES,DP832A,DP8C000001,00.01.16"
    assert actions._first_instrument() == "psu"


def test_an_unknown_instrument_name_is_an_error(bench) -> None:
    with pytest.raises(ValidationError, match="Invalid choice: nope"):
        execute("get_state", instrument="nope")


def test_release_and_acquire(bench) -> None:
    assert run("release", instrument="psu") == {"ok": True}
    assert actions.workers["psu"].state == "released"
    assert run("acquire", instrument="psu") == {"ok": True}
    assert actions.workers["psu"].state == "open"


def test_local_without_a_local_command_says_so(bench) -> None:
    assert run("local", instrument="psu") == {"ok": True}


def test_list_profiles_offers_auto_then_every_shipped_profile(profiles: list[Profile]) -> None:
    result = run("list_profiles")
    assert result["choices"][0]["value"] == "auto"
    assert {c["value"] for c in result["choices"][1:]} == {p.id for p in profiles}
    badges = {c["value"]: c["detail"].rsplit(", ", 1)[1] for c in result["choices"][1:]}
    assert badges == {p.id: p.evidence.badge for p in profiles}
    assert badges["siglent-sds"] == "transcript"


def _found(
    address: Any, idn: str | None, profile_id: str | None, source: str, error: str | None = None
) -> discovery.Found:
    return discovery.Found(
        address=address,
        idn=parse_idn(idn) if idn else None,
        profile_id=profile_id,
        category="psu" if profile_id else None,
        source=source,
        detail=address.where,
        error=error,
    )


def test_list_instruments_turns_found_units_into_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [
        _found(
            TcpAddress("192.168.1.40", 5555), "RIGOL TECHNOLOGIES,DP832A,X,1", "rigol-dp800", "mdns"
        ),
        _found(
            TcpAddress("192.168.1.41", 5025),
            None,
            None,
            "vxi11",
            "no reply from 192.168.1.41:5025 within 2 s",
        ),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    result = run("list_instruments", connection="lan")
    assert result["choices"] == [
        {
            "value": "192.168.1.40:5555",
            "label": "RIGOL TECHNOLOGIES DP832A (192.168.1.40:5555)",
            "detail": "profile rigol-dp800",
        },
        {
            "value": "192.168.1.41",
            "label": "192.168.1.41:5025",
            "detail": "no reply from 192.168.1.41:5025 within 2 s",
        },
    ]


def test_list_instruments_with_nothing_found_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [])
    result = run("list_instruments", connection="usb")
    assert result["choices"] == []
    assert result["message"].startswith("No instruments found. Searched: ")


def test_list_instruments_serial_lists_every_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "list_all_serial_ports",
        lambda: [discovery.SerialPortChoice("COM7", "USB Serial (COM7)", "1a86:7523")],
    )
    assert run("list_instruments", connection="serial")["choices"] == [
        {"value": "COM7", "label": "USB Serial (COM7)", "detail": "1a86:7523"}
    ]


def test_auto_config_adds_found_instruments_and_keeps_the_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [
        _found(
            TcpAddress("192.168.1.40", 5555),
            "RIGOL TECHNOLOGIES,DP832A,DP8C1,1",
            "rigol-dp800",
            "mdns",
        ),
        _found(
            VisaAddress("USB0::0xF4EC::0x1430::SDL1::INSTR"),
            "Siglent Technologies,SDL1020X,SDL1,1",
            "siglent-sdl1000x",
            "usb",
        ),
        _found(TcpAddress("192.168.1.9", 5025), None, None, "vxi11", "no reply"),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    form = {
        "instruments": [
            {"connection": "lan", "host": "192.168.1.40", "name": "bench psu", "rate_hz": 2}
        ]
    }
    result = run("auto_config", config=form)
    assert result["config"]["instruments"] == [
        {"connection": "lan", "host": "192.168.1.40", "name": "bench psu", "rate_hz": 2},
        {
            "connection": "usb",
            "resource": "USB0::0xF4EC::0x1430::SDL1::INSTR",
            "profile": "siglent-sdl1000x",
            "name": "SDL1020X",
        },
    ]
    assert result["message"] == (
        "Added 1: SDL1020X (USB0::0xF4EC::0x1430::SDL1::INSTR). "
        "Found but not identified: 192.168.1.9:5025: no reply."
    )


def test_auto_config_keeps_out_the_demo_bench_when_an_instrument_did_not_identify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [_found(VisaAddress("TCPIP::10.0.0.5::INSTR"), None, None, "vxi11", "no reply")]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    result = run("auto_config", config={"instruments": []})
    assert result["config"]["instruments"] == []
    assert result["message"] == "Found but not identified: TCPIP::10.0.0.5::INSTR: no reply."


def test_auto_config_matches_a_typed_host_name_to_the_address_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [_found(TcpAddress("127.0.0.1", 5025), "ACME,P1,1,1", "rigol-dp800", "scan")]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    form = {"instruments": [{"connection": "lan", "host": "localhost", "name": "psu"}]}
    result = run("auto_config", config=form)
    assert result["config"]["instruments"] == form["instruments"]
    assert result["message"].startswith("Already in the form: 127.0.0.1:5025")


def test_a_model_name_with_accents_becomes_a_name_the_config_accepts() -> None:
    import re

    for model, expected in (("Prüfgerät", "Prufgerat"), ("Ωµ", "instrument")):
        name = discovery.default_name(
            _found(TcpAddress("10.0.0.5", 5025), f"ACME,{model},1,1", None, "scan")
        )
        assert name == expected
        assert re.fullmatch(r"[A-Za-z0-9 _-]*", name)


def test_auto_config_with_nothing_found_adds_the_demo_bench(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [])
    result = run("auto_config", config={})
    assert [i["category"] for i in result["config"]["instruments"]] == [
        "psu",
        "load",
        "dmm",
        "acsource",
    ]
    assert result["message"].endswith(" Added the demo bench.")


def test_auto_config_with_nothing_new_keeps_the_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [])
    form = {"instruments": [{"connection": "demo", "category": "psu"}]}
    result = run("auto_config", config=form)
    assert result["config"] == form
    assert result["message"].startswith("No instruments found. Searched: ")


def test_list_instruments_keeps_a_port_the_instrument_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = [
        _found(
            TcpAddress("192.168.1.40", 5555), "RIGOL TECHNOLOGIES,DP832A,X,1", "rigol-dp800", "mdns"
        ),
        _found(
            TcpAddress("192.168.1.41", 5025),
            "Keysight Technologies,E36313A,Y,1",
            "keysight-e36300",
            "mdns",
        ),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    values = [c["value"] for c in run("list_instruments", connection="lan")["choices"]]
    assert values == ["192.168.1.40:5555", "192.168.1.41"]


def test_list_instruments_for_visa_offers_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [
        _found(
            VisaAddress("TCPIP::10.0.0.5::INSTR"),
            "Siglent Technologies,SDS824X HD,SN1,3.8",
            None,
            "vxi11",
        ),
        _found(
            TcpAddress("10.0.0.6", 5025), "RIGOL TECHNOLOGIES,DP832A,X,1", "rigol-dp800", "scan"
        ),
    ]
    asked: dict[str, Any] = {}

    def discover(*args: Any, **kwargs: Any) -> list[Any]:
        asked.update(kwargs)
        return found

    monkeypatch.setattr(discovery, "discover", discover)
    result = run("list_instruments", connection="visa")
    assert asked["lan"] is True and asked["usb"] is False and asked["prefer"] == "visa"
    assert [c["value"] for c in result["choices"]] == [
        "TCPIP::10.0.0.5::INSTR",
        "TCPIP::10.0.0.6::5025::SOCKET",
    ]
    assert result["choices"][0]["label"] == (
        "Siglent Technologies SDS824X HD (TCPIP::10.0.0.5::INSTR)"
    )


def test_list_instruments_names_an_instrument_a_worker_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _found(TcpAddress("10.0.0.5", 5025), None, None, "scan", discovery.IN_USE)
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [held])

    class Holder:
        where = "10.0.0.5:5025"
        idn = parse_idn("RIGOL TECHNOLOGIES,DP832A,X,1")

    monkeypatch.setitem(actions.workers, "psu", Holder())  # type: ignore[arg-type]
    choice = run("list_instruments", connection="lan")["choices"][0]
    assert choice["label"] == "RIGOL TECHNOLOGIES DP832A (10.0.0.5:5025)"
    assert choice["detail"] == "in use by this extension"


def _holder(name: str, entry: dict[str, Any], where: str, idn: str) -> Any:
    """A running worker as the actions see it."""
    return SimpleNamespace(
        cfg=SimpleNamespace(name=name, entry=entry), where=where, idn=parse_idn(idn)
    )


_SCOPE = {"connection": "visa", "resource": "TCPIP::10.0.0.5::INSTR", "name": "scope"}
_SCOPE_IDN = "Siglent Technologies,SDS824X HD,SN1,3.8"


def test_auto_config_hands_back_a_running_instrument_the_form_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty form while the scope records got the demo bench, and saving it dropped the scope."""
    held = _found(TcpAddress("10.0.0.5", 5025), None, None, "mdns", discovery.IN_USE)
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [held])
    monkeypatch.setitem(
        actions.workers, "scope", _holder("scope", _SCOPE, "TCPIP::10.0.0.5::INSTR", _SCOPE_IDN)
    )
    result = run("auto_config", config={})
    assert result["config"]["instruments"] == [_SCOPE]
    assert result["message"] == "Already recording: scope (TCPIP::10.0.0.5::INSTR)."


def test_auto_config_keeps_a_running_instrument_the_form_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = [
        _found(TcpAddress("10.0.0.5", 5025), None, None, "mdns", discovery.IN_USE),
        _found(TcpAddress("10.0.0.5", 5555), None, None, "scan", discovery.IN_USE),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: held)
    monkeypatch.setitem(
        actions.workers, "scope", _holder("scope", _SCOPE, "TCPIP::10.0.0.5::INSTR", _SCOPE_IDN)
    )
    form = {"instruments": [dict(_SCOPE, name="bench scope")]}
    result = run("auto_config", config=form)
    assert result["config"] == form
    assert result["message"] == "Already recording: scope (TCPIP::10.0.0.5::INSTR)."


def test_auto_config_knows_an_instrument_the_form_names_another_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = _found(TcpAddress("10.0.0.5", 5025), _SCOPE_IDN, "siglent-sds", "mdns")
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [found])
    form = {"instruments": [_SCOPE]}
    result = run("auto_config", config=form)
    assert result["config"] == form
    assert result["message"] == "Already in the form: 10.0.0.5:5025."


def test_list_instruments_names_an_instrument_held_under_its_other_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _found(TcpAddress("10.0.0.5", 5025), None, None, "mdns", discovery.IN_USE)
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [held])
    monkeypatch.setitem(
        actions.workers, "scope", _holder("scope", _SCOPE, "TCPIP::10.0.0.5::INSTR", _SCOPE_IDN)
    )
    choice = run("list_instruments", connection="lan")["choices"][0]
    assert choice["label"] == "Siglent Technologies SDS824X HD (10.0.0.5:5025)"


def test_list_instruments_for_lan_never_offers_a_resource_as_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VISA resource picked into the Host field made a config that could not connect."""
    found = [
        _found(
            TcpAddress("10.0.0.6", 5025), "RIGOL TECHNOLOGIES,DP832A,X,1", "rigol-dp800", "scan"
        ),
        _found(VisaAddress("TCPIP::10.0.0.5::INSTR"), _SCOPE_IDN, "siglent-sds", "vxi11"),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    result = run("list_instruments", connection="lan")
    assert [c["value"] for c in result["choices"]] == ["10.0.0.6"]
    assert result["message"] == (
        "Reachable only as a VISA resource: Siglent Technologies SDS824X HD "
        "(TCPIP::10.0.0.5::INSTR). Set Connection to VISA resource to use it."
    )


def test_auto_config_names_two_of_one_model_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    found = [
        _found(
            TcpAddress("10.0.0.6", 5555), "RIGOL TECHNOLOGIES,DP832A,A1,1", "rigol-dp800", "scan"
        ),
        _found(
            TcpAddress("10.0.0.7", 5555), "RIGOL TECHNOLOGIES,DP832A,A2,1", "rigol-dp800", "scan"
        ),
        _found(
            TcpAddress("10.0.0.8", 5555), "RIGOL TECHNOLOGIES,DP832A,A3,1", "rigol-dp800", "scan"
        ),
    ]
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: found)
    result = run("auto_config", config={})
    assert [i["name"] for i in result["config"]["instruments"]] == [
        "DP832A",
        "DP832A_1",
        "DP832A_2",
    ]


@pytest.fixture
def scope_bench(profiles: list[Profile]) -> Iterator[tuple[FakeInstrument, RecordingEmitter]]:
    """One running scope, `scope`, on the simulator."""
    profile = by_id(profiles, "siglent-sds")
    assert profile is not None
    fake = FakeInstrument(profile)
    emitter = RecordingEmitter()
    actions.workers.clear()
    cfg = instrument_config(name="scope", profile=profile.id, rate_hz=50)
    link = ScriptedLink(fake.handle, "scope")
    worker = InstrumentWorker(cfg, [profile], emitter, open=opener_for(link))
    worker.start()
    actions.workers["scope"] = worker
    assert wait_until(lambda: worker.state == "open", timeout_s=10), worker.health
    yield fake, emitter
    worker.stop()
    worker.join(5)
    actions.workers.clear()


def test_set_channel_sets_every_channel_value_it_is_given(scope_bench) -> None:
    fake, _ = scope_bench
    assert run(
        "set_channel",
        instrument="scope",
        channel=2,
        scale=0.2,
        offset=-0.1,
        coupling="ac",
        probe=10.0,
        visible=False,
    ) == {
        "scale": {"set": 0.2, "readback": 0.2},
        "offset": {"set": -0.1, "readback": -0.1},
        "coupling": "ac",
        "probe": {"set": 10.0, "readback": 10.0},
        "visible": False,
    }
    assert fake.scope is not None
    assert fake.scope.per_channel[2] == {
        "visible": False,
        "scale": 0.2,
        "offset": -0.1,
        "coupling": "ac",
        "bandwidth_limit": "full",
        "probe": 10.0,
    }
    assert fake.scope.per_channel[1]["scale"] == 0.5


def test_set_timebase_sets_the_sweep_and_the_delay(scope_bench) -> None:
    fake, _ = scope_bench
    assert run("set_timebase", instrument="scope", timebase=0.001, delay=0.0002) == {
        "timebase": {"set": 0.001, "readback": 0.001},
        "delay": {"set": 0.0002, "readback": 0.0002},
    }
    assert fake.scope is not None
    assert (fake.scope.shared["timebase"], fake.scope.shared["delay"]) == (0.001, 0.0002)


def test_set_trigger_sets_source_level_slope_and_mode(scope_bench) -> None:
    fake, _ = scope_bench
    assert run(
        "set_trigger", instrument="scope", source="ch2", level=0.3, slope="falling", mode="normal"
    ) == {
        "trigger_source": "ch2",
        "trigger_level": {"set": 0.3, "readback": 0.3},
        "trigger_slope": "falling",
        "trigger_mode": "normal",
    }
    assert fake.scope is not None
    shared = fake.scope.shared
    assert shared["trigger_source"] == "ch2" and shared["trigger_level"] == 0.3
    assert shared["trigger_slope"] == "falling" and shared["trigger_mode"] == "normal"


def test_run_control_sends_the_command_it_names(scope_bench) -> None:
    fake, _ = scope_bench
    assert fake.scope is not None
    assert run("run_control", instrument="scope", command="stop") == {
        "ok": True,
        "command": "stop",
    }
    assert fake.scope.running is False
    assert run("run_control", instrument="scope", command="single")["command"] == "single"
    assert fake.scope.shared["trigger_mode"] == "single"
    assert run("run_control", instrument="scope", command="run")["command"] == "run"
    assert fake.scope.running is True


def test_measure_shows_a_measurement_in_the_slot_asked_for_and_clears_it(scope_bench) -> None:
    fake, _ = scope_bench
    shown = run("measure", instrument="scope", channel=2, type="frequency", slot=3)
    assert shown["slot"] == 3 and shown["channel"] == 2 and shown["type"] == "frequency"
    assert shown["value"] == pytest.approx(1000.0)
    assert fake.scope is not None
    assert fake.scope.slots[3] == (True, "FREQ", "C2")
    assert run("measure", instrument="scope", channel=2, type="frequency", slot=3, off=True) == {
        "slot": 3,
        "off": True,
    }
    assert fake.scope.slots[3][0] is False


def test_capture_waveform_fetches_the_channel_and_points_it_is_given(scope_bench) -> None:
    _, emitter = scope_bench
    captured = run("capture_waveform", instrument="scope", channel=2, points=50)
    assert captured["channel"] == 2 and captured["samples"] == 50
    [(name, ch, samples)] = emitter.waveforms
    assert (name, ch, len(samples)) == ("scope", 2, 50)


def test_an_action_the_worker_does_not_answer_in_time_is_cancelled(
    dp800: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker never runs a request the person was told had failed."""
    idle = InstrumentWorker(instrument_config(name="idle"), [dp800], RecordingEmitter())
    monkeypatch.setattr(actions, "workers", {"idle": idle})
    with pytest.raises(RuntimeError, match=r"^idle did not answer within 0\.1 s$"):
        actions._ask("idle", "state", 0.6)
    [request] = idle._requests
    assert request.future.cancelled()


def test_an_action_for_an_instrument_that_is_not_configured_names_the_ones_that_are(
    monkeypatch: pytest.MonkeyPatch, dp800: Profile
) -> None:
    workers = {
        name: InstrumentWorker(instrument_config(name=name), [dp800], RecordingEmitter())
        for name in ("psu", "aux")
    }
    monkeypatch.setattr(actions, "workers", workers)
    with pytest.raises(ValueError, match=r"^no instrument is named nope; configured: psu, aux$"):
        actions._ask("nope", "state", 1.0)


def test_an_action_with_no_instrument_configured_says_how_to_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(actions, "workers", {})
    with pytest.raises(ActionExecutionError, match="no instrument is configured: add one"):
        execute("get_state")
