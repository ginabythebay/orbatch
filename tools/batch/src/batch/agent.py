from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from batch.body import has_test_plan
from batch.models import (
    Batch,
    BatchIssue,
    BatchLabel,
    DroppedChild,
    NextIssue,
    PlanRefusal,
    PlanWritten,
)
from batch.order import predecessors

_CLAIMABLE = frozenset({BatchLabel.QUEUED, BatchLabel.PLANNED})


class State(Protocol):
    def batch(self, targets: Sequence[int]) -> Batch: ...
    def set_state(self, issue_number: int, label: BatchLabel) -> None: ...


class PlanningAgent:
    """The verbs a planning session calls to walk its targets' queued issues."""

    def __init__(self, state: State) -> None:
        self._state: State = state

    def next_issue(self, targets: Sequence[int]) -> NextIssue | None:
        """The next queued issue with no plan yet; queued ones that already have
        a plan are advanced on the way past, so a restart resumes rather than
        re-plans.
        """
        while True:
            batch = self._state.batch(targets)
            nxt = _next_queued(batch)
            if nxt is None:
                return None
            if has_test_plan(nxt.body):
                self._state.set_state(nxt.number, BatchLabel.PLANNED)
                continue
            return NextIssue(
                number=nxt.number,
                title=nxt.title,
                body=nxt.body,
                predecessors=predecessors(batch, nxt),
            )

    def anomalies(self, targets: Sequence[int]) -> tuple[DroppedChild, ...]:
        return self._state.batch(targets).anomalies

    def plan_written(self, targets: Sequence[int], issue_number: int) -> PlanWritten:
        """Checks the agent's claim against the issue as GitHub holds it now.

        Re-planning an issue `next_issue` already advanced is a confirming
        no-op; anything past `planned` is refused, so a mistyped number
        cannot demote live work.
        """
        found = _find(self._state.batch(targets), issue_number)
        if found is None:
            return PlanWritten(number=issue_number, refusal=PlanRefusal.NOT_IN_BATCH)
        if found.state not in _CLAIMABLE:
            return PlanWritten(
                number=issue_number,
                state=found.state,
                refusal=PlanRefusal.WRONG_STATE,
            )
        if not has_test_plan(found.body):
            return PlanWritten(
                number=issue_number, state=found.state, refusal=PlanRefusal.NO_PLAN
            )
        if found.state is BatchLabel.QUEUED:
            self._state.set_state(issue_number, BatchLabel.PLANNED)
        return PlanWritten(number=issue_number, state=BatchLabel.PLANNED)


def _next_queued(batch: Batch) -> BatchIssue | None:
    return next(
        (issue for issue in batch.issues if issue.state is BatchLabel.QUEUED), None
    )


def _find(batch: Batch, issue_number: int) -> BatchIssue | None:
    return next((issue for issue in batch.issues if issue.number == issue_number), None)
