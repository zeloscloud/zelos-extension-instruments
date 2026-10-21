"""Host time for readings."""

import time
from collections.abc import Callable

# Host time follows the wall clock only past this: NTP slews smaller differences.
_ANCHOR_NS = 1_000_000_000


class HostClock:
    """Maps a monotonic reading clock onto the wall clock.

    Readings are stamped from `perf_counter_ns`, which ticks finely on every platform (Python
    3.11's `monotonic_ns` ticks every 15.6 ms on Windows), and anchored to the wall clock once.
    The anchor moves only when the two drift apart by more than a second, so a slewing clock
    never puts samples out of order.
    """

    def __init__(
        self,
        now: Callable[[], int] = time.perf_counter_ns,
        wall: Callable[[], int] = time.time_ns,
    ) -> None:
        self._now = now
        self._wall = wall
        self._offset = wall() - now()

    def now(self) -> int:
        """The monotonic reading clock."""
        return self._now()

    def epoch_ns(self, mono_ns: int) -> int:
        """A reading clock value as wall-clock nanoseconds."""
        offset = self._wall() - self._now()
        if abs(offset - self._offset) > _ANCHOR_NS:
            self._offset = offset
        return mono_ns + self._offset
