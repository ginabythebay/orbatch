from __future__ import annotations

from collections.abc import Callable, Sequence

from batch.models import (
    BatchLabel,
    DroppedChild,
    HaltReason,
    IssueOutcome,
    RunResult,
)
from batch.watch import watch

EPIC = 1492


def _outcome(number: int, halt: HaltReason | None = None) -> IssueOutcome:
    state = BatchLabel.STUCK if halt else BatchLabel.READY_FOR_REVIEW
    return IssueOutcome(number=number, base="main", state=state, halt=halt)


def _pass(*outcomes: IssueOutcome) -> RunResult:
    return RunResult(targets=(EPIC,), outcomes=outcomes)


class Harness:
    def __init__(
        self, passes: Sequence[RunResult], queued: Sequence[Sequence[int]] = ()
    ) -> None:
        self.passes: list[RunResult] = list(passes)
        self.queued: list[tuple[int, ...]] = [tuple(each) for each in queued]
        self.slept: list[float] = []
        self.reported: list[RunResult] = []
        self.announced: list[tuple[int, ...]] = []
        self.on_sleep: Callable[[], None] | None = None

    def run(self) -> RunResult:
        return self.passes.pop(0)

    def probe(self) -> tuple[int, ...]:
        return self.queued.pop(0)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    def report(self, result: RunResult) -> None:
        self.reported.append(result)

    def announce(self, pending: Sequence[int]) -> None:
        self.announced.append(tuple(pending))

    def watch(self, interval: float = 60.0) -> RunResult:
        return watch(
            self.run,
            self.probe,
            sleep=self.sleep,
            report=self.report,
            announce=self.announce,
            interval=interval,
        )


class TestWatch:
    def test_an_empty_pass_waits_for_the_queued_issue_planning_will_land(self) -> None:
        harness = Harness([_pass(), _pass(_outcome(10))], queued=[(EPIC,), ()])

        result = harness.watch()

        assert len(harness.slept) == 1
        assert [outcome.number for outcome in result.outcomes] == [10]

    def test_an_empty_pass_with_nothing_queued_returns_at_once(self) -> None:
        anomaly = DroppedChild(
            number=1503,
            title="Teardown on merge",
            state="CLOSED",
            labels=(BatchLabel.PLANNED,),
            reason="closed, labelled planned",
        )
        harness = Harness(
            [RunResult(targets=(EPIC,), anomalies=(anomaly,))], queued=[()]
        )

        result = harness.watch()

        assert harness.slept == []
        assert result.outcomes == ()
        assert result.targets == (EPIC,)
        assert result.anomalies == (anomaly,)

    def test_a_pass_that_did_work_is_not_an_exit_condition(self) -> None:
        harness = Harness(
            [_pass(_outcome(10)), _pass(_outcome(11))], queued=[(EPIC,), ()]
        )

        result = harness.watch()

        assert [outcome.number for outcome in result.outcomes] == [10, 11]

    def test_a_halt_returns_even_though_more_work_is_queued(self) -> None:
        harness = Harness(
            [_pass(_outcome(10, HaltReason.VERIFICATION_FAILED))], queued=[(EPIC,)]
        )

        result = harness.watch()

        assert result.halted is HaltReason.VERIFICATION_FAILED
        assert harness.queued == [(EPIC,)]
        assert harness.slept == []

    def test_outcomes_from_successive_passes_aggregate_in_order(self) -> None:
        harness = Harness(
            [_pass(_outcome(10)), _pass(), _pass(_outcome(11), _outcome(12))],
            queued=[(EPIC,), (EPIC,), ()],
        )

        result = harness.watch()

        assert [outcome.number for outcome in result.outcomes] == [10, 11, 12]

    def test_an_empty_pass_reports_nothing(self) -> None:
        harness = Harness([_pass(), _pass(_outcome(10))], queued=[(EPIC,), ()])

        _ = harness.watch()

        assert harness.reported == [_pass(_outcome(10))]

    def test_idle_polls_announce_the_wait_once_per_streak(self) -> None:
        harness = Harness(
            [_pass(), _pass(), _pass(), _pass(_outcome(10)), _pass(), _pass()],
            queued=[(EPIC,), (EPIC,), (EPIC,), (EPIC,), (EPIC,), ()],
        )

        _ = harness.watch()

        assert harness.announced == [(EPIC,), (EPIC,)]
        assert len(harness.slept) == 5

    def test_sleeps_use_the_configured_interval(self) -> None:
        harness = Harness([_pass(), _pass()], queued=[(EPIC,), ()])

        _ = harness.watch(interval=5.0)

        assert harness.slept == [5.0]

    def test_the_announcement_names_the_targets_still_holding_work(self) -> None:
        harness = Harness(
            [_pass(), _pass(), _pass()], queued=[(1771, 1492), (1492,), ()]
        )

        _ = harness.watch()

        assert harness.announced == [(1771, 1492), (1492,)]
