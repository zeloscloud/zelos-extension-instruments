"""Asking an instrument for its identity when it opens."""

import dataclasses

import pytest

from tests.conftest import Dp832, ScriptedLink, instrument_config, start_open
from zelos_extension_instruments.connect import link_settings
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


@pytest.mark.parametrize("skip_blank", [True, False])
def test_the_link_skips_blank_lines_when_the_profile_says_so(
    skip_blank: bool, dp800: Profile
) -> None:
    profile = dataclasses.replace(
        dp800, link=dataclasses.replace(dp800.link, skip_blank=skip_blank)
    )
    assert link_settings(instrument_config(), profile).skip_blank is skip_blank
