"""Identities and replies real units sent, collected from published logs, driver tests and posts.

Each entry names where it was seen. A profile change that stops matching a real unit, or stops
parsing what one sends, fails here.
"""

import pytest

from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import Profile, find, parse_idn

IDENTITIES = [
    # Keithley
    (
        "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1163289,C30   Mar 17 2006 09:29:29/A02  /K/J",
        "keithley-2400",
        "https://github.com/epics-modules/asyn/blob/HEAD/docs/source/asynRecordControl.rst",
    ),
    (
        "KEITHLEY INSTRUMENTS INC.,MODEL 2400,4502039,D02 Jan 20 2021 10:18:49/B01  /W/N",
        "keithley-2400",
        "https://github.com/ylylyl98/SpectralSweep-pyside6",
    ),
    (
        "KEITHLEY INSTRUMENTS INC.,MODEL 2000,1103202,A19  /A02",
        "keithley-2000",
        "https://github.com/AndreaGlatthor/Keithley-2000-scan/blob/main/README.md",
    ),
    (
        "KEITHLEY INSTRUMENTS INC.,MODEL 2110,1408023,01.01-00-00",
        "keithley-2110",
        "https://github.com/Hideto-65/ax2/blob/HEAD/I-V_251201.ipynb",
    ),
    (
        "KEITHLEY INSTRUMENTS,MODEL DMM6500,04645469,1.7.14j",
        "keithley-dmm6500",
        "https://github.com/Shy1011/instrument_driver",
    ),
    (
        "Keithley instruments, 2230G-30-1, 9100433, 1.16-1.04",
        "keithley-2220-2230",
        "https://github.com/ngalitzki/ArnoldLab_DRData/blob/HEAD/4K_only/prologix/load_curve2.ipynb",
    ),
    (
        "Keithley Instruments Inc.,Model 2260B-80-27,1403238,01.72.20150702",
        "keithley-2260b",
        "https://github.com/sandialabs/pyscan/blob/main/drivers_test_notebooks/keithley2260b_test_notebook.ipynb",
    ),
    # HP / Agilent / Keysight
    ("HEWLETT-PACKARD,34401A,0,10-5-2", "agilent-34401a", "EEVblog forum, a 34401A over GPIB"),
    ("HEWLETT-PACKARD,34401A,0,7-5-2", "agilent-34401a", "EEVblog forum, RS-232 terminal"),
    # Rigol
    (
        "RIGOL TECHNOLOGIES,DP832,DP8G223300053,00.01.16",
        "rigol-dp800",
        "https://fixturfab.com/resources/how-to-test/rigol-pyvisa-python",
    ),
    (
        "RIGOL TECHNOLOGIES,DL3021A,DL3A193000578,00.01.04.00.05",
        "rigol-dl3000",
        "https://mekatronik.org/forum/threads/rigol-elektronik-yuk-dl3021-den-dl3031-e-donusum.3134/",
    ),
    # Siglent
    (
        "Siglent Technologies,SPD1305X,XXXXXXXXXX,2.01.01.07R1,V1.0",
        "siglent-spd1000x",
        "EEVblog forum, an SPD1305X owner",
    ),
    (
        "Siglent Technologies,SPD3303X-E,SPD3XHBC3R0662,1.01.01.02.05,V3.0",
        "siglent-spd3303x",
        "EEVblog forum, an SPD3303X-E owner",
    ),
    (
        "Siglent Technologies,SDM3065X,<serial>,3.01.01.10",
        "siglent-sdm3000",
        "https://www.iu3jsx.it/wp/2023/01/29/bench-multimeter-remote-programming-using-python/",
    ),
    # Others
    (
        "Chroma ATE,61604,001060,1.25,1.34,1.20",
        "chroma-61600",
        "https://github.com/sigrokproject/libsigrok/commit/0c08023f506c164e6d7bdf46cd82ef437a468997",
    ),
    (
        "EA Elektro-Automatik GmbH & Co. KG, PS 9040-20 T",
        None,
        "https://github.com/tektronix/keithley/blob/HEAD/Instrument_Examples/ElektroAutomatik/PowerSupply/simple_voltage_source.py",
    ),
]


@pytest.mark.parametrize(
    ("idn", "expected", "source"), IDENTITIES, ids=[i[0][:40] for i in IDENTITIES]
)
def test_a_real_identity_picks_its_profile(
    idn: str, expected: str | None, source: str, profiles: list[Profile]
) -> None:
    found = find(profiles, parse_idn(idn))
    assert (found.id if found else None) == expected, source


def _reader(profiles: list[Profile], profile_id: str, name: str) -> object:
    profile = next(p for p in profiles if p.id == profile_id)
    return profile.reader(name)


def test_a_real_2280s_reply_reads_current_and_voltage(profiles: list[Profile]) -> None:
    """Real units answer :MEAS:VOLT? with current, voltage and time, each with its unit letter."""
    profile = next(p for p in profiles if p.id == "keithley-2280s")
    reader = next(r for r in profile.signals if r.name == "measure")
    reply = "-1.234567E-05A,+5.000123E+00V,+1.234000E+02s"
    assert engine.parse_reply(reader, reply, 1) == {"current": -1.234567e-05, "voltage": 5.000123}


@pytest.mark.parametrize(
    ("reply", "mode", "protection"),
    [
        # CANopen-Bench capture from a Kiprim DC605S, an OWON SPE re-badge.
        ("57.430,0.059,3.390,OFF,OFF,OFF,1", "cv", "none"),
        # sjoerdsimons/owon-spe, a test row from a real SPE driver.
        ("4.987,0.514,2.560,OFF,OFF,OFF,1", "cv", "none"),
    ],
)
def test_real_owon_status_replies_read_mode_and_protection(
    reply: str, mode: str, protection: str, profiles: list[Profile]
) -> None:
    profile = next(p for p in profiles if p.id == "owon-spe-p4000")
    mode_reader = next(r for r in profile.state if r.name == "mode")
    assert engine.parse_reply(mode_reader, reply, 1) == {"mode": mode}
    protection_reader = next(r for r in profile.state if r.name == "protection")
    flags = [flag.value for flag in protection_reader.flags if engine.flag_is_set(flag, reply, 1)]
    assert (flags[0] if flags else "none") == protection
