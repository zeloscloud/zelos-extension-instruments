"""One thread per instrument: opens the link, identifies the unit, polls, serves requests.

Everything the thread sends comes from the profile; everything it records goes through the
emitter. Requests from actions arrive on a queue and are served between poll steps.
"""

import concurrent.futures
import dataclasses
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Generator
from contextlib import suppress
from typing import Any, Literal

from . import connect, daq_ops, engine, opening, scope_ops, serve, system_ops
from .clock import NS_PER_S, HostClock
from .config import InstrumentConfig, poll_key
from .emitter import Emitter
from .engine import ParseError, RangeError, Selector, Step
from .link import (
    Address,
    Link,
    LinkError,
    LinkSettings,
    LinkTimeout,
    open_link,
)
from .profile import (
    ENUM_FIELDS,
    INSTRUMENT_FIELDS,
    SIGNAL_FIELDS,
    Idn,
    Profile,
    Range,
    parse_idn,
)

logger = logging.getLogger(__name__)

_MS = 1_000_000
_SLOW_NS = 1 * NS_PER_S
_STATUS_NS = 1 * NS_PER_S
_RATE_WINDOW_NS = 10 * NS_PER_S
_BACKOFF_MIN_S = 0.5
_BACKOFF_MAX_S = 10.0
# Consecutive timeouts in the poll loop before the link is reopened.
_TIMEOUTS_BEFORE_RECONNECT = 3
# Consecutive scans that do not read as set up before the link is reopened, which sets it up again.
_BAD_SCANS_BEFORE_RECONNECT = 3
# A setpoint that changes within this long after our own set is ours, not an outside change.
_OWN_CHANGE_NS = 2 * NS_PER_S
_LOG_EVERY_NS = 60 * NS_PER_S
# Readings kept for Get State.
_KEPT = 50
# Replies read while getting back in step after one went astray.
_RESYNC_READS = 8

RequestKind = Literal[
    "set",
    "read",
    "state",
    "scpi",
    "clear",
    "local",
    "release",
    "acquire",
    "measure",
    "waveform",
    "command",
    "system",
]
_State = Literal["connecting", "open", "released", "failed", "stopped"]
_Future = concurrent.futures.Future[dict[str, Any]]
_WRITE_KINDS = frozenset({"set", "local", "measure", "command", "scpi", "waveform", "system"})

Opener = Callable[[Address, LinkSettings], Link]


@dataclasses.dataclass
class _Request:
    kind: RequestKind
    args: dict[str, Any]
    future: _Future


class _Throttle:
    """The first event is logged in full, then one a minute with a count of the rest."""

    def __init__(self) -> None:
        self._logged_ns: int | None = None
        self._held = 0

    def tally(self, now: int) -> int | None:
        if self._logged_ns is None:
            self._logged_ns = now
            return 0
        self._held += 1
        if now - self._logged_ns < _LOG_EVERY_NS:
            return None
        held, self._logged_ns, self._held = self._held, now, 0
        return held


@dataclasses.dataclass
class _Channel:
    """What the worker last saw on one channel."""

    values: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Field to the reading clock time of our own last set of it.
    own_set_ns: dict[str, int] = dataclasses.field(default_factory=dict)
    ranges: dict[str, Range] = dataclasses.field(default_factory=dict)


class InstrumentWorker(threading.Thread):
    """Owns one instrument; actions reach it through request()."""

    def __init__(
        self,
        cfg: InstrumentConfig,
        profiles: list[Profile],
        emitter: Emitter,
        *,
        open: Opener = open_link,
    ) -> None:
        super().__init__(name=f"instrument {cfg.name}", daemon=True)
        self.cfg = cfg
        self._profiles = profiles
        self._emitter = emitter
        self._open = open
        self._now = time.perf_counter_ns
        self._clock = HostClock(self._now)
        self._stopping = threading.Event()
        self.where = cfg.where
        self._wake = threading.Event()
        self._requests: deque[_Request] = deque()
        self._lock = threading.Lock()
        self._link: Link | None = None
        self.state: _State = "connecting"
        self.health = f"Connecting to {self.where}."
        self.profile: Profile | None = None
        self.idn: Idn | None = None
        self.channels = 0
        self._channel: dict[int, _Channel] = {}
        self._plan: engine.Plan | None = None
        self._dropped: set[str] = set()
        self._selector = Selector(None)
        self._registered = False
        self._function = "other"
        self._fast_times: deque[int] = deque()
        self._rtt_ms = math.nan
        self._errors = 0
        self._last_error: str | None = None
        self._timeouts = 0
        self._timeouts_total = 0
        self._bad_scans = 0
        self._queries = 0
        self._parse_throttle: dict[str, _Throttle] = {}
        self._backoff_s = _BACKOFF_MIN_S
        self._recent: deque[tuple[int, str, str]] = deque(maxlen=_KEPT)
        self._next_slow_ns = 0
        # The slow round in progress: its steps run between fast passes.
        self._slow: Generator[None, None, None] | None = None
        self._next_status_ns = 0
        self._next_fast_ns = 0
        self._next_poll_ns: dict[str, int] = {}
        self._poll_values: dict[str, float] = {}
        # A scope's live measurement slots, and the instrument-wide values last read.
        self._slots: dict[int, tuple[int, str]] = {}
        # Slots that are on but show something the profile does not record; never reused.
        self._busy_slots: set[int] = set()
        # Whole scans an action ran; a slow round's scan that overlapped one is stale.
        self._slot_scans = 0
        self._acquisition: dict[str, Any] = {}
        self._cycles_since_connect = 0
        self._link_timeout_s = 2.0
        self._fast_at: dict[int | None, int] = {}
        self._interval_ns = int(NS_PER_S / cfg.rate_hz)
        # While the front panel is locked on purpose, the worker never hands it back.
        self._panel_locked = False
        # No reconnect before this monotonic time, as while the instrument reboots.
        self._reconnect_at = 0.0

    def request(self, kind: RequestKind, **args: Any) -> _Future:
        """Queue a request; the worker answers through the future."""
        future: _Future = concurrent.futures.Future()
        with self._lock:
            self._requests.append(_Request(kind, args, future))
        self._wake.set()
        return future

    def request_stop(self) -> None:
        """Ask the worker to end without waiting, so several can wind down together."""
        self._stopping.set()
        self._wake.set()

    def stop(self) -> None:
        """Ask the worker to end; closes a link blocked in a read."""
        self.request_stop()
        # The worker hands the panel back itself; closing the link wakes one still blocked.
        self.join(self._link_timeout_s + 0.5)
        link = self._link
        if link is not None and self.is_alive():
            link.close()

    def run(self) -> None:
        try:
            while not self._stopping.is_set():
                if self.state == "released":
                    self._serve_requests()
                    self._wake.wait(0.5)
                    self._wake.clear()
                    continue
                try:
                    if self._link is None and (away_s := self._reconnect_at - time.monotonic()) > 0:
                        self._wait(away_s)
                        continue
                    if self._link is None and not self._connect():
                        self._serve_requests()
                        self._wait(self._backoff_s)
                        continue
                    self._cycle()
                    if self._cycles_since_connect == 0:
                        # The link held for a whole cycle: the next loss starts the backoff over.
                        self._backoff_s = _BACKOFF_MIN_S
                    self._cycles_since_connect += 1
                except LinkError as e:
                    self._lost(str(e))
                    self._wait(self._backoff_s)
                except Exception:
                    logger.exception("%s: unexpected error; reconnecting", self.cfg.name)
                    self._lost("an unexpected error")
                    self._wait(self._backoff_s)
        finally:
            self._hand_back()
            self._close()
            self.state = "stopped"
            self.health = "Stopped."
            self._fail_pending("the extension is stopping")

    def _wait(self, seconds: float) -> None:
        """Wait out the schedule, serving requests as they arrive: returning early for one would
        run an extra pass and push the schedule, or retry a connect inside the backoff."""
        deadline = time.monotonic() + seconds
        link = self._link
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(min(remaining, 0.5)):
                self._wake.clear()
                self._serve_requests()
                if self._link is not link or self.state == "released":
                    return

    def _connect(self) -> bool:
        """Open the link and identify the instrument; False with the health set when it fails."""
        return opening.open_instrument(self)

    def _install(self, profile: Profile, idn: Idn | None, channels: list[int]) -> None:
        self.channels = len(channels)
        self._channel = {ch: _Channel() for ch in channels}
        model = idn.model if idn else ""
        for ch, state in self._channel.items():
            for key in profile.ranges:
                r = profile.range_for(key, model, ch)
                if r is not None:
                    state.ranges[key] = r
            for override in profile.range_overrides:
                if override.model.search(model) and override.channel in (None, ch):
                    for key, r in override.ranges.items():
                        state.ranges.setdefault(key, r)
        self._dropped = set()
        self._plan = opening.plan(self, profile, set())
        if not self._registered:
            self._emitter.add_instrument(self.cfg.name, profile, self.channels, self.cfg.poll)
            self._registered = True
        self._next_slow_ns = 0
        self._slow = None
        self._next_status_ns = 0
        self._next_fast_ns = 0
        self._next_poll_ns = {}

    def _failed_open(self, e: LinkError) -> None:
        self._link = None
        self.state = "failed"
        advice = connect.advice(e)
        self.health = (
            f"Cannot open {self.where}: {str(e).rstrip('.')}. "
            f"{advice + ' ' if advice else ''}Retrying."
        )
        self._log_once("open", f"cannot open {self.where}: {e}")
        self._backoff_s = min(self._backoff_s * 2, _BACKOFF_MAX_S)

    def _lost(self, reason: str) -> None:
        self._close()
        self.state = "connecting"
        self.health = f"Lost {self.where}: {reason.rstrip('.')}. Reconnecting."
        self._note("warn", f"disconnected from {self.where}: {reason}")
        self._emit_status(connected=False)
        self._backoff_s = (
            min(self._backoff_s * 2, _BACKOFF_MAX_S) if self._backoff_s else _BACKOFF_MIN_S
        )

    def _close(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            link.close()
        self._selector.forget()

    def disconnect_for(self, seconds: float, health: str) -> None:
        """Let go of the link without handing the panel back, and reconnect no sooner than
        `seconds` from now, as while the instrument restarts."""
        self._close()
        self.state = "connecting"
        self.health = health
        self._note("info", f"disconnected from {self.where} for {seconds:g} s")
        self._emit_status(connected=False)
        self._backoff_s = _BACKOFF_MIN_S
        self._reconnect_at = time.monotonic() + seconds

    def _cycle(self) -> None:
        """One pass: the fast steps on their schedule, then slow steps in the time left before
        the next fast pass, serving requests in between."""
        assert self._plan is not None and self.profile is not None
        start = self._now()
        if self._slow is None and start >= self._next_slow_ns:
            first = self._next_slow_ns == 0
            self._next_slow_ns = start + _SLOW_NS
            self._slow = self._slow_round()
            if first:
                # State first, so a meter's first reading already knows its function.
                while self._slow is not None:
                    self._slow_step()
        # Before the batch, never inside it: a request must not sit between a query and its stamp.
        self._serve_requests()
        if self._stopping.is_set() or self._link is None:
            return
        fast_rows: dict[int | None, dict[str, Any]] = {}
        self._fast_at = {}
        for step in self._plan.fast:
            if self._stopping.is_set() or self._link is None:
                return
            self._run_step(step, fast_rows)
        if self.profile.daq is not None and self.cfg.scan and self._link is not None:
            self._read_scan(fast_rows)
        if self._slots and self._link is not None:
            sent = self._now()
            scope_ops.read_slots(self, fast_rows)
            done = self._now()
            self._rtt_ms = (done - sent) / _MS / max(1, len(self._slots))
            for ch in fast_rows:
                self._fast_at.setdefault(ch, (sent + done) // 2)
        now = self._now()
        for ch, values in fast_rows.items():
            self._record(ch, values, self._fast_at.get(ch, now))
        self._fast_times.append(now)
        while self._fast_times and self._fast_times[0] < now - _RATE_WINDOW_NS:
            self._fast_times.popleft()
        self._poll_table(now)
        if now >= self._next_status_ns:
            self._next_status_ns = now + _STATUS_NS
            self._emit_status(connected=True)
            self.health = self._open_health()
        # A fixed schedule, so a cycle that ran late does not delay the next.
        self._next_fast_ns = max(self._next_fast_ns + self._interval_ns, start)
        # Slow steps fill the slack; one runs every cycle so a round always ends.
        longest = 0
        while self._slow is not None and not self._stopping.is_set():
            began = self._now()
            if longest and began + longest > self._next_fast_ns:
                break
            self._slow_step()
            longest = max(longest, self._now() - began)
        self._serve_requests()
        remaining_s = (self._next_fast_ns - self._now()) / NS_PER_S
        if remaining_s > 0:
            self._wait(remaining_s)

    def _slow_step(self) -> None:
        """One query of the slow round in progress; the round ends when its steps do."""
        assert self._slow is not None
        try:
            next(self._slow)
        except StopIteration:
            self._slow = None
        except BaseException:
            self._slow = None
            raise

    def _slow_round(self) -> Generator[None, None, None]:
        """The slow steps one query at a time; each channel is recorded once its last slow
        step is in, at that moment, so its row never lands behind a later fast reading."""
        assert self._plan is not None and self.profile is not None
        steps = self._plan.slow
        last = {step.channel: index for index, step in enumerate(steps)}
        rows: dict[int | None, dict[str, Any]] = {}
        flags: dict[int | None, list[tuple[str, bool]]] = {}
        for index, step in enumerate(steps):
            self._serve_requests()
            if self._stopping.is_set() or self._link is None:
                return
            self._run_step(step, rows, flags)
            if last[step.channel] == index:
                found = flags.pop(step.channel, None)
                if found is not None:
                    label = next((value for value, is_set in found if is_set), "none")
                    rows.setdefault(step.channel, {})["protection"] = label
                values = rows.pop(step.channel, None)
                if values:
                    self._record(step.channel, values, self._now())
            yield
        if self.profile.scope is not None and self.profile.scope.slots is not None:
            scans = self._slot_scans
            live, busy = yield from scope_ops.slot_steps(self)
            if self._slot_scans == scans:
                self._slots, self._busy_slots = live, busy
        self._drain_errors()
        # The health line names the last instrument error as soon as it is read.
        self.health = self._open_health()
        yield
        self._keep_panel()

    def _run_step(
        self,
        step: Step,
        rows: dict[int | None, dict[str, Any]],
        flags: dict[int | None, list[tuple[str, bool]]] | None = None,
    ) -> None:
        sent = self._now()
        try:
            self._select(step.channel)
            reply = self._query(step.query)
        except LinkTimeout as e:
            self._timed_out(step.key, step.query, e)
            return
        self._timeouts = 0
        rtt = self._now() - sent
        if step.cadence == "fast":
            self._rtt_ms = rtt / _MS
            # The reading's moment: halfway through its own round trip.
            self._fast_at[step.channel] = sent + rtt // 2
        try:
            if step.flag is not None:
                assert flags is not None
                flags.setdefault(step.channel, []).append(
                    (step.flag.value, engine.flag_is_set(step.flag, reply, step.channel))
                )
            else:
                values = engine.parse_reply(step.reader, reply, step.channel)
                rows.setdefault(step.channel, {}).update(values)
        except ParseError as e:
            self._log_once(step.key, f"{step.query}: {e}")
            for name in step.reader.fields:
                if name in ENUM_FIELDS:
                    rows.setdefault(step.channel, {})[name] = engine.unknown_label(name)
            self._resync(f"{step.query} answered {reply.strip()!r}")

    def _timed_out(self, key: str, query: str, e: LinkTimeout) -> None:
        self._timeouts += 1
        self._timeouts_total += 1
        self._log_once(f"timeout {key}", f"{query}: {e}")
        if self._timeouts >= _TIMEOUTS_BEFORE_RECONNECT:
            raise LinkError(f"{self._timeouts} queries in a row got no reply") from e
        # The reply may still come; it must not land under the next query's field.
        try:
            self._resync(f"{query} got no reply")
        except LinkError:
            self._clear_link()

    def _read_scan(self, rows: dict[int | None, dict[str, Any]]) -> None:
        """One scan of a daq unit, stamped halfway through its round trip."""
        assert self.profile is not None and self.profile.daq is not None
        query = self.profile.daq.read[-1]
        sent = self._now()
        try:
            daq_ops.read(self, rows)
        except LinkTimeout as e:
            self._timed_out("scan", query, e)
            return
        except ParseError as e:
            self._bad_scans += 1
            if self._bad_scans >= _BAD_SCANS_BEFORE_RECONNECT:
                raise LinkError(
                    f"{self._bad_scans} scans in a row did not read as set up: {e}"
                ) from e
            self._log_once("scan", f"{query}: {e}")
            self._resync(f"{query}: {e}")
            return
        self._timeouts = 0
        self._bad_scans = 0
        done = self._now()
        self._rtt_ms = (done - sent) / _MS
        for ch in rows:
            self._fast_at.setdefault(ch, sent + (done - sent) // 2)

    def _record(self, ch: int | None, values: dict[str, Any], at: int) -> None:
        """Write one channel's values, stamped at `at`, and note changes."""
        assert self.profile is not None
        time_ns = self._clock.epoch_ns(at)
        shared = INSTRUMENT_FIELDS[self.profile.category]
        if ch is None and shared:
            wide = {k: v for k, v in values.items() if k in shared}
            if wide:
                self._note_acquisition_changes(wide)
                self._acquisition.update(wide)
                self._emitter.acquisition_row(self.cfg.name, wide, time_ns)
            values = {k: v for k, v in values.items() if k not in shared}
            if not values:
                return
        targets = [ch] if ch is not None else list(self._channel)
        for target in targets:
            state = self._channel.get(target)
            if state is None:
                continue
            if self.profile.category == "dmm":
                if "function" in values:
                    self._function = values["function"]
                if "value" in values:
                    self._emitter.reading(self.cfg.name, self._function, values["value"], time_ns)
                row = {k: v for k, v in values.items() if k != "value"}
            else:
                row = values
            output = row.get("output", state.values.get("output"))
            if self.profile.category == "psu" and "mode" in row and output is False:
                # A supply reports how it would regulate even while its output is off.
                row = {**row, "mode": "off"}
            self._note_changes(target, state, row, at)
            state.values.update(row)
            self._emitter.row(self.cfg.name, target, row, time_ns)

    def _note_acquisition_changes(self, row: dict[str, Any]) -> None:
        for name, value in row.items():
            old = self._acquisition.get(name)
            if old is None or old == value or (isinstance(value, float) and _same(old, value)):
                continue
            if name in ("trigger_mode", "trigger_source", "trigger_slope", "timebase"):
                self._note("info", f"{name} {_fmt(old)} -> {_fmt(value)}")

    def _note_changes(self, ch: int, state: _Channel, row: dict[str, Any], at: int) -> None:
        for name, value in row.items():
            old = state.values.get(name)
            if (
                old is None
                or old == value
                or (isinstance(value, float) and isinstance(old, float) and _same(old, value))
            ):
                continue
            if name in ("mode", "protection", "function"):
                level = "warn" if name == "protection" and value != "none" else "info"
                self._note(level, f"ch{ch} {name} {old} -> {value}")
            elif name.endswith("_set") or name == "output":
                own = state.own_set_ns.get(name)
                if own is not None and at - own < _OWN_CHANGE_NS:
                    continue
                self._note("info", f"ch{ch} {name} changed {_fmt(old)} to {_fmt(value)} (external)")

    def _poll_table(self, now: int) -> None:
        for row in self.cfg.poll:
            due = self._next_poll_ns.get(row.name, 0)
            if now < due:
                continue
            self._next_poll_ns[row.name] = now + int(row.every_s * NS_PER_S)
            channels = list(self._channel) if "{ch}" in row.query else [None]
            for ch in channels:
                self._serve_requests()
                if self._link is None:
                    return
                query = engine.render(row.query, ch=ch)
                sent = self._now()
                try:
                    value = engine.parse_float(self._query(query))
                except LinkTimeout as e:
                    self._log_once(f"poll {row.name}", f"{query}: {e}")
                    self._recover(f"{query}: {e}")
                    continue
                except ParseError as e:
                    self._log_once(f"poll {row.name}", f"{query}: {e}")
                    self._resync(f"{query}: {e}")
                    continue
                done = self._now()
                if not self._plan or not self._plan.fast:
                    # With no measurement queries, the poll table is what the round trip means.
                    self._rtt_ms = (done - sent) / _MS
                at = self._clock.epoch_ns(sent + (done - sent) // 2)
                key = poll_key(row.name, ch)
                self._poll_values[key] = value
                self._emitter.poll_value(self.cfg.name, key, value, at)

    def _drain_errors(self, quiet: bool = False) -> int:
        """Read the error queue; how many errors it held."""
        assert self.profile is not None
        errors = self.profile.errors
        if errors is None:
            return 0
        found = 0
        for query in (errors.query, *errors.then):
            for _ in range(errors.drain):
                try:
                    reply = self._query(query)
                except LinkTimeout as e:
                    self._recover(f"{query}: {e}")
                    return found
                error = engine.read_error(errors, reply)
                if error is None:
                    break
                if error.code is None:
                    self._resync(f"unexpected reply {reply.strip()!r} to {query}")
                    return found
                found += 1
                self._errors += 1
                self._last_error = f"{error.code},{error.text}"
                if not quiet:
                    self._log_once(
                        f"instrument error {error.code}",
                        f"instrument error {self._last_error}",
                        source="instrument",
                    )
        return found

    def _keep_panel(self) -> None:
        assert self.profile is not None
        link = self.profile.link
        if (
            self.cfg.keep_panel
            and not self._panel_locked
            and link.locks_panel
            and link.local
            and not link.needs_remote
            and not self._requests
        ):
            for command in link.local:
                self._send(command)

    def _hand_back(self) -> None:
        """Give the front panel back when the extension stops holding the instrument."""
        link = self._link
        profile = self.profile
        if link is None or profile is None or self.state != "open":
            return
        if profile.link.local and profile.link.locks_panel:
            with suppress(Exception):
                for command in profile.link.local:
                    link.write(command)

    def _emit_status(self, *, connected: bool) -> None:
        if not self._registered:
            return
        rate = self._rate_hz() if connected else 0.0
        self._emitter.status(
            self.cfg.name,
            self._rtt_ms if connected else math.nan,
            rate,
            self._errors,
            connected,
            self._clock.epoch_ns(self._now()),
        )

    def _query(self, command: str) -> str:
        assert self._link is not None
        self._queries += 1
        reply = self._link.query(command)
        self._recent.append((self._now(), command, reply))
        return reply

    def _send(self, command: str) -> None:
        """A command with no reply, or with the profile's `ack`."""
        assert self._link is not None and self.profile is not None
        ack = self.profile.link.ack
        if ack is None:
            self._link.write(command)
            self._recent.append((self._now(), command, ""))
            return
        reply = self._query(command)
        if reply.strip() != ack:
            raise RangeError(f"{self.where} answered {reply.strip()!r} to {command}")

    def _select(self, ch: int | None) -> None:
        command = self._selector.command_for(ch)
        if command is not None:
            self._send(command)
            self._selector.mark(ch)

    def _clear_link(self) -> None:
        if self._link is not None:
            self._link.clear()
        self._selector.forget()

    def _recover(self, reason: str) -> None:
        """After a silent or garbled query: get the replies back in step, or at least drop
        whatever still arrives, so a late reply never lands under the next query."""
        try:
            self._resync(reason)
        except LinkError:
            self._clear_link()

    def _resync(self, reason: str) -> None:
        """Get the replies back in step with the queries after one went astray.

        A command the instrument answered unexpectedly, or a query it did not answer, leaves
        every later reply one behind. Drop what is pending, ask for the identity, and read until
        it comes back; a link that never does is reopened.
        """
        assert self._link is not None and self.profile is not None
        self._clear_link()
        remote = self.profile.link.remote
        if remote:
            # A unit that dropped out of remote mode answers nothing until it is back in it.
            self._link.write(remote)
            lock = self.profile.system.lock
            if self._panel_locked and lock is not None:
                self._link.write(lock)
        idn = self.profile.match.idn
        self._link.write(idn)
        for _ in range(_RESYNC_READS):
            try:
                reply = self._link.read()
            except LinkTimeout:
                break
            if self._is_identity(reply):
                self._recent.append((self._now(), idn, reply))
                self._note("warn", f"replies were out of step ({reason}); back in step")
                return
        raise LinkError(f"replies stayed out of step ({reason})")

    def _is_identity(self, reply: str) -> bool:
        """Whether a reply is the identity, not some other comma-separated answer."""
        if self.idn is not None:
            return parse_idn(reply) == self.idn
        return reply.count(",") >= 3

    def _serve_requests(self) -> None:
        while True:
            with self._lock:
                if not self._requests:
                    return
                request = self._requests.popleft()
            if not request.future.set_running_or_notify_cancel():
                # The action stopped waiting: the instrument must not get the request later.
                self._note("warn", f"{request.kind} was dropped: the action stopped waiting")
                continue
            try:
                result = self._serve(request)
            except LinkError as e:
                request.future.set_result({"error": str(e)})
                if e.kind in ("timeout", "reply") and self._link is not None:
                    # One silent or garbled reply is this request's failure, not the link's.
                    self._recover(f"{request.kind}: {e}")
                    continue
                raise
            except (RangeError, ParseError, ValueError) as e:
                request.future.set_result({"error": str(e)})
            except Exception as e:
                logger.exception("%s: %s failed", self.cfg.name, request.kind)
                request.future.set_result({"error": f"{request.kind} failed: {e}"})
            else:
                request.future.set_result(result)

    def _serve(self, request: _Request) -> dict[str, Any]:
        kind, args = request.kind, request.args
        if kind == "state":
            return serve.state_report(
                self,
            )
        if kind == "release":
            self._hand_back()
            self._close()
            self.state = "released"
            self.health = f"Released {self.where}; run Acquire to take it back."
            self._note("info", f"released {self.where}")
            return {"ok": True}
        if kind == "acquire":
            if self.state != "released":
                return {"ok": True, "message": "the instrument was not released"}
            self.state = "connecting"
            self._backoff_s = _BACKOFF_MIN_S
            if not self._connect():
                raise RangeError(self.health)
            self._note("info", f"acquired {self.where}")
            return {"ok": True}
        if self._link is None or self.profile is None:
            raise RangeError(self.health)
        if kind in _WRITE_KINDS:
            # An error a poll query raised must not be pinned on this request.
            self._drain_errors(quiet=True)
        if kind == "system":
            return system_ops.run(self, args)
        if kind == "scpi":
            return serve.scpi(self, args["command"], args.get("timeout_s"))
        if kind == "local":
            local = self.profile.link.local
            if not local:
                raise RangeError("this profile has no local command")
            for command in local:
                self._send(command)
            self._panel_locked = False
            self._after_write("local")
            return {"ok": True}
        ch = int(args.get("channel") or min(self._channel, default=1))
        if ch not in self._channel:
            if self.profile.daq is not None:
                raise RangeError(f"channel must be one of {', '.join(map(str, self._channel))}")
            raise RangeError(f"channel must be from 1 to {self.channels}")
        if kind == "read":
            return serve.read_now(self, ch)
        if kind == "clear":
            return serve.clear_protection(self, ch)
        if kind == "set":
            return serve.set_many(self, ch, args)
        if kind == "measure":
            return scope_ops.measure(self, ch, args)
        if kind == "waveform":
            return scope_ops.capture(self, ch, args)
        if kind == "command":
            return serve.run_command(self, str(args["name"]), ch)
        raise RangeError(f"unknown request {kind}")

    def _after_write(self, what: str, *, log: bool = True) -> None:
        """Check the error queue after a write; the instrument's own text fails the request."""
        assert self.profile is not None
        errors = self.profile.errors
        queries = () if errors is None else (errors.query, *errors.then)
        reported: list[engine.InstrumentError] = []
        for query in queries:
            assert errors is not None
            reply = self._query(query)
            error = engine.read_error(errors, reply)
            if error is not None and error.code is None:
                # Not an error entry at all: the replies are out of step with the queries.
                self._resync(f"unexpected reply {reply.strip()!r} to {query}")
                error = engine.read_error(errors, self._query(query))
            if error is None:
                continue
            reported.append(error)
            for _ in range(errors.drain - 1):
                more = engine.read_error(errors, self._query(query))
                if more is None:
                    break
                reported.append(more)
        if reported:
            self._errors += len(reported)
            texts = [f"{e.code},{e.text}" if e.code is not None else e.text for e in reported]
            self._last_error = texts[-1]
            self._note("warn", f"{what}: instrument error {'; '.join(texts)}", source="action")
            raise RangeError(f"the instrument reported {'; '.join(texts)}")
        if log:
            self._note("info", what, source="action")

    def _rate_hz(self) -> float:
        """The readings per second achieved over the last window."""
        return len(self._fast_times) / (_RATE_WINDOW_NS / NS_PER_S)

    def _open_health(self) -> str:
        who = f"{self.idn.manufacturer} {self.idn.model}".strip() if self.idn else self.where
        rate = self._rate_hz()
        if self.profile is not None and self.profile.daq is not None:
            fields = sum(len(names) for names in opening.channel_fields(self).values())
        else:
            fields = len(self._emitter.fields(self.cfg.name)) if self._registered else 0
        # A generator reads only its settings, so it has no reading rate to report.
        signals = self.profile is None or bool(SIGNAL_FIELDS[self.profile.category])
        text = f"Connected to {who}. Reading {fields} {'signals' if signals else 'settings'}"
        if self.cfg.poll:
            text += f" and {len(self.cfg.poll)} extra readings"
        text += f" on {self.channels} channel"
        text += "s" if self.channels != 1 else ""
        text += f" at {rate:.1f} Hz (target {self.cfg.rate_hz:g} Hz)." if signals else "."
        if (
            signals
            and rate
            and rate < self.cfg.rate_hz * 0.8
            and self._fast_times
            and len(self._fast_times) > 5
        ):
            text += " The instrument answers slower than the target; readings run back to back."
        if self._last_error:
            text += f" Last instrument error: {self._last_error}."
        return text

    def _note(self, level: str, message: str, source: str = "instruments") -> None:
        if self._registered:
            self._emitter.note(
                self.cfg.name, level, message, self._clock.epoch_ns(self._now()), source
            )
        else:
            logger.log(
                logging.WARNING if level == "warn" else logging.INFO,
                "%s: %s",
                self.cfg.name,
                message,
            )

    def _log_once(self, key: str, message: str, source: str = "instruments") -> None:
        throttle = self._parse_throttle.setdefault(key, _Throttle())
        held = throttle.tally(self._now())
        if held is None:
            return
        suffix = f" ({held} more since the last note)" if held else ""
        self._note("warn", message + suffix, source=source)

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending, self._requests = list(self._requests), deque()
        for request in pending:
            # An action that stopped waiting cancelled its future; it takes no result.
            if request.future.set_running_or_notify_cancel():
                request.future.set_result({"error": reason})


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)
