import pytest
from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_instruments.naming import (
    MAX_BYTES,
    Namer,
    ascii_name,
    safe_name,
    unique_name,
)


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("psu", "psu"),
        ("My PSU-1", "My PSU-1"),
        ("Temp C", "Temp C"),
        ("a/b", "a_b"),
        ("a.b", "a_b"),
        ("x/", "x"),
    ],
)
def test_safe_name_keeps_or_sanitizes(text: str, name: str) -> None:
    assert safe_name(text) == name


@pytest.mark.parametrize("text", ["", "   ", "___", " _ ", "/"])
def test_safe_name_is_never_empty(text: str) -> None:
    assert safe_name(text) == "unnamed"


def test_safe_name_stops_at_64_characters() -> None:
    assert safe_name("x" * 100) == "x" * 64
    assert safe_name("x" * 64) == "x" * 64


def test_safe_name_does_not_end_in_a_space_or_underscore_after_the_cut() -> None:
    assert safe_name("a" * 63 + " b") == "a" * 63


def test_safe_name_stops_at_the_byte_limit() -> None:
    name = safe_name("日" * 50)
    assert len(name) == 42
    assert len(name.encode()) <= MAX_BYTES


def test_safe_name_takes_a_smaller_byte_limit() -> None:
    assert safe_name("日" * 50, 20) == "日" * 6
    assert safe_name("abcdefghijklmnop", 10) == "abcdefghij"


@given(st.text(max_size=300), st.integers(min_value=4, max_value=MAX_BYTES))
def test_safe_name_always_fits_its_limits(text: str, max_bytes: int) -> None:
    name = safe_name(text, max_bytes)
    assert name
    assert len(name) <= 64
    assert len(name.encode()) <= max_bytes


@given(st.text(max_size=100))
def test_safe_name_is_idempotent(text: str) -> None:
    once = safe_name(text)
    assert safe_name(once) == once


def test_ascii_name_folds_accents() -> None:
    assert ascii_name("café") == "cafe"


@pytest.mark.parametrize("text", ["日本", "---", "", "  "])
def test_ascii_name_is_none_without_a_letter_or_digit(text: str) -> None:
    assert ascii_name(text) is None


def test_unique_name_adds_suffixes_ignoring_case() -> None:
    used: set[str] = set()
    names = [unique_name(raw, used) for raw in ("psu", "PSU", "psu", "Psu")]
    assert names == ["psu", "PSU_2", "psu_3", "Psu_4"]
    assert used == set(names)


def test_unique_name_compares_the_sanitized_name() -> None:
    used: set[str] = set()
    assert [unique_name(raw, used) for raw in ("x/", "x_", "x")] == ["x", "x_2", "x_3"]


def test_unique_name_does_not_suffix_a_free_name() -> None:
    used = {"psu"}
    assert unique_name("load", used) == "load"
    assert used == {"psu", "load"}


def test_unique_name_treats_used_names_case_insensitively() -> None:
    assert unique_name("psu", {"PSU"}) == "psu_2"


def test_unique_name_keeps_the_suffix_inside_the_limit() -> None:
    used = {"a" * 64}
    name = unique_name("a" * 100, used)
    assert name == "a" * 62 + "_2"
    assert len(name) == 64


def test_unique_name_keeps_the_suffix_inside_the_byte_limit() -> None:
    used: set[str] = set()
    first = unique_name("abcdefghijklmnop", used, 10)
    second = unique_name("abcdefghijklmnoq", used, 10)
    assert (first, second) == ("abcdefghij", "abcdefgh_2")
    assert len(second.encode()) <= 10


@given(st.lists(st.text(max_size=80), max_size=30))
def test_unique_name_never_repeats_ignoring_case(raws: list[str]) -> None:
    used: set[str] = set()
    names = [unique_name(raw, used) for raw in raws]
    assert len({n.casefold() for n in names}) == len(names)
    assert all(len(n) <= 64 and len(n.encode()) <= MAX_BYTES for n in names)


def test_namer_is_stable_for_a_repeated_raw_name() -> None:
    namer = Namer()
    assert namer.name("a b") == "a b"
    assert namer.name("a/b") == "a_b"
    assert namer.name("a b") == "a b"
    assert namer.name("a/b") == "a_b"


def test_namer_separates_different_raw_names_that_sanitize_alike() -> None:
    namer = Namer()
    assert [namer.name(raw) for raw in ("a/b", "a.b", "A_B")] == ["a_b", "a_b_2", "A_B_3"]
    assert namer.name("a.b") == "a_b_2"


def test_namer_avoids_reserved_names_ignoring_case() -> None:
    namer = Namer(("time_ns",))
    assert namer.name("time_ns") == "time_ns_2"
    assert namer.name("TIME_NS") == "TIME_NS_3"
    assert namer.name("time_ns") == "time_ns_2"


def test_namer_applies_its_byte_limit() -> None:
    namer = Namer(max_bytes=10)
    assert namer.name("abcdefghijklmnop") == "abcdefghij"
    assert namer.name("abcdefghijklmnoq") == "abcdefgh_2"
