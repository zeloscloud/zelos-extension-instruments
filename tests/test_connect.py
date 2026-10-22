"""Asking an instrument for its identity when it opens."""

import pytest

from tests.conftest import Dp832, ScriptedLink, start_open
from zelos_extension_instruments.link import LinkTimeout
from zelos_extension_instruments.profile import Profile


@pytest.mark.parametrize("first", ["silent", "Invalid Character"])
def test_identity_is_asked_again_after_a_silent_or_garbled_first_reply(
    first: str, dp800: Profile, dp832: Dp832
) -> None:
    """An HMP ignores the first *IDN? after a connect; stale bytes made a GPD answer Invalid
    Character; sigrok asks again."""
    original = dp832.__call__
    asked = {"n": 0}

    def wakes_late(command: str) -> str | None:
        if command == "*IDN?":
            asked["n"] += 1
            if asked["n"] == 1:
                if first == "silent":
                    raise LinkTimeout("no reply")
                return first
        return original(command)

    link = ScriptedLink(wakes_late)
    worker, _ = start_open(dp800, link)
    try:
        assert worker.idn is not None and worker.idn.model == "DP832A"
        assert link.cleared >= 1
    finally:
        worker.stop()
        worker.join(2)
