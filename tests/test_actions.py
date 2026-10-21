"""The actions as the agent runs them: through the SDK registry, against workers on scripted
links."""

from collections.abc import Iterator
from typing import Any

import pytest
from zelos_sdk.actions import ActionExecutionError, ValidationError

from tests.conftest import ScriptedLink, instrument_config, settings_for, wait_until
from tests.test_worker import Dp832, RecordingEmitter
from zelos_extension_instruments import actions, discovery
from zelos_extension_instruments.link import TcpAddress, VisaAddress
from zelos_extension_instruments.profile import Profile, parse_idn
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

        worker = InstrumentWorker(cfg, [dp800], emitter, settings_for(cfg), open=opener)
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
            "name": "SDL1020X_SDL1",
        },
    ]
    assert result["message"] == (
        "Added 1: SDL1020X_SDL1 (USB0::0xF4EC::0x1430::SDL1::INSTR). "
        "Not identified: 192.168.1.9:5025: no reply."
    )


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
    assert result["message"].startswith("No new instruments found. No instruments found.")


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


def test_auto_config_reports_an_instrument_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _found(TcpAddress("10.0.0.5", 5025), None, None, "scan", discovery.IN_USE)
    monkeypatch.setattr(discovery, "discover", lambda *a, **k: [held])
    result = run("auto_config", config={"instruments": [{"connection": "demo", "category": "psu"}]})
    assert result["config"]["instruments"] == [{"connection": "demo", "category": "psu"}]
    assert result["message"].startswith("No new instruments found.")
    assert result["message"].endswith(" Already running in this extension: 10.0.0.5:5025.")
