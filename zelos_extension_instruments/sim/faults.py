"""Misbehavior a FakeServer applies to its replies, to exercise a client's recovery."""

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Reply:
    """What the server does with one reply."""

    stall: float = 0.0
    garbage: bool = False
    half: bool = False
    close_before: bool = False
    close_after: bool = False


class Faults:
    """The faults a FakeServer consults before each reply; safe to change from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._restart_hooks: list[Callable[[], None]] = []
        self.refuse_second_client = False
        self._drop_after: int | None = None
        self._stall = 0.0
        self._garbage = False
        self._half = False

    def drop_after(self, n: int) -> None:
        """Close the connection once `n` more replies are sent; 0 closes before the next."""
        if n < 0:
            raise ValueError("n must be at least 0")
        with self._lock:
            self._drop_after = n

    def stall(self, seconds: float) -> None:
        """Delay every reply by `seconds`; 0 stops stalling."""
        if seconds < 0:
            raise ValueError("seconds must be at least 0")
        with self._lock:
            self._stall = seconds

    def garbage_once(self) -> None:
        """Send bytes that are not text in place of the next reply."""
        with self._lock:
            self._garbage = True

    def half_reply_once(self) -> None:
        """Send the first half of the next reply, then close the connection."""
        with self._lock:
            self._half = True

    def restart(self) -> None:
        """Close every client connection, as a power cycle would; the instrument keeps its state."""
        for hook in list(self._restart_hooks):
            hook()

    def reset(self) -> None:
        """Clear every fault."""
        with self._lock:
            self.refuse_second_client = False
            self._drop_after = None
            self._stall = 0.0
            self._garbage = False
            self._half = False

    def next_reply(self) -> Reply:
        """The faults for the reply about to be sent; consumes the one-shot ones."""
        with self._lock:
            if self._drop_after == 0:
                self._drop_after = None
                return Reply(stall=self._stall, close_before=True)
            close_after = False
            if self._drop_after is not None:
                self._drop_after -= 1
                close_after = self._drop_after == 0
                if close_after:
                    self._drop_after = None
            reply = Reply(self._stall, self._garbage, self._half, False, close_after)
            self._garbage = self._half = False
            return reply
