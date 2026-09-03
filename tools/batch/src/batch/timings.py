from __future__ import annotations

import time
from collections.abc import Callable


class Timings:
    """Live durations for the dashboard, held in memory: no state file exists,
    and nothing in labels, branches, or PRs records when a VM started."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic: Callable[[], float] = monotonic
        self._started: dict[int, float] = {}
        self._totals: dict[int, float] = {}

    def start(self, issue: int) -> None:
        self._started[issue] = self._monotonic()
        _ = self._totals.pop(issue, None)

    def finish(self, issue: int) -> None:
        started = self._started.pop(issue, None)
        if started is not None:
            self._totals[issue] = self._monotonic() - started

    def elapsed(self, issue: int) -> float | None:
        started = self._started.get(issue)
        if started is not None:
            return self._monotonic() - started
        return self._totals.get(issue)
