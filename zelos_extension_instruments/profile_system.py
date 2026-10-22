"""The system functions part of the profile format: `[system]`, checked.

Every instrument has the IEEE 488.2 reset, self-test and operation-complete queries unless its
profile says otherwise; the rest are the family's own.
"""

import dataclasses
from typing import TYPE_CHECKING, Any, Literal

from .profile import System

if TYPE_CHECKING:
    from .profile_check import Check


_FRAMINGS: tuple[Literal["block", "bmp", "png"], ...] = ("block", "bmp", "png")


def load_system(c: "Check", raw: Any) -> System:
    """The `[system]` table; a key set to false means the instrument has none."""
    default = System()
    if raw is None:
        return default
    raw = c.table(raw, "system")
    c.unknown(raw, [f.name for f in dataclasses.fields(System)], "system")

    def command(key: str, needs: tuple[str, ...] = ()) -> str | None:
        value = raw.get(key, getattr(default, key))
        if value is None or value is False:
            return None
        if not isinstance(value, str) or not value.strip():
            c.fail(f"system.{key}", "must be a command, or false for none.")
            return None
        c.template(value, set(needs), f"system.{key}")
        if needs and not any(f"{{{n}}}" in value for n in needs):
            c.fail(f"system.{key}", f"must contain {' or '.join(f'{{{n}}}' for n in needs)}.")
        return value

    def commands(key: str) -> tuple[str, ...]:
        if raw.get(key) is False:
            return ()
        found = c.commands(raw, key, "system")
        for one in found:
            c.template(one, set(), f"system.{key}")
        return found

    def seconds(key: str, low: float, high: float) -> float:
        fallback = getattr(default, key)
        value = c.number(raw, key, fallback, "system")
        if value is None or not low <= value <= high:
            c.fail(f"system.{key}", f"must be from {low:g} to {high:g} seconds.")
            return fallback
        return value

    ok_text = c.string(raw, "self_test_ok", default.self_test_ok.pattern, "system")
    save = command("save", ("slot",))
    recall = command("recall", ("slot",))
    slots: tuple[int, int] | None = None
    if (slots_raw := raw.get("slots")) is not None:
        table = c.table(slots_raw, "system.slots")
        c.unknown(table, ("min", "max"), "system.slots")
        if "min" not in table or "max" not in table:
            c.fail("system.slots", "needs min and max.")
        low = c.integer(table, "min", 0, "system.slots")
        high = c.integer(table, "max", low, "system.slots")
        if high < low:
            c.fail("system.slots", "max must be at least min.")
        slots = (low, high)
        if save is None and recall is None:
            c.fail("system.slots", "is only for save and recall.")
    elif save is not None or recall is not None:
        c.fail("system.slots", "missing: save and recall need { min, max }.")
    display_text = command("display_text", ("text",))
    text_max: int | None = None
    if "display_text_max" in raw:
        text_max = c.integer(raw, "display_text_max", 1, "system", low=1)
        if display_text is None:
            c.fail("system.display_text_max", "is only for display_text.")
    elif display_text is not None:
        c.fail("system.display_text_max", "missing: display_text needs it.")
    lock = command("lock")
    unlock = command("unlock")
    if (lock is None) != (unlock is None):
        c.fail("system", "lock and unlock come together.")
    screenshot = commands("screenshot")
    return System(
        reset=command("reset"),
        self_test=command("self_test"),
        self_test_ok=c.pattern(ok_text or "", "system.self_test_ok", default.self_test_ok.pattern),
        self_test_timeout_s=seconds("self_test_timeout_s", 1, 600),
        opc=command("opc"),
        settle_timeout_s=seconds("settle_timeout_s", 1, 120),
        save=save,
        recall=recall,
        slots=slots,
        beep=command("beep"),
        display=command("display", ("on", "bit")),
        display_text=display_text,
        display_text_max=text_max,
        display_clear=command("display_clear"),
        lock=lock,
        unlock=unlock,
        reboot=command("reboot"),
        reboot_s=seconds("reboot_s", 1, 600),
        screenshot=screenshot,
        screenshot_framing=c.choice(raw, "screenshot_framing", _FRAMINGS, "block", "system"),
        screenshot_cleanup=commands("screenshot_cleanup"),
        setup_query=command("setup_query"),
        setup_load=command("setup_load"),
    )


def system_commands(system: System) -> tuple[str, ...]:
    """Every command the system functions send."""
    single = (
        system.reset,
        system.self_test,
        system.opc,
        system.save,
        system.recall,
        system.beep,
        system.display,
        system.display_text,
        system.display_clear,
        system.lock,
        system.unlock,
        system.reboot,
        system.setup_query,
        system.setup_load,
    )
    return (
        *(one for one in single if one is not None),
        *system.screenshot,
        *system.screenshot_cleanup,
    )
