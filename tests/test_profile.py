"""The profile model: finding a profile by identity, ranges, channels, queries and fields."""

import pytest

from tests.conftest import minimal, profile_from
from zelos_extension_instruments.profile import (
    ENUM_FIELDS,
    Idn,
    Profile,
    by_id,
    find,
    is_identity,
    parse_idn,
)
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.sim.fake import FakeInstrument

RIGOL_DP832 = "RIGOL TECHNOLOGIES,DP832,DP8A123456,00.01.16"


def with_model(profile_id: str, model: str, manufacturer: str = "(?i)^acme") -> Profile:
    raw = minimal()
    raw["id"] = profile_id
    raw["match"] = {"manufacturer": manufacturer, "model": model}
    return profile_from(raw)


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
    for profile in profiles:
        if profile.generic:
            continue
        idn = parse_idn(FakeInstrument(profile).idn)
        matching = [p.id for p in profiles if p.matches(idn)]
        assert matching == [profile.id], f"{idn.text} matches {matching}"
        assert find(profiles, idn) is profile


def test_every_enumerated_field_ends_in_unknown_or_other() -> None:
    """A reply not in a map reads as the table's last label."""
    assert all(labels[-1] in ("unknown", "other") for labels in ENUM_FIELDS.values())


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("RIGOL TECHNOLOGIES,DP832,DP8A1,00.01.16", True),
        ("LAMBDA,GEN60-12.5", True),
        ("EA Elektro-Automatik GmbH & Co. KG, PS 9040-20 T", True),
        ("12.0,0.5,6.0", False),
        ("0.00E+00", False),
        ("", False),
    ],
)
def test_an_identity_names_a_maker(reply: str, expected: bool) -> None:
    assert is_identity(reply) is expected
