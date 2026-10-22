"""How a worker opens an instrument: the link, the identity and the profile that matches it, the
channels, the poll plan and the optional queries it answers. Functions of the worker, run on its
thread."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from . import connect, daq_ops, engine
from .engine import ParseError, RangeError, Selector, Step
from .link import Link, LinkError, LinkTimeout
from .profile import (
    INSTRUMENT_FIELDS,
    Idn,
    Profile,
    Reader,
    by_id,
    find,
    is_identity,
    parse_idn,
)
from .profile_load import load_file

if TYPE_CHECKING:
    from .worker import InstrumentWorker


def open_instrument(worker: InstrumentWorker) -> bool:
    """Open the link and identify the instrument; False with the health set when it fails."""
    profile = _chosen_profile(worker)
    if profile is None:
        return False
    link_settings = connect.link_settings(worker.cfg, profile)
    worker._link_timeout_s = link_settings.timeout_s
    address = connect.address(worker.cfg, profile)
    worker.where = worker.cfg.where if worker.cfg.connection == "demo" else address.where
    try:
        link = worker._open(address, link_settings)
    except LinkError as e:
        worker._failed_open(e)
        return False
    worker._link = link
    if worker._stopping.is_set():
        worker._close()
        return False
    worker._selector = Selector(profile.link.select)
    # A banner, or bytes a previous client left behind, must not become the identity.
    link.clear()
    try:
        if profile.link.first:
            connect.send_first(link, profile)
        idn = _identify(worker, link, profile)
    except (LinkError, RangeError) as e:
        worker._close()
        worker._failed_open(e if isinstance(e, LinkError) else LinkError(str(e)))
        return False
    if worker._stopping.is_set():
        worker._close()
        return False
    try:
        profile = _resolve_profile(worker, profile, idn)
        wanted = connect.link_settings(worker.cfg, profile)
        worker._selector = Selector(profile.link.select)
        worker._link_timeout_s = wanted.timeout_s
        if wanted != link_settings:
            # The matched profile frames lines differently from the one used to identify.
            link.close()
            worker._link = link = worker._open(connect.address(worker.cfg, profile), wanted)
        worker.profile, worker.idn = profile, idn
        if profile.link.remote:
            worker._send(profile.link.remote)
        if not _remote_held(worker, profile):
            return False
        for command in profile.link.init:
            worker._send(command)
        if profile.daq is not None:
            channels = daq_ops.channels(worker.cfg)
        else:
            count = worker.cfg.channels or _channel_count(worker, profile, idn)
            channels = list(range(1, count + 1))
        worker._install(profile, idn, channels)
        # Errors another program or a power cycle left in the queue are not this session's.
        worker._drain_errors(quiet=True)
        if profile.daq is not None and not _set_up_scan(worker):
            return False
        _read_ranges(worker)
        # An error a range query raised must not be pinned on the first optional query.
        worker._drain_errors(quiet=True)
        _probe_optional(worker)
        worker._emitter.add_channels(worker.cfg.name, channel_fields(worker))
        worker._emitter.identity(
            worker.cfg.name,
            {
                "manufacturer": idn.manufacturer if idn else "",
                "model": idn.model if idn else "",
                "serial": idn.serial if idn else "",
                "firmware": idn.firmware if idn else "",
                "profile": profile.id,
                "address": worker.where,
            },
            worker._clock.epoch_ns(worker._now()),
        )
    except (LinkError, RangeError, ParseError) as e:
        worker._lost(str(e))
        return False
    worker._timeouts = 0
    worker._bad_scans = 0
    worker._cycles_since_connect = 0
    # A panel lock does not outlast the session or a reboot that ended it.
    worker._panel_locked = False
    worker.state = "open"
    worker.health = worker._open_health()
    worker._note("info", f"connected to {worker.where}: {worker.idn.text if worker.idn else ''}")
    return True


def _remote_held(worker: InstrumentWorker, profile: Profile) -> bool:
    """Whether the instrument took remote control, for a family that says so when asked."""
    if profile.link.remote_check is None:
        return True
    query, expect = profile.link.remote_check
    reply = worker._query(query).strip()
    if reply == expect:
        return True
    worker._close()
    worker._failed_open(
        LinkError(f"the instrument refused remote control ({query} answered {reply})")
    )
    return False


def _set_up_scan(worker: InstrumentWorker) -> bool:
    """Check the configured addresses and set the scan up; a refusal is the config's to fix."""
    try:
        daq_ops.check(worker)
        daq_ops.setup(worker)
        worker._after_write("scan set up", log=False)
    except RangeError as e:
        worker._close()
        worker._failed_open(LinkError(str(e), "config"))
        return False
    return True


def _chosen_profile(worker: InstrumentWorker) -> Profile | None:
    """The profile named in the config, or a generic one to identify with."""
    choice = worker.cfg.profile
    if worker.cfg.connection == "demo":
        return by_id(worker._profiles, f"generic-{worker.cfg.category}") or worker._profiles[0]
    if choice == "auto":
        return by_id(worker._profiles, "ieee488") or worker._profiles[0]
    if choice.endswith(".toml"):
        try:
            return load_file(choice)
        except Exception as e:
            worker.state = "failed"
            worker.health = f"Cannot load the profile {choice}: {e}"
            return None
    profile = by_id(worker._profiles, choice)
    if profile is None:
        worker.state = "failed"
        worker.health = f"No profile is named {choice}. Pick one from the list, or auto."
    return profile


def _identify(worker: InstrumentWorker, link: Link, profile: Profile) -> Idn | None:
    command = profile.match.idn
    try:
        reply = connect.ask_identity(link, command)
    except LinkTimeout:
        text = f"accepted the connection but did not answer {command}"
        if worker.cfg.profile == "auto":
            text += "; an instrument that speaks a dialect without it needs its profile picked"
        raise LinkError(text, "held") from None
    if not is_identity(reply):
        raise LinkError(f"{worker.where} answered {command} with {reply!r}")
    return parse_idn(reply)


def _resolve_profile(worker: InstrumentWorker, chosen: Profile, idn: Idn | None) -> Profile:
    """`auto` picks the matching profile; a chosen one is kept and the identity checked."""
    if worker.cfg.profile != "auto" or worker.cfg.connection == "demo":
        if idn is not None and not chosen.generic and not chosen.matches(idn):
            worker._note(
                "warn",
                f"{idn.manufacturer} {idn.model} does not match the profile {chosen.id}; "
                "using it anyway",
            )
        return chosen
    assert idn is not None
    matched = find(worker._profiles, idn, worker.cfg.connection)
    if matched is None:
        raise LinkError(
            f"no profile matches {idn.manufacturer} {idn.model}; choose a generic profile "
            "for its category, or write one"
        )
    return matched


def _channel_count(worker: InstrumentWorker, profile: Profile, idn: Idn | None) -> int:
    if profile.match.channels_query is not None:
        try:
            return max(1, engine.parse_int(worker._query(profile.match.channels_query)))
        except (ParseError, LinkTimeout) as e:
            worker._recover(f"{profile.match.channels_query}: {e}")
    return profile.channels_for(idn.model if idn else "")


def plan(worker: InstrumentWorker, profile: Profile, dropped: set[str]) -> engine.Plan:
    """The poll plan: a daq unit reads its scan channels' signals and its relays' state."""
    if profile.daq is None:
        return engine.compile_plan(profile, worker.channels, dropped)
    scan = sorted(row.channel for row in worker.cfg.scan)
    relays = sorted(worker.cfg.relays)
    state = profile.state
    relay = profile.setters.get("relay")
    if profile.reader("closed") is None and relay is not None and relay.verify is not None:
        state = (*state, _closed_reader(relay.verify, relay.per_channel))
    fast = [
        Step("fast", ch, reader)
        for reader in profile.signals
        for ch in (scan if reader.per_channel else [None])
    ]
    slow = [
        Step("slow", ch, reader, flag)
        for reader in state
        for ch in (relays if reader.per_channel else [None])
        for flag in (reader.flags or (None,))
    ]
    return engine.Plan(
        tuple(s for s in fast if s.key not in dropped),
        tuple(s for s in slow if s.key not in dropped),
    )


def _read_ranges(worker: InstrumentWorker) -> None:
    """Fill ranges that come from queries."""
    assert worker.profile is not None
    for ch, state in worker._channel.items():
        for key, r in list(state.ranges.items()):
            if r.min_query is None and r.max_query is None:
                continue
            low, high = r.min, r.max
            try:
                worker._select(ch)
                if r.max_query is not None:
                    high = engine.parse_float(worker._query(engine.render(r.max_query, ch=ch)))
                if r.min_query is not None:
                    low = engine.parse_float(worker._query(engine.render(r.min_query, ch=ch)))
            except (ParseError, LinkTimeout) as e:
                worker._recover(f"range {key}: {e}")
                continue
            if high is not None and math.isfinite(high):
                state.ranges[key] = r.with_bounds(
                    low if low is None or math.isfinite(low) else 0.0, high
                )


def channel_fields(worker: InstrumentWorker) -> dict[int, set[str]]:
    """The fields each channel's readers produce, once the probe has dropped what the
    instrument refuses; an instrument-wide reader's fields land on every channel."""
    assert worker._plan is not None and worker.profile is not None
    shared = INSTRUMENT_FIELDS[worker.profile.category]
    fields: dict[int, set[str]] = {ch: set() for ch in worker._channel}
    for step in worker._plan:
        names = {"protection"} if step.flag is not None else set(step.reader.fields) - set(shared)
        for ch in [step.channel] if step.channel is not None else list(fields):
            fields.setdefault(ch, set()).update(names)
    for row in worker.cfg.scan if worker.profile.daq is not None else ():
        fields[row.channel].add(row.function)
    return fields


def _probe_optional(worker: InstrumentWorker) -> None:
    """Run each optional query once; drop the ones the instrument refuses."""
    assert worker._plan is not None and worker.profile is not None
    dropped: set[str] = set()
    for step in worker._plan:
        if not step.reader.optional:
            continue
        # An event cannot gain a field later, so one bad reply gets a second try.
        for attempt in (1, 2):
            try:
                worker._select(step.channel)
                reply = worker._query(step.query)
                if step.flag is not None:
                    engine.flag_is_set(step.flag, reply, step.channel)
                else:
                    engine.parse_reply(step.reader, reply, step.channel)
                if worker._drain_errors(quiet=True):
                    dropped.add(step.key)
                break
            except (LinkTimeout, ParseError) as e:
                worker._recover(f"{step.query}: {e}")
                if attempt == 2:
                    dropped.add(step.key)
    if dropped:
        names = sorted({key.split("@")[0] for key in dropped})
        worker._note(
            "info", f"not answered by this instrument, so not recorded: {', '.join(names)}"
        )
        worker._dropped = dropped
        worker._plan = plan(worker, worker.profile, dropped)


def _closed_reader(query: str, per_channel: bool) -> Reader:
    """Reads a relay's state through the relay setter's own readback query."""
    return Reader(
        name="closed",
        query=query,
        parse="bool",
        fields=("closed",),
        index=None,
        map={},
        words={},
        mask=(0,),
        shift=(0,),
        hex=False,
        optional=False,
        per_channel=per_channel,
        flags=(),
    )
