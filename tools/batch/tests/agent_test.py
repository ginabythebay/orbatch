from __future__ import annotations

from batch.agent import PlanningAgent
from batch.models import (
    BatchIssue,
    BatchLabel,
    DroppedChild,
    NextIssue,
    PlanRefusal,
    PlanWritten,
)
from batch.testing.payloads import (
    EPIC,
    FakeState,
    batch_issue,
    closed_child,
    unlabeled_child,
)

PLAN = "## Test Plan\n\n1. Something worth testing.\n"


def queued(number: int, *, body: str = "", title: str = "") -> BatchIssue:
    return batch_issue(number, BatchLabel.QUEUED, body=body, title=title)


def agent_over(*issues: BatchIssue) -> tuple[PlanningAgent, FakeState]:
    state = FakeState(*issues)
    return PlanningAgent(state), state


class TestNextIssue:
    def test_hands_over_the_first_queued_issue_and_its_predecessors(self) -> None:
        agent, _ = agent_over(
            batch_issue(8, BatchLabel.READY_FOR_REVIEW),
            batch_issue(9, BatchLabel.PLANNED),
            queued(10, title="Stack manager", body="Do the thing."),
            queued(11),
        )

        assert agent.next_issue((EPIC,)) == NextIssue(
            number=10,
            title="Stack manager",
            body="Do the thing.",
            predecessors=(8, 9),
        )

    def test_an_issue_that_already_has_a_plan_is_advanced_not_handed_over(
        self,
    ) -> None:
        agent, state = agent_over(
            queued(10, body=PLAN), queued(11, body=PLAN), queued(12)
        )

        handed = agent.next_issue((EPIC,))

        assert handed is not None
        assert handed.number == 12
        assert state.states() == {
            10: BatchLabel.PLANNED,
            11: BatchLabel.PLANNED,
            12: BatchLabel.QUEUED,
        }


class TestPlanWritten:
    def test_a_queued_issue_carrying_a_plan_advances(self) -> None:
        agent, state = agent_over(queued(10, body=PLAN), queued(11))

        assert agent.plan_written((EPIC,), 10) == PlanWritten(
            number=10, state=BatchLabel.PLANNED
        )
        assert state.states() == {10: BatchLabel.PLANNED, 11: BatchLabel.QUEUED}

    def test_an_issue_past_planning_is_refused_even_carrying_a_plan(self) -> None:
        agent, state = agent_over(
            batch_issue(10, BatchLabel.IMPLEMENTING, body=PLAN), queued(11)
        )

        assert agent.plan_written((EPIC,), 10) == PlanWritten(
            number=10,
            state=BatchLabel.IMPLEMENTING,
            refusal=PlanRefusal.WRONG_STATE,
        )
        assert state.transitions == []

    def test_a_claim_is_checked_against_the_body_as_it_stands_now(self) -> None:
        agent, state = agent_over(queued(10))
        _ = agent.next_issue((EPIC,))

        refused = agent.plan_written((EPIC,), 10)

        assert refused == PlanWritten(
            number=10, state=BatchLabel.QUEUED, refusal=PlanRefusal.NO_PLAN
        )
        assert state.states() == {10: BatchLabel.QUEUED}

        state.write_body(10, PLAN)

        assert agent.plan_written((EPIC,), 10).refusal is None
        assert state.states() == {10: BatchLabel.PLANNED}


class TestAnomalies:
    def test_only_closed_children_that_still_carry_a_label_are_named(self) -> None:
        state = FakeState(
            dropped=(unlabeled_child(3), closed_child(1503, BatchLabel.QUEUED))
        )

        anomalies: tuple[DroppedChild, ...] = PlanningAgent(state).anomalies((EPIC,))

        assert anomalies == (closed_child(1503, BatchLabel.QUEUED),)
