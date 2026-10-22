"""What every action file shares: the configured workers, the instrument and channel fields,
and the call that asks a worker."""

from typing import Any

from zelos_sdk.actions import SelectField, action, register_field_type

from .worker import InstrumentWorker, RequestKind

#: Instrument name to its worker; app.run() fills it before the main loop starts.
workers: dict[str, InstrumentWorker] = {}

TIMEOUT_S = 10.0
# A wait ends this much before the action's own timeout, so the caller gets the reason.
MARGIN_S = 0.5


def first_instrument() -> str | None:
    return next(iter(workers), None)


@register_field_type("instrument")
class _InstrumentField(SelectField):
    """An instrument name field whose form default is the first configured instrument."""

    def to_json_schema(self, current_values: dict[str, Any] | None = None) -> dict[str, Any]:
        schema = super().to_json_schema(current_values)
        schema["default"] = first_instrument()
        return schema


with_instrument = action.input(
    "instrument",
    type="instrument",
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
    """The worker's reply; its error is raised as it is."""
    if instrument is None:
        instrument = first_instrument()
        if instrument is None:
            raise ValueError(
                "no instrument is configured: add one in the extension's settings, or press "
                "Auto-configure there"
            )
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
