from __future__ import annotations

from orbit.filtering import partition_filtered, partition_standalone
from orbit.github.models import Issue, MilestoneIssue
from orbit.tree import FilteredRun


def _issues(*numbers: int) -> list[Issue]:
    return [Issue(number=n, state="OPEN", title=f"Issue {n}") for n in numbers]


def _keep_even(issue: Issue) -> bool:
    return issue.number % 2 == 0


class TestPartitionFiltered:
    def test_run_in_the_middle(self) -> None:
        rows = partition_filtered(_issues(2, 3, 5, 4), _keep_even)
        assert rows == [
            Issue(number=2, state="OPEN", title="Issue 2"),
            FilteredRun(count=2, numbers=(3, 5)),
            Issue(number=4, state="OPEN", title="Issue 4"),
        ]

    def test_runs_at_head_and_tail(self) -> None:
        rows = partition_filtered(_issues(1, 2, 3, 5), _keep_even)
        assert rows == [
            FilteredRun(count=1, numbers=(1,)),
            Issue(number=2, state="OPEN", title="Issue 2"),
            FilteredRun(count=2, numbers=(3, 5)),
        ]

    def test_everything_dropped_is_one_run(self) -> None:
        rows = partition_filtered(_issues(1, 3, 5), _keep_even)
        assert rows == [FilteredRun(count=3, numbers=(1, 3, 5))]

    def test_nothing_dropped_yields_no_placeholders(self) -> None:
        rows = partition_filtered(_issues(2, 4), _keep_even)
        assert rows == _issues(2, 4)

    def test_empty_input(self) -> None:
        assert partition_filtered(_issues(), _keep_even) == []


def _milestone_issue(
    number: int, *, parent: int | None = None, is_epic: bool = False
) -> MilestoneIssue:
    return MilestoneIssue(
        number=number,
        state="OPEN",
        title=f"Issue {number}",
        parent_number=parent,
        is_epic=is_epic,
    )


class TestPartitionStandalone:
    def test_a_parentless_leaf_is_standalone(self) -> None:
        issue = _milestone_issue(7)
        assert partition_standalone([issue]) == ([], [issue])

    def test_a_parentless_epic_is_structured(self) -> None:
        epic = _milestone_issue(5, is_epic=True)
        assert partition_standalone([epic]) == ([epic], [])

    def test_a_parented_issue_is_structured(self) -> None:
        child = _milestone_issue(6, parent=5)
        assert partition_standalone([child]) == ([child], [])

    def test_keeps_the_given_order_within_each_bucket(self) -> None:
        epic = _milestone_issue(5, is_epic=True)
        child = _milestone_issue(6, parent=5)
        first, second = _milestone_issue(7), _milestone_issue(8)
        assert partition_standalone([epic, first, child, second]) == (
            [epic, child],
            [first, second],
        )
