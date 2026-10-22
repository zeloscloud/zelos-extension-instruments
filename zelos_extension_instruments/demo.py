"""The demo bench: fake instruments in this process, running a battery test scenario.

A supply charges a battery, a load discharges it, a multimeter reads the shunt, and an AC source
feeds the charger. The scenario moves on its own so the plots show something from the start.
"""

import math
import threading
import time

from .config import InstrumentConfig
from .link import Address, Link, LinkSettings, LinkTimeout
from .profile import Profile, by_id
from .profile_load import shipped
from .sim.fake import FakeInstrument
from .waveform import block_payload
from .worker import Opener

#: Channels per demo instrument; the supply has two so the bench shows a second rail.
CHANNELS = {"psu": 2, "load": 1, "dmm": 1, "acsource": 1}

_IDN = {
    "psu": "Zelos,Demo PSU 3030,DEMO0001,1.0",
    "load": "Zelos,Demo Load 300,DEMO0002,1.0",
    "dmm": "Zelos,Demo DMM 6500,DEMO0003,1.0",
    "acsource": "Zelos,Demo AC 1000,DEMO0004,1.0",
}


class InProcessLink:
    """A Link that talks to a FakeInstrument without a socket."""

    def __init__(self, fake: FakeInstrument, where: str) -> None:
        self._fake = fake
        self.where = where
        self._pending: list[str | bytes] = []
        self._closed = False

    def write(self, command: str) -> None:
        reply = self._fake.handle(command)
        if reply is not None:
            self._pending.append(reply)

    def read(self, timeout_s: float | None = None) -> str:
        if not self._pending:
            raise LinkTimeout(f"no reply from {self.where}")
        reply = self._pending.pop(0)
        return reply.decode("utf-8", errors="replace") if isinstance(reply, bytes) else reply

    def read_block(self, timeout_s: float | None = None) -> bytes:
        if not self._pending:
            raise LinkTimeout(f"no reply from {self.where}")
        reply = self._pending.pop(0)
        return block_payload(reply if isinstance(reply, bytes) else reply.encode())

    def read_any(self, timeout_s: float | None = None) -> str | bytes:
        if self._pending and isinstance(self._pending[0], bytes):
            return self.read_block(timeout_s)
        return self.read(timeout_s)

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def clear(self) -> None:
        self._pending.clear()

    def close(self) -> None:
        self._closed = True


class Bench:
    """The shared scenario every demo instrument plays a part in."""

    _instance: "Bench | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "Bench":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._start = time.monotonic()
        self.fakes: dict[str, FakeInstrument] = {}
        self._thread: threading.Thread | None = None

    def fake(self, category: str) -> FakeInstrument:
        if category not in self.fakes:
            profile = by_id(shipped(), f"generic-{category}")
            assert profile is not None
            self.fakes[category] = _make(profile, category)
            self._ensure_running()
        return self.fakes[category]

    def _ensure_running(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="demo bench", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            t = time.monotonic() - self._start
            self._step(t)
            time.sleep(0.05)

    def _step(self, t: float) -> None:
        # A 90 s cycle: 60 s of charging into a rising battery voltage, 30 s of discharge.
        phase = t % 90.0
        battery = (
            11.0 + 1.6 * min(1.0, phase / 60.0) if phase < 60 else 12.6 - 0.8 * (phase - 60) / 30
        )
        if (psu := self.fakes.get("psu")) is not None:
            # A charger sees the battery as a load whose resistance rises as it fills.
            psu.set_load_ohms(1, max(0.5, battery / max(0.1, 14.4 - battery) * 0.4))
            psu.set_load_ohms(2, 24.0 + 6.0 * math.sin(t / 7))
        if (load := self.fakes.get("load")) is not None:
            load.set_source_volts(1, battery)
        if (dmm := self.fakes.get("dmm")) is not None:
            shunt_mv = (2.0 + 0.5 * math.sin(t / 3)) * (1.0 if phase < 60 else -1.4)
            dmm.set_reading("dc_voltage", battery + 0.02 * math.sin(t * 2))
            dmm.set_reading("dc_current", shunt_mv)
            dmm.set_reading("resistance", 100.0 + 0.3 * math.sin(t / 5))
        if (ac := self.fakes.get("acsource")) is not None:
            ac.set_load_ohms(1, 52.0 if phase < 60 else 400.0)


def _make(profile: Profile, category: str) -> FakeInstrument:
    fake = FakeInstrument(
        profile, model=f"Demo {category}", channels=CHANNELS[category], idn=_IDN[category]
    )
    if category == "psu":
        fake.handle("INST:NSEL 1")
        fake.handle("VOLT 14.400")
        fake.handle("CURR 2.000")
        fake.handle("OUTP ON")
        fake.handle("INST:NSEL 2")
        fake.handle("VOLT 5.000")
        fake.handle("CURR 1.000")
        fake.handle("OUTP ON")
    elif category == "load":
        fake.handle("FUNC CURR")
        fake.handle("CURR 1.500")
        fake.handle("INP ON")
    elif category == "acsource":
        fake.handle("VOLT 230.000")
        fake.handle("FREQ 50.000")
        fake.handle("OUTP ON")
    return fake


def demo_opener(cfg: InstrumentConfig) -> Opener:
    """An opener that connects a demo instrument to the shared bench."""

    def open(address: Address, settings: LinkSettings) -> Link:
        return InProcessLink(Bench.get().fake(cfg.category), cfg.where)

    return open
