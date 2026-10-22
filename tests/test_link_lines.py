"""The line and block reader every link shares."""

from collections.abc import Callable
from typing import Any

import pytest

from zelos_extension_instruments.link import (
    Address,
    Lines,
    LinkSettings,
    SerialAddress,
    TcpAddress,
    VisaAddress,
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
