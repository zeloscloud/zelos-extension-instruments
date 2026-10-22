import copy
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest

from zelos_extension_instruments import profile as profile_module
from zelos_extension_instruments.profile import (
    SHIPPED_DIR,
    Idn,
    Profile,
    ProfileError,
    by_id,
    check_paths,
    find,
    from_dict,
    load_dir,
    load_file,
    main,
    parse_idn,
    shipped,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DP800_PATH = REPO_ROOT / "profiles" / "psu" / "rigol-dp800.toml"
RIGOL_DP832 = "RIGOL TECHNOLOGIES,DP832,DP8A123456,00.01.16"


def minimal() -> dict[str, Any]:
    """The smallest profile that passes every check."""
    return {
        "schema": 1,
        "id": "acme-psu",
        "name": "Acme PSU",
        "category": "psu",
        "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-20"},
        "match": {"manufacturer": "(?i)^acme", "model": "^P1$"},
        "signals": {"voltage": {"query": "MEAS:VOLT? {ch}", "parse": "float"}},
    }


def minimal_dmm() -> dict[str, Any]:
    raw = minimal()
    raw.update(
        id="acme-dmm",
        category="dmm",
        signals={"value": {"query": "READ?", "parse": "float"}},
        dmm={"functions": {"dc_voltage": "V"}},
    )
    return raw


def set_(raw: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    node = raw
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


def drop(raw: dict[str, Any], path: str) -> None:
    *parents, leaf = path.split(".")
    node = raw
    for key in parents:
        node = node[key]
    del node[leaf]


def as_load(raw: dict[str, Any], setters: dict[str, Any]) -> None:
    raw["category"] = "load"
    raw["setters"] = setters


def problems_of(raw: dict[str, Any], name: str | None = None) -> list[str]:
    path = f"{raw.get('id', 'x')}.toml" if name is None else name
    with pytest.raises(ProfileError) as caught:
        from_dict(raw, path)
    return caught.value.problems


def profile_from(raw: dict[str, Any]) -> Profile:
    return from_dict(raw, f"{raw['id']}.toml")


def with_model(profile_id: str, model: str, manufacturer: str = "(?i)^acme") -> Profile:
    raw = minimal()
    raw["id"] = profile_id
    raw["match"] = {"manufacturer": manufacturer, "model": model}
    return profile_from(raw)


def test_minimal_profiles_are_valid() -> None:
    assert profile_from(minimal()).id == "acme-psu"
    assert profile_from(minimal_dmm()).category == "dmm"


def test_load_file_reads_the_shipped_example(dp800: Profile) -> None:
    assert (dp800.id, dp800.name, dp800.category) == ("rigol-dp800", "Rigol DP800 series", "psu")
    assert dp800.generic is False
    assert dp800.path == DP800_PATH
    assert dp800.evidence.badge == "manual"
    assert dp800.evidence.date == "2026-10-21"
    assert dp800.match.channels == 1
    assert [m.channels for m in dp800.match.models] == [2, 3]
    assert dp800.link.lan_port == 5555
    assert dp800.link.remote == "SYST:REMOTE"
    assert dp800.link.local == ("SYST:LOCAL",)
    assert dp800.link.timeout_s == 2.0
    assert dp800.link.write_terminator == "\n"
    assert dp800.errors is not None
    assert dp800.errors.query == "SYST:ERR?"
    assert dp800.errors.drain == 20
    assert [r.name for r in dp800.signals] == ["measure"]
    assert [r.name for r in dp800.state] == [
        "voltage_set",
        "current_set",
        "ovp_set",
        "ocp_set",
        "ovp_on",
        "ocp_on",
        "remote_sense",
        "output",
        "mode",
        "protection",
    ]
    assert set(dp800.setters) == {
        "voltage",
        "current",
        "ovp",
        "ocp",
        "ovp_on",
        "ocp_on",
        "output",
        "clear",
    }
    assert dp800.dmm is None


def test_docs_example_is_the_shipped_file(dp800: Profile) -> None:
    docs = (REPO_ROOT / "docs" / "profiles.md").read_text(encoding="utf-8")
    block = re.search(r"```toml\n(.*?)```", docs, re.DOTALL)
    assert block is not None
    assert from_dict(tomllib.loads(block.group(1)), "rigol-dp800.toml") == dp800


def test_shipped_loads_every_file_under_profiles(profiles: list[Profile]) -> None:
    files = sorted(SHIPPED_DIR.rglob("*.toml"))
    assert len(files) > 0
    assert sorted(p.path for p in profiles if p.path) == files
    assert len({p.id for p in profiles}) == len(profiles)


def test_shipped_ids_equal_file_stems(profiles: list[Profile]) -> None:
    for p in profiles:
        assert p.path is not None
        assert p.id == p.path.stem


def test_shipped_badges_are_earned(profiles: list[Profile]) -> None:
    """A profile is only promoted by a transcript or the bench, never by hand."""
    transcripts = SHIPPED_DIR.parent / "tests" / "transcripts"
    recorded = (
        {d.name for d in transcripts.iterdir() if d.is_dir()} if transcripts.is_dir() else set()
    )
    promoted = {p.id: p.evidence.badge for p in profiles if p.evidence.badge != "manual"}
    assert promoted == dict.fromkeys(recorded, "transcript")


def test_shipped_has_a_generic_profile_for_each_category(profiles: list[Profile]) -> None:
    assert {p.category for p in profiles if p.generic} == {"psu", "load", "dmm", "acsource"}


def test_by_id(profiles: list[Profile]) -> None:
    assert by_id(profiles, "rigol-dp800") is not None
    assert by_id(profiles, "nope") is None


def test_find_resolves_a_shipped_identity(profiles: list[Profile]) -> None:
    found = find(profiles, parse_idn(RIGOL_DP832))
    assert found is not None
    assert found.id == "rigol-dp800"


def test_find_never_returns_a_generic_profile(profiles: list[Profile]) -> None:
    assert find(profiles, parse_idn("NOBODY,X1,1,1")) is None
    generic = [p for p in profiles if p.generic]
    assert generic
    assert find(generic, parse_idn(RIGOL_DP832)) is None
    for p in generic:
        assert p.matches(parse_idn(RIGOL_DP832)) is False


def test_find_picks_the_longest_model_pattern() -> None:
    broad = with_model("acme-broad", "^P")
    narrow = with_model("acme-narrow", "^P1[0-9]$")
    idn = parse_idn("ACME,P12,SN1,1.0")
    assert find([broad, narrow], idn) is narrow
    assert find([narrow, broad], idn) is narrow


def test_find_skips_profiles_that_do_not_match() -> None:
    long_but_wrong = with_model("acme-other", "^QQQQQQQQQQ$")
    wrong_vendor = with_model("globex-psu", "^P1[0-9]$", manufacturer="(?i)^globex")
    short = with_model("acme-short", "^P")
    assert find([long_but_wrong, wrong_vendor, short], parse_idn("ACME,P12,1,1")) is short


def test_find_breaks_a_length_tie_by_id() -> None:
    first = with_model("acme-a", "^P1$")
    second = with_model("acme-b", "^P1$")
    assert find([first, second], parse_idn("ACME,P1,1,1")) is second
    assert find([second, first], parse_idn("ACME,P1,1,1")) is second


def test_find_returns_none_for_no_profiles() -> None:
    assert find([], parse_idn(RIGOL_DP832)) is None


def test_matches_checks_manufacturer_and_model(dp800: Profile) -> None:
    assert dp800.matches(parse_idn(RIGOL_DP832))
    assert dp800.matches(parse_idn("rigol technologies,DP811A,1,1"))
    assert not dp800.matches(parse_idn("ACME,DP832,1,1"))
    assert not dp800.matches(parse_idn("RIGOL TECHNOLOGIES,DS1054Z,1,1"))


@pytest.mark.parametrize(
    ("model", "channels"),
    [("DP811A", 1), ("DP821A", 2), ("DP832", 3), ("DP832A", 3), ("unknown", 1)],
)
def test_channels_for(dp800: Profile, model: str, channels: int) -> None:
    assert dp800.channels_for(model) == channels


def test_channels_for_takes_the_first_matching_override() -> None:
    raw = minimal()
    raw["match"]["models"] = [
        {"model": "^P1", "channels": 4},
        {"model": "^P12", "channels": 8},
    ]
    p = profile_from(raw)
    assert p.channels_for("P12") == 4
    assert p.channels_for("P2") == 1


def test_range_for_uses_the_default_range(dp800: Profile) -> None:
    voltage = dp800.range_for("voltage", "DP832", 1)
    assert voltage is not None
    assert (voltage.min, voltage.max, voltage.digits) == (0.0, 30.0, 3)
    current = dp800.range_for("current", "DP832", 1)
    assert current is not None
    assert current.max == 3.0


def test_range_for_applies_a_model_and_channel_override(dp800: Profile) -> None:
    channel_three = dp800.range_for("voltage", "DP832", 3)
    assert channel_three is not None
    assert channel_three.max == 5.0
    other_channel = dp800.range_for("voltage", "DP832", 2)
    assert other_channel is not None
    assert other_channel.max == 30.0
    other_model = dp800.range_for("voltage", "DP821A", 3)
    assert other_model is not None
    assert other_model.max == 30.0


def test_range_for_falls_back_for_a_key_the_override_lacks(dp800: Profile) -> None:
    current = dp800.range_for("current", "DP832", 3)
    assert current is not None
    assert current.max == 3.0


def test_range_for_unknown_key_is_none(dp800: Profile) -> None:
    assert dp800.range_for("power", "DP832", 1) is None


def test_range_for_override_without_channel_covers_every_channel() -> None:
    raw = minimal()
    raw["ranges"] = {
        "voltage": {"max": 10},
        "models": [
            {"model": "^P1", "voltage": {"max": 20}},
            {"model": "^P1", "voltage": {"max": 30}},
        ],
    }
    p = profile_from(raw)
    for channel in (1, 2, 7):
        found = p.range_for("voltage", "P1", channel)
        assert found is not None
        assert found.max == 20.0
    found = p.range_for("voltage", "P2", 1)
    assert found is not None
    assert found.max == 10.0


def test_range_with_queries_has_no_static_bounds() -> None:
    raw = minimal()
    raw["ranges"] = {"voltage": {"max_query": "VOLT? MAX", "min_query": "VOLT? MIN", "digits": 2}}
    found = profile_from(raw).range_for("voltage", "P1", 1)
    assert found is not None
    assert (found.min, found.max, found.digits) == (None, None, 2)
    assert (found.min_query, found.max_query) == ("VOLT? MIN", "VOLT? MAX")


def test_parse_idn_four_fields() -> None:
    idn = parse_idn(RIGOL_DP832)
    assert idn == Idn("RIGOL TECHNOLOGIES", "DP832", "DP8A123456", "00.01.16")
    assert idn.text == RIGOL_DP832


def test_parse_idn_strips_spaces_and_the_line_end() -> None:
    idn = parse_idn("  KEYSIGHT , E36312A , MY123 , 1.2.3 \r\n")
    assert idn == Idn("KEYSIGHT", "E36312A", "MY123", "1.2.3")


def test_parse_idn_two_fields() -> None:
    assert parse_idn("ACME,X1") == Idn("ACME", "X1", "", "")


def test_parse_idn_one_field_and_none() -> None:
    assert parse_idn("ACME") == Idn("ACME", "", "", "")
    assert parse_idn("") == Idn("", "", "", "")


def test_parse_idn_five_fields_keeps_the_rest_in_firmware() -> None:
    idn = parse_idn("ACME, X1, SN9, 1.0, build 7")
    assert idn == Idn("ACME", "X1", "SN9", "1.0,build 7")
    assert idn.text == "ACME,X1,SN9,1.0,build 7"


def test_verify_query_defaults_to_the_state_reader(dp800: Profile) -> None:
    assert dp800.verify_query(dp800.setters["voltage"]) == ":SOUR{ch}:VOLT?"
    assert dp800.verify_query(dp800.setters["current"]) == ":SOUR{ch}:CURR?"
    assert dp800.verify_query(dp800.setters["output"]) == ":OUTP? CH{ch}"


def test_verify_query_is_none_without_a_reader(dp800: Profile) -> None:
    assert dp800.verify_query(dp800.setters["clear"]) is None


def test_verify_query_prefers_the_setters_own() -> None:
    raw = minimal()
    raw["setters"] = {"voltage": {"command": "VOLT {value}", "verify": "VOLT? {ch}"}}
    p = profile_from(raw)
    assert p.verify_query(p.setters["voltage"]) == "VOLT? {ch}"


def test_range_key() -> None:
    raw = minimal()
    raw["setters"] = {
        "voltage": {"command": "V {value}"},
        "ovp": {"command": "OVP {value}"},
        "ocp": {"command": "OCP {value}"},
        "current": {"command": "I {value}", "range": "power"},
        "output": {"command": "OUTP {on}"},
    }
    p = profile_from(raw)
    assert p.range_key(p.setters["voltage"]) == "voltage"
    assert p.range_key(p.setters["ovp"]) == "voltage"
    assert p.range_key(p.setters["ocp"]) == "current"
    assert p.range_key(p.setters["current"]) == "power"
    assert p.range_key(p.setters["output"]) is None


def test_fields_lists_signals_then_state_with_units(dp800: Profile) -> None:
    assert list(dp800.fields.items()) == [
        ("voltage", "V"),
        ("current", "A"),
        ("power", "W"),
        ("voltage_set", "V"),
        ("current_set", "A"),
        ("ovp_set", "V"),
        ("ocp_set", "A"),
        ("ovp_on", None),
        ("ocp_on", None),
        ("remote_sense", None),
        ("output", None),
        ("mode", None),
        ("protection", None),
    ]


def test_reader_finds_the_reader_that_fills_a_field(dp800: Profile) -> None:
    reader = dp800.reader("power")
    assert reader is not None
    assert reader.name == "measure"
    assert dp800.reader("frequency") is None


def test_load_file_reports_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as caught:
        load_file(tmp_path / "missing.toml")
    assert "cannot be read" in caught.value.problems[0]
    assert str(caught.value).startswith("missing.toml: ")


def test_load_file_reports_text_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ProfileError) as caught:
        load_file(path)
    assert caught.value.problems == ["is not UTF-8 text."]


def test_load_file_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text("schema = = 1", encoding="utf-8")
    with pytest.raises(ProfileError) as caught:
        load_file(path)
    assert caught.value.problems[0].startswith("is not valid TOML")


def test_load_dir_reads_nested_directories(tmp_path: Path) -> None:
    (tmp_path / "psu").mkdir()
    shutil.copy(DP800_PATH, tmp_path / "psu" / "rigol-dp800.toml")
    assert [p.id for p in load_dir(tmp_path)] == ["rigol-dp800"]
    assert load_dir(tmp_path / "psu")[0].path == tmp_path / "psu" / "rigol-dp800.toml"


def test_profile_error_names_the_file_and_every_problem() -> None:
    error = ProfileError("dir/a.toml", ["id: bad.", "name: worse."])
    assert str(error) == "a.toml: id: bad. name: worse."
    assert error.path == Path("dir/a.toml")


LINT_CASES: list[tuple[str, Any, str, str]] = [
    ("unknown top-level key", lambda r: set_(r, "bogus", 1), "profile", "unknown key bogus."),
    ("bad schema", lambda r: set_(r, "schema", 2), "schema", "must be 1."),
    (
        "bad id",
        lambda r: set_(r, "id", "Bad_ID"),
        "id",
        "use lower-case letters, digits and hyphens.",
    ),
    ("missing name", lambda r: drop(r, "name"), "name", "missing or empty."),
    ("bad category", lambda r: set_(r, "category", "oven"), "category", "must be one of"),
    (
        "missing evidence source",
        lambda r: drop(r, "evidence.source"),
        "evidence.source",
        "missing or empty.",
    ),
    ("bad badge", lambda r: set_(r, "evidence.badge", "gold"), "evidence.badge", "must be one of"),
    (
        "bad date",
        lambda r: set_(r, "evidence.date", "20/10/2026"),
        "evidence.date",
        "must be YYYY-MM-DD.",
    ),
    (
        "bad regex",
        lambda r: set_(r, "match.model", "^P(1"),
        "match.model",
        "is not a valid regular expression",
    ),
    (
        "missing manufacturer",
        lambda r: drop(r, "match.manufacturer"),
        "match.manufacturer",
        "missing or empty.",
    ),
    (
        "bad override channels",
        lambda r: set_(r, "match.models", [{"model": "^P", "channels": 0}]),
        "match.models[0].channels",
        "must be a whole number of at least 1.",
    ),
    (
        "select without {ch}",
        lambda r: set_(r, "link.select", "INST:NSEL 1"),
        "link.select",
        "must contain {ch} or {ch0}.",
    ),
    (
        "unknown placeholder in a query",
        lambda r: set_(r, "signals.voltage.query", "MEAS:VOLT? {chan}"),
        "signals.voltage.query",
        "unknown placeholder chan.",
    ),
    (
        "unmatched brace",
        lambda r: set_(r, "signals.voltage.query", "MEAS:VOLT? {"),
        "signals.voltage.query",
        "has an unmatched brace.",
    ),
    (
        "placeholder in remote",
        lambda r: set_(r, "link.remote", "SYST:REM {ch}"),
        "link.remote",
        "unknown placeholder ch.",
    ),
    (
        "signals field not in the category",
        lambda r: set_(
            r,
            "signals",
            {"measure": {"query": "M?", "parse": "floats", "fields": ["voltage", "bogus"]}},
        ),
        "signals.measure",
        "bogus is not a signals field of this category",
    ),
    (
        "floats without fields",
        lambda r: set_(r, "signals", {"voltage": {"query": "M?", "parse": "floats"}}),
        "signals.voltage.fields",
        'missing: parse = "floats" needs it.',
    ),
    (
        "fields with a non-floats parser",
        lambda r: set_(r, "signals.voltage.fields", ["voltage", "current"]),
        "signals.voltage.fields",
        'only parse = "floats" fills several fields.',
    ),
    (
        "map parser without a map",
        lambda r: set_(r, "state", {"mode": {"query": "M?", "parse": "map"}}),
        "state.mode.map",
        'missing: parse = "map" needs it.',
    ),
    (
        "map label not in the enum labels",
        lambda r: set_(
            r, "state", {"mode": {"query": "M?", "parse": "map", "map": {"A": "turbo"}}}
        ),
        "state.mode.map",
        "'turbo' is not one of off, cv, cc",
    ),
    (
        "enum field with a float parser",
        lambda r: set_(r, "state", {"mode": {"query": "M?", "parse": "float"}}),
        "state.mode",
        'mode needs parse = "map" or "bits".',
    ),
    (
        "bool field with a float parser",
        lambda r: set_(r, "state", {"output": {"query": "O?", "parse": "float"}}),
        "state.output",
        'output needs parse = "bool" or "bits".',
    ),
    (
        "bits without mask",
        lambda r: set_(r, "state", {"output": {"query": "O?", "parse": "bits"}}),
        "state.output.mask",
        'missing: parse = "bits" needs it.',
    ),
    (
        "flags on a non-protection reader",
        lambda r: set_(
            r,
            "state",
            {"mode": {"flags": [{"query": "Q?", "parse": "bool", "value": "ovp"}]}},
        ),
        "state.mode",
        "flags are only for protection.",
    ),
    (
        "flag with a float parser",
        lambda r: set_(
            r,
            "state",
            {"protection": {"flags": [{"query": "Q?", "parse": "float", "value": "ovp"}]}},
        ),
        "state.protection.flags[0].parse",
        "must be bool or bits.",
    ),
    (
        "flag value outside the labels",
        lambda r: set_(
            r,
            "state",
            {"protection": {"flags": [{"query": "Q?", "parse": "bool", "value": "none"}]}},
        ),
        "state.protection.flags[0].value",
        "must be one of",
    ),
    (
        "empty flags",
        lambda r: set_(r, "state", {"protection": {"flags": []}}),
        "state.protection.flags",
        "must be a non-empty list of tables.",
    ),
    (
        "bool map to a word other than on or off",
        lambda r: set_(
            r,
            "state",
            {"remote_sense": {"query": "SENS?", "parse": "bool", "map": {"EXT": "external"}}},
        ),
        "state.remote_sense.map",
        'must map each word to "on" or "off" for parse = "bool".',
    ),
    (
        "value setter without {value}",
        lambda r: set_(r, "setters", {"voltage": {"command": "VOLT 5"}}),
        "setters.voltage.command",
        "must contain {value}.",
    ),
    (
        "output setter without {on} or {bit}",
        lambda r: set_(r, "setters", {"output": {"command": "OUTP 1"}}),
        "setters.output.command",
        "must contain {on} or {bit}.",
    ),
    (
        "master on a setter other than output",
        lambda r: set_(
            r,
            "setters",
            {
                "ovp_on": {
                    "command": "VOLT:PROT {bit}",
                    "master": {"command": "OUTP:GEN {bit}", "query": "OUTP:GEN?", "armed": "X?"},
                }
            },
        ),
        "setters.ovp_on.master",
        "is only for the output setter.",
    ),
    (
        "master command without {on} or {bit}",
        lambda r: set_(
            r,
            "setters",
            {
                "output": {
                    "command": "OUTP:SEL {bit}",
                    "master": {"command": "OUTP:GEN 1", "query": "OUTP:GEN?", "armed": "OUTP:SEL?"},
                }
            },
        ),
        "setters.output.master.command",
        "must contain {on} or {bit}.",
    ),
    (
        "master query for one channel",
        lambda r: set_(
            r,
            "setters",
            {
                "output": {
                    "command": "OUTP:SEL {bit}",
                    "master": {
                        "command": "OUTP:GEN {bit}",
                        "query": "OUTP:GEN? {ch}",
                        "armed": "OUTP:SEL?",
                    },
                }
            },
        ),
        "setters.output.master.query",
        "unknown placeholder ch.",
    ),
    (
        "function setter without {function}",
        lambda r: as_load(r, {"function": {"command": "FUNC CC", "map": {"cc": "CURR"}}}),
        "setters.function.command",
        "must contain {function}.",
    ),
    (
        "function setter without a map",
        lambda r: as_load(r, {"function": {"command": "FUNC {function}"}}),
        "setters.function.map",
        "missing: function needs a map",
    ),
    (
        "function setter map label not a mode",
        lambda r: as_load(
            r, {"function": {"command": "FUNC {function}", "map": {"turbo": "TURB"}}}
        ),
        "setters.function.map",
        "'turbo' is not one of",
    ),
    (
        "setter not in the category",
        lambda r: set_(r, "setters", {"power": {"command": "POW {value}"}}),
        "setters.power",
        "is not a setter of this category",
    ),
    (
        "setter with an unknown placeholder",
        lambda r: set_(r, "setters", {"clear": {"command": "CLE {value}"}}),
        "setters.clear.command",
        "unknown placeholder value.",
    ),
    (
        "setter range key outside the range keys",
        lambda r: set_(r, "setters", {"voltage": {"command": "V {value}", "range": "gain"}}),
        "setters.voltage.range",
        "must be one of voltage, current",
    ),
    (
        "range with max below min",
        lambda r: set_(r, "ranges", {"voltage": {"min": 5, "max": 1}}),
        "ranges.voltage",
        "max must be above min.",
    ),
    (
        "range without max or max_query",
        lambda r: set_(r, "ranges", {"voltage": {"min": 0}}),
        "ranges.voltage",
        "needs max or max_query.",
    ),
    (
        "range step not positive",
        lambda r: set_(r, "ranges", {"voltage": {"max": 5, "step": 0}}),
        "ranges.voltage.step",
        "must be positive.",
    ),
    (
        "range override channel not a channel number",
        lambda r: set_(r, "ranges", {"models": [{"model": "^P", "channel": 0}]}),
        "ranges.models[0].channel",
        "must be a channel number from 1.",
    ),
    (
        "dmm category without [dmm]",
        lambda r: (set_(r, "category", "dmm"), set_(r, "signals", {"value": {"query": "R?"}})),
        "dmm",
        "missing: a DMM profile lists its functions.",
    ),
    (
        "[dmm] on a psu",
        lambda r: set_(r, "dmm", {"functions": {"dc_voltage": "V"}}),
        "dmm",
        "is only for the dmm category.",
    ),
    (
        "generic profile with a manufacturer",
        lambda r: (set_(r, "generic", True), drop(r, "match.model")),
        "match.manufacturer",
        "a generic profile is chosen by category, not matched.",
    ),
    (
        "two readers filling the same field",
        lambda r: set_(
            r,
            "signals",
            {
                "measure": {
                    "query": "M?",
                    "parse": "floats",
                    "fields": ["voltage", "current"],
                },
                "voltage": {"query": "V?", "parse": "float"},
            },
        ),
        "voltage",
        "reads voltage, which measure reads too.",
    ),
    ("no signals", lambda r: drop(r, "signals.voltage"), "signals", "missing: a profile reads"),
    (
        "bad link number",
        lambda r: set_(r, "link.timeout_s", 0),
        "link.timeout_s",
        "must be positive.",
    ),
    (
        "bad link choice",
        lambda r: set_(r, "link.parity", "sometimes"),
        "link.parity",
        "must be one of none, even, odd",
    ),
    (
        "errors without a query",
        lambda r: set_(r, "errors", {"drain": 5}),
        "errors.query",
        "missing or empty.",
    ),
    (
        "errors ok pattern invalid",
        lambda r: set_(r, "errors", {"query": "E?", "ok": "("}),
        "errors.ok",
        "is not a valid regular expression",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "key", "sentence"),
    [pytest.param(m, k, s, id=name) for name, m, k, s in LINT_CASES],
)
def test_lint_refuses_one_broken_thing(mutate: Any, key: str, sentence: str) -> None:
    raw = minimal()
    mutate(raw)
    found = problems_of(raw)
    assert any(key in p and sentence in p for p in found), found
    assert all(p.endswith(".") for p in found), found


def test_lint_refuses_an_id_that_is_not_the_file_stem() -> None:
    assert problems_of(minimal(), name="other.toml") == ["id: must equal the file name, other."]


def test_lint_skips_the_stem_check_for_a_name_without_a_toml_suffix() -> None:
    assert from_dict(minimal(), "anything").id == "acme-psu"


def test_lint_reports_every_problem_in_one_error() -> None:
    raw = minimal()
    set_(raw, "evidence.date", "soon")
    set_(raw, "match.model", "(")
    drop(raw, "name")
    found = problems_of(raw)
    assert len(found) == 3
    assert {p.split(":")[0] for p in found} == {"name", "evidence.date", "match.model"}


@pytest.mark.parametrize(
    "command", ["*RST", "*rst", "OUTP ON", ":OUTP:STAT ON", "SYST:PRES", "VOLT 5"]
)
def test_lint_refuses_an_init_command_that_changes_an_output(command: str) -> None:
    raw = minimal()
    set_(raw, "link.init", ["*CLS", command])
    found = problems_of(raw)
    assert any("link.init" in p and f"'{command}' could change an output" in p for p in found)
    assert not any("'*CLS'" in p for p in found)


@pytest.mark.parametrize(
    "command",
    ["OUTPUT ON", "OUTPut:STATe ON", "SYSTEM:PRESET", "VOLTAGE 5", "SYST:CLE;*RST", "  *RST"],
)
def test_lint_refuses_long_form_and_compound_init_commands(command: str) -> None:
    raw = minimal()
    set_(raw, "link.init", [command])
    assert any("link.init" in p for p in problems_of(raw))


@pytest.mark.parametrize("command", ["*CLS", "*IDN?", "SYST:BEEP:STAT OFF"])
def test_lint_allows_init_commands_that_leave_outputs_alone(command: str) -> None:
    raw = minimal()
    set_(raw, "link.init", [command])
    assert profile_from(raw).link.init == (command,)


def test_lint_accepts_the_features_it_checks() -> None:
    raw = minimal()
    raw["link"] = {"select": "INST:NSEL {ch}", "remote": "SYST:REM", "init": ["*CLS"]}
    raw["signals"] = {
        "measure": {"query": "M? {ch}", "parse": "floats", "fields": ["voltage", "current"]}
    }
    raw["state"] = {
        "output": {"query": "O?", "parse": "bits", "mask": 1},
        "mode": {"query": "M?", "parse": "map", "map": {'"CV"': "cv"}},
        "protection": {"flags": [{"query": "P?", "parse": "bits", "mask": 4, "value": "ovp"}]},
    }
    raw["setters"] = {
        "voltage": {"command": "VOLT {value}"},
        "output": {"command": "OUTP {bit}"},
    }
    raw["ranges"] = {"voltage": {"max": 30}}
    p = profile_from(raw)
    assert p.link.select == "INST:NSEL {ch}"
    assert p.state[1].map == {"cv": "cv"}
    assert p.state[2].flags[0].mask == (4,)


def test_lint_accepts_a_generic_profile_without_signals_or_match() -> None:
    raw = minimal()
    raw["generic"] = True
    raw.pop("match")
    drop(raw, "signals.voltage")
    generic = profile_from(raw)
    assert generic.generic
    assert generic.matches(parse_idn("ACME,P1,1,1")) is False


def test_lint_requires_a_dmm_to_read_value() -> None:
    raw = minimal_dmm()
    raw["signals"] = {}
    assert any("signals.value" in p for p in problems_of(raw))


def test_lint_checks_dmm_function_labels() -> None:
    raw = minimal_dmm()
    raw["dmm"] = {
        "function_query": "FUNC?",
        "function_map": {'"VOLT"': "turbo"},
        "functions": {"dc_voltage": "V", "warp": "V"},
    }
    found = problems_of(raw)
    assert sum("dmm: 'turbo' is not one of" in p for p in found) == 1
    assert sum("dmm: 'warp' is not one of" in p for p in found) == 1


def test_lint_requires_a_function_map_with_a_function_query() -> None:
    raw = minimal_dmm()
    raw["dmm"] = {"function_query": "FUNC?", "functions": {"dc_voltage": "V"}}
    assert any("dmm.function_map" in p and "missing" in p for p in problems_of(raw))


def test_dmm_function_map_keys_lose_their_quotes_and_case() -> None:
    raw = minimal_dmm()
    raw["dmm"] = {
        "function_query": "FUNC?",
        "function_map": {'"VOLT:DC"': "dc_voltage"},
        "functions": {"dc_voltage": "V", "continuity": ""},
    }
    dmm = profile_from(raw).dmm
    assert dmm is not None
    assert dmm.function_map == {"volt:dc": "dc_voltage"}
    assert dmm.functions == {"dc_voltage": "V", "continuity": None}


def test_from_dict_leaves_its_input_alone() -> None:
    raw = minimal()
    before = copy.deepcopy(raw)
    profile_from(raw)
    assert raw == before


def write_bad(directory: Path) -> Path:
    bad = directory / "bad.toml"
    bad.write_text('schema = 1\nid = "bad"\n', encoding="utf-8")
    return bad


def test_check_paths_reports_each_file(tmp_path: Path) -> None:
    good = tmp_path / "rigol-dp800.toml"
    shutil.copy(DP800_PATH, good)
    bad = write_bad(tmp_path)
    results = dict(check_paths([tmp_path]))
    assert results[good] == []
    assert any(p.startswith("name: missing") for p in results[bad])


def test_check_paths_takes_files_and_reports_unreadable_ones(tmp_path: Path) -> None:
    good = tmp_path / "rigol-dp800.toml"
    shutil.copy(DP800_PATH, good)
    assert list(check_paths([good])) == [(good, [])]
    ((_, problems),) = list(check_paths([tmp_path / "missing.toml"]))
    assert "cannot be read" in problems[0]


def test_main_exits_nonzero_when_a_file_is_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(DP800_PATH, tmp_path / "rigol-dp800.toml")
    bad = write_bad(tmp_path)
    assert main(["check", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert f"{bad}:" in out
    assert "  name: missing or empty." in out
    assert "1 of 2 profiles OK." in out


def test_main_exits_zero_when_every_file_is_good(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(DP800_PATH, tmp_path / "rigol-dp800.toml")
    assert main(["check", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "1 of 1 profiles OK.\n"


def test_main_checks_the_shipped_profiles() -> None:
    assert main(["check", str(SHIPPED_DIR)]) == 0


@pytest.mark.parametrize("argv", [[], ["check"], ["lint", "x.toml"]])
def test_main_prints_usage_for_bad_arguments(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert capsys.readouterr().out.startswith("usage: ")


def test_main_reads_sys_argv_when_not_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shutil.copy(DP800_PATH, tmp_path / "rigol-dp800.toml")
    monkeypatch.setattr("sys.argv", ["profile", "check", str(tmp_path)])
    assert main() == 0


def test_shipped_directory_is_the_repo_profiles_directory() -> None:
    assert profile_module.SHIPPED_DIR == REPO_ROOT / "profiles"
    assert [p.id for p in shipped()] == [p.id for p in load_dir(SHIPPED_DIR)]


def test_a_scope_profiles_fields_include_every_measurement_its_slots_can_show() -> None:
    scope = by_id(shipped(), "siglent-sds")
    assert scope is not None and scope.scope is not None and scope.scope.slots is not None
    fields = scope.fields
    for name in scope.scope.slots.types.values():
        assert name in fields, name
    assert fields["pkpk"] == "V" and fields["frequency"] == "Hz" and fields["scale"] == "V"
    assert "timebase" not in fields and scope.instrument_fields["timebase"] == "s"


def test_every_profiles_own_identity_matches_exactly_one_profile(profiles: list[Profile]) -> None:
    """A loose match in one family must not claim another family's instruments."""
    from zelos_extension_instruments.profile import find
    from zelos_extension_instruments.sim import FakeInstrument

    for profile in profiles:
        if profile.generic:
            continue
        idn = parse_idn(FakeInstrument(profile).idn)
        matching = [p.id for p in profiles if p.matches(idn)]
        assert matching == [profile.id], f"{idn.text} matches {matching}"
        assert find(profiles, idn) is profile
