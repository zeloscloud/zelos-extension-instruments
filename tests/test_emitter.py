import logging
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest
import zelos_sdk

from tests.conftest import TIME, Recording
from zelos_extension_instruments.config import PollRow
from zelos_extension_instruments.emitter import Emitter
from zelos_extension_instruments.profile import ENUM_FIELDS, Profile
from zelos_extension_instruments.profile_load import from_dict

SECOND = 1_000_000_000
LOGGER = "zelos_extension_instruments.emitter"


class SpySource:
    """Forwards to a real source and keeps what the emitter registered."""

    def __init__(self, source: zelos_sdk.TraceSource) -> None:
        self._source = source
        self.fields: dict[str, dict[str, tuple[Any, str | None]]] = {}
        self.tables: dict[tuple[str, str], dict[int, str]] = {}
        self.batches: list[tuple[str, pa.RecordBatch]] = []

    def add_event(self, name: str, schema: Any, *args: Any) -> zelos_sdk.TraceSourceEvent:
        if isinstance(schema, list):
            self.fields[name] = {f.name: (f.data_type, f.unit) for f in schema}
        return self._source.add_event(name, schema, *args)

    def add_value_table(self, name: str, field_name: str, data: dict[int, str]) -> None:
        self.tables[(name, field_name)] = dict(data)
        self._source.add_value_table(name, field_name, data)

    def log_batch(self, name: str, batch: pa.RecordBatch) -> None:
        self.batches.append((name, batch))
        self._source.log_batch(name, batch)

    def source(self) -> zelos_sdk.TraceSource:
        return cast(zelos_sdk.TraceSource, self)


def dmm_profile() -> Profile:
    return from_dict(
        {
            "schema": 1,
            "id": "acme-dmm",
            "name": "Acme DMM",
            "category": "dmm",
            "evidence": {"badge": "manual", "source": "Acme manual", "date": "2026-10-20"},
            "match": {"manufacturer": "(?i)^acme", "model": "^D1$"},
            "signals": {"value": {"query": "READ?", "parse": "float"}},
            "state": {
                "function": {"query": "FUNC?", "parse": "map", "map": {"VOLT": "dc_voltage"}}
            },
            "dmm": {
                "function_query": "FUNC?",
                "function_map": {"VOLT": "dc_voltage"},
                "functions": {"dc_voltage": "V", "resistance": "Ohm", "continuity": None},
            },
        },
        "acme-dmm.toml",
    )


@pytest.fixture
def spy(trace_file: Recording) -> SpySource:
    return SpySource(trace_file.source)


def emitter_for(spy: SpySource) -> Emitter:
    return Emitter(spy.source())


def register(emitter: Emitter, name: str, profile: Profile, channels: int, *poll: Any) -> None:
    """Register an instrument as a worker does: its events, then every channel's fields."""
    emitter.add_instrument(name, profile, channels, *poll)
    emitter.add_channels(name, {ch: set(profile.fields) for ch in range(1, channels + 1)})


def test_row_writes_numbers_a_bool_and_enum_indexes(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 2)
    emitter.row(
        "psu",
        1,
        {"voltage": 12.5, "current": 1, "output": True, "mode": "cc", "protection": "ovp"},
        2 * SECOND,
    )
    emitter.row("psu", 2, {"voltage_set": 5.0, "output": False, "mode": "cv"}, 3 * SECOND)
    events = trace_file.events()
    (first,) = events["psu/ch1"]
    assert first[TIME] == 2.0
    assert first["voltage"] == 12.5
    assert first["current"] == 1.0
    assert first["output"] is True
    assert first["mode"] == 2
    assert first["protection"] == 1
    assert first["power"] is None
    (second,) = events["psu/ch2"]
    assert second[TIME] == 3.0
    assert second["voltage_set"] == 5.0
    assert second["output"] is False
    assert second["mode"] == 1
    assert second["voltage"] is None


def test_row_writes_the_label_table_index_for_every_label(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    for i, label in enumerate(ENUM_FIELDS["mode"]):
        emitter.row("psu", 1, {"mode": label}, (i + 1) * SECOND)
    rows = trace_file.events()["psu/ch1"]
    assert [r["mode"] for r in rows] == [0, 1, 2, 3, 4, 5, 6]


def test_row_writes_the_last_index_for_an_unknown_label(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 1, {"mode": "turbo", "protection": "meltdown"}, SECOND)
    (row,) = trace_file.events()["psu/ch1"]
    assert row["mode"] == len(ENUM_FIELDS["mode"]) - 1 == 6
    assert row["protection"] == len(ENUM_FIELDS["protection"]) - 1 == 5


def test_row_ignores_a_field_the_profile_does_not_read(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 1, {"voltage": 1.0, "frequency": 50.0, "bogus": 3}, SECOND)
    (row,) = trace_file.events()["psu/ch1"]
    assert row["voltage"] == 1.0
    assert "frequency" not in row
    assert "bogus" not in row


def test_row_with_only_unknown_fields_writes_nothing(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 1, {"bogus": 3}, SECOND)
    emitter.row("psu", 1, {}, 2 * SECOND)
    assert trace_file.events()["psu/ch1"] == []


def test_row_for_a_channel_the_instrument_lacks_is_dropped(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 2, {"voltage": 1.0}, SECOND)
    events = trace_file.events()
    assert "psu/ch2" not in events
    assert events["psu/ch1"] == []


def test_row_writes_nan_for_a_missing_number(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 1, {"voltage": None, "current": float("nan")}, SECOND)
    (row,) = trace_file.events()["psu/ch1"]
    assert row["voltage"] != row["voltage"]
    assert row["current"] != row["current"]


def test_row_for_an_unregistered_instrument_raises(spy: SpySource) -> None:
    with pytest.raises(KeyError):
        emitter_for(spy).row("nobody", 1, {"voltage": 1.0}, SECOND)


def test_reading_writes_one_event_per_function_with_its_unit(
    trace_file: Recording, spy: SpySource
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "dmm", dmm_profile(), 1)
    emitter.reading("dmm", "dc_voltage", 3.3, SECOND)
    emitter.reading("dmm", "resistance", 470.0, 2 * SECOND)
    emitter.reading("dmm", "dc_voltage", 3.4, 3 * SECOND)
    events = trace_file.events()
    assert events["dmm/dc_voltage"] == [
        {TIME: 1.0, "dc_voltage": 3.3},
        {TIME: 3.0, "dc_voltage": 3.4},
    ]
    assert events["dmm/resistance"] == [{TIME: 2.0, "resistance": 470.0}]
    # A function the meter never read has no event, so no empty signal in the trace.
    assert "dmm/continuity" not in events
    assert spy.fields["dmm/dc_voltage"]["dc_voltage"][1] == "V"
    assert spy.fields["dmm/resistance"]["resistance"][1] == "Ohm"
    assert "dmm/continuity" not in spy.fields


def test_reading_for_an_unknown_function_is_dropped_and_logged_once(
    trace_file: Recording, spy: SpySource, caplog: pytest.LogCaptureFixture
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "dmm", dmm_profile(), 1)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        emitter.reading("dmm", "temperature", 21.0, SECOND)
        emitter.reading("dmm", "temperature", 22.0, 2 * SECOND)
        emitter.reading("dmm", "capacitance", 1e-6, 3 * SECOND)
    warnings = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert warnings == [
        "dmm: no event for function temperature; its readings are dropped",
        "dmm: no event for function capacitance; its readings are dropped",
    ]
    events = trace_file.events()
    assert "dmm/temperature" not in events
    assert "dmm/capacitance" not in events


def test_the_dmm_channel_event_carries_value_and_function_labels(
    trace_file: Recording, spy: SpySource
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "dmm", dmm_profile(), 1)
    emitter.row("dmm", 1, {"function": "dc_voltage"}, SECOND)
    emitter.row("dmm", 1, {"function": "mystery"}, 2 * SECOND)
    rows = trace_file.events()["dmm/ch1"]
    assert [r["function"] for r in rows] == [0, 12]
    assert emitter.fields("dmm") == {"function": None}


def test_poll_value_writes_the_rows_field(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    poll = (PollRow("Temp", "TEMP?", "C", 1.0), PollRow("Fan", "FAN?", None, 1.0))
    register(emitter, "psu", dp800, 1, poll)
    emitter.poll_value("psu", "Temp", 41.5, SECOND)
    emitter.poll_value("psu", "Fan", 1200.0, 2 * SECOND)
    events = trace_file.events()
    assert [(r[TIME], r["Temp"], r["Fan"]) for r in events["psu/poll"]] == [
        (1.0, 41.5, None),
        (2.0, None, 1200.0),
    ]
    assert spy.fields["psu/poll"]["Temp"][1] == "C"
    assert spy.fields["psu/poll"]["Fan"][1] is None


def test_poll_value_names_never_collide(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    poll = (
        PollRow("time_ns", "A?", None, 1.0),
        PollRow("a/b", "B?", None, 1.0),
        PollRow("A_B", "C?", None, 1.0),
        PollRow("a_b", "D?", None, 1.0),
    )
    register(emitter, "psu", dp800, 1, poll)
    for i, row in enumerate(poll):
        emitter.poll_value("psu", row.name, float(i), (i + 1) * SECOND)
    assert list(spy.fields["psu/poll"]) == ["time_ns_1", "a_b", "A_B_1", "a_b_2"]
    rows = trace_file.events()["psu/poll"]
    assert [next(v for k, v in r.items() if k != TIME and v is not None) for r in rows] == [
        0.0,
        1.0,
        2.0,
        3.0,
    ]


def test_poll_value_without_a_poll_table_is_ignored(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.poll_value("psu", "Temp", 1.0, SECOND)
    assert "psu/poll" not in trace_file.events()


def test_status_writes_the_four_fields(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.status("psu", 12.5, 9.8, 3, True, SECOND)
    emitter.status("psu", 0.0, 0.0, 4, False, 2 * SECOND)
    assert trace_file.events()["psu/status"] == [
        {TIME: 1.0, "rtt_ms": 12.5, "rate_hz": 9.8, "errors": 3, "connected": True},
        {TIME: 2.0, "rtt_ms": 0.0, "rate_hz": 0.0, "errors": 4, "connected": False},
    ]
    units = {name: unit for name, (_, unit) in spy.fields["psu/status"].items()}
    assert units == {"rtt_ms": "ms", "rate_hz": "Hz", "errors": None, "connected": None}


def test_note_writes_a_log_line(trace_file: Recording, spy: SpySource, dp800: Profile) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    emitter.note("psu", "INFO", "connected", SECOND)
    emitter.note("psu", "ERROR", "-113 Undefined header", 2 * SECOND, source="probe")
    assert trace_file.events()["psu/log"] == [
        {
            TIME: 1.0,
            "level": "INFO",
            "message": "connected",
            "name": "instruments",
            "file": "",
            "line": 0,
        },
        {
            TIME: 2.0,
            "level": "ERROR",
            "message": "-113 Undefined header",
            "name": "probe",
            "file": "",
            "line": 0,
        },
    ]


def test_instruments_write_to_their_own_events(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "a", dp800, 1)
    register(emitter, "b", dp800, 1)
    emitter.row("a", 1, {"voltage": 1.0}, SECOND)
    emitter.row("b", 1, {"voltage": 2.0}, SECOND)
    events = trace_file.events()
    assert [r["voltage"] for r in events["a/ch1"]] == [1.0]
    assert [r["voltage"] for r in events["b/ch1"]] == [2.0]


def test_the_trace_lists_every_event_of_an_instrument(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 2, (PollRow("Temp", "T?", None, 1.0),))
    events = trace_file.events()
    assert sorted(events) == [
        "psu/ch1",
        "psu/ch2",
        "psu/identity",
        "psu/log",
        "psu/poll",
        "psu/status",
    ]


def test_fields_returns_the_profiles_fields_with_units(spy: SpySource, dp800: Profile) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    fields = emitter.fields("psu")
    assert fields == dp800.fields
    fields.clear()
    assert emitter.fields("psu") == dp800.fields


def test_channel_fields_carry_their_units_and_types(spy: SpySource, dp800: Profile) -> None:
    """The reader API lists no units, so the registered metadata is checked at the source."""
    register(emitter_for(spy), "psu", dp800, 2)
    for ch in ("psu/ch1", "psu/ch2"):
        fields = spy.fields[ch]
        assert fields["voltage"] == (zelos_sdk.DataType.Float64, "V")
        assert fields["current"] == (zelos_sdk.DataType.Float64, "A")
        assert fields["power"] == (zelos_sdk.DataType.Float64, "W")
        assert fields["voltage_set"] == (zelos_sdk.DataType.Float64, "V")
        assert fields["current_set"] == (zelos_sdk.DataType.Float64, "A")
        assert fields["output"][0] == zelos_sdk.DataType.Boolean
        assert fields["mode"][0] == zelos_sdk.DataType.Int8
        assert fields["protection"][0] == zelos_sdk.DataType.Int8
        assert list(fields) == list(dp800.fields)


def test_enum_fields_register_value_tables_on_every_channel(spy: SpySource, dp800: Profile) -> None:
    register(emitter_for(spy), "psu", dp800, 2)
    for ch in ("psu/ch1", "psu/ch2"):
        assert spy.tables[(ch, "mode")] == dict(enumerate(ENUM_FIELDS["mode"]))
        assert spy.tables[(ch, "protection")] == dict(enumerate(ENUM_FIELDS["protection"]))
    assert {field for _, field in spy.tables} == {"mode", "protection"}
    assert spy.tables[("psu/ch1", "mode")][2] == "cc"


def test_the_dmm_function_enum_has_a_value_table(spy: SpySource) -> None:
    register(emitter_for(spy), "dmm", dmm_profile(), 1)
    table = spy.tables[("dmm/ch1", "function")]
    assert table[0] == "dc_voltage"
    assert table[12] == "other"


@pytest.mark.xfail(
    strict=True,
    raises=RuntimeError,
    reason="the SDK reader's field-path grammar is `source/event.field`, so an event name "
    "containing `/` such as `psu/ch1` cannot be addressed",
)
def test_value_tables_read_back_from_the_trace(trace_file: Recording, dp800: Profile) -> None:
    emitter = Emitter(trace_file.source)
    register(emitter, "psu", dp800, 1)
    emitter.row("psu", 1, {"mode": "cc"}, SECOND)
    trace_file.source.flush()
    trace_file.writer.close()
    with zelos_sdk.TraceReader(str(trace_file.path)) as reader:
        segment = reader.list_data_segments()[0].id
        table = reader.get_value_table(segment, "Instruments/psu/ch1.mode")
    assert table == dict(enumerate(ENUM_FIELDS["mode"]))


def test_the_status_event_has_no_value_tables(spy: SpySource, dp800: Profile) -> None:
    emitter_for(spy).add_instrument("psu", dp800, 1)
    assert [key for key in spy.tables if key[0] == "psu/status"] == []


def test_a_waveform_is_one_batch_at_exact_nanoseconds(
    trace_file: Recording, spy: SpySource, profiles: list[Profile]
) -> None:
    """Samples 2 ns apart near today's epoch: float seconds would round them 256 ns apart."""
    scope = next(p for p in profiles if p.id == "siglent-sds")
    emitter = emitter_for(spy)
    register(emitter, "scope", scope, 2)
    times = 1_790_000_000_123_456_789 + np.arange(5, dtype=np.int64) * 2
    volts = np.array([0.1, 0.2, -0.3, 0.4, 0.5])
    emitter.waveform("scope", 2, times, volts)
    [(name, batch)] = spy.batches
    assert name == "scope/ch2/waveform"
    assert batch.column("time_ns").cast(pa.int64()).to_pylist() == times.tolist()
    assert spy.fields[name] == {"voltage": (zelos_sdk.DataType.Float64, "V")}
    rows = trace_file.events()[name]
    # The reader orders rows by float seconds, which cannot tell these samples apart.
    assert sorted(r["voltage"] for r in rows) == sorted(volts.tolist())
    assert [r[TIME] for r in rows] == pytest.approx(times / 1e9, abs=1e-6)


def test_a_waveform_for_a_channel_without_one_is_dropped(
    spy: SpySource, profiles: list[Profile]
) -> None:
    scope = next(p for p in profiles if p.id == "siglent-sds")
    emitter = emitter_for(spy)
    register(emitter, "scope", scope, 1)
    emitter.waveform("scope", 3, np.zeros(2, dtype=np.int64), np.zeros(2))
    assert spy.batches == []


def test_identity_is_written_as_its_own_event(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    emitter = emitter_for(spy)
    register(emitter, "psu", dp800, 1)
    identity = {
        "manufacturer": "RIGOL TECHNOLOGIES",
        "model": "DP832A",
        "serial": "DP8C000001",
        "firmware": "00.01.16",
        "profile": "rigol-dp800",
        "address": "192.168.1.40:5555",
    }
    emitter.identity("psu", identity, SECOND)
    assert trace_file.events()["psu/identity"] == [{TIME: 1.0, **identity}]


def test_a_channel_registers_only_the_fields_its_readers_produce(
    trace_file: Recording, spy: SpySource, dp800: Profile
) -> None:
    """A field no reader produces was registered anyway and showed as an empty signal."""
    emitter = emitter_for(spy)
    emitter.add_instrument("psu", dp800, 2)
    emitter.add_channels("psu", {1: {"voltage", "current"}, 2: set()})
    assert spy.fields["psu/ch1"] == {
        "current": (zelos_sdk.DataType.Float64, "A"),
        "voltage": (zelos_sdk.DataType.Float64, "V"),
    }
    assert "psu/ch2" not in spy.fields


def test_a_waveform_event_exists_only_once_a_channel_is_captured(
    trace_file: Recording, spy: SpySource, profiles: list[Profile]
) -> None:
    scope = next(p for p in profiles if p.id == "siglent-sds")
    emitter = emitter_for(spy)
    register(emitter, "scope", scope, 4)
    assert not any(name.endswith("/waveform") for name in spy.fields)
    emitter.waveform("scope", 2, np.array([SECOND], dtype=np.int64), np.array([0.5]))
    assert [name for name in spy.fields if name.endswith("/waveform")] == ["scope/ch2/waveform"]
