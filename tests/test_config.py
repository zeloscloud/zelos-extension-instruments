import json
from pathlib import Path
from typing import Any

import pytest

from zelos_extension_instruments import config
from zelos_extension_instruments.config import ConfigError, Limits, from_dict, load

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "config.schema.json").read_text("utf-8"))

LAN: dict[str, Any] = {"connection": "lan", "host": "192.168.1.40", "name": "psu"}


def problems_of(raw: Any) -> list[str]:
    with pytest.raises(ConfigError) as caught:
        from_dict(raw)
    return caught.value.problems


def one(item: dict[str, Any], **top: Any) -> config.InstrumentConfig:
    return from_dict({"instruments": [item], **top}).instruments[0]


@pytest.mark.parametrize(
    "example", SCHEMA["examples"], ids=lambda e: f"example{SCHEMA['examples'].index(e)}"
)
def test_from_dict_accepts_the_schema_examples(example: dict[str, Any]) -> None:
    settings = from_dict(example)
    assert len(settings.instruments) == len(example["instruments"])


def test_schema_examples_cover_every_connection_but_visa() -> None:
    connections = {
        item["connection"] for example in SCHEMA["examples"] for item in example["instruments"]
    }
    assert {"lan", "usb", "serial", "demo"} <= connections


def test_a_minimal_lan_instrument_gets_every_default() -> None:
    cfg = one({"connection": "lan", "host": "10.0.0.5"})
    assert cfg.name == ""
    assert cfg.connection == "lan"
    assert cfg.host == "10.0.0.5"
    assert cfg.port is None
    assert cfg.resource is None
    assert cfg.serial_port is None
    assert cfg.baud is None
    assert cfg.backend == "auto"
    assert cfg.category == "psu"
    assert cfg.profile == "auto"
    assert cfg.rate_hz == 10.0
    assert cfg.keep_panel is True
    assert cfg.limits == Limits(None, None, None, None)
    assert cfg.channels is None
    assert cfg.timeout_s is None
    assert cfg.delay_ms is None
    assert cfg.line_ending is None
    assert cfg.poll == ()


def test_settings_defaults() -> None:
    settings = from_dict({"instruments": [LAN]})
    assert settings.prefix == "Instruments"
    assert settings.log_level == "INFO"


def test_top_level_advanced_overrides_the_defaults() -> None:
    settings = from_dict(
        {"instruments": [LAN], "advanced": {"prefix": "  Bench ", "log_level": "DEBUG"}}
    )
    assert settings.prefix == "Bench"
    assert settings.log_level == "DEBUG"


def test_explicit_values_are_kept() -> None:
    cfg = one(
        {
            "connection": "lan",
            "host": "h",
            "port": 5555,
            "name": "  load ",
            "profile": " rigol-dl3000 ",
            "rate_hz": 2.5,
            "advanced": {
                "keep_panel": False,
                "channels": 4,
                "timeout_s": 5,
                "delay_ms": 30,
                "line_ending": "CRLF",
                "limits": {
                    "max_voltage": 12,
                    "max_current": 2.5,
                    "max_power": 40,
                    "max_frequency": 60,
                },
            },
        }
    )
    assert cfg.port == 5555
    assert cfg.name == "load"
    assert cfg.profile == "rigol-dl3000"
    assert cfg.rate_hz == 2.5
    assert cfg.keep_panel is False
    assert cfg.channels == 4
    assert cfg.timeout_s == 5.0
    assert cfg.delay_ms == 30
    assert cfg.limits == Limits(12.0, 2.5, 40.0, 60.0)


@pytest.mark.parametrize(("name", "ending"), [("LF", "\n"), ("CRLF", "\r\n"), ("CR", "\r")])
def test_line_ending_names_become_characters(name: str, ending: str) -> None:
    assert one({**LAN, "advanced": {"line_ending": name}}).line_ending == ending


def test_poll_rows_get_their_defaults() -> None:
    cfg = one(
        {
            **LAN,
            "advanced": {
                "poll": [
                    {"name": "Temp 1 ", "query": "MEAS:TEMP?"},
                    {"name": "Fan", "query": "FAN? {ch}", "unit": "rpm", "every_s": 0.5},
                    {"name": "Blank unit", "query": "X?", "unit": ""},
                ]
            },
        }
    )
    assert [(r.name, r.query, r.unit, r.every_s) for r in cfg.poll] == [
        ("Temp 1", "MEAS:TEMP?", None, 1.0),
        ("Fan", "FAN? {ch}", "rpm", 0.5),
        ("Blank unit", "X?", None, 1.0),
    ]


def test_limits_for_key() -> None:
    limits = Limits(max_voltage=1, max_current=2, max_power=3, max_frequency=4)
    assert [limits.for_key(k) for k in ("voltage", "current", "power", "frequency")] == [
        1,
        2,
        3,
        4,
    ]
    assert limits.for_key("resistance") is None
    assert Limits().for_key("voltage") is None


def test_where_for_lan() -> None:
    assert one({"connection": "lan", "host": "10.0.0.5"}).where == "10.0.0.5"
    assert one({"connection": "lan", "host": "10.0.0.5", "port": 5555}).where == "10.0.0.5:5555"


def test_where_brackets_an_ipv6_host() -> None:
    assert one({"connection": "lan", "host": "fe80::1"}).where == "[fe80::1]"
    assert one({"connection": "lan", "host": "fe80::1", "port": 5}).where == "[fe80::1]:5"


@pytest.mark.parametrize("connection", ["usb", "visa"])
def test_where_for_a_resource(connection: str) -> None:
    resource = "USB0::0x1AB1::0x0E11::DP8C123::INSTR"
    assert one({"connection": connection, "resource": resource}).where == resource


def test_where_for_demo() -> None:
    assert one({"connection": "demo"}).where == "demo psu"
    assert one({"connection": "demo", "category": "load"}).where == "demo load"


def test_visa_backend_is_kept() -> None:
    cfg = one({"connection": "visa", "resource": "GPIB0::5::INSTR", "backend": "ivi"})
    assert cfg.backend == "ivi"


def test_serial_port_lands_in_serial_port_and_port_stays_none() -> None:
    cfg = one({"connection": "serial", "port": "COM7", "baud": 115200})
    assert cfg.serial_port == "COM7"
    assert cfg.port is None
    assert cfg.baud == 115200
    assert cfg.where == "COM7"


@pytest.mark.parametrize(
    ("item", "sentence"),
    [
        ({"connection": "lan"}, "Instrument 1 (lan), Host: missing."),
        ({"connection": "lan", "host": ""}, "Instrument 1 (lan), Host: must not be empty."),
        ({"connection": "usb"}, "Instrument 1 (usb), Device: missing."),
        ({"connection": "visa"}, "Instrument 1 (visa), Resource: missing."),
        ({"connection": "serial"}, "Instrument 1 (serial), Port: missing."),
        (
            {"connection": "bogus", "host": "h"},
            'Instrument 1 (bogus), Connection: must be "lan", "usb", "serial", "visa" or "demo".',
        ),
        ({"host": "h"}, "Instrument 1, Connection: missing."),
        ({**LAN, "rate_hz": 0}, "Instrument 1 (lan), Readings per second: must be above 0."),
        (
            {**LAN, "rate_hz": "fast"},
            "Instrument 1 (lan), Readings per second: must be a number.",
        ),
        (
            {**LAN, "name": "a/b"},
            "Instrument 1 (lan), Name: use letters, digits, spaces, _ and - only.",
        ),
        (
            {**LAN, "name": "x" * 65},
            "Instrument 1 (lan), Name: must be at most 64 characters.",
        ),
        ({**LAN, "port": 70000}, "Instrument 1 (lan), Port: must be from 1 to 65535."),
        (
            {**LAN, "advanced": {"line_ending": "LFCR"}},
            'Instrument 1 (lan), Line ending: must be "LF", "CRLF" or "CR".',
        ),
        (
            {**LAN, "advanced": {"channels": 0}},
            "Instrument 1 (lan), Channels: must be from 1 to 64.",
        ),
        (
            {**LAN, "advanced": {"limits": {"max_voltage": -1}}},
            "Instrument 1 (lan), Max voltage (V): must be at least 0.",
        ),
        (
            {**LAN, "advanced": {"poll": [{"name": "t"}]}},
            "Instrument 1 (lan), Query: missing.",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_one_sentence_per_mistake(item: dict[str, Any], sentence: str) -> None:
    assert problems_of({"instruments": [item]}) == [sentence]


def test_the_prefix_names_the_instrument_by_position() -> None:
    found = problems_of({"instruments": [LAN, {"connection": "lan"}, {"connection": "usb"}]})
    assert found == [
        "Instrument 2 (lan), Host: missing.",
        "Instrument 3 (usb), Device: missing.",
    ]


def test_every_instrument_mistake_is_reported_together() -> None:
    found = problems_of({"instruments": [{"connection": "lan", "name": "a/b", "rate_hz": 0}]})
    assert sorted(found) == [
        "Instrument 1 (lan), Host: missing.",
        "Instrument 1 (lan), Name: use letters, digits, spaces, _ and - only.",
        "Instrument 1 (lan), Readings per second: must be above 0.",
    ]


def test_an_instrument_that_is_not_a_group_of_settings() -> None:
    assert problems_of({"instruments": ["psu"]}) == ["Instrument 1: must be a group of settings."]


def test_duplicate_names_are_refused_ignoring_case() -> None:
    found = problems_of({"instruments": [LAN, {**LAN, "host": "h2", "name": "PSU"}]})
    assert found == ["Instrument 2 (lan): name 'PSU' is already used by instrument 1."]


def test_duplicate_names_ignore_surrounding_spaces() -> None:
    found = problems_of({"instruments": [LAN, {**LAN, "name": " psu "}]})
    assert found == ["Instrument 2 (lan): name 'psu' is already used by instrument 1."]


def test_each_duplicate_points_at_the_first_user() -> None:
    found = problems_of({"instruments": [LAN, {**LAN, "name": "Psu"}, {**LAN, "name": "PSU"}]})
    assert found == [
        "Instrument 2 (lan): name 'Psu' is already used by instrument 1.",
        "Instrument 3 (lan): name 'PSU' is already used by instrument 1.",
    ]


def test_unnamed_instruments_do_not_collide() -> None:
    settings = from_dict({"instruments": [{**LAN, "name": ""}, {"connection": "lan", "host": "h"}]})
    assert [i.name for i in settings.instruments] == ["", ""]


def test_duplicate_poll_row_names_are_refused_ignoring_case() -> None:
    rows = [{"name": "Temp", "query": "T?"}, {"name": "temp", "query": "U?"}]
    found = problems_of({"instruments": [{**LAN, "advanced": {"poll": rows}}]})
    assert found == ["Instrument 1 (lan), Extra readings: 'temp' is listed twice."]


def test_the_same_poll_row_name_on_two_instruments_is_fine() -> None:
    advanced = {"poll": [{"name": "Temp", "query": "T?"}]}
    settings = from_dict(
        {"instruments": [{**LAN, "advanced": advanced}, {**LAN, "name": "b", "advanced": advanced}]}
    )
    assert len(settings.instruments) == 2


def test_an_unknown_advanced_key_for_all_instruments_is_refused() -> None:
    found = problems_of({"instruments": [LAN], "advanced": {"bogus": 1}})
    assert found == ["Settings, Advanced (all instruments): unknown setting bogus."]


def test_a_bad_log_level_is_refused() -> None:
    found = problems_of({"instruments": [LAN], "advanced": {"log_level": "LOUD"}})
    assert found == ['Settings, Log level: must be "DEBUG", "INFO", "WARNING" or "ERROR".']


@pytest.mark.parametrize("prefix", ["", "   ", "a/b"])
def test_a_bad_prefix_is_refused(prefix: str) -> None:
    found = problems_of({"instruments": [LAN], "advanced": {"prefix": prefix}})
    assert len(found) == 1
    assert found[0].startswith("Settings, Prefix: ")


@pytest.mark.parametrize("raw", [{}, {"instruments": []}, {"instruments": None}, [], None, "x"])
def test_no_instruments_asks_for_one(raw: Any) -> None:
    assert problems_of(raw) == ["Add at least one instrument."]


def test_config_error_joins_its_sentences() -> None:
    error = ConfigError(["One.", "Two."])
    assert str(error) == "One.; Two."
    assert error.problems == ["One.", "Two."]


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes | str) -> Path:
    path = tmp_path / "config.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(path))
    return path


def test_load_reads_zelos_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, json.dumps({"instruments": [LAN]}))
    settings = load()
    assert [i.name for i in settings.instruments] == ["psu"]


def test_load_takes_an_explicit_path_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(tmp_path / "other.json"))
    path = tmp_path / "mine.json"
    path.write_text(json.dumps({"instruments": [{**LAN, "name": "mine"}]}), encoding="utf-8")
    assert load(path).instruments[0].name == "mine"


def test_load_reads_a_missing_file_as_no_instruments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(ConfigError) as caught:
        load()
    assert caught.value.problems == ["Add at least one instrument."]


def test_load_defaults_to_config_json_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZELOS_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"instruments": [LAN]}), encoding="utf-8")
    assert load().instruments[0].host == "192.168.1.40"


def test_load_reports_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, '{"instruments": [')
    with pytest.raises(ConfigError) as caught:
        load()
    (problem,) = caught.value.problems
    assert problem.startswith("The configuration is not valid JSON: ")
    assert problem.endswith(".")


def test_load_reports_text_that_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path, monkeypatch, b"\xff\xfe{\x00")
    with pytest.raises(ConfigError) as caught:
        load()
    assert caught.value.problems == [f"The configuration file {path} is not UTF-8 text."]


def test_load_reports_a_file_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZELOS_CONFIG_PATH", str(tmp_path))
    with pytest.raises(ConfigError) as caught:
        load()
    (problem,) = caught.value.problems
    assert problem.startswith(f"The configuration file {tmp_path} cannot be read: ")


def test_load_validates_what_it_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, monkeypatch, json.dumps({"instruments": [{"connection": "lan"}]}))
    with pytest.raises(ConfigError) as caught:
        load()
    assert caught.value.problems == ["Instrument 1 (lan), Host: missing."]
