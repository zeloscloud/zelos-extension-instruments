"""What every action file shares: the configured workers, the instrument and channel fields,
and the call that asks a worker."""

from typing import Any

from zelos_sdk.actions import action

from .worker import InstrumentWorker, RequestKind

#: Instrument name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, InstrumentWorker] = {}

TIMEOUT_S = 10.0
# A wait ends this much before the action's own timeout, so the caller gets the reason.
MARGIN_S = 0.5


with_instrument = action.select(
    "instrument",
    title="Instrument",
    choices=lambda *_: sorted(workers),
    required=False,
)
with_channel = action.integer(
    "channel", title="Channel", minimum=1, maximum=9999, required=False, default=1
)


def ask(
    instrument: str | None, kind: RequestKind, action_timeout_s: float, **args: Any
) -> dict[str, Any]:
    """The worker's reply; its error is raised as it is. Unset `instrument` is the only one."""
    if instrument is None:
        if not workers:
            raise ValueError(
                "no instrument is configured: add one in the extension's settings, or press "
                "Auto-configure there"
            )
        if len(workers) > 1:
            names = ", ".join(sorted(workers))
            raise ValueError(
                f"several instruments are configured; set instrument to one of: {names}"
            )
        instrument = next(iter(workers))
    worker = workers.get(instrument)
    if worker is None:
        raise ValueError(f"no instrument is named {instrument}; configured: {', '.join(workers)}")
    future = worker.request(kind, **args)
    wait_s = action_timeout_s - MARGIN_S
    try:
        result = future.result(timeout=wait_s)
    except TimeoutError:
        # The worker must not run the request later, after the person was told it failed.
        future.cancel()
        raise RuntimeError(f"{instrument} did not answer within {wait_s:g} s") from None
    if "error" in result and len(result) == 1:
        raise RuntimeError(result["error"])
    return result
