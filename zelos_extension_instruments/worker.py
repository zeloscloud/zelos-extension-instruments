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
from collections.abc import Callable
from typing import Any, Literal

from . import engine
from .clock import HostClock
from .config import InstrumentConfig, Settings, poll_key
from .emitter import Emitter
from .engine import ParseError, RangeError, Selector, Step
from .link import (
    Address,
    Link,
    LinkError,
    LinkTimeout,
    SerialAddress,
    TcpAddress,
    VisaAddress,
    open_link,
)
from .link import (
    Settings as LinkSettings,
)
from .profile import (
    ENUM_FIELDS,
    VERIFY_FIELD,
    Idn,
    Profile,
    Range,
    Setter,
    by_id,
    find,
    load_file,
    parse_idn,
)

logger = logging.getLogger(__name__)

_MS = 1_000_000
_S = 1_000_000_000
_SLOW_NS = 1 * _S
_STATUS_NS = 1 * _S
_RATE_WINDOW_NS = 10 * _S
_BACKOFF_MIN_S = 0.5
_BACKOFF_MAX_S = 10.0
# Consecutive timeouts in the poll loop before the link is reopened.
_TIMEOUTS_BEFORE_RECONNECT = 3
# A setpoint that changes within this long after our own set is ours, not an outside change.
_OWN_CHANGE_NS = 2 * _S
_LOG_EVERY_NS = 60 * _S
# Readings kept for Get State.
_KEPT = 50
# Replies read while getting back in step after one went astray.
_RESYNC_READS = 8

RequestKind = Literal[
    "set", "output", "function", "read", "state", "scpi", "clear", "local", "release", "acquire"
]
_State = Literal["connecting", "open", "released", "failed", "stopped"]
_Future = concurrent.futures.Future[dict[str, Any]]

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
        settings: Settings,
        *,
        open: Opener = open_link,
        now: Callable[[], int] = time.perf_counter_ns,
        clock: HostClock | None = None,
    ) -> None:
        super().__init__(name=f"instrument {cfg.name}", daemon=True)
        self.cfg = cfg
        self._profiles = profiles
        self._emitter = emitter
        self._settings = settings
        self._open = open
        self._now = now
        self._clock = clock or HostClock(now)
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
        self._queries = 0
        self._parse_throttle: dict[str, _Throttle] = {}
        self._backoff_s = _BACKOFF_MIN_S
        self._recent: deque[tuple[int, str, str]] = deque(maxlen=_KEPT)
        self._next_slow_ns = 0
        self._next_status_ns = 0
        self._next_fast_ns = 0
        self._next_poll_ns: dict[str, int] = {}
        self._poll_values: dict[str, float] = {}
        self._interval_ns = int(_S / cfg.rate_hz)

    # -- threads other than the worker's --------------------------------------------------------

    def request(self, kind: RequestKind, **args: Any) -> _Future:
        """Queue a request; the worker answers through the future."""
        future: _Future = concurrent.futures.Future()
        with self._lock:
            self._requests.append(_Request(kind, args, future))
        self._wake.set()
        return future

    def stop(self) -> None:
        """Ask the worker to end; closes a link blocked in a read."""
        self._stopping.set()
        self._wake.set()
        link = self._link
        if link is not None:
            link.close()

    # -- the worker's thread --------------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self._stopping.is_set():
                if self.state == "released":
                    self._serve_requests()
                    self._wake.wait(0.5)
                    self._wake.clear()
                    continue
                if self._link is None and not self._connect():
                    self._serve_requests()
                    self._wait(self._backoff_s)
                    continue
                try:
                    self._cycle()
                except LinkError as e:
                    self._lost(str(e))
                except Exception:
                    logger.exception("%s: unexpected error; reconnecting", self.cfg.name)
                    self._lost("an unexpected error")
        finally:
            self._close()
            self.state = "stopped"
            self.health = "Stopped."
            self._fail_pending("the extension is stopping")

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(min(remaining, 0.5)):
                self._wake.clear()
                if self._requests:
                    return

    # -- connecting --------------------------------------------------------------------------

    def _connect(self) -> bool:
        """Open the link and identify the instrument; False with the health set when it fails."""
        profile = self._chosen_profile()
        if profile is None:
            return False
        link_settings = self._link_settings(profile)
        address = self._address(profile)
        self.where = self.cfg.where if self.cfg.connection == "demo" else address.where
        try:
            link = self._open(address, link_settings)
        except LinkError as e:
            self._failed_open(e)
            return False
        self._link = link
        self._selector = Selector(profile.link.select)
        try:
            idn = self._identify(link, profile)
        except LinkError as e:
            self._close()
            self._failed_open(e)
            return False
        try:
            profile = self._resolve_profile(profile, idn)
            wanted = self._link_settings(profile)
            if wanted != link_settings:
                # The matched profile frames lines differently from the one used to identify.
                link.close()
                self._link = link = self._open(self._address(profile), wanted)
            self.profile, self.idn = profile, idn
            if profile.link.remote:
                self._send(profile.link.remote)
            for command in profile.link.init:
                self._send(command)
            channels = self.cfg.channels or self._channel_count(profile, idn)
            self._install(profile, idn, channels)
            self._read_ranges()
            self._probe_optional()
        except LinkError as e:
            self._lost(str(e))
            return False
        self._backoff_s = _BACKOFF_MIN_S
        self._timeouts = 0
        self.state = "open"
        self.health = self._open_health()
        self._note("info", f"connected to {self.where}: {self.idn.text if self.idn else ''}")
        return True

    def _chosen_profile(self) -> Profile | None:
        """The profile named in the config, or a generic one to identify with."""
        choice = self.cfg.profile
        if self.cfg.connection == "demo":
            return by_id(self._profiles, f"generic-{self.cfg.category}") or self._profiles[0]
        if choice == "auto":
            return by_id(self._profiles, "ieee488") or self._profiles[0]
        if choice.endswith(".toml"):
            try:
                return load_file(choice)
            except Exception as e:
                self.state = "failed"
                self.health = f"Cannot load the profile {choice}: {e}"
                return None
        profile = by_id(self._profiles, choice)
        if profile is None:
            self.state = "failed"
            self.health = f"No profile is named {choice}. Pick one from the list, or auto."
        return profile

    def _address(self, profile: Profile) -> Address:
        cfg = self.cfg
        link = profile.link
        if cfg.connection == "demo":
            return TcpAddress(cfg.where, 0)
        if cfg.connection == "lan":
            assert cfg.host is not None
            return TcpAddress(cfg.host, cfg.port or link.lan_port)
        if cfg.connection == "serial":
            assert cfg.serial_port is not None
            return SerialAddress(
                cfg.serial_port,
                baud=cfg.baud or link.baud,
                data_bits=link.data_bits,
                parity=link.parity,  # type: ignore[arg-type]
                stop_bits=link.stop_bits,
                flow_control=link.flow_control,  # type: ignore[arg-type]
            )
        assert cfg.resource is not None
        return VisaAddress(cfg.resource, cfg.backend)

    def _link_settings(self, profile: Profile) -> LinkSettings:
        cfg = self.cfg
        link = profile.link
        return LinkSettings(
            write_terminator=cfg.line_ending or link.write_terminator,
            read_terminator=cfg.line_ending or link.read_terminator,
            timeout_s=cfg.timeout_s or link.timeout_s,
            delay_ms=link.delay_ms if cfg.delay_ms is None else cfg.delay_ms,
        )

    def _identify(self, link: Link, profile: Profile) -> Idn | None:
        try:
            reply = link.query(profile.match.idn)
        except LinkTimeout:
            text = f"accepted the connection but did not answer {profile.match.idn}"
            if self.cfg.profile == "auto":
                text += "; an instrument that speaks a dialect without it needs its profile picked"
            raise LinkError(text, "held") from None
        idn = parse_idn(reply)
        if not idn.manufacturer and not idn.model:
            raise LinkError(f"{self.where} answered {profile.match.idn} with {reply!r}")
        return idn

    def _resolve_profile(self, chosen: Profile, idn: Idn | None) -> Profile:
        """`auto` picks the matching profile; a chosen one is kept and the identity checked."""
        if self.cfg.profile != "auto" or self.cfg.connection == "demo":
            if idn is not None and not chosen.generic and not chosen.matches(idn):
                self._note(
                    "warn",
                    f"{idn.manufacturer} {idn.model} does not match the profile {chosen.id}; "
                    "using it anyway",
                )
            return chosen
        assert idn is not None
        matched = find(self._profiles, idn)
        if matched is None:
            raise LinkError(
                f"no profile matches {idn.manufacturer} {idn.model}; choose a generic profile "
                "for its category, or write one"
            )
        return matched

    def _channel_count(self, profile: Profile, idn: Idn | None) -> int:
        if profile.match.channels_query is not None:
            try:
                return max(1, engine.parse_int(self._query(profile.match.channels_query)))
            except (ParseError, LinkTimeout):
                pass
        return profile.channels_for(idn.model if idn else "")

    def _install(self, profile: Profile, idn: Idn | None, channels: int) -> None:
        self.profile = profile
        self.idn = idn
        self.channels = channels
        self._channel = {ch: _Channel() for ch in range(1, channels + 1)}
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
        self._plan = engine.compile_plan(profile, channels)
        if not self._registered:
            self._emitter.add_instrument(self.cfg.name, profile, channels, self.cfg.poll)
            self._registered = True
        self._next_slow_ns = 0
        self._next_status_ns = 0
        self._next_fast_ns = 0
        self._next_poll_ns = {}

    def _read_ranges(self) -> None:
        """Fill ranges that come from queries."""
        assert self.profile is not None
        for ch, state in self._channel.items():
            for key, r in list(state.ranges.items()):
                if r.min_query is None and r.max_query is None:
                    continue
                low, high = r.min, r.max
                try:
                    self._select(ch)
                    if r.max_query is not None:
                        high = engine.parse_float(self._query(engine.render(r.max_query, ch=ch)))
                    if r.min_query is not None:
                        low = engine.parse_float(self._query(engine.render(r.min_query, ch=ch)))
                except (ParseError, LinkTimeout):
                    continue
                if high is not None and math.isfinite(high):
                    state.ranges[key] = r.with_bounds(
                        low if low is None or math.isfinite(low) else 0.0, high
                    )

    def _probe_optional(self) -> None:
        """Run each optional query once; drop the ones the instrument refuses."""
        assert self._plan is not None and self.profile is not None
        dropped: set[str] = set()
        for step in self._plan:
            if not step.reader.optional:
                continue
            try:
                self._select(step.channel)
                reply = self._query(step.query)
                if step.flag is not None:
                    engine.flag_is_set(step.flag, reply, step.channel)
                else:
                    engine.parse_reply(step.reader, reply, step.channel)
                if self._drain_errors(quiet=True):
                    dropped.add(step.key)
            except (LinkTimeout, ParseError):
                self._clear_link()
                dropped.add(step.key)
        if dropped:
            names = sorted({key.split("@")[0] for key in dropped})
            self._note(
                "info", f"not answered by this instrument, so not recorded: {', '.join(names)}"
            )
            self._dropped = dropped
            self._plan = engine.compile_plan(self.profile, self.channels, dropped)

    def _failed_open(self, e: LinkError) -> None:
        self._link = None
        self.state = "failed"
        self.health = f"Cannot open {self.where}: {str(e).rstrip('.')}. {_advice(e)} Retrying."
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

    # -- the poll cycle ----------------------------------------------------------------------

    def _cycle(self) -> None:
        """One pass: the slow steps when due, then the fast ones, serving requests in between."""
        assert self._plan is not None and self.profile is not None
        start = self._now()
        if start >= self._next_slow_ns:
            # State first, so a meter's first reading already knows its function.
            self._next_slow_ns = start + _SLOW_NS
            slow_rows: dict[int | None, dict[str, Any]] = {}
            flags: dict[int | None, list[tuple[str, bool]]] = {}
            for step in self._plan.slow:
                self._serve_requests()
                if self._stopping.is_set() or self._link is None:
                    return
                self._run_step(step, slow_rows, flags)
            for ch, found in flags.items():
                label = next((value for value, is_set in found if is_set), "none")
                slow_rows.setdefault(ch, {})["protection"] = label
            at = self._now()
            for ch, values in slow_rows.items():
                self._record(ch, values, at, fast=False)
            self._drain_errors()
            self._keep_panel()
        fast_rows: dict[int | None, dict[str, Any]] = {}
        for step in self._plan.fast:
            self._serve_requests()
            if self._stopping.is_set() or self._link is None:
                return
            self._run_step(step, fast_rows)
        now = self._now()
        for ch, values in fast_rows.items():
            self._record(ch, values, now, fast=True)
        self._fast_times.append(now)
        while self._fast_times and self._fast_times[0] < now - _RATE_WINDOW_NS:
            self._fast_times.popleft()
        self._poll_table(now)
        if now >= self._next_status_ns:
            self._next_status_ns = now + _STATUS_NS
            self._emit_status(connected=True)
            self.health = self._open_health()
        self._serve_requests()
        # A fixed schedule: the next cycle is due one interval after this one was, so the rate
        # holds whatever each cycle took, and a cycle that ran late does not delay the next.
        self._next_fast_ns = max(self._next_fast_ns + self._interval_ns, start)
        remaining_s = (self._next_fast_ns - self._now()) / _S
        if remaining_s > 0:
            self._wait(remaining_s)

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
            self._timeouts += 1
            self._timeouts_total += 1
            self._log_once(f"timeout {step.key}", f"{step.query}: {e}")
            if self._timeouts >= _TIMEOUTS_BEFORE_RECONNECT:
                raise LinkError(f"{self._timeouts} queries in a row got no reply") from e
            self._clear_link()
            return
        self._timeouts = 0
        rtt = self._now() - sent
        if step.cadence == "fast":
            self._rtt_ms = rtt / _MS
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
            throttle = self._parse_throttle.setdefault(step.key, _Throttle())
            held = throttle.tally(self._now())
            if held is not None:
                suffix = f" ({held} more since the last note)" if held else ""
                self._note("warn", f"{step.query}: {e}{suffix}")
            for name in step.reader.fields:
                if name in ENUM_FIELDS:
                    rows.setdefault(step.channel, {})[name] = engine.unknown_label(name)
            self._resync(f"{step.query} answered {reply.strip()!r}")

    def _record(self, ch: int | None, values: dict[str, Any], at: int, *, fast: bool) -> None:
        """Write one channel's values, stamped half a round trip before `at`, and note changes."""
        assert self.profile is not None
        time_ns = self._clock.epoch_ns(at - int(self._rtt_ms * _MS / 2) if fast else at)
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
            self._note_changes(target, state, row, at)
            state.values.update(row)
            self._emitter.row(self.cfg.name, target, row, time_ns)

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
            self._next_poll_ns[row.name] = now + int(row.every_s * _S)
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
                    self._clear_link()
                    self._log_once(f"poll {row.name}", f"{query}: {e}")
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

    def _drain_errors(self, quiet: bool = False) -> bool:
        """Read the error queue; True when it held an error."""
        assert self.profile is not None
        errors = self.profile.errors
        if errors is None:
            return False
        found = False
        for _ in range(errors.drain):
            try:
                reply = self._query(errors.query)
            except LinkTimeout:
                self._clear_link()
                return found
            error = engine.read_error(errors, reply)
            if error is None:
                return found
            if error.code is None:
                self._resync(f"unexpected reply {reply.strip()!r} to {errors.query}")
                return found
            found = True
            self._errors += 1
            self._last_error = (
                f"{error.code},{error.text}" if error.code is not None else error.text
            )
            if not quiet:
                self._note("warn", f"instrument error {self._last_error}", source="instrument")
        return found

    def _keep_panel(self) -> None:
        assert self.profile is not None
        link = self.profile.link
        if self.cfg.keep_panel and link.locks_panel and link.local and not self._requests:
            self._send(link.local)

    def _emit_status(self, *, connected: bool) -> None:
        if not self._registered:
            return
        rate = len(self._fast_times) / (_RATE_WINDOW_NS / _S) if connected else 0.0
        self._emitter.status(
            self.cfg.name,
            self._rtt_ms if connected else math.nan,
            rate,
            self._errors,
            connected,
            self._clock.epoch_ns(self._now()),
        )

    # -- the wire ----------------------------------------------------------------------------

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

    def _clear_link(self) -> None:
        if self._link is not None:
            self._link.clear()
        self._selector.forget()

    def _resync(self, reason: str) -> None:
        """Get the replies back in step with the queries after one went astray.

        A command the instrument answered unexpectedly, or a query it did not answer, leaves
        every later reply one behind. Drop what is pending, ask for the identity, and read until
        it comes back; a link that never does is reopened.
        """
        assert self._link is not None and self.profile is not None
        self._clear_link()
        idn = self.profile.match.idn
        self._link.write(idn)
        for _ in range(_RESYNC_READS):
            try:
                reply = self._link.read()
            except LinkTimeout:
                break
            if "," in reply or (self.idn is not None and self.idn.model in reply):
                self._recent.append((self._now(), idn, reply))
                self._note("warn", f"replies were out of step ({reason}); back in step")
                return
        raise LinkError(f"replies stayed out of step ({reason})")

    # -- requests ----------------------------------------------------------------------------

    def _serve_requests(self) -> None:
        while True:
            with self._lock:
                if not self._requests:
                    return
                request = self._requests.popleft()
            try:
                result = self._serve(request)
            except LinkError as e:
                request.future.set_result({"error": str(e)})
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
            return self._state_report()
        if kind == "release":
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
        if kind == "scpi":
            return self._scpi(args["command"], args.get("timeout_s"))
        if kind == "local":
            local = self.profile.link.local
            if local is None:
                raise RangeError("this profile has no local command")
            self._send(local)
            self._after_write("local")
            return {"ok": True}
        ch = int(args.get("channel") or 1)
        if ch not in self._channel:
            raise RangeError(f"channel must be from 1 to {self.channels}")
        if kind == "read":
            return self._read_now(ch)
        if kind == "clear":
            return self._clear(ch)
        if kind == "set":
            return self._set_many(ch, args)
        if kind == "output":
            return self._set_output(ch, bool(args["on"]))
        if kind == "function":
            return self._set_function(ch, str(args["function"]))
        raise RangeError(f"unknown request {kind}")

    def _setter(self, name: str) -> Setter:
        assert self.profile is not None
        setter = self.profile.setters.get(name)
        if setter is None:
            raise RangeError(f"the profile {self.profile.id} cannot set {name}")
        return setter

    def _set_many(self, ch: int, args: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name in ("voltage", "current", "power", "resistance", "frequency", "ovp", "ocp"):
            if args.get(name) is None:
                continue
            results[name] = self._set_value(ch, name, float(args[name]))
        if args.get("function"):
            results["function"] = self._set_function(ch, str(args["function"]))["function"]
        if args.get("output") is not None:
            results["output"] = self._set_output(ch, bool(args["output"]))["output"]
        if not results:
            raise RangeError("give at least one value to set")
        return results

    def _set_value(self, ch: int, name: str, value: float) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter(name)
        key = self.profile.range_key(setter)
        range_ = self._channel[ch].ranges.get(key) if key else None
        engine.check_range(value, range_, self.cfg.limits.for_key(key or ""), name)
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, value=value, range_=range_))
        self._after_write(f"set ch{ch} {name} {engine.format_value(value, range_)}")
        readback: float | None = None
        verify = self.profile.verify_query(setter)
        if verify is not None:
            readback = engine.parse_float(self._query(engine.render(verify, ch=ch)))
            if not engine.verified(value, readback, range_):
                raise RangeError(
                    f"{name} reads back {readback:g} after setting {value:g}; "
                    "the instrument may have clipped it or refused it"
                )
        field_name = VERIFY_FIELD.get(name)
        if field_name is not None and readback is not None:
            self._mark_own(ch, field_name, readback)
        return {"set": value, "readback": readback}

    def _set_output(self, ch: int, on: bool) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter("output")
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, on=on))
        self._after_write(f"set ch{ch} output {'on' if on else 'off'}")
        verify = self.profile.verify_query(setter)
        readback: bool | None = None
        if verify is not None:
            reader = self.profile.reader("output")
            reply = self._query(engine.render(verify, ch=ch))
            readback = (
                bool(engine.parse_reply(reader, reply, ch)["output"])
                if reader is not None and reader.query == verify
                else engine.parse_bool(reply)
            )
            if readback != on:
                raise RangeError(f"output reads back {'on' if readback else 'off'}")
            self._mark_own(ch, "output", readback)
        return {"output": on if readback is None else readback}

    def _set_function(self, ch: int, function: str) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter("function")
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, function=function))
        self._after_write(f"set ch{ch} function {function}")
        verify = self.profile.verify_query(setter)
        if verify is not None:
            field_name = "mode" if self.profile.category == "load" else "function"
            reader = self.profile.reader(field_name)
            if reader is not None and reader.query == verify:
                label = engine.parse_reply(reader, self._query(engine.render(verify, ch=ch)), ch)[
                    field_name
                ]
                if label != function:
                    raise RangeError(f"function reads back {label}")
                self._mark_own(ch, field_name, label)
        return {"function": function}

    def _clear(self, ch: int) -> dict[str, Any]:
        assert self.profile is not None
        setter = self.profile.setters.get("clear")
        if setter is not None:
            self._select(ch if setter.per_channel else None)
            for command in engine.clear_commands(setter, ch=ch):
                self._send(command)
            self._after_write(f"clear ch{ch} protection")
        if self.profile.errors is not None:
            drained = 0
            for _ in range(self.profile.errors.drain):
                reply = self._query(self.profile.errors.query)
                if engine.read_error(self.profile.errors, reply) is None:
                    break
                drained += 1
            return {"ok": True, "errors_cleared": drained}
        return {"ok": True}

    def _read_now(self, ch: int) -> dict[str, Any]:
        """A fresh reading of every fast signal on one channel."""
        assert self._plan is not None and self.profile is not None
        values: dict[str, Any] = {}
        for step in self._plan.fast:
            if step.channel not in (ch, None):
                continue
            self._select(step.channel)
            values.update(engine.parse_reply(step.reader, self._query(step.query), step.channel))
        if self.profile.category == "dmm" and "value" in values:
            values["function"] = self._function
        for row in self.cfg.poll:
            key = poll_key(row.name, ch if "{ch}" in row.query else None)
            if key in self._poll_values:
                values[key] = self._poll_values[key]
        return values

    def _scpi(self, command: str, timeout_s: float | None) -> dict[str, Any]:
        assert self._link is not None
        command = command.strip()
        if not command:
            raise RangeError("give a command")
        self._selector.forget()
        # A query keeps its `?` on the header, with or without a parameter after it.
        is_query = "?" in command
        self._note("info", f"> {command}", source="tx")
        if is_query:
            reply = self._query(command)
            self._note("info", f"< {reply}", source="rx")
            return {"reply": reply}
        self._send(command)
        self._after_write(command, log=False)
        return {"ok": True}

    def _after_write(self, what: str, *, log: bool = True) -> None:
        """Check the error queue after a write; the instrument's own text fails the request."""
        assert self.profile is not None
        errors = self.profile.errors
        if errors is not None:
            reply = self._query(errors.query)
            error = engine.read_error(errors, reply)
            if error is not None and error.code is None:
                # Not an error entry at all: the replies are out of step with the queries.
                self._resync(f"unexpected reply {reply.strip()!r} to {errors.query}")
                reply = self._query(errors.query)
                error = engine.read_error(errors, reply)
            if error is not None:
                self._errors += 1
                self._last_error = (
                    f"{error.code},{error.text}" if error.code is not None else error.text
                )
                self._note("warn", f"{what}: instrument error {self._last_error}", source="action")
                raise RangeError(f"the instrument reported {self._last_error}")
        if log:
            self._note("info", what, source="action")

    def _mark_own(self, ch: int, field_name: str, value: Any) -> None:
        state = self._channel[ch]
        state.own_set_ns[field_name] = self._now()
        state.values[field_name] = value
        self._emitter.row(self.cfg.name, ch, {field_name: value}, self._clock.epoch_ns(self._now()))

    def _state_report(self) -> dict[str, Any]:
        profile = self.profile
        channels = {}
        for ch, state in self._channel.items():
            channels[f"ch{ch}"] = {
                "values": dict(state.values),
                "ranges": {
                    k: {"min": r.min, "max": r.max, "digits": r.digits}
                    for k, r in state.ranges.items()
                },
            }
        return {
            "state": self.state,
            "health": self.health,
            "where": self.where,
            "identity": self.idn.text if self.idn else None,
            "profile": None
            if profile is None
            else {
                "id": profile.id,
                "name": profile.name,
                "category": profile.category,
                "badge": profile.evidence.badge,
                "notes": profile.notes,
                "setters": sorted(profile.setters),
                "single_client": profile.link.single_client,
            },
            "channels": channels,
            "limits": dataclasses.asdict(self.cfg.limits),
            "counters": {
                "queries": self._queries,
                "timeouts": self._timeouts_total,
                "instrument_errors": self._errors,
                "last_error": self._last_error,
                "rtt_ms": None if math.isnan(self._rtt_ms) else round(self._rtt_ms, 2),
                "rate_hz": round(len(self._fast_times) / (_RATE_WINDOW_NS / _S), 2),
                "target_hz": self.cfg.rate_hz,
                "dropped": sorted(self._dropped),
            },
            "recent": [{"command": c, "reply": r} for _, c, r in list(self._recent)[-10:]],
        }

    def _open_health(self) -> str:
        who = f"{self.idn.manufacturer} {self.idn.model}".strip() if self.idn else self.where
        rate = len(self._fast_times) / (_RATE_WINDOW_NS / _S)
        fields = len(self._emitter.fields(self.cfg.name)) if self._registered else 0
        text = f"Connected to {who}. Reading {fields} signals"
        if self.cfg.poll:
            text += f" and {len(self.cfg.poll)} extra readings"
        text += f" on {self.channels} channel"
        text += "s" if self.channels != 1 else ""
        text += f" at {rate:.1f} Hz (target {self.cfg.rate_hz:g} Hz)."
        if (
            rate
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

    def _log_once(self, key: str, message: str) -> None:
        throttle = self._parse_throttle.setdefault(key, _Throttle())
        held = throttle.tally(self._now())
        if held is None:
            return
        suffix = f" ({held} more since the last note)" if held else ""
        self._note("warn", message + suffix)

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending, self._requests = list(self._requests), deque()
        for request in pending:
            request.future.set_result({"error": reason})


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _advice(e: LinkError) -> str:
    return {
        "refused": "Nothing answers on that port; check the host and port, and that the "
        "instrument's LAN socket is on.",
        "not_found": "No such device; check the port or resource, and that the instrument is "
        "plugged in and on.",
        "unresolved": "The host name cannot be looked up; use its address.",
        "busy": "It is in use by another program; close that program.",
        "held": "Instruments serve one client on their SCPI socket: close the other program "
        "that uses it, or power-cycle the instrument.",
        "permission": "This user may not open it; check the device permissions.",
        "timeout": "It did not answer in time; check the address and the cable.",
        "config": "Fix the setting the message names.",
    }.get(e.kind, "")
