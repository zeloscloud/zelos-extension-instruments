"""The line and block reader every link shares."""

from zelos_extension_instruments.link import Lines


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
