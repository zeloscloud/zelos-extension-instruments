"""Transcripts recorded from real instruments replay against their profiles.

A profile earns the transcript badge only when every query of its plan appears in a transcript
recorded from a unit, and every recorded reply parses. The recorder writes the files; nothing
here is derived from a profile.
"""

from pathlib import Path

import pytest

from zelos_extension_instruments import engine
from zelos_extension_instruments.profile import by_id, parse_idn
from zelos_extension_instruments.profile_load import shipped
from zelos_extension_instruments.record import TRANSCRIPTS, load_transcript

FILES = sorted(TRANSCRIPTS.rglob("*.txt")) if TRANSCRIPTS.is_dir() else []


def _header(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and ": " in line:
            key, _, value = line[2:].partition(": ")
            found.setdefault(key, value)
    return found


@pytest.mark.parametrize("path", FILES, ids=[f"{p.parent.name}/{p.stem}" for p in FILES])
def test_transcript_replays_against_its_profile(path: Path) -> None:
    header = _header(path)
    profile = by_id(shipped(), header["profile"])
    assert profile is not None, f"{path} names an unknown profile"
    idn = parse_idn(header["identity"])
    assert profile.matches(idn), f"{profile.id} does not match {idn.text}"
    channels = int(header.get("channels") or profile.channels_for(idn.model))
    replies = {command: reply for command, reply in load_transcript(path) if reply is not None}
    plan = engine.compile_plan(profile, channels)
    missing = [step.query for step in plan if step.query not in replies]
    assert not missing, f"queries never recorded: {missing}"
    for step in plan:
        reply = replies[step.query]
        if step.flag is not None:
            engine.flag_is_set(step.flag, reply, step.channel)
        else:
            engine.parse_reply(step.reader, reply, step.channel)
    if profile.errors is not None and profile.errors.query in replies:
        engine.read_error(profile.errors, replies[profile.errors.query])


def test_transcript_badges_match_the_files() -> None:
    """A profile with a transcript carries the badge, and one without does not."""
    recorded = {p.parent.name for p in FILES}
    for profile in shipped():
        if profile.evidence.badge == "transcript":
            assert profile.id in recorded, f"{profile.id} claims a transcript it does not have"
        if profile.evidence.badge == "manual":
            assert profile.id not in recorded, f"{profile.id} has a transcript; set its badge"
