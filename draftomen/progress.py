"""Print sparse progress lines for long admin commands."""

from __future__ import annotations

from collections.abc import Callable
import time


class ProgressReporter:
    """Print a progress line when another step of the total completes and the interval has passed.
    Completion always prints; without a known total, only the interval applies.
    """

    def __init__(
        self,
        *,
        label: str,
        total: int | None,
        unit: str = "",
        step: float = 0.1,
        interval_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._label = label
        self._total = total if total is not None and total > 0 else None
        self._unit = unit
        self._step = step
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._started = clock()
        self._last_printed_at = self._started
        self._last_step = 0

    def update(self, *, done: int) -> None:
        """Record the completed amount and print only when a new step is reached."""

        now = self._clock()
        too_soon = now - self._last_printed_at < self._interval_seconds
        if self._total is None:
            if too_soon:
                return
        else:
            steps = round(1 / self._step)
            reached = min(done * steps // self._total, steps)
            if reached <= self._last_step or (too_soon and reached < steps):
                return
            self._last_step = reached
        self._last_printed_at = now
        print(self._line(done=done, now=now), flush=True)

    def _line(self, *, done: int, now: float) -> str:
        elapsed = f"{now - self._started:.0f}s"
        if self._total is None:
            return f"{self._label}: {_amount(done, unit=self._unit)} after {elapsed}"
        percent = min(100, int(done * 100 / self._total))
        return (
            f"{self._label}: {percent}% "
            f"({_amount(done, unit=self._unit)} of {_amount(self._total, unit=self._unit)}) "
            f"after {elapsed}"
        )


def _amount(value: int, *, unit: str) -> str:
    if unit == "bytes":
        return f"{value / 1_000_000:,.1f} MB"
    return f"{value:,}{f' {unit}' if unit else ''}"


__all__ = ["ProgressReporter"]

