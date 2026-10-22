"""One thread per instrument: opens the link, identifies the unit, polls, serves requests.

Everything the thread sends comes from the profile; everything it records goes through the
emitter. Requests from actions arrive on a queue and are served between poll steps.
"""

import base64
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

from . import engine, scope_ops
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
    COMMAND_SETTERS,
    ENUM_FIELDS,
    INSTRUMENT_FIELDS,
    LABEL_SETTERS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    VERIFY_FIELD,
    Idn,
    Master,
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
    "set",
    "output",
    "function",
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
]
_State = Literal["connecting", "open", "released", "failed", "stopped"]
_Future = concurrent.futures.Future[dict[str, Any]]
_WRITE_KINDS = frozenset(
    {"set", "output", "function", "local", "measure", "command", "scpi", "waveform"}
)
# A block reply the SCPI action returns whole, as base64; longer ones return their size.
_BLOCK_RETURN_MAX = 65536

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
        # The worker hands the panel back on its own thread; a thread still blocked in a read
        # after the link timeout is woken by closing the link.
        self.join(self._link_timeout_s + 0.5)
        link = self._link
        if link is not None and self.is_alive():
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
                try:
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
        self._link_timeout_s = link_settings.timeout_s
        address = self._address(profile)
        self.where = self.cfg.where if self.cfg.connection == "demo" else address.where
        try:
            link = self._open(address, link_settings)
        except LinkError as e:
            self._failed_open(e)
            return False
        self._link = link
        self._selector = Selector(profile.link.select)
        # A banner, or bytes a previous client left behind, must not become the identity.
        link.clear()
        try:
            if profile.link.first:
                self._send_first(link, profile)
            idn = self._identify(link, profile)
        except (LinkError, RangeError) as e:
            self._close()
            self._failed_open(e if isinstance(e, LinkError) else LinkError(str(e)))
            return False
        try:
            profile = self._resolve_profile(profile, idn)
            wanted = self._link_settings(profile)
            self._selector = Selector(profile.link.select)
            self._link_timeout_s = wanted.timeout_s
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
            # Errors another program or a power cycle left in the queue are not this session's.
            self._drain_errors(quiet=True)
            self._read_ranges()
            self._probe_optional()
            self._emitter.add_channels(self.cfg.name, self._channel_fields())
            self._emitter.identity(
                self.cfg.name,
                {
                    "manufacturer": idn.manufacturer if idn else "",
                    "model": idn.model if idn else "",
                    "serial": idn.serial if idn else "",
                    "firmware": idn.firmware if idn else "",
                    "profile": profile.id,
                    "address": self.where,
                },
                self._clock.epoch_ns(self._now()),
            )
        except (LinkError, RangeError, ParseError) as e:
            self._lost(str(e))
            return False
        self._timeouts = 0
        self._cycles_since_connect = 0
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
            command_delay_ms=link.command_delay_ms,
            device_clear=link.device_clear,
        )

    def _send_first(self, link: Link, profile: Profile) -> None:
        """The command an instrument needs before it answers anything, with its ack."""
        command = profile.link.first
        assert command is not None
        link.write(command)
        if profile.link.ack is not None:
            reply = link.read()
            if reply.strip() != profile.link.ack:
                raise LinkError(f"{self.where} answered {command} with {reply.strip()!r}")

    def _identify(self, link: Link, profile: Profile) -> Idn | None:
        """The identity, asked twice: some units ignore the first query after a connect, and
        bytes left from before can garble the first reply."""
        command = profile.match.idn
        for attempt in (1, 2):
            try:
                reply = link.query(command)
            except LinkTimeout:
                if attempt == 1:
                    link.clear()
                    continue
                text = f"accepted the connection but did not answer {command}"
                if self.cfg.profile == "auto":
                    text += (
                        "; an instrument that speaks a dialect without it needs its profile picked"
                    )
                raise LinkError(text, "held") from None
            if "," in reply:
                return parse_idn(reply)
            if attempt == 1:
                link.clear()
                continue
            raise LinkError(f"{self.where} answered {command} with {reply!r}")
        raise AssertionError("unreachable")

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
            except (ParseError, LinkTimeout) as e:
                self._recover(f"{profile.match.channels_query}: {e}")
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
        self._slow = None
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
                except (ParseError, LinkTimeout) as e:
                    self._recover(f"range {key}: {e}")
                    continue
                if high is not None and math.isfinite(high):
                    state.ranges[key] = r.with_bounds(
                        low if low is None or math.isfinite(low) else 0.0, high
                    )

    def _channel_fields(self) -> dict[int, set[str]]:
        """The fields each channel's readers produce, once the probe has dropped what the
        instrument refuses; an instrument-wide reader's fields land on every channel."""
        assert self._plan is not None and self.profile is not None
        shared = INSTRUMENT_FIELDS[self.profile.category]
        fields: dict[int, set[str]] = {ch: set() for ch in self._channel}
        for step in self._plan:
            names = (
                {"protection"} if step.flag is not None else set(step.reader.fields) - set(shared)
            )
            for ch in [step.channel] if step.channel is not None else list(fields):
                fields.setdefault(ch, set()).update(names)
        return fields

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
            except (LinkTimeout, ParseError) as e:
                self._recover(f"{step.query}: {e}")
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
        # Requests are served before the batch, never inside it: a reading's time is the time
        # of its own query, and a slow request must not sit between a query and its stamp.
        self._serve_requests()
        if self._stopping.is_set() or self._link is None:
            return
        fast_rows: dict[int | None, dict[str, Any]] = {}
        self._fast_at = {}
        for step in self._plan.fast:
            if self._stopping.is_set() or self._link is None:
                return
            self._run_step(step, fast_rows)
        if self._slots and self._link is not None:
            sent = self._now()
            scope_ops.read_slots(self, fast_rows)
            done = self._now()
            self._rtt_ms = (done - sent) / _MS / max(1, len(self._slots))
            for ch in fast_rows:
                self._fast_at.setdefault(ch, (sent + done) // 2)
        now = self._now()
        for ch, values in fast_rows.items():
            self._record(ch, values, self._fast_at.get(ch, now), fast=True)
        self._fast_times.append(now)
        while self._fast_times and self._fast_times[0] < now - _RATE_WINDOW_NS:
            self._fast_times.popleft()
        self._poll_table(now)
        if now >= self._next_status_ns:
            self._next_status_ns = now + _STATUS_NS
            self._emit_status(connected=True)
            self.health = self._open_health()
        # A fixed schedule: the next cycle is due one interval after this one was, so the rate
        # holds whatever each cycle took, and a cycle that ran late does not delay the next.
        self._next_fast_ns = max(self._next_fast_ns + self._interval_ns, start)
        # Slow steps fill the time before the next fast pass, so the fast readings keep their
        # spacing; one runs every cycle regardless, so a round ends even with no time to spare.
        longest = 0
        while self._slow is not None and not self._stopping.is_set():
            began = self._now()
            if longest and began + longest > self._next_fast_ns:
                break
            self._slow_step()
            longest = max(longest, self._now() - began)
        self._serve_requests()
        remaining_s = (self._next_fast_ns - self._now()) / _S
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
                    self._record(step.channel, values, self._now(), fast=False)
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
            self._timeouts += 1
            self._timeouts_total += 1
            self._log_once(f"timeout {step.key}", f"{step.query}: {e}")
            if self._timeouts >= _TIMEOUTS_BEFORE_RECONNECT:
                raise LinkError(f"{self._timeouts} queries in a row got no reply") from e
            # The reply may still come; it must not land under the next query's field.
            try:
                self._resync(f"{step.query} got no reply")
            except LinkError:
                self._clear_link()
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
            return False
        found = 0
        for _ in range(errors.drain):
            try:
                reply = self._query(errors.query)
            except LinkTimeout as e:
                self._recover(f"{errors.query}: {e}")
                return found
            error = engine.read_error(errors, reply)
            if error is None:
                return found
            if error.code is None:
                self._resync(f"unexpected reply {reply.strip()!r} to {errors.query}")
                return found
            found += 1
            self._errors += 1
            self._last_error = (
                f"{error.code},{error.text}" if error.code is not None else error.text
            )
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

    # -- requests ----------------------------------------------------------------------------

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
            return self._state_report()
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
        if kind == "scpi":
            return self._scpi(args["command"], args.get("timeout_s"))
        if kind == "local":
            local = self.profile.link.local
            if not local:
                raise RangeError("this profile has no local command")
            for command in local:
                self._send(command)
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
        if kind == "measure":
            return scope_ops.measure(self, ch, args)
        if kind == "waveform":
            return scope_ops.capture(self, ch, args)
        if kind == "command":
            return self._command(str(args["name"]))
        raise RangeError(f"unknown request {kind}")

    def _setter(self, name: str) -> Setter:
        assert self.profile is not None
        setter = self.profile.setters.get(name)
        if setter is None:
            raise RangeError(f"the profile {self.profile.id} cannot set {name}")
        return setter

    def _set_many(self, ch: int, args: dict[str, Any]) -> dict[str, Any]:
        wanted = {k: v for k, v in args.items() if v is not None and k != "channel"}
        if not wanted:
            raise RangeError("give at least one value to set")
        for name, value in wanted.items():
            # Every value is checked before any goes out, so a refusal leaves nothing changed.
            self._check_setting(ch, name, value)
        results: dict[str, Any] = {}
        try:
            for name, value in wanted.items():
                if name in VALUE_SETTERS:
                    results[name] = self._set_value(ch, name, float(value))
                elif name == "function":
                    results[name] = self._set_function(ch, str(value))["function"]
                elif name in SWITCH_SETTERS:
                    results[name] = self._set_switch(ch, name, bool(value))[name]
                else:
                    results[name] = self._set_label(ch, name, str(value))[name]
        except (RangeError, ParseError, LinkError) as e:
            if results:
                raise RangeError(f"{e}; already set: {', '.join(results)}") from e
            raise
        return results

    def _check_setting(self, ch: int, name: str, value: Any) -> None:
        assert self.profile is not None
        if name in VALUE_SETTERS:
            setter = self._setter(name)
            key = self.profile.range_key(setter)
            range_ = self._channel[ch].ranges.get(key) if key else None
            limit = self.cfg.limits.for_key(key or "")
            if range_ is not None or limit is not None or self.profile.category != "scope":
                # A scope setting without a range (an offset, a trigger level) harms nothing;
                # the readback still has to agree. An output's level must be bounded.
                engine.check_range(float(value), range_, limit, name)
            engine.check_resolution(float(value), range_, name)
        elif name == "function":
            setter = self._setter("function")
            if str(value) not in setter.map:
                raise RangeError(f"function must be one of {', '.join(setter.map)}")
        elif name in SWITCH_SETTERS:
            self._setter(name)
        elif name in LABEL_SETTERS:
            setter = self._setter(name)
            if str(value) not in setter.map:
                raise RangeError(f"{name} must be one of {', '.join(setter.map)}")
        else:
            raise RangeError(f"{name} is not something that can be set")

    def _set_label(self, ch: int, name: str, label: str) -> dict[str, Any]:
        """Set an enumerated setting by its label; the readback must agree."""
        assert self.profile is not None
        setter = self._setter(name)
        word = setter.map.get(label)
        if word is None:
            raise RangeError(f"{name} must be one of {', '.join(setter.map)}")
        self._select(ch if setter.per_channel else None)
        self._send(engine.render(setter.command, ch=ch if setter.per_channel else None, value=word))
        self._after_write(f"set ch{ch} {name} {label}")
        field_name = VERIFY_FIELD[name]
        reader = self.profile.reader(field_name)
        verify = self.profile.verify_query(setter)
        if verify is None or reader is None:
            return {name: label, "unverified": "the profile has no readback"}
        reply = self._query(engine.render(verify, ch=ch if reader.per_channel else None))
        readback = engine.parse_reply(reader, reply, ch)[field_name]
        if readback != label:
            raise RangeError(f"{name} reads back {readback}")
        self._mark_own(ch, field_name, readback)
        return {name: label}

    def _command(self, name: str) -> dict[str, Any]:
        """Send a setter that is a bare command, such as a scope's run or stop."""
        if name not in COMMAND_SETTERS or name == "clear":
            raise RangeError(f"{name} is not a command of this instrument")
        setter = self._setter(name)
        self._select(None)
        for command in setter.commands:
            self._send(engine.render(command, ch=None))
        self._after_write(name)
        return {"ok": True, "command": name}

    def _set_value(self, ch: int, name: str, value: float) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter(name)
        key = self.profile.range_key(setter)
        range_ = self._channel[ch].ranges.get(key) if key else None
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, value=value, range_=range_))
        self._after_write(f"set ch{ch} {name} {engine.format_value(value, range_)}")
        readback: float | None = None
        verify = self.profile.verify_query(setter)
        if verify is not None:
            reply = self._query(engine.render(verify, ch=ch))
            field_name = VERIFY_FIELD.get(name, "")
            reader = self.profile.reader(field_name)
            # The reader knows the reply's scale and form; a bare verify query is a number in
            # the instrument's unit.
            readback = (
                float(engine.parse_reply(reader, reply, ch)[field_name])
                if reader is not None and reader.query == verify
                else engine.parse_float(reply) * setter.scale
            )
            if not engine.verified(value, readback, range_):
                raise RangeError(
                    f"{name} reads back {readback:g} after setting {value:g}; "
                    "the instrument may have clipped it or refused it"
                )
        field_name = VERIFY_FIELD.get(name)
        if field_name is not None and readback is not None:
            self._mark_own(ch, field_name, readback)
        if readback is None:
            return {"set": value, "readback": None, "unverified": "the profile has no readback"}
        return {"set": value, "readback": readback}

    def _set_output(self, ch: int, on: bool) -> dict[str, Any]:
        return self._set_switch(ch, "output", on)

    def _set_switch(self, ch: int, name: str, on: bool) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter(name)
        master = setter.master if on else None
        if master is not None and engine.parse_bool(self._query(master.query)):
            master = None
        if master is not None:
            # Raising the master switches on every armed channel, not only this one.
            armed = [n for n in range(1, self.channels + 1) if n != ch and self._armed(master, n)]
            if armed:
                names = ", ".join(f"ch{n}" for n in armed)
                raise RangeError(
                    f"the master output is off, and raising it would also switch on {names}; "
                    "switch those off first"
                )
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, on=on))
        if master is not None:
            self._send(engine.render(master.command, on=True))
        self._after_write(f"set ch{ch} {name} {'on' if on else 'off'}")
        verify = self.profile.verify_query(setter)
        readback: bool | None = None
        if verify is not None:
            reader = self.profile.reader(name)
            reply = self._query(engine.render(verify, ch=ch))
            readback = (
                bool(engine.parse_reply(reader, reply, ch)[name])
                if reader is not None and reader.query == verify
                else engine.parse_bool(reply)
            )
            if readback != on:
                raise RangeError(f"{name} reads back {'on' if readback else 'off'}")
            self._mark_own(ch, name, readback)
        return {name: on if readback is None else readback}

    def _armed(self, master: Master, ch: int) -> bool:
        self._select(ch)
        return engine.parse_bool(self._query(engine.render(master.armed, ch=ch)))

    def _set_function(self, ch: int, function: str) -> dict[str, Any]:
        assert self.profile is not None
        setter = self._setter("function")
        self._select(ch if setter.per_channel else None)
        self._send(engine.setter_command(setter, ch=ch, function=function))
        self._after_write(f"set ch{ch} function {function}")
        verify = self.profile.verify_query(setter)
        field_name = "mode" if self.profile.category == "load" else "function"
        reader = self.profile.reader(field_name)
        if verify is None or reader is None:
            return {"function": function, "unverified": "the profile has no readback"}
        reply = self._query(engine.render(verify, ch=ch))
        label = engine.parse_reply(reader, reply, ch)[field_name]
        if label != function:
            raise RangeError(f"function reads back {label}")
        self._mark_own(ch, field_name, label)
        return {"function": function}

    def _clear(self, ch: int) -> dict[str, Any]:
        """Clear a latched protection, and report how many queued errors were drained."""
        assert self.profile is not None
        cleared = self._drain_errors(quiet=True)
        setter = self.profile.setters.get("clear")
        if setter is not None:
            self._select(ch if setter.per_channel else None)
            for command in engine.clear_commands(setter, ch=ch):
                self._send(command)
            self._after_write(f"clear ch{ch} protection")
        if self.profile.errors is None:
            return {"ok": True}
        return {"ok": True, "errors_cleared": cleared + self._drain_errors(quiet=True)}

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
        if self._slots:
            rows: dict[int | None, dict[str, Any]] = {}
            scope_ops.read_slots(self, rows)
            values.update(rows.get(ch, {}))
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
            self._queries += 1
            self._link.write(command)
            answer = self._link.read_any(timeout_s)
            if isinstance(answer, bytes):
                self._recent.append((self._now(), command, f"<block {len(answer)} bytes>"))
                self._note("info", f"< <block {len(answer)} bytes>", source="rx")
                result: dict[str, Any] = {"block_bytes": len(answer)}
                if len(answer) <= _BLOCK_RETURN_MAX:
                    result["base64"] = base64.b64encode(answer).decode()
                return result
            self._recent.append((self._now(), command, answer))
            self._note("info", f"< {answer}", source="rx")
            return {"reply": answer}
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
                reported = [error]
                for _ in range(errors.drain - 1):
                    more = engine.read_error(errors, self._query(errors.query))
                    if more is None:
                        break
                    reported.append(more)
                self._errors += len(reported)
                texts = [f"{e.code},{e.text}" if e.code is not None else e.text for e in reported]
                self._last_error = texts[-1]
                self._note("warn", f"{what}: instrument error {'; '.join(texts)}", source="action")
                raise RangeError(f"the instrument reported {'; '.join(texts)}")
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
            "acquisition": dict(self._acquisition) if self._acquisition else None,
            "slots": {
                str(slot): {"channel": ch, "type": field_name}
                for slot, (ch, field_name) in sorted(self._slots.items())
            }
            if self._slots
            else None,
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
