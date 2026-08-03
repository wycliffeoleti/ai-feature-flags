"""Injectable time.

Rollout plans are written in real durations — 1% for 2 hours, then 5% for 6. Two
consumers need those same durations to pass faster than real time:

* tests, which must be deterministic and instant, use :class:`FakeClock`;
* the portfolio demo, which must show a multi-day ramp in under four minutes,
  uses :class:`ScaledClock`.

Because the controller only ever reads ``clock.now()``, neither case requires a
second code path or a "demo mode" branch in the rollout logic. The plan stays
written in hours in both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of the current instant, always timezone-aware UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """Real wall-clock time."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when a test tells it to."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, amount: float | timedelta) -> None:
        """Move time forward by seconds or a :class:`~datetime.timedelta`."""
        delta = amount if isinstance(amount, timedelta) else timedelta(seconds=amount)
        if delta < timedelta(0):
            raise ValueError("time must not run backwards")
        self._now += delta


class ScaledClock:
    """Multiplies elapsed time from an underlying clock.

    With ``factor=3600`` a real second becomes a simulated hour, so the guide's
    1%/2h → 100% ramp completes in about a minute and a half of real time while
    the plan itself still reads in hours.
    """

    __slots__ = ("_base", "_factor", "_origin", "_base_origin")

    def __init__(self, base: Clock, factor: float) -> None:
        if factor <= 0.0:
            raise ValueError("scale factor must be positive")
        self._base = base
        self._factor = factor
        self._base_origin = base.now()
        self._origin = self._base_origin

    def now(self) -> datetime:
        elapsed = (self._base.now() - self._base_origin).total_seconds()
        return self._origin + timedelta(seconds=elapsed * self._factor)
