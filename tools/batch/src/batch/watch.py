from __future__ import annotations

from collections.abc import Callable, Sequence

from batch.models import IssueOutcome, RunResult

DEFAULT_WATCH_INTERVAL = 60.0


def watch(
    run: Callable[[], RunResult],
    waiting: Callable[[], tuple[int, ...]],
    *,
    sleep: Callable[[float], None],
    report: Callable[[RunResult], None],
    announce: Callable[[Sequence[int]], None],
    interval: float,
) -> RunResult:
    """Repeat `run` while any target still holds work planning has yet to release.

    `report` sees each pass that did something; `announce` fires once per idle
    streak, and again whenever the set of targets held up changes.
    """
    outcomes: list[IssueOutcome] = []
    targets: tuple[int, ...] = ()
    announced: tuple[int, ...] = ()
    while True:
        result = run()
        targets = result.targets
        outcomes.extend(result.outcomes)
        if result.outcomes:
            report(result)
            announced = ()
        pending = () if result.halted is not None else waiting()
        if not pending:
            return RunResult(
                targets=targets,
                outcomes=tuple(outcomes),
                anomalies=result.anomalies,
            )
        if pending != announced:
            announce(pending)
            announced = pending
        sleep(interval)
