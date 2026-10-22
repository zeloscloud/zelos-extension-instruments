"""The extension end to end: run() with a config, a scripted supply, and a trace read back."""

import threading
import time
from typing import Any

from tests.conftest import Dp832, Recording, ScriptedLink, wait_until
from zelos_extension_instruments import actions, app, config
from zelos_extension_instruments.profile import Profile


def test_run_records_a_supply_and_serves_actions(
    trace_file: Recording, profiles: list[Profile]
) -> None:
    unit = Dp832()
    settings = config.from_dict(
        {
            "instruments": [
                {"connection": "lan", "host": "192.168.1.40", "port": 5555, "rate_hz": 20},
            ]
        }
    )
    stop = threading.Event()
    opened: list[str] = []

    def opener(address: Any, link_settings: Any) -> ScriptedLink:
        opened.append(address.where)
        return ScriptedLink(unit, address.where)

    thread = threading.Thread(
        target=app.run,
        kwargs={
            "source": trace_file.source,
            "settings": settings,
            "profiles": profiles,
            "open": opener,
            "stop": stop,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert wait_until(lambda: "192_168_1_40_5555" in actions.workers)
        worker = actions.workers["192_168_1_40_5555"]
        assert wait_until(lambda: worker.state == "open")
        assert opened == ["192.168.1.40:5555"]
        assert (
            actions.set_values._action.execute(channel=2, voltage=3.3).value["voltage"]["readback"]
            == 3.3
        )
        assert wait_until(lambda: any(r for r in worker._channel[1].values.items()), timeout_s=5)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    events = trace_file.events()
    ch1 = [r for r in events["192_168_1_40_5555/ch1"] if r.get("voltage") is not None]
    assert ch1 and ch1[0]["voltage"] == 5.0 and ch1[0]["current"] == 0.5
    ch2 = [r for r in events["192_168_1_40_5555/ch2"] if r.get("voltage_set") == 3.3]
    assert ch2, "the setpoint lands in the trace"
    messages = [r["message"] for r in events["192_168_1_40_5555/log"]]
    assert any(m.startswith("connected to 192.168.1.40:5555") for m in messages)
    assert "set ch2 voltage 3.300" in messages
    statuses = events["192_168_1_40_5555/status"]
    assert statuses and statuses[-1]["connected"] is True


def test_run_exits_with_one_sentence_per_config_mistake(
    monkeypatch: Any, tmp_path: Any, caplog: Any
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"instruments": [{"connection": "lan"}]}', encoding="utf-8")
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))
    import pytest

    with pytest.raises(SystemExit) as exit_info:
        app.run(stop=threading.Event())
    assert exit_info.value.code == 1
    assert "Instrument 1 (lan), Host: missing." in caplog.text


def test_default_names_come_from_the_address_and_never_collide() -> None:
    settings = config.from_dict(
        {
            "instruments": [
                {"connection": "lan", "host": "bench.local"},
                {"connection": "lan", "host": "bench.local", "port": 5025},
                {"connection": "serial", "port": "/dev/ttyUSB0"},
                {"connection": "visa", "resource": "USB0::0x1AB1::0x0E11::DP8C1::INSTR"},
                {"connection": "demo", "category": "load"},
            ]
        }
    )
    names = [cfg.name for cfg in app._with_default_names(settings.instruments)]
    assert names == [
        "bench_local",
        "bench_local_5025",
        "dev_ttyUSB0",
        "USB0_0x1AB1_0x0E11_DP8C1_INSTR",
        "demo load",
    ]


def test_user_profiles_in_the_data_dir_override_shipped_ones(
    monkeypatch: Any, tmp_path: Any
) -> None:
    own = tmp_path / "profiles"
    own.mkdir()
    from zelos_extension_instruments.profile_load import SHIPPED_DIR

    text = (SHIPPED_DIR / "psu" / "rigol-dp800.toml").read_text(encoding="utf-8")
    (own / "rigol-dp800.toml").write_text(
        text.replace('name = "Rigol DP800 series"', 'name = "Mine"'), encoding="utf-8"
    )
    (own / "acme-psu.toml").write_text(
        text.replace('id = "rigol-dp800"', 'id = "acme-psu"').replace("(?i)^rigol", "^ACME"),
        encoding="utf-8",
    )
    monkeypatch.setenv("ZELOS_DATA_DIR", str(tmp_path))
    loaded = {p.id: p for p in app.load_profiles()}
    assert loaded["rigol-dp800"].name == "Mine"
    assert "acme-psu" in loaded


def test_the_demo_bench_records_all_four_instruments(
    trace_file: Recording, profiles: list[Profile]
) -> None:
    settings = config.from_dict(
        {
            "instruments": [
                {"connection": "demo", "category": c, "rate_hz": 20}
                for c in ("psu", "load", "dmm", "acsource")
            ]
        }
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=app.run,
        kwargs={
            "source": trace_file.source,
            "settings": settings,
            "profiles": profiles,
            "stop": stop,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert wait_until(
            lambda: (
                len(actions.workers) == 4
                and all(w.state == "open" for w in actions.workers.values())
            ),
            timeout_s=15,
        ), {n: w.health for n, w in actions.workers.items()}
        psu = actions.workers["demo psu"]
        assert wait_until(lambda: psu._channel[1].values.get("voltage") is not None, timeout_s=10)
        result = actions.set_values._action.execute(
            instrument="demo psu", channel=2, voltage=4.0, current=0.5
        ).value
        assert result["voltage"]["readback"] == 4.0
        assert actions.output._action.execute(
            instrument="demo load", channel=1, on=False
        ).value == {"output": False}
        time.sleep(1.5)
    finally:
        stop.set()
        thread.join(5)
    events = trace_file.events()
    assert any((r.get("voltage") or 0) > 0 for r in events["demo psu/ch1"])
    assert any(r.get("voltage_set") == 4.0 for r in events["demo psu/ch2"])
    assert any(r.get("current") is not None for r in events["demo load/ch1"])
    assert events["demo dmm/dc_voltage"], "the meter's readings land under its function"
    assert any(r.get("frequency") == 50.0 for r in events["demo acsource/ch1"])
    assert any(r["message"] == "set ch2 voltage 4.000" for r in events["demo psu/log"])
    assert any(r["message"] == "set ch1 output off" for r in events["demo load/log"])


def test_stopping_several_instruments_lets_them_wind_down_together(
    trace_file: Recording, profiles: list[Profile]
) -> None:
    hosts = ("192.168.1.41", "192.168.1.42", "192.168.1.43")
    settings = config.from_dict(
        {"instruments": [{"connection": "lan", "host": h, "port": 5555} for h in hosts]}
    )

    def opener(address: Any, link_settings: Any) -> ScriptedLink:
        unit = Dp832()

        def answer(command: str) -> str | None:
            if command == "SYST:LOCAL":
                time.sleep(0.6)
            return unit(command)

        return ScriptedLink(answer, address.where)

    stop = threading.Event()
    thread = threading.Thread(
        target=app.run,
        kwargs={
            "source": trace_file.source,
            "settings": settings,
            "profiles": profiles,
            "open": opener,
            "stop": stop,
        },
        daemon=True,
    )
    thread.start()
    assert wait_until(
        lambda: (
            len(actions.workers) == 3 and all(w.state == "open" for w in actions.workers.values())
        )
    )
    started = time.monotonic()
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.5
