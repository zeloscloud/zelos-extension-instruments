"""The engine: reply parsing, value checks and formatting, plans and setter commands."""

import dataclasses
import math
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_instruments.engine import (
    InstrumentError,
    ParseError,
    RangeError,
    Selector,
    Step,
    check_range,
    check_resolution,
    clear_commands,
    compile_plan,
    flag_is_set,
    format_value,
    parse_bool,
    parse_float,
    parse_int,
    parse_reply,
    read_error,
    render,
    setter_command,
    unknown_label,
    verified,
)
from zelos_extension_instruments.profile import Errors, Flag, Parse, Profile, Range, Reader, Setter

THRESHOLD = 9.9e37
FINITE = st.floats(allow_nan=False, allow_infinity=False, width=64)
BELOW_THRESHOLD = FINITE.filter(lambda x: abs(x) < 9e37)


def reader(
    parse: Parse = "float",
    *,
    fields: tuple[str, ...] = ("x",),
    index: int | None = None,
    map: dict[str, str] | None = None,
    mask: int | tuple[int, ...] = 0,
    shift: int | tuple[int, ...] = 0,
    hex: bool = False,
    query: str | None = "Q? {ch}",
    per_channel: bool = True,
    flags: tuple[Flag, ...] = (),
    name: str = "r",
) -> Reader:
    return Reader(
        name=name,
        query=query,
        parse=parse,
        fields=fields,
        index=index,
        map=map or {},
        words={label: key for key, label in (map or {}).items()},
        mask=mask if isinstance(mask, tuple) else (mask,),
        shift=shift if isinstance(shift, tuple) else (shift,),
        hex=hex,
        optional=False,
        per_channel=per_channel,
        flags=flags,
    )


def flag(
    parse: Parse = "bool",
    *,
    query: str = "F? {ch}",
    value: str = "ovp",
    map: dict[str, str] | None = None,
    mask: int | tuple[int, ...] = 0,
    shift: int | tuple[int, ...] = 0,
    hex: bool = False,
    index: int | None = None,
) -> Flag:
    return Flag(
        query,
        parse,
        value,
        map or {},
        mask if isinstance(mask, tuple) else (mask,),
        shift if isinstance(shift, tuple) else (shift,),
        hex,
        index,
    )


def range_(
    low: float | None = 0.0,
    high: float | None = 30.0,
    digits: int = 3,
    step: float | None = None,
) -> Range:
    return Range(low, high, digits, step, None, None)


def setter(
    name: str,
    command: str,
    *,
    map: dict[str, str] | None = None,
    per_channel: bool = True,
) -> Setter:
    return Setter(name, command, (command,), None, None, map or {}, per_channel)


@given(BELOW_THRESHOLD)
def test_parse_float_round_trips_g_format(x: float) -> None:
    assert math.isclose(parse_float(f"{x:g}"), x, rel_tol=1e-5, abs_tol=1e-300)


@given(BELOW_THRESHOLD)
def test_parse_float_round_trips_scientific_format(x: float) -> None:
    assert math.isclose(parse_float(f"{x:.6E}"), x, rel_tol=1e-6, abs_tol=1e-300)


@given(BELOW_THRESHOLD)
def test_parse_float_round_trips_repr_exactly(x: float) -> None:
    assert parse_float(repr(x)) == x


@given(BELOW_THRESHOLD.filter(lambda x: x >= 0))
def test_parse_float_accepts_a_leading_plus(x: float) -> None:
    assert math.isclose(parse_float(f"+{x:.6E}"), x, rel_tol=1e-6, abs_tol=1e-300)


@given(BELOW_THRESHOLD, st.sampled_from(["V", "A", "W", "Hz", "Ohm", " V", "s"]))
def test_parse_float_ignores_trailing_unit_letters(x: float, unit: str) -> None:
    assert math.isclose(parse_float(f"{x:.6E}{unit}"), x, rel_tol=1e-6, abs_tol=1e-300)


@given(BELOW_THRESHOLD, st.sampled_from(['"{}"', ' "{}"\r\n', '"{}V"', "  {}  "]))
def test_parse_float_strips_quotes_and_space(x: float, template: str) -> None:
    assert math.isclose(parse_float(template.format(f"{x:.6E}")), x, rel_tol=1e-6, abs_tol=1e-300)


@given(
    st.floats(min_value=THRESHOLD, allow_infinity=False, allow_nan=False),
    st.sampled_from([1.0, -1.0]),
)
def test_parse_float_reads_the_sentinels_and_beyond_as_nan(x: float, sign: float) -> None:
    assert math.isnan(parse_float(f"{sign * x:.6E}"))


@pytest.mark.parametrize("text", ["9.9E+37", "+9.91E+37", "9.91E37", "-9.9E37", "1e39", "9.9e38"])
def test_parse_float_reads_scpi_sentinels_as_nan(text: str) -> None:
    assert math.isnan(parse_float(text))


@pytest.mark.parametrize("text", ["9.8999E37", "1e37", "-9.89e37"])
def test_parse_float_keeps_values_below_the_sentinel(text: str) -> None:
    assert math.isfinite(parse_float(text))


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("12.000", 12.0),
        ("+1.23456E-02", 0.0123456),
        ("1.2e3", 1200.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("-0.25 V", -0.25),
        ('"3.3"', 3.3),
        ("7", 7.0),
    ],
)
def test_parse_float_reads_every_scpi_form(text: str, value: float) -> None:
    assert parse_float(text) == value


@pytest.mark.parametrize("text", ["", "   ", "abc", "--", "nan", "inf", ",", "ON", "V"])
def test_parse_float_raises_on_garbage(text: str) -> None:
    with pytest.raises(ParseError, match="expected a number"):
        parse_float(text)


def test_parse_float_error_quotes_the_reply() -> None:
    with pytest.raises(ParseError, match=re.escape("'oops'")):
        parse_float(" oops \n")


@pytest.mark.parametrize(
    ("text", "hex", "value"),
    [
        ("31", False, 31),
        ("+31", False, 31),
        ("-4", False, -4),
        ("31.0", False, 31),
        ("1E2", False, 100),
        ('"7"', False, 7),
        ("0x1F", False, 31),
        ("0X1f", False, 31),
        ("#H1F", False, 31),
        ("#h1f", False, 31),
        ("1F", True, 31),
        ("1f", True, 31),
        ("0x1F", True, 31),
        ("#H1F", True, 31),
        (" 00FF \n", True, 255),
    ],
)
def test_parse_int(text: str, hex: bool, value: int) -> None:
    assert parse_int(text, hex) == value


@pytest.mark.parametrize("text", ["", "abc", "1F", "0xZZ", "#H", "nan", "1,2"])
def test_parse_int_raises_on_garbage(text: str) -> None:
    with pytest.raises(ParseError, match="expected a whole number"):
        parse_int(text)


def test_parse_int_raises_on_non_hex_text_in_hex_format() -> None:
    with pytest.raises(ParseError):
        parse_int("xyz", hex=True)


@pytest.mark.parametrize("text", ["inf", "1e999"])
def test_parse_int_raises_parse_error_on_overflow(text: str) -> None:
    with pytest.raises(ParseError):
        parse_int(text)


def test_parse_int_keeps_large_integers_exact() -> None:
    assert parse_int("9007199254740993") == 9007199254740993


@pytest.mark.parametrize("text", ["1", "on", "ON", "On", "true", "TRUE", "True", "yes", "YES"])
def test_parse_bool_true_spellings(text: str) -> None:
    assert parse_bool(text) is True


@pytest.mark.parametrize("text", ["0", "off", "OFF", "Off", "false", "FALSE", "no", "No"])
def test_parse_bool_false_spellings(text: str) -> None:
    assert parse_bool(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [(' "ON" ', True), ("1\r\n", True), ('"0"', False), ("\tOFF\n", False)],
)
def test_parse_bool_strips_quotes_and_space(text: str, expected: bool) -> None:
    assert parse_bool(text) is expected


@pytest.mark.parametrize("text", ["", "2", "-1", "maybe", "1,0", "O N", "enabled"])
def test_parse_bool_raises_on_garbage(text: str) -> None:
    with pytest.raises(ParseError, match="expected ON/OFF or 1/0"):
        parse_bool(text)


def test_parse_reply_float() -> None:
    assert parse_reply(reader("float", fields=("voltage",)), " +1.5E+00\n") == {"voltage": 1.5}


def test_parse_reply_float_index_picks_one_value() -> None:
    r = reader("float", fields=("current",), index=1)
    assert parse_reply(r, "1.0,2.5,3.0") == {"current": 2.5}
    assert parse_reply(r, "1.0, 2.5") == {"current": 2.5}


def test_parse_reply_index_past_the_end_raises() -> None:
    with pytest.raises(ParseError, match="expected at least 3 values"):
        parse_reply(reader("float", index=2), "1.0,2.5")


def test_parse_reply_float_maps_the_sentinel_to_nan() -> None:
    assert math.isnan(parse_reply(reader("float"), "9.91E+37")["x"])


def test_parse_reply_float_raises_on_text() -> None:
    with pytest.raises(ParseError):
        parse_reply(reader("float"), "ERR")


def test_parse_reply_floats_fills_each_field_in_order() -> None:
    r = reader("floats", fields=("voltage", "current", "power"))
    assert parse_reply(r, "1.0,2.0,3.0") == {"voltage": 1.0, "current": 2.0, "power": 3.0}


def test_parse_reply_floats_accepts_extra_values() -> None:
    r = reader("floats", fields=("voltage", "current"))
    assert parse_reply(r, "1.0,2.0,3.0,4.0") == {"voltage": 1.0, "current": 2.0}


def test_parse_reply_floats_rejects_too_few_values() -> None:
    r = reader("floats", fields=("voltage", "current", "power"))
    with pytest.raises(ParseError, match="expected 3 values, got '1.0,2.0'"):
        parse_reply(r, "1.0,2.0")


def test_parse_reply_floats_rejects_a_non_number() -> None:
    r = reader("floats", fields=("voltage", "current"))
    with pytest.raises(ParseError):
        parse_reply(r, "1.0,abc")


def test_parse_reply_floats_reads_sentinels_as_nan() -> None:
    r = reader("floats", fields=("voltage", "current"))
    row = parse_reply(r, "1.0,9.9E37")
    assert row["voltage"] == 1.0
    assert math.isnan(row["current"])


def test_parse_reply_int_decimal_and_hex() -> None:
    assert parse_reply(reader("int"), "42") == {"x": 42}
    assert parse_reply(reader("int"), "0x2A") == {"x": 42}
    assert parse_reply(reader("int", hex=True), "2A") == {"x": 42}
    assert parse_reply(reader("int", index=1), "1,7") == {"x": 7}


def test_parse_reply_bool() -> None:
    assert parse_reply(reader("bool", fields=("output",)), "ON") == {"output": True}
    assert parse_reply(reader("bool", fields=("output",)), "0\n") == {"output": False}
    assert parse_reply(reader("bool", index=1), "0,1") == {"x": True}


def test_parse_reply_bool_reads_the_instruments_own_words_through_its_map() -> None:
    sense = reader("bool", map={"int": "off", "ext": "on"})
    assert parse_reply(sense, "EXT") == {"x": True}
    assert parse_reply(sense, '"INT"') == {"x": False}
    assert parse_reply(sense, "1") == {"x": True}
    with pytest.raises(ParseError):
        parse_reply(sense, "AUTO")


def test_parse_reply_bool_raises_on_garbage() -> None:
    with pytest.raises(ParseError):
        parse_reply(reader("bool"), "maybe")


def test_parse_reply_str_keeps_the_text() -> None:
    assert parse_reply(reader("str"), "  Some text, here \r\n") == {"x": "Some text, here"}


def test_parse_reply_map_is_case_insensitive_and_strips_quotes() -> None:
    r = reader("map", fields=("mode",), map={"cv": "cv", "cc": "cc"})
    assert parse_reply(r, "CV") == {"mode": "cv"}
    assert parse_reply(r, '"Cc"\r\n') == {"mode": "cc"}
    assert parse_reply(r, ' "cv" ') == {"mode": "cv"}


def test_parse_reply_map_index() -> None:
    r = reader("map", fields=("mode",), index=1, map={"cc": "cc"})
    assert parse_reply(r, "1,CC") == {"mode": "cc"}


def test_parse_reply_map_rejects_an_unmapped_reply() -> None:
    r = reader("map", map={"cv": "cv"}, name="mode")
    with pytest.raises(ParseError, match="'XX' is not in the map of mode"):
        parse_reply(r, "XX")


def test_parse_reply_map_gives_an_unmapped_reply_the_catch_all_label() -> None:
    r = reader("map", fields=("output_load",), map={"50": "fifty_ohm", "*": "other"})
    assert parse_reply(r, "50") == {"output_load": "fifty_ohm"}
    assert parse_reply(r, "+1.000000000000000E+02") == {"output_load": "other"}
    shape = keyed("shape", "WVTP", {"sine": "sine", "*": "other"})
    assert parse_reply(shape, "C1:BSWV WVTP,NOISE") == {"shape": "other"}


def test_parse_reply_map_matches_keys_the_profile_loader_folds(dp800: Profile) -> None:
    mode = dp800.reader("mode")
    assert mode is not None
    assert parse_reply(mode, '"UR"') == {"mode": "unregulated"}


def test_parse_reply_bits_without_a_map_is_a_bool() -> None:
    r = reader("bits", fields=("output",), mask=0x04, shift=2)
    assert parse_reply(r, "4") == {"output": True}
    assert parse_reply(r, "3") == {"output": False}
    assert parse_reply(r, "0xFF") == {"output": True}


def test_parse_reply_bits_with_a_map_is_a_label() -> None:
    r = reader("bits", fields=("mode",), mask=0xF0, shift=4, map={"1": "cv", "2": "cc"})
    assert parse_reply(r, "0x10") == {"mode": "cv"}
    assert parse_reply(r, "47") == {"mode": "cc"}
    assert parse_reply(r, "0x1F") == {"mode": "cv"}


def test_parse_reply_bits_hex_format_and_index() -> None:
    r = reader("bits", fields=("mode",), mask=0xF0, shift=4, hex=True, index=1, map={"10": "cc"})
    assert parse_reply(r, "0,A5") == {"mode": "cc"}


def test_parse_reply_bits_rejects_a_value_not_in_the_map() -> None:
    r = reader("bits", mask=0x03, map={"1": "cv"}, name="mode")
    with pytest.raises(ParseError, match="bit value 2 of mode is not in its map"):
        parse_reply(r, "2")


def test_parse_reply_bits_rejects_non_numbers() -> None:
    with pytest.raises(ParseError):
        parse_reply(reader("bits", mask=1), "high")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("1", True), ("0", False), ('"ON"', True), ("off", False)],
)
def test_flag_is_set_for_a_bool_flag(reply: str, expected: bool) -> None:
    assert flag_is_set(flag("bool"), reply) is expected


def test_flag_is_set_for_a_bool_flag_with_an_index() -> None:
    assert flag_is_set(flag("bool", index=1), "0,1") is True
    assert flag_is_set(flag("bool", index=1), "1,0") is False


def test_flag_is_set_for_bits_without_a_map() -> None:
    f = flag("bits", mask=0x04, shift=2)
    assert flag_is_set(f, "4") is True
    assert flag_is_set(f, "3") is False
    assert flag_is_set(f, "0x0C") is True


def test_flag_is_set_for_bits_with_a_map() -> None:
    f = flag("bits", mask=0x03, map={"0": "none", "1": "ovp", "2": "false", "3": "0"})
    assert flag_is_set(f, "1") is True
    assert flag_is_set(f, "0") is False
    assert flag_is_set(f, "2") is False
    assert flag_is_set(f, "3") is False


def test_flag_is_set_when_the_map_lacks_the_value() -> None:
    f = flag("bits", mask=0x03, map={"1": "ovp"})
    assert flag_is_set(f, "2") is False


def test_flag_is_set_raises_on_garbage() -> None:
    with pytest.raises(ParseError):
        flag_is_set(flag("bool"), "maybe")


def test_unknown_label() -> None:
    assert unknown_label("mode") == "unknown"
    assert unknown_label("protection") == "other"
    assert unknown_label("function") == "other"


def test_render_fills_every_placeholder() -> None:
    template = "C{ch} V{value} {on} {bit} F{function}"
    assert render(template, ch=2, value="5.000", on=True, function="CURR") == (
        "C2 V5.000 ON 1 FCURR"
    )
    assert render(template, ch=2, value="5.000", on=False, function="CURR") == (
        "C2 V5.000 OFF 0 FCURR"
    )


def test_render_without_placeholders_returns_the_text() -> None:
    assert render("*IDN?") == "*IDN?"


def test_render_channel_zero_is_a_channel() -> None:
    assert render("CH{ch}", ch=0) == "CH0"


@pytest.mark.parametrize(
    ("template", "ch"),
    [("CH{ch}", None), ("V {value}", 1), ("OUTP {on}", 1), ("FUNC {function}", 1)],
)
def test_render_raises_on_a_missing_placeholder(template: str, ch: int | None) -> None:
    with pytest.raises(ValueError, match="cannot render"):
        render(template, ch=ch)


@pytest.mark.parametrize("template", ["X {0}", "X {", "X }"])
def test_render_raises_on_a_malformed_template(template: str) -> None:
    with pytest.raises(ValueError, match="cannot render"):
        render(template, ch=1, value="1", on=True, function="F")


def test_compile_plan_orders_fast_then_slow_per_channel(dp800: Profile) -> None:
    plan = compile_plan(dp800, 2)
    assert [s.key for s in plan.fast] == ["measure@1", "measure@2"]
    assert [s.key for s in plan.slow] == [
        "voltage_set@1",
        "voltage_set@2",
        "current_set@1",
        "current_set@2",
        "ovp_set@1",
        "ovp_set@2",
        "ocp_set@1",
        "ocp_set@2",
        "ovp_on@1",
        "ovp_on@2",
        "ocp_on@1",
        "ocp_on@2",
        "remote_sense@1",
        "remote_sense@2",
        "output@1",
        "output@2",
        "mode@1",
        "mode@2",
        "protection#0@1",
        "protection#1@1",
        "protection#0@2",
        "protection#1@2",
    ]
    assert [s.cadence for s in plan.fast] == ["fast", "fast"]
    assert {s.cadence for s in plan.slow} == {"slow"}
    assert [s.key for s in plan] == [s.key for s in (*plan.fast, *plan.slow)]


def test_compile_plan_expands_flags_into_their_own_steps(dp800: Profile) -> None:
    plan = compile_plan(dp800, 1)
    flags = [s for s in plan.slow if s.flag is not None]
    assert [s.query for s in flags] == [":OUTP:OVP:QUES? CH1", ":OUTP:OCP:QUES? CH1"]
    assert [s.flag.value for s in flags if s.flag] == ["ovp", "ocp"]


def test_compile_plan_runs_a_channel_less_reader_once(dp800: Profile) -> None:
    board = reader("float", fields=("voltage",), query="TEMP?", per_channel=False, name="temp")
    custom = dataclasses.replace(dp800, signals=(board, *dp800.signals), state=())
    plan = compile_plan(custom, 3)
    assert [s.key for s in plan.fast] == ["temp@None", "measure@1", "measure@2", "measure@3"]
    assert plan.fast[0].channel is None
    assert plan.fast[0].query == "TEMP?"


def test_compile_plan_removes_dropped_keys(dp800: Profile) -> None:
    plan = compile_plan(dp800, 2, {"measure@2", "mode@1", "protection#1@1", "nope@9"})
    assert [s.key for s in plan.fast] == ["measure@1"]
    keys = [s.key for s in plan.slow]
    assert "mode@1" not in keys
    assert "mode@2" in keys
    assert "protection#1@1" not in keys
    assert "protection#0@1" in keys
    assert len(keys) == len(compile_plan(dp800, 2).slow) - 2


def test_compile_plan_accepts_a_frozenset_and_zero_channels(dp800: Profile) -> None:
    assert [s.key for s in compile_plan(dp800, 1, frozenset({"measure@1"})).fast] == []
    empty = compile_plan(dp800, 0)
    assert (empty.fast, empty.slow) == ((), ())


def test_step_query_renders_the_channel(dp800: Profile) -> None:
    plan = compile_plan(dp800, 3)
    assert plan.fast[2].query == ":MEAS:ALL? CH3"
    assert plan.slow[0].query == ":SOUR1:VOLT?"


def test_step_query_without_a_channel_cannot_render_a_channel_template() -> None:
    step = Step("fast", None, reader(query="Q? {ch}"))
    with pytest.raises(ValueError, match="cannot render"):
        _ = step.query


def test_step_key_names_reader_flag_and_channel() -> None:
    r = reader(name="protection", flags=(flag(value="ovp"), flag(value="ocp")))
    assert Step("slow", 2, r).key == "protection@2"
    assert Step("slow", 2, r, r.flags[0]).key == "protection#0@2"
    assert Step("slow", None, r, r.flags[1]).key == "protection#1@None"


def test_format_value_uses_the_range_digits() -> None:
    assert format_value(1.23456, range_(digits=2)) == "1.23"
    assert format_value(1.5, range_(digits=0)) == "2"
    assert format_value(1.5, range_(digits=5)) == "1.50000"
    assert format_value(-0.5, range_(digits=1)) == "-0.5"


def test_format_value_keeps_every_digit_without_a_range() -> None:
    """A value without a range is sent as it is; the readback decides whether it took."""
    assert format_value(1.23456, None) == "1.23456"
    assert format_value(1e-6, None) == "1e-06"
    assert format_value(5.0, None) == "5"


def test_check_range_accepts_values_inside_the_range() -> None:
    for value in (0.0, 12.5, 30.0):
        check_range(value, range_(0, 30), None, "voltage")


@pytest.mark.parametrize("value", [-0.1, 30.1])
def test_check_range_refuses_values_outside_the_profile_range(value: float) -> None:
    with pytest.raises(RangeError, match="voltage must be from 0 to 30"):
        check_range(value, range_(0, 30), None, "voltage")


def test_check_range_uses_a_nonzero_minimum() -> None:
    with pytest.raises(RangeError, match="must be from 2 to 30"):
        check_range(1.0, range_(2, 30), None, "voltage")


def test_check_range_treats_a_missing_minimum_as_zero() -> None:
    check_range(0.0, range_(None, 30), None, "voltage")
    with pytest.raises(RangeError, match="must be from 0 to 30"):
        check_range(-1.0, range_(None, 30), None, "voltage")


def test_check_range_refuses_values_over_the_config_limit() -> None:
    check_range(10.0, range_(0, 30), 10.0, "voltage")
    with pytest.raises(RangeError, match="voltage must be at most 10, the configured limit"):
        check_range(10.5, range_(0, 30), 10.0, "voltage")


def test_check_range_applies_the_range_before_the_limit() -> None:
    with pytest.raises(RangeError, match="must be from 0 to 30"):
        check_range(40.0, range_(0, 30), 50.0, "voltage")
    with pytest.raises(RangeError, match="must be from 0 to 30"):
        check_range(40.0, range_(0, 30), 35.0, "voltage")


def test_check_range_with_a_limit_but_no_range() -> None:
    check_range(5.0, None, 10.0, "current")
    check_range(5.0, range_(0, None), 10.0, "current")
    with pytest.raises(RangeError, match="current must be at most 10"):
        check_range(11.0, None, 10.0, "current")


@pytest.mark.parametrize("range_arg", [None, range_(0, None)])
def test_check_range_without_a_range_or_limit_asks_for_a_limit(range_arg: Range | None) -> None:
    with pytest.raises(
        RangeError, match="no range is known for power on this profile; set a limit"
    ):
        check_range(1.0, range_arg, None, "power")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_check_range_refuses_non_finite_values(value: float) -> None:
    for range_arg, limit in ((range_(0, 30), None), (None, 10.0), (None, None)):
        with pytest.raises(RangeError, match="voltage must be a finite number"):
            check_range(value, range_arg, limit, "voltage")


def test_setter_command_formats_a_value_with_the_range_digits() -> None:
    s = setter("voltage", ":SOUR{ch}:VOLT {value}")
    assert setter_command(s, ch=2, value=5.0, range_=range_(digits=3)) == ":SOUR2:VOLT 5.000"
    assert setter_command(s, ch=1, value=5.0, range_=range_(digits=1)) == ":SOUR1:VOLT 5.0"
    assert setter_command(s, ch=1, value=5.0) == ":SOUR1:VOLT 5"


def test_setter_command_renders_on_and_bit() -> None:
    s = setter("output", ":OUTP CH{ch},{on}")
    assert setter_command(s, ch=2, on=True) == ":OUTP CH2,ON"
    assert setter_command(s, ch=2, on=False) == ":OUTP CH2,OFF"
    assert setter_command(setter("output", "OUTP{ch} {bit}"), ch=3, on=True) == "OUTP3 1"


def test_setter_command_maps_a_function_to_the_instrument_word() -> None:
    s = setter("function", "CONF:{function}", map={"dc_voltage": "VOLT:DC", "cc": "CURR"})
    assert setter_command(s, ch=None, function="dc_voltage") == "CONF:VOLT:DC"
    assert setter_command(s, ch=None, function="cc") == "CONF:CURR"


def test_setter_command_refuses_an_unmapped_function() -> None:
    s = setter("function", "CONF:{function}", map={"cc": "CURR"})
    with pytest.raises(RangeError, match="cp is not a function this profile can set"):
        setter_command(s, ch=None, function="cp")


def test_setter_command_skips_the_channel_for_an_instrument_wide_setter() -> None:
    s = setter("clear", "*CLS", per_channel=False)
    assert setter_command(s, ch=2) == "*CLS"


def test_setter_command_needs_the_channel_for_a_channel_command() -> None:
    with pytest.raises(ValueError, match="cannot render"):
        setter_command(setter("clear", "CLE CH{ch}"), ch=None)


def test_setter_command_leaves_a_missing_value_unrendered() -> None:
    with pytest.raises(ValueError, match="cannot render"):
        setter_command(setter("voltage", "VOLT {value}"), ch=1)


def test_verified_uses_the_step_with_a_tolerance_of_one_and_a_half() -> None:
    r = range_(step=0.1)
    assert verified(5.0, 5.14, r)
    assert verified(5.0, 4.86, r)
    assert not verified(5.0, 5.16, r)
    assert not verified(5.0, 4.84, r)


def test_verified_uses_the_digits_without_a_step() -> None:
    r = range_(digits=3)
    assert verified(5.0, 5.0014, r)
    assert not verified(5.0, 5.0016, r)
    assert verified(5.0, 5.0, range_(digits=0))
    assert verified(5.0, 6.4, range_(digits=0))
    assert not verified(5.0, 6.6, range_(digits=0))


def test_verified_without_a_range_allows_a_part_in_a_thousand() -> None:
    assert verified(5.0, 5.004, None)
    assert not verified(5.0, 5.006, None)
    assert not verified(1e-6, 0.0, None)


@pytest.mark.parametrize("readback", [math.nan, math.inf, -math.inf])
def test_verified_refuses_a_non_finite_readback(readback: float) -> None:
    assert not verified(5.0, readback, range_(step=1e9))


ERRORS = Errors(query="SYST:ERR?", ok=re.compile(r"^\s*[+-]?0\b"), drain=20)


@pytest.mark.parametrize("reply", ['0,"No error"', '+0,"No error"', "0", " 0\r\n", '-0,"x"'])
def test_read_error_reads_no_error(reply: str) -> None:
    assert read_error(ERRORS, reply) is None


def test_read_error_splits_code_and_message() -> None:
    assert read_error(ERRORS, '-113,"Undefined header"') == InstrumentError(
        -113, "Undefined header"
    )
    assert read_error(ERRORS, '+102,"Syntax error;extra"\r\n') == InstrumentError(
        102, "Syntax error;extra"
    )


def test_read_error_keeps_the_text_of_a_reply_without_a_code() -> None:
    assert read_error(ERRORS, "Command error") == InstrumentError(None, "Command error")
    assert read_error(ERRORS, "abc,def") == InstrumentError(None, "abc,def")


@pytest.mark.parametrize(("reply", "code"), [("1", 1), ("+5\n", 5), ("-113", -113)])
def test_read_error_reads_a_bare_whole_number_as_its_code_where_the_profile_says_so(
    reply: str, code: int
) -> None:
    bare = dataclasses.replace(ERRORS, bare=True)
    assert read_error(bare, reply) == InstrumentError(code, reply.strip())
    # Elsewhere a bare number at the error query is a late reply, out of step.
    assert read_error(ERRORS, reply) == InstrumentError(None, reply.strip())


def test_read_error_honors_the_profiles_ok_pattern() -> None:
    errors = Errors(query="E?", ok=re.compile(r"^OK"), drain=5)
    assert read_error(errors, "OK") is None
    assert read_error(errors, "0,fine") == InstrumentError(0, "fine")


def test_selector_sends_on_first_use_and_on_a_change_only() -> None:
    selector = Selector("INST:NSEL {ch}")
    assert selector.command_for(1) == "INST:NSEL 1"
    # Until the command is marked as sent, the selection is still unknown.
    assert selector.command_for(1) == "INST:NSEL 1"
    selector.mark(1)
    assert selector.command_for(1) is None
    assert selector.command_for(2) == "INST:NSEL 2"
    selector.mark(2)
    assert selector.command_for(2) is None
    assert selector.command_for(1) == "INST:NSEL 1"


def test_selector_forget_resends() -> None:
    selector = Selector("INST:NSEL {ch}")
    selector.command_for(1)
    selector.forget()
    assert selector.command_for(1) == "INST:NSEL 1"


def test_selector_ignores_a_channel_less_command_and_keeps_the_selection() -> None:
    selector = Selector("INST:NSEL {ch}")
    assert selector.command_for(2) == "INST:NSEL 2"
    selector.mark(2)
    assert selector.command_for(None) is None
    assert selector.command_for(2) is None


def test_selector_without_a_select_command_never_sends() -> None:
    selector = Selector(None)
    assert selector.command_for(1) is None
    assert selector.command_for(2) is None
    selector.forget()
    assert selector.command_for(1) is None


def test_an_unranged_value_keeps_its_digits_and_verifies_relatively() -> None:
    assert format_value(1e-6, None) == "1e-06"
    assert format_value(0.2834, None) == "0.2834"
    assert format_value(12.3456, Range(0, 30, 3, None, None, None)) == "12.346"
    assert verified(1e-6, 1e-6, None)
    assert verified(0.2834, 0.28341, None)
    assert not verified(1e-6, 0.0, None), "a tiny value must not pass as zero"
    assert not verified(0.2834, 0.28, None), "a snapped value is a refusal"


def test_a_value_finer_than_the_instruments_digits_is_refused() -> None:
    three = Range(min=0.0, max=30.0, digits=3, step=None, min_query=None, max_query=None)
    check_resolution(5.001, three, "voltage")
    with pytest.raises(RangeError, match="finer than the instrument's 3 decimals"):
        check_resolution(0.00012, three, "current")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10M", 10e6),
        ("10k", 10e3),
        ("1.00GSa/s", 1e9),
        ("5mV", 0.005),
        ("2.5e6 pts", 2.5e6),
        ("-0.25 V", -0.25),
        ("3.58E+00V", 3.58),
    ],
)
def test_parse_float_reads_metric_prefixes_and_units(text: str, expected: float) -> None:
    assert parse_float(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["C1:VDIV 5.00E-01V", "C1:PAVA PKPK,3.58E+00V", "INR 8193"])
def test_parse_float_refuses_a_header(text: str) -> None:
    with pytest.raises(ParseError):
        parse_float(text)


@pytest.mark.parametrize("text", ["1.5 !!", "3.3,4.4", "5 (V)"])
def test_parse_float_refuses_a_number_followed_by_something_that_is_not_a_unit(text: str) -> None:
    with pytest.raises(ParseError, match="expected a number"):
        parse_float(text)


def test_check_range_without_a_range_refuses_a_value_below_the_negative_limit() -> None:
    check_range(-10.0, None, 10.0, "current")
    with pytest.raises(RangeError, match="current must be from -10 to 10, the configured limit"):
        check_range(-10.5, None, 10.0, "current")
    with pytest.raises(RangeError, match="current must be from -10 to 10, the configured limit"):
        check_range(-10.5, range_(0, None), 10.0, "current")


def test_parse_reply_bits_names_a_channel_the_masks_do_not_reach() -> None:
    r = reader("bits", fields=("output",), mask=(1, 2), shift=(0, 1))
    assert parse_reply(r, "2", 2) == {"output": True}
    with pytest.raises(ParseError, match="no mask or shift for channel 3"):
        parse_reply(r, "2", 3)


def test_render_fills_the_system_scope_generator_and_scan_placeholders() -> None:
    assert render("DISP:TEXT '{text}'", text="HELLO") == "DISP:TEXT 'HELLO'"
    assert render("TRIG:A:LEV:CH{src} {value}", src=2, value="0.5") == "TRIG:A:LEV:CH2 0.5"
    assert render("C{ch}:MDWV {modulation},DEPTH,{value}", ch=1, modulation="AM", value="50") == (
        "C1:MDWV AM,DEPTH,50"
    )
    assert render("CONF:TEMP TC,{tc},(@{channels})", tc="K", channels="101,102") == (
        "CONF:TEMP TC,K,(@101,102)"
    )
    assert render("DATA:REM? {count}", count=3) == "DATA:REM? 3"


def keyed(field: str, key: str, map: dict[str, str] | None = None, scale: float = 1.0) -> Reader:
    return dataclasses.replace(
        reader("keyed", fields=(field,), map=map), key=key, name=field, scale=scale
    )


BSWV = "C1:BSWV WVTP,SINE,FRQ,100HZ,PERI,0.01S,AMP,2V,OFST,-1V,HLEV,0V,LLEV,-2V,PHSE,0"


@pytest.mark.parametrize(
    ("key", "value"),
    [("FRQ", 100.0), ("peri", 0.01), ("AMP", 2.0), ("OFST", -1.0), ("LLEV", -2.0), ("PHSE", 0.0)],
)
def test_parse_reply_keyed_reads_a_number_after_its_key(key: str, value: float) -> None:
    assert parse_reply(keyed("frequency_set", key), BSWV) == {"frequency_set": value}


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("100HZ", 100.0),
        ("5KHZ", 5000.0),
        ("1MHZ", 1e6),
        ("2V", 2.0),
        ("-1V", -1.0),
        ("500MV", 0.5),
        ("0.01S", 0.01),
        ("10US", 1e-5),
        ("4VPP", 4.0),
        ("50", 50.0),
        ("2.5e3HZ", 2500.0),
    ],
)
def test_parse_reply_keyed_strips_a_unit_and_scales_by_its_prefix(token: str, value: float) -> None:
    got = parse_reply(keyed("frequency_set", "FRQ"), f"FRQ,{token}")["frequency_set"]
    assert got == pytest.approx(value)


def test_parse_reply_keyed_applies_the_reader_scale() -> None:
    assert parse_reply(keyed("width_set", "WIDTH", scale=1e-3), "WIDTH,2") == {"width_set": 0.002}


def test_parse_reply_keyed_maps_a_label_and_reads_a_switch() -> None:
    shape = keyed("shape", "WVTP", {"sine": "sine", "square": "square"})
    assert parse_reply(shape, BSWV) == {"shape": "sine"}
    with pytest.raises(ParseError, match="not in the map"):
        parse_reply(shape, "C1:BSWV WVTP,NOISE")
    burst = keyed("burst", "STATE")
    assert parse_reply(burst, "C1:BTWV STATE,ON,PRD,0.01S") == {"burst": True}
    assert parse_reply(burst, "C1:BTWV STATE,OFF") == {"burst": False}


def test_parse_reply_keyed_leaves_a_missing_key_unknown() -> None:
    reply = "C1:MDWV STATE,OFF"
    assert math.isnan(
        parse_reply(keyed("modulation_depth_set", "DEPTH"), reply)["modulation_depth_set"]
    )
    assert parse_reply(keyed("modulation_type", "MDSP", {"am": "am"}), reply) == {
        "modulation_type": "other"
    }
    assert parse_reply(keyed("burst", "BURST"), reply) == {}
    assert parse_reply(keyed("modulation", "STATE"), reply) == {"modulation": False}


def test_parse_reply_keyed_without_a_header_and_with_a_key_that_ends_the_reply() -> None:
    assert parse_reply(keyed("frequency_set", "FRQ"), "WVTP,SINE,FRQ,5KHZ") == {
        "frequency_set": 5000.0
    }
    assert math.isnan(parse_reply(keyed("frequency_set", "FRQ"), "WVTP,FRQ")["frequency_set"])


def test_parse_reply_keyed_looks_for_its_key_only_before_the_until_token() -> None:
    rate = dataclasses.replace(keyed("modulation_frequency_set", "FRQ"), until="carr")
    fsk = "C1:MDWV FSK,STATE,ON,KFRQ,100HZ,CARR,WVTP,SINE,FRQ,1000HZ"
    assert math.isnan(parse_reply(rate, fsk)["modulation_frequency_set"])
    assert parse_reply(keyed("modulation_frequency_set", "FRQ"), fsk) == {
        "modulation_frequency_set": 1000.0
    }
    am = "C1:MDWV AM,STATE,ON,FRQ,100HZ,CARR,WVTP,SINE,FRQ,1000HZ"
    assert parse_reply(rate, am) == {"modulation_frequency_set": 100.0}


def test_parse_reply_keyed_refuses_a_value_that_is_not_a_number() -> None:
    with pytest.raises(ParseError, match="expected a number"):
        parse_reply(keyed("frequency_set", "FRQ"), "FRQ,FAST")
    with pytest.raises(ParseError, match="expected a number"):
        parse_reply(keyed("frequency_set", "FRQ"), "FRQ,5QHZ")


APPLY = '"SIN,+5.0E+03,+3.0E+00,-3.0E+00,+4.0E+00"'


def test_parse_reply_index_reads_a_quoted_list() -> None:
    assert parse_reply(reader("float", index=1), APPLY) == {"x": 5000.0}
    assert parse_reply(reader("float", index=4), APPLY) == {"x": 4.0}
    assert parse_reply(reader("map", index=0, map={"sin": "sine"}), APPLY) == {"x": "sine"}
    assert parse_reply(reader("str", index=0), APPLY) == {"x": "SIN"}
    assert parse_reply(reader("str"), APPLY) == {"x": APPLY}


def test_check_range_takes_only_one_of_the_choices() -> None:
    nplc = dataclasses.replace(range_(0.02, 100), choices=(0.02, 0.2, 1.0, 10.0, 100.0))
    check_range(0.2, nplc, None, "nplc")
    check_range(10.000000000001, nplc, None, "nplc")
    with pytest.raises(RangeError, match="nplc must be one of 0.02, 0.2, 1, 10, 100"):
        check_range(5, nplc, None, "nplc")
    check_resolution(0.02, dataclasses.replace(nplc, digits=0), "nplc")


def test_setter_command_sends_the_off_command_of_a_switch() -> None:
    relay = dataclasses.replace(setter("relay", "ROUTE:CLOSE (@{ch})"), off="ROUTE:OPEN (@{ch})")
    assert setter_command(relay, ch=101, on=True) == "ROUTE:CLOSE (@101)"
    assert setter_command(relay, ch=101, on=False) == "ROUTE:OPEN (@101)"


def test_setter_command_fills_the_trigger_source_and_the_modulation() -> None:
    level = setter("trigger_level", "TRIG:A:LEV:CH{src} {value}", per_channel=False)
    assert setter_command(level, ch=None, value=0.5, src=3) == "TRIG:A:LEV:CH3 0.5"
    depth = setter("modulation_depth", "C{ch}:MDWV {modulation},DEPTH,{value}")
    assert setter_command(depth, ch=2, value=80, modulation="AM") == "C2:MDWV AM,DEPTH,80"


def test_clear_commands_sends_every_command_of_a_command_setter() -> None:
    trigger = dataclasses.replace(
        setter("trigger", "TRIG:SOUR BUS"), commands=("TRIG:SOUR BUS", "SOUR{ch}:TRIG")
    )
    assert clear_commands(trigger, ch=2) == ["TRIG:SOUR BUS", "SOUR2:TRIG"]
    force = dataclasses.replace(setter("force", "TRIG:FORC CH{src}"), per_channel=False)
    assert clear_commands(force, ch=1, src=4) == ["TRIG:FORC CH4"]
