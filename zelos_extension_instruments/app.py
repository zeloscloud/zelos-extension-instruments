"""The extension's life cycle: one InstrumentWorker per configured instrument until SIGTERM.

Given a `source`, run() writes there and never connects to the agent; given a `stop` event, it
installs no signal handlers.
"""

import dataclasses
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import zelos_sdk
from zelos_sdk.hooks.logging import TraceLoggingHandler

from . import ACTION_PREFIX, actions, config
from .config import ConfigError, InstrumentConfig, Settings
from .emitter import Emitter
from .naming import NAME_BYTES, unique_name
from .profile import Profile, ProfileError, load_dir, shipped
from .worker import InstrumentWorker, Opener

logger = logging.getLogger(__name__)


def run(
    source: zelos_sdk.TraceSource | None = None,
    *,
    settings: Settings | None = None,
    profiles: list[Profile] | None = None,
    open: Opener | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Run the extension until `stop` is set, or until SIGTERM or SIGINT."""
    if settings is None:
        try:
            settings = config.load()
        except ConfigError as e:
            # The supervisor does not restart the extension: these lines are the user's feedback.
            for problem in e.problems:
                logger.error("%s", problem)
            sys.exit(1)
    if profiles is None:
        try:
            profiles = load_profiles()
        except ProfileError as e:
            for problem in e.problems:
                logger.error("%s: %s", e.path, problem)
            sys.exit(1)
    if source is None:
        source = zelos_sdk.init_global_source(settings.prefix)
        zelos_sdk.init(name=ACTION_PREFIX, actions=True)
    if stop is None:
        stop = _stop_on_signals()

    handler = TraceLoggingHandler(source)
    handler.setLevel(settings.log_level)
    handler.addFilter(lambda record: not record.name.startswith("zelos_sdk.actions"))
    root = logging.getLogger()
    root.addHandler(handler)
    logging.getLogger(__package__).setLevel(settings.log_level)

    emitter = Emitter(source)
    actions.workers.clear()
    try:
        for cfg in _with_default_names(settings.instruments):
            worker = _worker(cfg, profiles, emitter, settings, open)
            worker.start()
            actions.workers[cfg.name] = worker
        # A timed wait: an untimed Event.wait() may not wake for Ctrl-C on Windows.
        while not stop.wait(0.5):
            pass
    finally:
        workers = list(actions.workers.values())
        for worker in workers:
            worker.stop()
        deadline = time.monotonic() + 1.0
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        source.flush()
        root.removeHandler(handler)


def _worker(
    cfg: InstrumentConfig,
    profiles: list[Profile],
    emitter: Emitter,
    settings: Settings,
    open: Opener | None,
) -> InstrumentWorker:
    if cfg.connection == "demo":
        from .demo import CHANNELS, demo_opener

        cfg = dataclasses.replace(cfg, channels=cfg.channels or CHANNELS[cfg.category])
        return InstrumentWorker(cfg, profiles, emitter, settings, open=demo_opener(cfg))
    if open is None:
        return InstrumentWorker(cfg, profiles, emitter, settings)
    return InstrumentWorker(cfg, profiles, emitter, settings, open=open)


def load_profiles() -> list[Profile]:
    """The shipped profiles, then the user's own from the data directory's `profiles/`."""
    profiles = shipped()
    data_dir = os.environ.get("ZELOS_DATA_DIR")
    if data_dir and (user_dir := Path(data_dir) / "profiles").is_dir():
        own = load_dir(user_dir)
        ids = {p.id for p in own}
        profiles = [p for p in profiles if p.id not in ids] + own
    return profiles


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    return stop


def _with_default_names(instruments: tuple[InstrumentConfig, ...]) -> list[InstrumentConfig]:
    """A name for every instrument from the config alone, so data lands in one place."""
    used = {cfg.name for cfg in instruments if cfg.name}
    named: list[InstrumentConfig] = []
    for cfg in instruments:
        if not cfg.name:
            cfg = dataclasses.replace(cfg, name=unique_name(cfg.where, used, max_bytes=NAME_BYTES))
        named.append(cfg)
    return named
