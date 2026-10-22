"""The line and block reader every link shares."""

from collections.abc import Callable
from typing import Any

import pytest

from zelos_extension_instruments.link import (
    Address,
    LineLink,
    Lines,
    LinkSettings,
    LinkTimeout,
    SerialAddress,
    TcpAddress,
    VisaAddress,
    definite_block,
    open_link,
    serial,
    tcp,
    visa,
)


def test_a_hex_reply_is_a_number_not_a_block() -> None:
    lines = Lines("\n")
    lines.feed(b"#H1F\n")
    assert lines.starts_block() is False
    assert lines.pop() == "#H1F"
    lines.feed(b"#")
    assert lines.starts_block() is None
    lines.feed(b"3005hello")
    assert lines.starts_block() is True
    assert lines.pop_block() == b"hello"


def test_a_line_ending_that_arrives_after_a_block_is_not_an_empty_reply() -> None:
    lines = Lines("\n")
    lines.feed(b"#3005hello")
    assert lines.pop_block() == b"hello"
    lines.feed(b"\n")
    assert lines.pop() is None
    lines.feed(b"\nNEXT\n")
    assert lines.pop() == "NEXT"


def test_a_line_ending_that_arrives_after_a_block_does_not_hide_the_next_block() -> None:
    lines = Lines("\n")
    lines.feed(b"#13abc")
    assert lines.pop_block() == b"abc"
    lines.feed(b"\n#14wxyz\n")
    assert lines.starts_block() is True
    assert lines.pop_block() == b"wxyz"


def test_a_block_is_held_back_until_its_header_and_payload_are_whole() -> None:
    lines = Lines("\n")
    lines.feed(b"#3")
    assert lines.pop_block() is None
    lines.feed(b"005he")
    assert lines.pop_block() is None
    lines.feed(b"llo\r\n")
    assert lines.pop_block() == b"hello"
    assert lines.pop() is None


def test_open_link_opens_the_kind_of_link_the_address_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, Address, LinkSettings]] = []

    def opener(kind: str) -> Callable[[Address, LinkSettings], Any]:
        def open_(address: Address, settings: LinkSettings) -> str:
            opened.append((kind, address, settings))
            return kind

        return open_

    monkeypatch.setattr(tcp, "open_tcp", opener("tcp"))
    monkeypatch.setattr(serial, "open_serial", opener("serial"))
    monkeypatch.setattr(visa, "open_visa", opener("visa"))
    settings = LinkSettings(timeout_s=0.5)
    addresses = [TcpAddress("10.0.0.5", 5025), SerialAddress("COM3"), VisaAddress("USB0::1::2::S")]

    assert [open_link(address, settings) for address in addresses] == ["tcp", "serial", "visa"]
    assert opened == [
        ("tcp", addresses[0], settings),
        ("serial", addresses[1], settings),
        ("visa", addresses[2], settings),
    ]


def test_a_blank_line_is_a_reply_unless_the_link_skips_blank_lines() -> None:
    kept = Lines("\n")
    kept.feed(b"\n1.5\n")
    assert kept.pop() == ""
    skipping = Lines("\n", skip_blank=True)
    skipping.feed(b"\n")
    assert skipping.pop() is None
    skipping.feed(b" \r\n1.5\n\n")
    assert skipping.pop() == "1.5"
    assert skipping.pop() is None


def test_a_skipped_blank_line_does_not_hide_a_block() -> None:
    lines = Lines("\n", skip_blank=True)
    lines.feed(b"\n#15hello\n")
    assert lines.starts_block() is True
    assert lines.pop_block() == b"hello"


class _Fed(LineLink):
    """A line link that receives the chunks it is given."""

    def __init__(self, chunks: list[bytes], settings: LinkSettings) -> None:
        super().__init__("fed", settings)
        self._chunks = chunks
        self.sent: list[bytes] = []

    def _send(self, data: bytes) -> None:
        self.sent.append(data)

    def _receive(self, deadline: float, size: int) -> None:
        if not self._chunks:
            raise LinkTimeout("no reply")
        self._lines.feed(self._chunks.pop(0))


def test_a_link_skips_blank_lines_when_its_settings_say_so() -> None:
    assert _Fed([b"\n", b"1.5\n"], LinkSettings()).query("MEAS?") == ""
    assert _Fed([b"\n", b"1.5\n"], LinkSettings(skip_blank=True)).query("MEAS?") == "1.5"


def test_raw_bytes_are_held_back_until_that_many_have_arrived() -> None:
    lines = Lines("\n")
    lines.feed(b"BM\n")
    assert lines.pop_bytes(4) is None
    lines.feed(b"\x00\xff")
    assert lines.pop_bytes(4) == b"BM\n\x00"
    assert lines.pop_bytes(1) == b"\xff"


def test_raw_bytes_after_a_block_skip_the_line_ending_behind_the_block() -> None:
    lines = Lines("\n")
    lines.feed(b"#12ab")
    assert lines.pop_block() == b"ab"
    lines.feed(b"\nBM")
    assert lines.pop_bytes(2) == b"BM"


def test_a_definite_block_carries_its_length_and_the_digits_of_its_length() -> None:
    assert definite_block(b"hello") == b"#15hello"
    assert definite_block(b"x" * 1234) == b"#41234" + b"x" * 1234
    assert definite_block(b"") == b"#10"


def test_a_link_reads_raw_bytes_across_chunks() -> None:
    link = _Fed([b"\x89P", b"NG\r\n", b"\x1a\n"], LinkSettings())
    assert link.read_bytes(8) == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(LinkTimeout):
        link.read_bytes(1)


def test_a_link_writes_a_block_after_the_command_and_ends_it_with_the_terminator() -> None:
    link = _Fed([], LinkSettings(write_terminator="\r\n"))
    link.write_block(":SYST:SET ", b"\n\x00")
    assert link.sent == [b":SYST:SET #12\n\x00\r\n"]
