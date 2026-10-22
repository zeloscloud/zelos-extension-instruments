"""What a worker does with a data-acquisition unit: check the configured addresses, set the scan
up at connect, and read one scan each fast pass.

The scan channels and relays come from the configuration; the profile says how the unit is told.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import engine
from .engine import ParseError, RangeError

if TYPE_CHECKING:
    from .config import InstrumentConfig, ScanChannel
    from .profile_daq import Daq
    from .worker import InstrumentWorker


def channels(cfg: InstrumentConfig) -> list[int]:
    """Every address the instrument has: its scan channels and its relays, ascending."""
    return sorted({row.channel for row in cfg.scan} | set(cfg.relays))


def check(worker: InstrumentWorker) -> None:
    """Refuse a configured channel the profile cannot address or read as configured."""
    daq = _daq(worker)
    for ch in channels(worker.cfg):
        if not daq.channel_pattern.fullmatch(str(ch)):
            raise RangeError(f"channel {ch} is not an address this instrument has")
    for row in worker.cfg.scan:
        if row.function not in daq.configure:
            raise RangeError(f"channel {row.channel}: this profile cannot read {row.function}")
        if row.nplc is not None and (daq.nplc is None or row.function not in daq.nplc.map):
            raise RangeError(
                f"channel {row.channel}: this profile cannot set the integration time of "
                f"{row.function}"
            )


def setup(worker: InstrumentWorker) -> None:
    """Configure each function group, then each integration time, then the scan list."""
    daq = _daq(worker)
    rows = _scan(worker)
    if not rows:
        return
    by_function: dict[tuple[str, str | None], list[int]] = {}
    for row in rows:
        by_function.setdefault((row.function, row.thermocouple), []).append(row.channel)
    for (function, tc), group in by_function.items():
        for command in daq.configure[function]:
            worker._send(engine.render(command, channels=_list(group), tc=tc))
    if daq.nplc is not None:
        by_value: dict[tuple[str, float], list[int]] = {}
        for row in rows:
            if row.nplc is not None:
                by_value.setdefault((row.function, row.nplc), []).append(row.channel)
        for (function, nplc), group in by_value.items():
            worker._send(
                engine.render(
                    daq.nplc.command,
                    channels=_list(group),
                    value=engine.format_value(nplc, None),
                    function=daq.nplc.map[function],
                )
            )
    # Some units redefine the scan list on every configure, so the scan list goes last.
    everything = [row.channel for row in rows]
    for command in daq.scan:
        worker._send(engine.render(command, channels=_list(everything), count=len(everything)))


def read(worker: InstrumentWorker, rows: dict[int | None, dict[str, Any]]) -> None:
    """One scan: each scan channel's reading lands in its row under its function's name."""
    daq = _daq(worker)
    scan = _scan(worker)
    if not scan:
        return
    commands = [engine.render(command, count=len(scan)) for command in daq.read]
    for command in commands[:-1]:
        worker._send(command)
    reply = worker._query(commands[-1]).strip()
    parts = reply.split(",") if reply else []
    if len(parts) != len(scan):
        raise ParseError(f"expected {len(scan)} readings, got {len(parts)}: {reply!r}")
    values = [engine.parse_float(part) for part in parts]
    for row, value in zip(scan, values, strict=True):
        rows.setdefault(row.channel, {})[row.function] = value


def _daq(worker: InstrumentWorker) -> Daq:
    profile = worker.profile
    assert profile is not None and profile.daq is not None
    return profile.daq


def _scan(worker: InstrumentWorker) -> list[ScanChannel]:
    return sorted(worker.cfg.scan, key=lambda row: row.channel)


def _list(group: list[int]) -> str:
    """A scan list's addresses as the instrument takes them: 101,102,105."""
    return ",".join(str(ch) for ch in group)
