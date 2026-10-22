"""The fake data-acquisition unit a daq profile builds: configuration, scans, relays, errors."""

from typing import Any

import pytest

from tests.conftest import minimal, profile_from
from tests.test_daq_ops import BUFFER_STYLE, KEYSIGHT_STYLE
from zelos_extension_instruments.profile_load import from_dict
from zelos_extension_instruments.sim.fake_daq import FakeDaq


def fake(raw: dict[str, Any] = KEYSIGHT_STYLE) -> FakeDaq:
    return FakeDaq(from_dict(raw, f"{raw['id']}.toml"))


def test_it_answers_its_identity_and_an_empty_error_queue() -> None:
    unit = fake()
    assert unit.handle("*IDN?") == "acme,D1,SIM00001,1.0"
    assert unit.handle("SYST:ERR?") == '0,"No error"'


def test_an_unknown_command_queues_an_undefined_header_error() -> None:
    unit = fake()
    assert unit.handle("BOGUS:CMD 1") is None
    assert unit.handle("SYST:ERR?") == '-113,"Undefined header"'
    assert unit.handle("SYST:ERR?") == '0,"No error"'


def test_a_configure_sets_each_channel_of_its_list_and_ranges_spell_out() -> None:
    unit = fake()
    unit.handle("CONF:VOLT:DC AUTO,DEF,(@101:103,105)")
    unit.handle("CONF:TEMP TC,K,(@104)")
    assert unit.functions == {
        101: "dc_voltage",
        102: "dc_voltage",
        103: "dc_voltage",
        105: "dc_voltage",
        104: "temperature",
    }
    assert unit.thermocouples == {104: "K"}


def test_a_scan_reads_each_channel_in_ascending_order_with_its_functions_value() -> None:
    unit = fake()
    unit.handle("CONF:RES AUTO,DEF,(@102)")
    unit.handle("CONF:VOLT:DC AUTO,DEF,(@101)")
    unit.handle("ROUT:SCAN (@102,101)")
    unit.set_reading(101, 0.004271)
    assert unit.handle("READ?") == "+4.27100000E-03,+1.00000000E+03"


def test_a_scan_of_an_unconfigured_channel_is_a_settings_conflict() -> None:
    unit = fake()
    unit.handle("ROUT:SCAN (@101)")
    assert unit.scan_list == ()
    assert unit.handle("SYST:ERR?") == '-221,"Settings conflict"'


def test_a_read_without_a_scan_list_answers_nothing() -> None:
    unit = fake()
    assert unit.handle("READ?") is None
    assert unit.handle("SYST:ERR?") == '-221,"Settings conflict"'


def test_an_integration_time_for_another_function_is_a_settings_conflict() -> None:
    unit = fake()
    unit.handle("CONF:VOLT:DC AUTO,DEF,(@101)")
    unit.handle("SENS:RES:NPLC 10,(@101)")
    assert unit.nplc == {}
    assert unit.handle("SYST:ERR?") == '-221,"Settings conflict"'
    unit.handle("SENS:VOLT:DC:NPLC 10,(@101)")
    assert unit.nplc == {101: 10.0}


def test_a_buffered_read_returns_the_count_asked_for() -> None:
    unit = fake(BUFFER_STYLE)
    unit.handle('SENS:FUNC "VOLT:DC",(@101,102)')
    unit.handle("ROUT:SCAN:CRE (@101,102)")
    unit.handle("ROUT:SCAN:COUN:SCAN 1")
    assert unit.handle("INIT") is None
    assert unit.handle("*WAI") is None
    assert unit.handle('TRAC:DATA? 1,1,"defbuffer1",READ') == "+5.00000000E+00"
    assert unit.handle('TRAC:DATA? 1,2,"defbuffer1",READ') == "+5.00000000E+00,+5.00000000E+00"
    assert unit.handle("SYST:ERR?") == '0,"No error"'


def test_relays_close_open_and_read_back() -> None:
    unit = fake()
    unit.handle("ROUT:CLOSE (@105)")
    assert unit.closed == {105}
    assert unit.handle("ROUT:CLOSE? (@105)") == "1"
    unit.handle("ROUT:OPEN (@105)")
    assert unit.handle("ROUT:CLOSE? (@105)") == "0"


def test_a_reset_forgets_the_configuration_and_opens_every_relay() -> None:
    unit = fake()
    unit.handle("CONF:VOLT:DC AUTO,DEF,(@101)")
    unit.handle("ROUT:SCAN (@101)")
    unit.handle("ROUT:CLOSE (@110)")
    unit.handle("*RST")
    assert (unit.functions, unit.scan_list, unit.closed) == ({}, (), set())


def test_only_a_daq_profile_makes_a_fake_unit() -> None:
    with pytest.raises(ValueError, match="is not a daq profile"):
        FakeDaq(profile_from(minimal()))
