from __future__ import annotations

from collections.abc import Callable
from typing import cast
from unittest.mock import Mock, patch

import pytest

from ghgql.fake import FakeTransport
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL
from orbit.config import Milestones
from orbit.github.client import GitHubClient
from orbit.github.models import (
    AlreadyDoneError,
    ChildRef,
    CloseReason,
    CreatedIssue,
    IssueDetail,
    Surface,
)
from orbit.github.orchestrators import (
    apply_labels,
    close_issue,
    create_epic,
    create_leaf,
    move_issue,
    reorder_issue,
    schedule_issue,
)

_MILESTONES = Milestones(current="sprint 42", backlog="Icebox")


def _client() -> GitHubClient:
    """A client whose GitHub calls every test patches out."""
    return GitHubClient(
        GitHubGraphQL(FakeTransport([])), Repo("example-org", "example-repo")
    )


def _issue_detail(
    *,
    node_id: str = "I_1",
    number: int = 42,
    state: str = "OPEN",
    title: str = "Fix widget",
    body: str = "",
    labels: tuple[str, ...] = (),
    milestone_id: str | None = None,
    milestone_title: str | None = None,
    parent_number: int | None = None,
    parent_node_id: str | None = None,
    parent_title: str | None = None,
) -> IssueDetail:
    return IssueDetail(
        node_id=node_id,
        number=number,
        state=state,
        title=title,
        body=body,
        labels=labels,
        milestone_id=milestone_id,
        milestone_title=milestone_title,
        parent_number=parent_number,
        parent_node_id=parent_node_id,
        parent_title=parent_title,
    )


def _fetch_by_number(*details: IssueDetail) -> Callable[[int], IssueDetail]:
    """A fetch stub that fails fast rather than feeding an unbounded walk."""
    by_number = {detail.number: detail for detail in details}
    budget = len(by_number) + 2
    calls = 0

    def fetch(number: int) -> IssueDetail:
        nonlocal calls
        calls += 1
        if calls > budget:
            raise AssertionError(
                f"fetch_issue_detail called {calls} times, over budget"
            )
        return by_number[number]

    return fetch


class TestMoveIssue:
    def test_moves_unattached_issue_to_epic(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, title="Fix widget")
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="OPEN",
            title="Sprint epic",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "add_sub_issue") as mock_add,
            patch.object(client, "set_issue_milestone") as mock_ms,
        ):
            result = move_issue(client, 42, 800)

        assert result.issue_number == 42
        assert result.issue_title == "Fix widget"
        assert result.epic_number == 800
        assert result.epic_title == "Sprint epic"
        assert result.old_epic_number is None
        assert result.old_epic_title is None
        assert result.milestone == "developer velocity"
        mock_add.assert_called_once_with("I_2", "I_1")
        mock_ms.assert_called_once_with("I_1", "MI_1")

    def test_reparents_from_old_epic_to_new(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            title="Fix widget",
            parent_number=700,
            parent_node_id="I_old",
            parent_title="Old epic",
        )
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            title="New epic",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "remove_sub_issue") as mock_remove,
            patch.object(client, "add_sub_issue") as mock_add,
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.old_epic_number == 700
        assert result.old_epic_title == "Old epic"
        assert result.epic_number == 800
        mock_remove.assert_called_once_with("I_old", "I_1")
        mock_add.assert_called_once_with("I_2", "I_1")

    def test_reopens_closed_epic_and_nests(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="CLOSED",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        with (
            patch.object(
                client, "fetch_issue_detail", side_effect=_fetch_by_number(issue, epic)
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "add_sub_issue") as mock_add,
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == (800,)
        mock_reopen.assert_called_once_with("I_2")
        mock_add.assert_called_once_with("I_2", "I_1")

    def test_reports_nothing_reopened_for_open_chain(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            labels=("epic",),
            parent_number=700,
            parent_node_id="I_3",
        )
        parent = _issue_detail(node_id="I_3", number=700, labels=("epic",))
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=_fetch_by_number(issue, epic, parent),
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "add_sub_issue"),
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == ()
        mock_reopen.assert_not_called()

    def test_reopens_closed_ancestor_above_an_open_one(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="CLOSED",
            labels=("epic",),
            parent_number=700,
            parent_node_id="I_3",
        )
        middle = _issue_detail(
            node_id="I_3",
            number=700,
            labels=("epic",),
            parent_number=600,
            parent_node_id="I_4",
        )
        root = _issue_detail(
            node_id="I_4", number=600, state="CLOSED", labels=("epic",)
        )
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=_fetch_by_number(issue, epic, middle, root),
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "add_sub_issue"),
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == (800, 600)
        assert [call.args[0] for call in mock_reopen.call_args_list] == ["I_2", "I_4"]

    def test_reopens_closed_parent_of_an_open_epic(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            labels=("epic",),
            parent_number=700,
            parent_node_id="I_3",
        )
        parent = _issue_detail(
            node_id="I_3", number=700, state="CLOSED", labels=("epic",)
        )
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=_fetch_by_number(issue, epic, parent),
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "add_sub_issue"),
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == (700,)
        mock_reopen.assert_called_once_with("I_3")

    def test_terminates_on_a_cyclic_parent_chain(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="CLOSED",
            labels=("epic",),
            parent_number=700,
            parent_node_id="I_3",
        )
        parent = _issue_detail(
            node_id="I_3",
            number=700,
            state="CLOSED",
            labels=("epic",),
            parent_number=800,
            parent_node_id="I_2",
        )
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=_fetch_by_number(issue, epic, parent),
            ),
            patch.object(client, "reopen_issue_by_id"),
            patch.object(client, "add_sub_issue"),
            patch.object(client, "set_issue_milestone"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == (800, 700)

    def test_reports_reopen_and_conversion_together(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        not_epic = _issue_detail(
            node_id="I_2", number=800, state="CLOSED", labels=("bug",)
        )
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=_fetch_by_number(issue, not_epic),
            ),
            patch.object(client, "reopen_issue_by_id"),
            patch.object(client, "add_sub_issue"),
            patch.object(client, "add_label"),
            patch.object(client, "set_issue_milestone"),
            patch.object(client, "fetch_label_id", return_value="zz"),
        ):
            result = move_issue(client, 42, 800)

        assert result.reopened == (800,)
        assert result.converted_dest_to_epic

    def test_reopens_closed_parent_of_an_already_nested_issue(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            parent_number=800,
            parent_node_id="I_2",
            parent_title="Epic",
        )
        epic = _issue_detail(
            node_id="I_2", number=800, state="CLOSED", labels=("epic",)
        )
        with (
            patch.object(
                client, "fetch_issue_detail", side_effect=_fetch_by_number(issue, epic)
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "add_sub_issue") as mock_add,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = move_issue(client, 42, 800)

        assert result.already_done
        assert result.reopened == (800,)
        mock_reopen.assert_called_once_with("I_2")
        mock_add.assert_not_called()
        mock_remove.assert_not_called()

    def test_converts_target_without_epic_label(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        not_epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="OPEN",
            labels=("bug",),
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, not_epic]),
            patch.object(client, "add_sub_issue"),
            patch.object(client, "add_label") as mock_add_label,
            patch.object(client, "set_issue_milestone"),
            patch.object(client, "fetch_label_id", return_value="zz"),
        ):
            mr = move_issue(client, 42, 800)
        assert mock_add_label.call_count == 1
        assert mr.converted_dest_to_epic

    def test_skips_milestone_when_epic_has_none(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            labels=("epic",),
            milestone_id=None,
            milestone_title=None,
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "add_sub_issue"),
            patch.object(client, "set_issue_milestone") as mock_ms,
        ):
            result = move_issue(client, 42, 800)

        mock_ms.assert_not_called()
        assert result.milestone is None

    def test_rejects_self_move(self) -> None:
        client = _client()
        with pytest.raises(RuntimeError, match="cannot be moved under itself"):
            move_issue(client, 800, 800)

    def test_rejects_already_under_target_epic(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            parent_number=800,
            parent_node_id="I_2",
            parent_title="Epic",
        )
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            labels=("epic",),
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            pytest.raises(AlreadyDoneError, match="already under epic #800"),
        ):
            move_issue(client, 42, 800)

    def test_reparent_removes_before_adding(self) -> None:
        client = _client()
        call_order: list[str] = []

        def on_remove(_parent: str, _child: str) -> None:
            call_order.append("remove")

        def on_add(_parent: str, _child: str) -> None:
            call_order.append("add")

        issue = _issue_detail(
            node_id="I_1",
            number=42,
            parent_number=700,
            parent_node_id="I_old",
            parent_title="Old",
        )
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="dev",
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "remove_sub_issue", side_effect=on_remove),
            patch.object(client, "add_sub_issue", side_effect=on_add),
            patch.object(client, "set_issue_milestone"),
        ):
            move_issue(client, 42, 800)

        assert call_order == ["remove", "add"]


def _fake_label_id(name: str) -> str:
    return f"LA_{name}"


class TestApplyLabels:
    def test_resolves_and_adds_each_in_order(self) -> None:
        client = _client()
        with (
            patch.object(
                client, "fetch_label_id", side_effect=_fake_label_id
            ) as mock_fetch,
            patch.object(client, "add_label") as mock_add,
        ):
            apply_labels(client, "I_1", ["a", "b"])
        assert [c.args[0] for c in mock_fetch.call_args_list] == ["a", "b"]
        assert [c.args for c in mock_add.call_args_list] == [
            ("I_1", "LA_a"),
            ("I_1", "LA_b"),
        ]

    def test_empty_adds_nothing(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_label_id") as mock_fetch,
            patch.object(client, "add_label") as mock_add,
        ):
            apply_labels(client, "I_1", [])
        mock_fetch.assert_not_called()
        mock_add.assert_not_called()


class TestCloseIssue:
    def test_closes_open_issue(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, title="Fix widget")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment"),
            patch.object(client, "close_issue_by_id") as mock_close,
        ):
            result = close_issue(client, 42, CloseReason.COMPLETED, Surface.CLI)
        mock_close.assert_called_once_with("I_1", CloseReason.COMPLETED)
        assert result.number == 42
        assert result.reason == CloseReason.COMPLETED
        assert result.already_done is False

    def test_passes_through_reason(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment"),
            patch.object(client, "close_issue_by_id") as mock_close,
        ):
            close_issue(client, 42, CloseReason.NOT_PLANNED, Surface.CLI)
        mock_close.assert_called_once_with("I_1", CloseReason.NOT_PLANNED)

    def test_rejects_already_closed_issue(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, state="CLOSED")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "close_issue_by_id") as mock_close,
            pytest.raises(AlreadyDoneError, match="already closed"),
        ):
            close_issue(client, 42, CloseReason.COMPLETED, Surface.CLI)
        mock_close.assert_not_called()

    def test_comments_before_closing(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        parent = Mock()
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment") as mock_comment,
            patch.object(client, "close_issue_by_id") as mock_close,
        ):
            parent.attach_mock(mock_comment, "add_comment")
            parent.attach_mock(mock_close, "close_issue_by_id")
            close_issue(client, 42, CloseReason.COMPLETED, Surface.CLI)
        calls = cast(list[tuple[str, object, object]], parent.mock_calls)
        assert [name for name, _args, _kwargs in calls] == [
            "add_comment",
            "close_issue_by_id",
        ]

    def test_failed_comment_aborts_the_close(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(
                client, "add_comment", side_effect=RuntimeError("comment rejected")
            ),
            patch.object(client, "close_issue_by_id") as mock_close,
            pytest.raises(RuntimeError, match="comment rejected"),
        ):
            close_issue(client, 42, CloseReason.COMPLETED, Surface.CLI)
        mock_close.assert_not_called()

    @pytest.mark.parametrize(
        ("surface", "expected"),
        [
            (Surface.CLI, "Closed by the orbit CLI (completed)."),
            (Surface.TUI, "Closed by the orbit TUI (completed)."),
        ],
    )
    def test_each_surface_stamps_its_own_text(
        self, surface: Surface, expected: str
    ) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment") as mock_comment,
            patch.object(client, "close_issue_by_id"),
        ):
            close_issue(client, 42, CloseReason.COMPLETED, surface)
        mock_comment.assert_called_once_with("I_1", expected)

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (CloseReason.COMPLETED, "Closed by the orbit CLI (completed)."),
            (CloseReason.DUPLICATE, "Closed by the orbit CLI (duplicate)."),
            (CloseReason.NOT_PLANNED, "Closed by the orbit CLI (not planned)."),
        ],
    )
    def test_each_reason_renders_in_the_text(
        self, reason: CloseReason, expected: str
    ) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment") as mock_comment,
            patch.object(client, "close_issue_by_id"),
        ):
            close_issue(client, 42, reason, Surface.CLI)
        mock_comment.assert_called_once_with("I_1", expected)

    def test_already_closed_issue_gets_no_comment(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, state="CLOSED")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "add_comment") as mock_comment,
            patch.object(client, "close_issue_by_id"),
            pytest.raises(AlreadyDoneError, match="already closed"),
        ):
            close_issue(client, 42, CloseReason.COMPLETED, Surface.CLI)
        mock_comment.assert_not_called()


class TestScheduleIssue:
    def test_targets_the_milestone_it_is_given(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, title="Fix widget")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(
                client, "fetch_milestone_id", return_value="MI_current"
            ) as mock_ms_id,
            patch.object(client, "set_issue_milestone") as mock_ms,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = schedule_issue(client, 42, _MILESTONES.current)

        assert result.issue_number == 42
        assert result.issue_title == "Fix widget"
        assert result.milestone == _MILESTONES.current
        assert result.old_epic_number is None
        assert result.old_epic_title is None
        mock_ms_id.assert_called_once_with(_MILESTONES.current)
        mock_ms.assert_called_once_with("I_1", "MI_current")
        mock_remove.assert_not_called()

    def test_targets_explicit_milestone(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, milestone_title="Backlog")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(
                client, "fetch_milestone_id", return_value="MI_web"
            ) as mock_ms_id,
            patch.object(client, "set_issue_milestone") as mock_ms,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = schedule_issue(client, 42, "web frontend 1")

        assert result.milestone == "web frontend 1"
        mock_ms_id.assert_called_once_with("web frontend 1")
        mock_ms.assert_called_once_with("I_1", "MI_web")
        mock_remove.assert_not_called()

    def test_unknown_milestone_raises(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(
                client,
                "fetch_milestone_id",
                side_effect=RuntimeError("Milestone 'bogus' not found"),
            ),
            patch.object(client, "set_issue_milestone") as mock_ms,
            pytest.raises(RuntimeError, match="Milestone 'bogus' not found"),
        ):
            schedule_issue(client, 42, "bogus")
        mock_ms.assert_not_called()

    def test_parentless_epic_move_sets_milestone_without_detach(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_epic", number=1107, milestone_title="web frontend 1"
        )
        with (
            patch.object(
                client, "fetch_issue_detail", return_value=issue
            ) as mock_detail,
            patch.object(client, "fetch_milestone_id", return_value="MI_current"),
            patch.object(client, "set_issue_milestone") as mock_ms,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = schedule_issue(client, 1107, _MILESTONES.current)

        assert result.old_epic_number is None
        mock_ms.assert_called_once_with("I_epic", "MI_current")
        mock_remove.assert_not_called()
        # Only the issue itself is fetched; there is no parent to look up.
        mock_detail.assert_called_once_with(1107)

    def test_detaches_when_epic_milestone_differs(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            milestone_title="degraded state",
            parent_number=905,
            parent_node_id="I_epic",
            parent_title="orbit — dev tool",
        )
        epic = _issue_detail(
            node_id="I_epic", number=905, milestone_title="degraded state"
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "set_issue_milestone") as mock_ms,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = schedule_issue(client, 42, _MILESTONES.backlog)

        assert result.old_epic_number == 905
        assert result.old_epic_title == "orbit — dev tool"
        assert result.milestone == _MILESTONES.backlog
        mock_remove.assert_called_once_with("I_epic", "I_1")
        mock_ms.assert_called_once_with("I_1", "MI_backlog")

    def test_keeps_parent_when_epic_milestone_matches(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            milestone_title="Backlog",
            parent_number=905,
            parent_node_id="I_epic",
            parent_title="orbit — dev tool",
        )
        epic = _issue_detail(
            node_id="I_epic", number=905, milestone_title=_MILESTONES.current
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "fetch_milestone_id", return_value="MI_current"),
            patch.object(client, "set_issue_milestone") as mock_ms,
            patch.object(client, "remove_sub_issue") as mock_remove,
        ):
            result = schedule_issue(client, 42, _MILESTONES.current)

        assert result.old_epic_number is None
        assert result.old_epic_title is None
        mock_remove.assert_not_called()
        mock_ms.assert_called_once_with("I_1", "MI_current")

    def test_rejects_when_nothing_would_change(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1", number=42, milestone_title=_MILESTONES.current
        )
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "fetch_milestone_id", return_value="MI_current"),
            patch.object(client, "set_issue_milestone") as mock_ms,
            pytest.raises(AlreadyDoneError, match="already in milestone"),
        ):
            schedule_issue(client, 42, _MILESTONES.current)
        mock_ms.assert_not_called()

    def test_detach_precedes_milestone_set(self) -> None:
        client = _client()
        call_order: list[str] = []

        def on_remove(_parent: str, _child: str) -> None:
            call_order.append("remove")

        def on_milestone(_issue: str, _ms: str) -> None:
            call_order.append("milestone")

        issue = _issue_detail(
            node_id="I_1",
            number=42,
            milestone_title="degraded state",
            parent_number=905,
            parent_node_id="I_epic",
            parent_title="Epic",
        )
        epic = _issue_detail(
            node_id="I_epic", number=905, milestone_title="degraded state"
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "remove_sub_issue", side_effect=on_remove),
            patch.object(client, "set_issue_milestone", side_effect=on_milestone),
        ):
            schedule_issue(client, 42, _MILESTONES.backlog)

        assert call_order == ["remove", "milestone"]

    def test_milestone_failure_reports_detachment(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            milestone_title="degraded state",
            parent_number=905,
            parent_node_id="I_epic",
            parent_title="Epic",
        )
        epic = _issue_detail(
            node_id="I_epic", number=905, milestone_title="degraded state"
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, epic]),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "remove_sub_issue"),
            patch.object(
                client, "set_issue_milestone", side_effect=RuntimeError("API error")
            ),
            pytest.raises(RuntimeError, match=r"detached from #905.*API error"),
        ):
            schedule_issue(client, 42, _MILESTONES.backlog)

    def test_milestone_failure_without_parent_omits_detachment_note(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42, milestone_title="Backlog")
        with (
            patch.object(client, "fetch_issue_detail", return_value=issue),
            patch.object(client, "fetch_milestone_id", return_value="MI_current"),
            patch.object(client, "remove_sub_issue") as mock_remove,
            patch.object(
                client, "set_issue_milestone", side_effect=RuntimeError("API error")
            ),
            pytest.raises(
                RuntimeError,
                match=r"^Milestone update failed for #42: API error$",
            ),
        ):
            schedule_issue(client, 42, _MILESTONES.current)
        mock_remove.assert_not_called()

    def test_fetches_parent_detail_for_epic_milestone(self) -> None:
        client = _client()
        issue = _issue_detail(
            node_id="I_1",
            number=42,
            milestone_title="Backlog",
            parent_number=905,
            parent_node_id="I_epic",
            parent_title="Epic",
        )
        epic = _issue_detail(
            node_id="I_epic", number=905, milestone_title="degraded state"
        )
        with (
            patch.object(
                client, "fetch_issue_detail", side_effect=[issue, epic]
            ) as mock_detail,
            patch.object(client, "fetch_milestone_id", return_value="MI_current"),
            patch.object(client, "set_issue_milestone"),
            patch.object(client, "remove_sub_issue"),
        ):
            schedule_issue(client, 42, _MILESTONES.current)

        assert mock_detail.call_count == 2
        assert mock_detail.call_args_list[1].args == (905,)


def _child_ref(node_id: str, number: int, title: str = "Ship widget") -> ChildRef:
    return ChildRef(node_id=node_id, number=number, title=title)


def _reject_positionless(
    _parent_node_id: str,
    _child_node_id: str,
    after_id: str | None = None,
    before_id: str | None = None,
) -> None:
    if after_id is None and before_id is None:
        raise AssertionError("GitHub rejects a positionless reprioritizeSubIssue")


def _sibling(node_id: str, number: int, title: str) -> IssueDetail:
    return _issue_detail(
        node_id=node_id,
        number=number,
        title=title,
        parent_number=800,
        parent_node_id="I_epic",
        parent_title="Sprint epic",
    )


class TestReorderIssue:
    def test_places_issue_after_sibling(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        reference = _sibling("I_2", 43, "Ship widget")
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, reference]),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
        ):
            result = reorder_issue(client, 42, after_number=43)

        mock_reorder.assert_called_once_with("I_epic", "I_1", after_id="I_2")
        assert result.issue_number == 42
        assert result.issue_title == "Fix widget"
        assert result.epic_number == 800
        assert result.epic_title == "Sprint epic"
        assert result.position == "after"
        assert result.reference_number == 43
        assert result.reference_title == "Ship widget"

    def test_places_issue_before_sibling(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        reference = _sibling("I_2", 43, "Ship widget")
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, reference]),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
        ):
            result = reorder_issue(client, 42, before_number=43)

        mock_reorder.assert_called_once_with("I_epic", "I_1", before_id="I_2")
        assert result.position == "before"
        assert result.reference_number == 43

    def test_places_issue_before_the_current_first_child(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        with (
            patch.object(
                client, "fetch_issue_detail", side_effect=[issue]
            ) as mock_detail,
            patch.object(
                client, "fetch_first_child", return_value=_child_ref("I_2", 43)
            ) as mock_first,
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
        ):
            result = reorder_issue(client, 42)

        mock_first.assert_called_once_with(800)
        mock_reorder.assert_called_once_with("I_epic", "I_1", before_id="I_2")
        assert mock_detail.call_count == 1
        assert result.position == "first"
        assert result.already_done is False

    def test_first_does_not_name_the_sibling_it_used(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue]),
            patch.object(
                client, "fetch_first_child", return_value=_child_ref("I_2", 43)
            ),
            patch.object(
                client, "reprioritize_sub_issue", side_effect=_reject_positionless
            ),
        ):
            result = reorder_issue(client, 42)

        assert result.position == "first"
        assert result.reference_number is None
        assert result.reference_title is None

    def test_already_first_is_a_no_op(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue]),
            patch.object(
                client, "fetch_first_child", return_value=_child_ref("I_1", 42)
            ),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
        ):
            result = reorder_issue(client, 42)

        mock_reorder.assert_not_called()
        assert result.already_done is True
        assert result.position == "first"
        assert result.epic_number == 800

    def test_rejects_epic_reporting_no_children(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue]),
            patch.object(client, "fetch_first_child", return_value=None),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError, match="no children"),
        ):
            reorder_issue(client, 42)
        mock_reorder.assert_not_called()

    def test_relative_placement_does_not_look_up_the_first_child(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        reference = _sibling("I_2", 43, "Ship widget")
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=[issue, reference, issue, reference],
            ),
            patch.object(client, "fetch_first_child") as mock_first,
            patch.object(
                client, "reprioritize_sub_issue", side_effect=_reject_positionless
            ),
        ):
            reorder_issue(client, 42, after_number=43)
            reorder_issue(client, 42, before_number=43)

        mock_first.assert_not_called()

    def test_rejects_issue_without_parent(self) -> None:
        client = _client()
        issue = _issue_detail(node_id="I_1", number=42)
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue]),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError, match="#42 is not in an epic"),
        ):
            reorder_issue(client, 42)
        mock_reorder.assert_not_called()

    def test_rejects_siblings_under_different_epics(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        reference = _issue_detail(
            node_id="I_2",
            number=43,
            parent_number=900,
            parent_node_id="I_other",
            parent_title="Other epic",
        )
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, reference]),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError) as exc_info,
        ):
            reorder_issue(client, 42, after_number=43)
        message = str(exc_info.value)
        assert "#800" in message
        assert "#900" in message
        mock_reorder.assert_not_called()

    def test_rejects_reference_without_parent(self) -> None:
        client = _client()
        issue = _sibling("I_1", 42, "Fix widget")
        reference = _issue_detail(node_id="I_2", number=43)
        with (
            patch.object(client, "fetch_issue_detail", side_effect=[issue, reference]),
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError, match="#43 is not in an epic"),
        ):
            reorder_issue(client, 42, after_number=43)
        mock_reorder.assert_not_called()

    def test_rejects_positioning_relative_to_itself(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_issue_detail") as mock_detail,
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError, match="relative to itself"),
        ):
            reorder_issue(client, 42, after_number=42)
        mock_detail.assert_not_called()
        mock_reorder.assert_not_called()

    def test_rejects_positioning_before_itself(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_issue_detail") as mock_detail,
            patch.object(
                client,
                "reprioritize_sub_issue",
                side_effect=_reject_positionless,
            ) as mock_reorder,
            pytest.raises(RuntimeError, match="relative to itself"),
        ):
            reorder_issue(client, 42, before_number=42)
        mock_detail.assert_not_called()
        mock_reorder.assert_not_called()


def _created() -> CreatedIssue:
    return CreatedIssue(node_id="I_9", number=9, title="a leaf")


def _create_responses(milestone: str) -> list[dict[str, object]]:
    return [
        {"repository": {"id": "R_1"}},
        {"repository": {"milestones": {"nodes": [{"id": "MI_1", "title": milestone}]}}},
        {"createIssue": {"issue": {"id": "I_9", "number": 9, "title": "a leaf"}}},
    ]


def _mutations(transport: FakeTransport) -> list[str]:
    return [
        call.query_text for call in transport.calls if "mutation" in call.query_text
    ]


class TestCreateLeaf:
    def test_standalone_lands_in_the_sprint_milestone_with_no_parent(self) -> None:
        transport = FakeTransport(_create_responses(_MILESTONES.current))
        client = GitHubClient(
            GitHubGraphQL(transport), Repo("example-org", "example-repo")
        )

        result = create_leaf(client, _MILESTONES, "standalone", "a leaf")

        assert result.number == 9
        assert result.epic_number is None
        assert result.milestone == _MILESTONES.current
        assert not any("addSubIssue" in text for text in _mutations(transport))

    def test_shelf_lands_in_the_backlog_milestone_with_no_parent(self) -> None:
        transport = FakeTransport(_create_responses(_MILESTONES.backlog))
        client = GitHubClient(
            GitHubGraphQL(transport), Repo("example-org", "example-repo")
        )

        result = create_leaf(client, _MILESTONES, "shelf", "a leaf")

        assert result.epic_number is None
        assert result.milestone == _MILESTONES.backlog
        assert not any("addSubIssue" in text for text in _mutations(transport))

    def test_standalone_applies_labels(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_repository_id", return_value="R_1"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(client, "create_issue", return_value=_created()),
            patch.object(client, "fetch_label_id", return_value="LA_bug") as mock_id,
            patch.object(client, "add_label") as mock_add,
        ):
            create_leaf(client, _MILESTONES, "standalone", "a leaf", labels=("bug",))

        mock_id.assert_called_once_with("bug")
        mock_add.assert_called_once_with("I_9", "LA_bug")

    def test_rejects_an_unrecognized_destination_naming_every_form(self) -> None:
        client = _client()
        with pytest.raises(RuntimeError) as excinfo:
            create_leaf(client, _MILESTONES, "nowhere", "a leaf")

        message = str(excinfo.value)
        assert "epic issue number" in message
        assert "'standalone'" in message
        assert "'shelf'" in message

    def test_reopens_closed_epic_and_its_closed_ancestor(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_2",
            number=800,
            state="CLOSED",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
            parent_number=700,
            parent_node_id="I_3",
        )
        parent = _issue_detail(
            node_id="I_3", number=700, state="CLOSED", labels=("epic",)
        )
        with (
            patch.object(
                client, "fetch_issue_detail", side_effect=_fetch_by_number(epic, parent)
            ),
            patch.object(client, "reopen_issue_by_id") as mock_reopen,
            patch.object(client, "fetch_repository_id", return_value="R_1"),
            patch.object(client, "create_issue", return_value=_created()),
            patch.object(client, "add_sub_issue") as mock_add,
        ):
            result = create_leaf(client, _MILESTONES, "800", "a leaf")

        assert result.reopened == (800, 700)
        assert [call.args[0] for call in mock_reopen.call_args_list] == ["I_2", "I_3"]
        mock_add.assert_called_once_with("I_2", "I_9")

    @pytest.mark.parametrize("destination", ["shelf", "standalone"])
    def test_parentless_destination_reopens_nothing(self, destination: str) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_issue_detail") as mock_fetch,
            patch.object(client, "fetch_repository_id", return_value="R_1"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(client, "create_issue", return_value=_created()),
        ):
            result = create_leaf(client, _MILESTONES, destination, "a leaf")

        assert result.reopened == ()
        mock_fetch.assert_not_called()


class TestCreateEpic:
    def test_lands_in_the_milestone_it_is_given(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_repository_id", return_value="R_1"),
            patch.object(
                client, "fetch_milestone_id", return_value="MI_1"
            ) as mock_ms_id,
            patch.object(client, "create_issue", return_value=_created()),
            patch.object(client, "fetch_label_id", return_value="LA_epic"),
            patch.object(client, "add_label"),
        ):
            result = create_epic(client, _MILESTONES.current, "an epic", [])

        mock_ms_id.assert_called_once_with(_MILESTONES.current)
        assert result.milestone == _MILESTONES.current
