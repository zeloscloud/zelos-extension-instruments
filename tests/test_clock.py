from hypothesis import given
from hypothesis import strategies as st

from zelos_extension_instruments.clock import HostClock

SECOND = 1_000_000_000


class Clocks:
    """A monotonic clock and a wall clock the test drives by hand."""

    def __init__(self, mono: int = 5 * SECOND, wall: int = 1_700_000_000 * SECOND) -> None:
        self.mono = mono
        self.wall = wall

    def now(self) -> int:
        return self.mono

    def time(self) -> int:
        return self.wall

    def tick(self, ns: int, wall_extra: int = 0) -> None:
        self.mono += ns
        self.wall += ns + wall_extra

    def clock(self) -> HostClock:
        return HostClock(now=self.now, wall=self.time)


def test_now_is_the_monotonic_clock() -> None:
    clocks = Clocks(mono=42)
    clock = clocks.clock()
    assert clock.now() == 42
    clocks.tick(8)
    assert clock.now() == 50


def test_epoch_maps_a_reading_onto_the_wall_clock() -> None:
    clocks = Clocks(mono=5 * SECOND, wall=1_700_000_000 * SECOND)
    clock = clocks.clock()
    assert clock.epoch_ns(5 * SECOND) == 1_700_000_000 * SECOND
    assert clock.epoch_ns(7 * SECOND) == 1_700_000_002 * SECOND
    assert clock.epoch_ns(4 * SECOND) == 1_699_999_999 * SECOND


def test_a_wall_clock_that_ticks_with_the_monotonic_clock_changes_nothing() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    start = clock.epoch_ns(clocks.now())
    clocks.tick(3 * SECOND)
    assert clock.epoch_ns(clocks.now()) == start + 3 * SECOND


def test_a_slew_under_a_second_keeps_the_anchor() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    anchored = clock.epoch_ns(clocks.now())
    clocks.tick(10 * SECOND, wall_extra=SECOND // 2)
    assert clock.epoch_ns(clocks.now()) == anchored + 10 * SECOND


def test_a_drift_of_exactly_a_second_keeps_the_anchor() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    anchored = clock.epoch_ns(clocks.now())
    clocks.tick(SECOND, wall_extra=SECOND)
    assert clock.epoch_ns(clocks.now()) == anchored + SECOND


def test_a_drift_past_a_second_forward_re_anchors() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    clocks.tick(SECOND, wall_extra=SECOND + 1)
    assert clock.epoch_ns(clocks.now()) == clocks.wall


def test_a_drift_past_a_second_backward_re_anchors() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    clocks.tick(SECOND, wall_extra=-(2 * SECOND))
    assert clock.epoch_ns(clocks.now()) == clocks.wall


def test_a_re_anchor_applies_to_later_readings() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    clocks.tick(SECOND, wall_extra=60 * SECOND)
    clock.epoch_ns(clocks.now())
    clocks.tick(SECOND)
    assert clock.epoch_ns(clocks.now()) == clocks.wall


def test_the_anchor_moves_once_per_jump() -> None:
    clocks = Clocks()
    clock = clocks.clock()
    clocks.tick(SECOND, wall_extra=5 * SECOND)
    jumped = clock.epoch_ns(clocks.now())
    clocks.tick(SECOND, wall_extra=SECOND // 2)
    assert clock.epoch_ns(clocks.now()) == jumped + SECOND


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=SECOND // 10),
            st.integers(min_value=-SECOND // 100, max_value=SECOND // 100),
        ),
        min_size=1,
        max_size=50,
    )
)
def test_stamps_never_go_backwards_while_the_wall_clock_slews(
    steps: list[tuple[int, int]],
) -> None:
    """Wobble of 10 ms over 50 steps drifts under a second."""
    clocks = Clocks()
    clock = clocks.clock()
    stamps = [clock.epoch_ns(clocks.now())]
    for ns, wobble in steps:
        clocks.tick(ns, wall_extra=wobble)
        stamps.append(clock.epoch_ns(clocks.now()))
    assert stamps == sorted(stamps)
