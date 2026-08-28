"""End-to-end pilot tests for the orbit TUI.

These drive the real Textual app headlessly with all GitHub calls
mocked, verifying behavior through keypresses and the status line —
not widget internals — so they should survive refactoring of the TUI.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Protocol, cast
from unittest.mock import call, patch

import pytest
from textual.binding import Binding
from textual.pilot import Pilot
from textual.widgets import OptionList, Static
from textual.widgets.tree import TreeNode
from textual.worker import WorkerError

from ghgql.errors import IssueNotFoundError
from ghgql.fake import FakeTransport
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL
from orbit.config import CommandMode, CustomCommand, Milestones, ProjectConfig
from orbit.github.client import GitHubClient
from orbit.github.models import (
    AlreadyDoneError,
    CloseReason,
    Epic,
    Issue,
    IssueDetail,
    MilestoneIssue,
    MilestoneSummary,
    MoveResult,
    ScheduleResult,
    SubIssueData,
    Surface,
)
from orbit.github.orchestrators import close_issue
from orbit.tui.app import OrbitApp, run_tui
from orbit.tui.screens import (
    BranchPromptScreen,
    DetailScreen,
    EpicPickerScreen,
    HelpScreen,
    IssueNumberPromptScreen,
    MilestonePickerScreen,
)
from orbit.tui.widgets import (
    FilteredNodeData,
    IssueList,
    IssueTree,
    StatusBar,
    TreeItemData,
)

# Each test boots a full Textual app (~0.3s); excluded from quick runs.
pytestmark = pytest.mark.slow

_REPO = ("example-org", "example-repo")

# 852 and 853 are adjacent dead epics (closed, nothing open beneath);
# 860 is closed but still has open work, so hide-closed keeps it
# reachable instead of merging it into their run.
_EPICS = [
    Epic(number=905, state="OPEN", title="orbit dev tool", open_count=3, total_count=5),
    Epic(number=852, state="CLOSED", title="Test speed", open_count=0, total_count=4),
    Epic(number=853, state="CLOSED", title="Old deploys", open_count=0, total_count=2),
    Epic(number=860, state="CLOSED", title="Half done", open_count=1, total_count=3),
    Epic(number=861, state="CLOSED", title="Trailing", open_count=0, total_count=1),
]

_SUBS = [
    SubIssueData(number=910, state="OPEN", title="leaf a", children=()),
    SubIssueData(
        number=911,
        state="CLOSED",
        title="nested epic",
        children=(SubIssueData(number=912, state="OPEN", title="deep", children=()),),
    ),
]

_FLAT_ISSUES = [
    MilestoneIssue(
        number=20, state="OPEN", title="flat a", parent_number=905, is_epic=False
    ),
    MilestoneIssue(
        number=21, state="OPEN", title="flat b", parent_number=905, is_epic=False
    ),
]

# The milestone as the standalone section sees it: an epic and its
# child, then three parentless issues, one of them closed.
_SPRINT = "sprint 42"
_BACKLOG = "Icebox"
_APP_MILESTONES = Milestones(current=_SPRINT, backlog=_BACKLOG)

_MILESTONE_ISSUES = [
    MilestoneIssue(
        number=905,
        state="OPEN",
        title="orbit dev tool",
        parent_number=None,
        is_epic=True,
    ),
    MilestoneIssue(
        number=910, state="OPEN", title="leaf a", parent_number=905, is_epic=False
    ),
    MilestoneIssue(
        number=30, state="OPEN", title="solo a", parent_number=None, is_epic=False
    ),
    MilestoneIssue(
        number=31, state="CLOSED", title="solo b", parent_number=None, is_epic=False
    ),
    MilestoneIssue(
        number=32, state="OPEN", title="solo c", parent_number=None, is_epic=False
    ),
]

# Two adjacent closed standalone issues: hide-closed covers both with
# one run, so revealing either brings its neighbour back too.
_MILESTONE_ISSUES_CLOSED_PAIR = [
    MilestoneIssue(
        number=30, state="OPEN", title="solo a", parent_number=None, is_epic=False
    ),
    MilestoneIssue(
        number=31, state="CLOSED", title="solo b", parent_number=None, is_epic=False
    ),
    MilestoneIssue(
        number=33, state="CLOSED", title="solo d", parent_number=None, is_epic=False
    ),
    MilestoneIssue(
        number=32, state="OPEN", title="solo c", parent_number=None, is_epic=False
    ),
]

_CLOSED_STANDALONE = [
    MilestoneIssue(
        number=30, state="CLOSED", title="solo a", parent_number=None, is_epic=False
    )
]

_DETAIL = IssueDetail(
    node_id="I_905",
    number=905,
    state="OPEN",
    title="orbit dev tool",
    body="## Body\n\nhello",
    labels=("epic",),
    milestone_id="MI_1",
    milestone_title="developer velocity",
    parent_number=None,
    parent_node_id=None,
    parent_title=None,
)

_MOVE_RESULT = MoveResult(
    issue_number=20,
    issue_title="flat a",
    epic_number=905,
    epic_title="orbit dev tool",
    old_epic_number=None,
    old_epic_title=None,
    milestone="developer velocity",
    converted_dest_to_epic=False,
)

# The picker highlights its first option; _MILESTONES[0].title is what
# a bare enter selects, so the schedule result echoes that milestone.
_MILESTONES = [
    MilestoneSummary(title="degraded state", state="OPEN"),
    MilestoneSummary(title="Backlog", state="OPEN"),
]

_SCHEDULE_RESULT = ScheduleResult(
    issue_number=20,
    issue_title="flat a",
    milestone="degraded state",
    old_epic_number=905,
    old_epic_title="orbit dev tool",
)

_SCHEDULE_RESULT_NO_EPIC = ScheduleResult(
    issue_number=20,
    issue_title="flat a",
    milestone="degraded state",
    old_epic_number=None,
    old_epic_title=None,
)

_PATCH_MOVE = "orbit.tui.app.move_issue"
_PATCH_SCHEDULE = "orbit.tui.app.schedule_issue"
_PATCH_CLOSE = "orbit.tui.app.close_issue"
_PATCH_BROWSER = "orbit.tui.app.open_url"

_PATCH_SPAWN = "orbit.tui.app.spawn"
_PATCH_RUN_ATTACHED = "orbit.tui.app.run_attached"

# Ancestry behind the fake fetch_parent_issue: 912 sits under nested
# epic 911, which sits under root epic 905. 8000 is a parent that is
# not a root epic of the tree, so 8888's chain leaves the milestone.
_PARENTS = {912: 911, 911: 905, 910: 905, 920: 860, 8888: 8000}

# The open work under closed epic 860, and a run of closed sub-issues
# under 905 — the two shapes hide-closed has to keep reachable.
_SUBS_860 = [SubIssueData(number=920, state="OPEN", title="still open", children=())]

_SUBS_905_WITH_DEAD_RUN = [
    SubIssueData(number=910, state="OPEN", title="leaf a", children=()),
    SubIssueData(number=913, state="CLOSED", title="dead a", children=()),
    SubIssueData(
        number=914,
        state="CLOSED",
        title="dead parent",
        children=(
            SubIssueData(number=915, state="CLOSED", title="dead deep", children=()),
        ),
    ),
]

# 916 is closed but keeps 918 alive, so it becomes a run of its own
# whose children hold a second run over its dead child 917.
_SUBS_905_WITH_NESTED_RUN = [
    SubIssueData(number=910, state="OPEN", title="leaf a", children=()),
    SubIssueData(
        number=916,
        state="CLOSED",
        title="dead parent",
        children=(
            SubIssueData(number=917, state="CLOSED", title="dead kid", children=()),
            SubIssueData(number=918, state="OPEN", title="live kid", children=()),
        ),
    ),
]

# Nothing on 932's path is closed, so hide-closed puts no placeholder
# between it and the root.
_SUBS_905_DEEP_OPEN = [
    SubIssueData(number=910, state="OPEN", title="leaf a", children=()),
    SubIssueData(
        number=930,
        state="OPEN",
        title="live parent",
        children=(
            SubIssueData(number=931, state="CLOSED", title="dead kid", children=()),
            SubIssueData(number=932, state="OPEN", title="deep live", children=()),
        ),
    ),
]


def _parent_under_905(_number: int) -> Issue | None:
    return Issue(number=905, state="OPEN", title="epic 905")


def _parent_of(number: int) -> Issue | None:
    parent = _PARENTS.get(number)
    if parent is None:
        return None
    return Issue(number=parent, state="OPEN", title=f"epic {parent}")


def _subs_of(number: int) -> list[SubIssueData]:
    return _SUBS_860 if number == 860 else _SUBS


_SPAWN_COMMAND = CustomCommand(key="w", label="Worktree", run="vwt {issue}")
_BRANCH_COMMAND = CustomCommand(key="w", label="Worktree", run="vwt {branch} {issue}")
_SUSPEND_COMMAND = CustomCommand(
    key="v", label="Edit", run="vim {issue}", mode=CommandMode.SUSPEND
)


@contextmanager
def _patched_github() -> Generator[GitHubClient]:
    """Yield a client whose GitHub calls all return canned data.

    Tests that assert on calls stack their own `patch(...)` on top;
    the innermost patch wins.
    """
    client = GitHubClient(GitHubGraphQL(FakeTransport([])), Repo(*_REPO))
    with (
        patch.object(client, "list_epics_by_milestone", return_value=_EPICS),
        patch.object(client, "fetch_sub_issue_tree", return_value=_SUBS),
        patch.object(client, "list_issues_by_milestone", return_value=_FLAT_ISSUES),
        patch.object(client, "fetch_issue_detail", return_value=_DETAIL),
        patch.object(client, "list_milestones", return_value=_MILESTONES),
        patch(_PATCH_MOVE, return_value=_MOVE_RESULT),
        patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT),
        patch(_PATCH_CLOSE),
        patch(_PATCH_BROWSER),
    ):
        yield client


def _app(client: GitHubClient, commands: Sequence[CustomCommand] = ()) -> OrbitApp:
    return OrbitApp(client, _APP_MILESTONES, commands)


def _status_text(app: OrbitApp) -> str:
    bar = app.query_one(StatusBar)
    return str(bar.query_one("#status-message", Static).content)


def _root_labels(tree: IssueTree) -> list[str]:
    return [str(node.label) for node in tree.root.children]


def _visible_labels(tree: IssueTree) -> list[str]:
    """The labels on screen: a node shows only while every node above
    it is expanded."""

    def below(node: TreeNode[TreeItemData]) -> Iterator[str]:
        for child in node.children:
            yield str(child.label)
            if child.is_expanded:
                yield from below(child)

    return list(below(tree.root))


def _the_app(pilot: Pilot[None]) -> OrbitApp:
    app = pilot.app
    assert isinstance(app, OrbitApp)
    return app


class _WorkersLike(Protocol):
    async def wait_for_complete(self) -> None: ...


async def _settle(pilot: Pilot[None]) -> None:
    """Let workers finish and their UI updates flush.

    `_wait_for_screen` is private API and is confined to this helper.
    """
    workers = cast(_WorkersLike, pilot.app.workers)
    for _ in range(8):
        with suppress(WorkerError):
            await workers.wait_for_complete()
        _ = await pilot._wait_for_screen()  # pyright: ignore[reportPrivateUsage]
        if not pilot.app.workers:
            return


class TestEpicsTree:
    @pytest.mark.asyncio
    async def test_loads_epics_on_start(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                tree = app.query_one(IssueTree)
                assert tree.display
                assert len(tree.root.children) == 5
                assert "Loaded 5 epics, 15 issues" in _status_text(app)

    @pytest.mark.asyncio
    async def test_right_arrow_expands_epic_lazily(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS
            ) as mock_subs,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                node = app.query_one(IssueTree).root.children[0]
                assert node.is_expanded
                assert len(node.children) == 2
                mock_subs.assert_called_once_with(905)

    @pytest.mark.asyncio
    async def test_nested_epics_populate_recursively(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                node = app.query_one(IssueTree).root.children[0]
                assert len(node.children[1].children) == 1

    @pytest.mark.asyncio
    async def test_left_arrow_collapses_epic(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                await pilot.press("left")
                node = app.query_one(IssueTree).root.children[0]
                assert not node.is_expanded

    @pytest.mark.asyncio
    async def test_load_error_lands_on_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_epics_by_milestone", side_effect=RuntimeError("boom")
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert "Error: boom" in _status_text(app)

    @pytest.mark.asyncio
    async def test_a_standalone_query_failure_lands_on_the_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_issues_by_milestone", side_effect=RuntimeError("boom")
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert "Error: boom" in _status_text(app)
                assert not app.query_one(IssueTree).root.children

    @pytest.mark.asyncio
    async def test_refresh_refetches_epics(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_epics_by_milestone", return_value=_EPICS
            ) as mock_epics,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                await pilot.press("r")
                await _settle(pilot)
                assert mock_epics.call_count == 2

    @pytest.mark.asyncio
    async def test_refresh_preserves_cursor_and_expansion(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                # Expand the first epic, then refresh.
                await pilot.press("right")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.root.children[0].is_expanded
                await pilot.press("r")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.root.children[0].is_expanded
                assert len(tree.root.children[0].children) == 2
                assert tree.selected_issue_number == 905

    @pytest.mark.asyncio
    async def test_refresh_preserves_nested_expansion(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                # Expand epic 905, move down to its nested epic 911, expand it.
                await pilot.press("right")
                await _settle(pilot)
                await pilot.press("down", "down", "right")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                nested = tree.root.children[0].children[1]
                assert nested.is_expanded
                assert tree.selected_issue_number == 911
                await pilot.press("r")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.root.children[0].is_expanded
                nested = tree.root.children[0].children[1]
                assert nested.is_expanded
                assert tree.selected_issue_number == 911

    @pytest.mark.asyncio
    async def test_refresh_preserves_flat_list_selection(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                # Highlight the second backlog issue (#21), then refresh.
                await pilot.press("down")
                await _settle(pilot)
                issue_list = app.query_one("#backlog-list", IssueList)
                assert issue_list.selected_issue_number == 21
                await pilot.press("r")
                await _settle(pilot)
                issue_list = app.query_one("#backlog-list", IssueList)
                assert issue_list.selected_issue_number == 21


class TestDetailScreen:
    @pytest.mark.asyncio
    async def test_enter_shows_metadata_and_escape_returns(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                header = str(app.screen.query_one("#detail-header", Static).content)
                assert "#905" in header
                assert "developer velocity" in header
                await pilot.press("escape")
                await _settle(pilot)
                assert not isinstance(app.screen, DetailScreen)

    @pytest.mark.asyncio
    async def test_enter_opens_detail_from_flat_list(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_issue_detail", return_value=_DETAIL
            ) as mock_detail,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                mock_detail.assert_called_once_with(20)

    @pytest.mark.asyncio
    async def test_fetch_error_shown_in_header(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_issue_detail", side_effect=RuntimeError("boom")
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                header = str(app.screen.query_one("#detail-header", Static).content)
                assert "Error loading #905: boom" in header


class TestDetailScreenActions:
    @pytest.mark.asyncio
    async def test_close_from_detail_pops_and_reports(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_CLOSE) as mock_close,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("x")
                await _settle(pilot)
                mock_close.assert_called_once_with(
                    client, 20, CloseReason.COMPLETED, Surface.TUI
                )
                assert not isinstance(app.screen, DetailScreen)
                assert "Closed #20" in _status_text(app)

    @pytest.mark.asyncio
    async def test_schedule_from_detail_opens_picker(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT) as mock_schedule,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("s")
                await _settle(pilot)
                assert isinstance(app.screen, MilestonePickerScreen)
                await pilot.press("enter")
                await _settle(pilot)
                mock_schedule.assert_called_once_with(client, 20, "degraded state")
                assert not isinstance(app.screen, DetailScreen)
                assert "Scheduled #20 → degraded state" in _status_text(app)

    @pytest.mark.asyncio
    async def test_edit_from_detail_opens_browser_and_dismisses(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_BROWSER) as mock_browser,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("t")
                await _settle(pilot)
                mock_browser.assert_called_once_with(
                    "https://github.com/example-org/example-repo/issues/905"
                )
                assert not isinstance(app.screen, DetailScreen)

    @pytest.mark.asyncio
    async def test_move_from_detail_opens_picker(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                await pilot.press("m")
                await _settle(pilot)
                assert isinstance(app.screen, EpicPickerScreen)

    @pytest.mark.asyncio
    async def test_view_switch_blocked_in_detail(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                # View switches and refresh have no meaning over the detail
                # screen and stay blocked.
                await pilot.press("b")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)


class TestViewSwitching:
    @pytest.mark.asyncio
    async def test_sprint_flat_view_fetches_current_milestone(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_issues_by_milestone", return_value=_FLAT_ISSUES
            ) as mock_list,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("c")
                await _settle(pilot)
                issue_list = app.query_one("#sprint-list", IssueList)
                assert issue_list.display
                assert not app.query_one(IssueTree).display
                assert not app.query_one("#backlog-list", IssueList).display
                assert issue_list.option_count == 2
                assert "Loaded 2 sprint issues" in _status_text(app)
                # The epics load queries the same milestone for its
                # standalone section, so only the sprint list's own
                # call — the labelled one — is counted here.
                assert mock_list.call_args_list.count(call(_SPRINT, label=None)) == 1

    @pytest.mark.asyncio
    async def test_soon_filter_is_noop_in_sprint_view(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("c")
                await _settle(pilot)
                await pilot.press("n")
                await _settle(pilot)
                assert "(soon only)" not in _status_text(app)

    @pytest.mark.asyncio
    async def test_backlog_view_with_soon_filter(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_issues_by_milestone", return_value=_FLAT_ISSUES
            ) as mock_list,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                assert "Loaded 2 backlog issues" in _status_text(app)
                mock_list.assert_called_with(_BACKLOG, label=None)
                await pilot.press("n")
                await _settle(pilot)
                assert "(soon only)" in _status_text(app)
                mock_list.assert_called_with(_BACKLOG, label="soon")

    @pytest.mark.asyncio
    async def test_e_returns_to_epics_tree(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("e")
                await _settle(pilot)
                assert app.query_one(IssueTree).display
                assert not app.query_one("#sprint-list", IssueList).display
                assert not app.query_one("#backlog-list", IssueList).display


class TestHideClosed:
    @pytest.mark.asyncio
    async def test_f_hides_closed_epics(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                labels = _root_labels(app.query_one(IssueTree))
                assert any("#905" in label for label in labels)
                assert not any("#852" in label or "#853" in label for label in labels)
                assert any("filtered" in label for label in labels)
                assert "Hide closed: on" in _status_text(app)

    @pytest.mark.asyncio
    async def test_adjacent_dead_epics_merge_into_one_node(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                labels = _root_labels(app.query_one(IssueTree))
                assert [label for label in labels if "filtered" in label] == [
                    "<2 issues filtered>",
                    "1/3 <1 issue filtered>",
                    "<1 issue filtered>",
                ]

    @pytest.mark.asyncio
    async def test_a_closed_epic_with_open_work_stays_reachable(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS
            ) as mock_subs,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "down", "right")
                await _settle(pilot)
                mock_subs.assert_called_once_with(860)
                node = app.query_one(IssueTree).root.children[2]
                assert node.is_expanded
                assert [str(child.label) for child in node.children] == [
                    "#910 leaf a",
                    "1/1 <1 issue filtered>",
                ]

    @pytest.mark.asyncio
    async def test_expanding_a_run_reveals_the_epics_it_covers(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "right")
                await _settle(pilot)
                node = app.query_one(IssueTree).root.children[1]
                assert [str(child.label) for child in node.children] == [
                    "#852 0/4 Test speed",
                    "#853 0/2 Old deploys",
                ]
                await pilot.press("left")
                await _settle(pilot)
                assert not node.is_expanded

    @pytest.mark.asyncio
    async def test_toggling_back_restores_the_full_tree(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                labels = _root_labels(app.query_one(IssueTree))
                assert len(labels) == 5
                assert not any("filtered" in label for label in labels)
                assert "Hide closed: off" in _status_text(app)

    @pytest.mark.asyncio
    async def test_expansion_survives_a_refresh_with_the_filter_on(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "down", "right")
                await _settle(pilot)
                await pilot.press("r")
                await _settle(pilot)
                node = app.query_one(IssueTree).root.children[2]
                assert node.is_expanded
                assert len(node.children) == 2

    @pytest.mark.asyncio
    async def test_a_refresh_leaves_epics_under_a_run_as_the_user_left_them(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS
            ) as mock_subs,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "right", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 852
                await pilot.press("r")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                run = tree.root.children[1]
                assert run.is_expanded
                assert not run.children[0].is_expanded
                assert tree.selected_issue_number == 852
                mock_subs.assert_not_called()

    @pytest.mark.asyncio
    async def test_goto_reaches_open_work_under_a_hidden_epic(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client,
                "fetch_sub_issue_tree",
                side_effect=_subs_of,
            ),
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "2", "0", "enter")
                await _settle(pilot)
                assert "Jumped to #920" in _status_text(app)
                assert app.query_one(IssueTree).selected_issue_number == 920

    @pytest.mark.asyncio
    async def test_expanding_a_nested_run_reveals_the_issues_it_covers(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("right", "down", "down", "right")
                await _settle(pilot)
                run = app.query_one(IssueTree).root.children[0].children[1]
                assert str(run.label) == "<2 issues filtered>"
                assert [str(child.label) for child in run.children] == [
                    "#913 dead a",
                    "#914 0/1 dead parent",
                ]

    @pytest.mark.asyncio
    async def test_enter_on_a_placeholder_opens_no_detail_screen(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "enter")
                await _settle(pilot)
                assert not isinstance(app.screen, DetailScreen)

    @pytest.mark.asyncio
    async def test_flat_lists_get_an_inert_placeholder_row(self) -> None:
        issues = [
            MilestoneIssue(
                number=number,
                state=state,
                title=title,
                parent_number=905,
                is_epic=False,
            )
            for number, state, title in (
                (30, "CLOSED", "done a"),
                (31, "CLOSED", "done b"),
                (32, "OPEN", "live"),
                (33, "CLOSED", "done c"),
            )
        ]
        with (
            _patched_github() as client,
            patch.object(client, "list_issues_by_milestone", return_value=issues),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("c")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                issue_list = app.query_one("#sprint-list", IssueList)
                assert issue_list.option_count == 3
                assert issue_list.get_option_at_index(0).disabled
                assert issue_list.get_option_at_index(2).disabled
                assert issue_list.selected_issue_number == 32

    @pytest.mark.asyncio
    async def test_the_filter_leaves_the_run_holding_the_cursor_collapsed(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 913
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                run = tree.root.children[0].children[1]
                assert str(run.label) == "<2 issues filtered>"
                assert not run.is_expanded
                labels = _visible_labels(tree)
                assert not any("#913" in label or "#914" in label for label in labels)

    @pytest.mark.asyncio
    async def test_the_run_that_swallowed_the_cursor_issue_holds_the_cursor(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                cursor = app.query_one(IssueTree).cursor_node
                assert cursor is not None
                assert isinstance(cursor.data, FilteredNodeData)
                assert 913 in cursor.data.numbers

    @pytest.mark.asyncio
    async def test_nested_runs_retarget_the_cursor_to_the_outer_one(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_NESTED_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down", "right", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 917
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                epic = tree.root.children[0]
                outer = epic.children[1]
                assert epic.is_expanded
                assert not outer.is_expanded
                assert tree.cursor_node is outer
                assert isinstance(outer.data, FilteredNodeData)
                assert outer.data.numbers == (916,)

    @pytest.mark.asyncio
    async def test_an_open_issue_keeps_the_cursor_and_its_ancestors_reopen(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_DEEP_OPEN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down", "right", "down", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 932
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 932
                assert "#932 deep live" in _visible_labels(tree)

    @pytest.mark.asyncio
    async def test_goto_still_opens_a_run_to_reach_a_closed_issue(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
            patch.object(client, "fetch_parent_issue", side_effect=_parent_under_905),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "3", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 913
                assert tree.root.children[0].children[1].is_expanded

    @pytest.mark.asyncio
    async def test_toggling_the_filter_back_off_returns_the_cursor_to_the_issue(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                assert app.query_one(IssueTree).selected_issue_number == 913

    @pytest.mark.asyncio
    async def test_a_run_holds_the_cursor_of_an_epic_it_never_materialises(
        self,
    ) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("down", "down", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 860
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                run = tree.root.children[2]
                assert not run.is_expanded
                assert tree.cursor_node is run
                assert isinstance(run.data, FilteredNodeData)
                assert run.data.numbers == (860,)

    @pytest.mark.asyncio
    async def test_a_run_holds_the_cursor_of_the_sub_issue_it_replaced(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_NESTED_RUN
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right", "down", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 916
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                run = tree.root.children[0].children[1]
                assert not run.is_expanded
                assert tree.cursor_node is run
                assert isinstance(run.data, FilteredNodeData)
                assert run.data.numbers == (916,)

    @pytest.mark.asyncio
    async def test_a_root_run_holds_the_cursor_of_the_epic_it_covers(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("down", "down")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 853
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                run = tree.root.children[1]
                assert not run.is_expanded
                assert tree.cursor_node is run
                assert isinstance(run.data, FilteredNodeData)
                assert run.data.numbers == (852, 853)

    @pytest.mark.asyncio
    async def test_the_toggle_is_blocked_on_the_detail_screen(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("escape")
                await _settle(pilot)
                assert not any(
                    "filtered" in label
                    for label in _root_labels(app.query_one(IssueTree))
                )


@contextmanager
def _standalone_github() -> Generator[GitHubClient]:
    """A client whose milestone holds the standalone issues of
    `_MILESTONE_ISSUES` alongside the usual epics."""
    with (
        _patched_github() as client,
        patch.object(
            client, "list_issues_by_milestone", return_value=_MILESTONE_ISSUES
        ),
    ):
        yield client


@contextmanager
def _standalone_milestone_github(
    issues: list[MilestoneIssue] = _MILESTONE_ISSUES,
) -> Generator[GitHubClient]:
    """Like `_standalone_github`, but only the sprint milestone holds
    the standalone issues — so the backlog list cannot resolve one and
    goto has to reach it through the tree."""

    def _issues_of(milestone: str, **_kwargs: object) -> list[MilestoneIssue]:
        return issues if milestone == _SPRINT else _FLAT_ISSUES

    with (
        _patched_github() as client,
        patch.object(client, "list_issues_by_milestone", side_effect=_issues_of),
    ):
        yield client


class TestStandaloneSection:
    @pytest.mark.asyncio
    async def test_standalone_issues_land_in_a_section_after_the_epics(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                tree = _the_app(pilot).query_one(IssueTree)
                labels = _root_labels(tree)
                assert labels[-1] == "STANDALONE"
                assert [
                    str(child.label) for child in tree.root.children[-1].children
                ] == [
                    "#30 solo a",
                    "#31 solo b",
                    "#32 solo c",
                ]

    @pytest.mark.asyncio
    async def test_a_milestone_without_standalone_issues_gets_no_section(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                labels = _root_labels(_the_app(pilot).query_one(IssueTree))
                assert "STANDALONE" not in labels

    @pytest.mark.asyncio
    async def test_the_section_starts_expanded_and_the_epics_stay_collapsed(
        self,
    ) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                tree = _the_app(pilot).query_one(IssueTree)
                assert tree.root.children[-1].is_expanded
                assert not any(node.is_expanded for node in tree.root.children[:-1])

    @pytest.mark.asyncio
    async def test_an_epic_and_its_child_are_not_standalone(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                tree = _the_app(pilot).query_one(IssueTree)
                section = tree.root.children[-1]
                assert not any(
                    "#905" in str(child.label) or "#910" in str(child.label)
                    for child in section.children
                )

    @pytest.mark.asyncio
    async def test_hide_closed_collapses_closed_standalone_into_a_placeholder(
        self,
    ) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                section = app.query_one(IssueTree).root.children[-1]
                assert [str(child.label) for child in section.children] == [
                    "#30 solo a",
                    "<1 issue filtered>",
                    "#32 solo c",
                ]

    @pytest.mark.asyncio
    async def test_an_all_closed_section_keeps_a_placeholder(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_issues_by_milestone", return_value=_CLOSED_STANDALONE
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                section = app.query_one(IssueTree).root.children[-1]
                assert str(section.label) == "STANDALONE"
                assert [str(child.label) for child in section.children] == [
                    "<1 issue filtered>"
                ]

    @pytest.mark.asyncio
    async def test_the_filter_toggle_round_trips_the_section(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                section = app.query_one(IssueTree).root.children[-1]
                assert [str(child.label) for child in section.children] == [
                    "#30 solo a",
                    "#31 solo b",
                    "#32 solo c",
                ]

    @pytest.mark.asyncio
    async def test_expanding_the_section_or_its_placeholder_fetches_nothing(
        self,
    ) -> None:
        with (
            _standalone_github() as client,
            patch.object(client, "fetch_sub_issue_tree") as mock_subs,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("down", "down", "down", "down")
                await pilot.press("left", "right")
                await _settle(pilot)
                await pilot.press("down", "down", "right")
                await _settle(pilot)
                section = app.query_one(IssueTree).root.children[-1]
                placeholder = section.children[1]
                assert section.is_expanded
                assert placeholder.is_expanded
                assert [str(child.label) for child in placeholder.children] == [
                    "#31 solo b"
                ]
                mock_subs.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refresh_returns_the_cursor_to_a_standalone_issue(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press(
                    "down", "down", "down", "down", "down", "down", "down"
                )
                assert app.query_one(IssueTree).selected_issue_number == 31
                await pilot.press("r")
                await _settle(pilot)
                assert app.query_one(IssueTree).selected_issue_number == 31

    @pytest.mark.asyncio
    async def test_a_collapsed_section_stays_collapsed_across_a_refresh(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("down", "down", "down", "down", "down", "left")
                assert not app.query_one(IssueTree).root.children[-1].is_expanded
                await pilot.press("r")
                await _settle(pilot)
                assert not app.query_one(IssueTree).root.children[-1].is_expanded

    @pytest.mark.asyncio
    async def test_an_expanded_section_stays_expanded_across_a_refresh(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("r")
                await _settle(pilot)
                assert app.query_one(IssueTree).root.children[-1].is_expanded

    @pytest.mark.asyncio
    async def test_a_section_appearing_on_a_refresh_starts_expanded(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client,
                "list_issues_by_milestone",
                side_effect=[_FLAT_ISSUES, _MILESTONE_ISSUES],
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert "STANDALONE" not in _root_labels(app.query_one(IssueTree))
                await pilot.press("r")
                await _settle(pilot)
                section = app.query_one(IssueTree).root.children[-1]
                assert str(section.label) == "STANDALONE"
                assert section.is_expanded

    @pytest.mark.asyncio
    async def test_the_status_line_counts_standalone_issues(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                assert "Loaded 5 epics, 18 issues" in _status_text(_the_app(pilot))

    @pytest.mark.asyncio
    async def test_a_filtered_standalone_issue_is_still_reachable(self) -> None:
        with _standalone_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.reveal_loaded(31)
                assert tree.selected_issue_number == 31


class TestHelpModal:
    @pytest.mark.asyncio
    async def test_question_mark_toggles_help(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                assert isinstance(app.screen, HelpScreen)
                await pilot.press("question_mark")
                await _settle(pilot)
                assert not isinstance(app.screen, HelpScreen)

    @pytest.mark.asyncio
    async def test_lists_both_filter_toggles(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                panel = str(pilot.app.screen.query_one("#help-panel", Static).content)
                assert "n  Toggle 'soon' filter (backlog)" in panel
                assert "f  Toggle hide-closed" in panel

    @pytest.mark.asyncio
    async def test_main_screen_actions_blocked_while_modal_open(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_epics_by_milestone", return_value=_EPICS
            ) as mock_epics,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                await pilot.press("r")
                await _settle(pilot)
                assert isinstance(app.screen, HelpScreen)
                mock_epics.assert_called_once()


class TestConfiguredMilestones:
    """The milestones come from the config the app was handed, never
    from a constant baked into orbit."""

    @pytest.mark.asyncio
    async def test_the_epic_tree_fetches_the_configured_current_milestone(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_epics_by_milestone", return_value=[]
            ) as mock_epics,
            patch.object(
                client, "list_issues_by_milestone", return_value=[]
            ) as mock_issues,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                mock_epics.assert_called_once_with(_SPRINT)
                mock_issues.assert_any_call(_SPRINT)

    @pytest.mark.asyncio
    async def test_the_epic_picker_fetches_the_configured_current_milestone(
        self,
    ) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "list_epics_by_milestone", return_value=_EPICS
            ) as mock_epics,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                mock_epics.reset_mock()
                await pilot.press("m")
                await _settle(pilot)
                assert isinstance(_the_app(pilot).screen, EpicPickerScreen)
                mock_epics.assert_called_once_with(_SPRINT)

    def test_run_tui_hands_the_app_the_loaded_config(self) -> None:
        client = GitHubClient(GitHubGraphQL(FakeTransport([])), Repo(*_REPO))
        config = ProjectConfig(milestones=_APP_MILESTONES, commands=(_SPAWN_COMMAND,))
        with patch("orbit.tui.app.OrbitApp") as mock_app:
            run_tui(client, config)
        mock_app.assert_called_once_with(client, _APP_MILESTONES, (_SPAWN_COMMAND,))


class TestMoveAction:
    @pytest.mark.asyncio
    async def test_move_via_epic_picker(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_MOVE, return_value=_MOVE_RESULT) as mock_move,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("m")
                await _settle(pilot)
                assert isinstance(app.screen, EpicPickerScreen)
                # The picker highlights its first option (epic #905);
                # enter selects it directly.
                await pilot.press("enter")
                await _settle(pilot)
                mock_move.assert_called_once_with(client, 20, 905)
                assert "Moved #20" in _status_text(app)

    @pytest.mark.asyncio
    async def test_picker_excludes_closed_epics(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("m")
                await _settle(pilot)
                screen = app.screen
                assert isinstance(screen, EpicPickerScreen)
                assert screen.query_one(OptionList).option_count == 1

    @pytest.mark.asyncio
    async def test_already_done_move_lands_on_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch(
                _PATCH_MOVE,
                side_effect=AlreadyDoneError("already under epic #905"),
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("m")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert "already under epic #905" in _status_text(app)

    @pytest.mark.asyncio
    async def test_move_reports_reopened_chain(self) -> None:
        result = _MOVE_RESULT.model_copy(update={"reopened": (905, 900)})
        with (
            _patched_github() as client,
            patch(_PATCH_MOVE, return_value=result),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("m")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                status = _status_text(app)
                assert "Moved #20" in status
                assert "reopened #905, #900" in status

    @pytest.mark.asyncio
    async def test_already_done_with_reopen_does_not_claim_a_move(self) -> None:
        result = _MOVE_RESULT.model_copy(
            update={"reopened": (905,), "already_done": True}
        )
        with (
            _patched_github() as client,
            patch(_PATCH_MOVE, return_value=result),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("m")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                status = _status_text(app)
                assert "already under epic #905" in status
                assert "reopened #905" in status
                assert "Moved #20" not in status

    @pytest.mark.asyncio
    async def test_picker_load_error_dismisses_and_reports(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client,
                "list_epics_by_milestone",
                # The app's own load comes first; the picker's is the one
                # under test.
                side_effect=[_EPICS, RuntimeError("boom")],
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("m")
                await _settle(pilot)
                assert not isinstance(app.screen, EpicPickerScreen)
                assert "Error loading epics: boom" in _status_text(app)

    @pytest.mark.asyncio
    async def test_escape_cancels_move(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_MOVE, return_value=_MOVE_RESULT) as mock_move,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("m")
                await _settle(pilot)
                await pilot.press("escape")
                await _settle(pilot)
                assert not isinstance(app.screen, EpicPickerScreen)
                mock_move.assert_not_called()


class TestScheduleAction:
    @pytest.mark.asyncio
    async def test_schedule_via_milestone_picker(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT) as mock_schedule,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("s")
                await _settle(pilot)
                assert isinstance(app.screen, MilestonePickerScreen)
                # The picker highlights its first option ("degraded
                # state"); enter selects it directly.
                await pilot.press("enter")
                await _settle(pilot)
                mock_schedule.assert_called_once_with(client, 20, "degraded state")
                assert (
                    "Scheduled #20 → degraded state (detached from epic #905)"
                    in _status_text(app)
                )

    @pytest.mark.asyncio
    async def test_schedule_uses_the_selected_milestone(self) -> None:
        # Guards against passing a hardcoded/first milestone: navigate
        # past the highlighted option and select the second one.
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT) as mock_schedule,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("s")
                await _settle(pilot)
                assert isinstance(app.screen, MilestonePickerScreen)
                await pilot.press("down")
                await pilot.press("enter")
                await _settle(pilot)
                mock_schedule.assert_called_once_with(client, 20, "Backlog")

    @pytest.mark.asyncio
    async def test_picker_lists_open_milestones(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("s")
                await _settle(pilot)
                screen = app.screen
                assert isinstance(screen, MilestonePickerScreen)
                assert screen.query_one(OptionList).option_count == len(_MILESTONES)

    @pytest.mark.asyncio
    async def test_schedule_without_epic_omits_detachment_note(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT_NO_EPIC),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("s")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert "Scheduled #20 → degraded state" in _status_text(app)
                assert "detached" not in _status_text(app)

    @pytest.mark.asyncio
    async def test_picker_load_error_dismisses_and_reports(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "list_milestones", side_effect=RuntimeError("boom")),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("s")
                await _settle(pilot)
                assert not isinstance(app.screen, MilestonePickerScreen)
                assert "Error loading milestones: boom" in _status_text(app)

    @pytest.mark.asyncio
    async def test_escape_cancels_schedule(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, return_value=_SCHEDULE_RESULT) as mock_schedule,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("s")
                await _settle(pilot)
                await pilot.press("escape")
                await _settle(pilot)
                assert not isinstance(app.screen, MilestonePickerScreen)
                mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_error_lands_on_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SCHEDULE, side_effect=RuntimeError("milestone not found")),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("s")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert "milestone not found" in _status_text(app)


class TestImmediateActions:
    @pytest.mark.asyncio
    async def test_close_issue(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_CLOSE) as mock_close,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("x")
                await _settle(pilot)
                mock_close.assert_called_once_with(
                    client, 20, CloseReason.COMPLETED, Surface.TUI
                )
                assert "Closed #20" in _status_text(app)

    @pytest.mark.asyncio
    async def test_comment_failure_lands_on_status_line(self) -> None:
        with _patched_github() as client:
            with (
                patch(_PATCH_CLOSE, new=close_issue),
                patch.object(
                    client, "add_comment", side_effect=RuntimeError("comment rejected")
                ),
                patch.object(client, "close_issue_by_id") as mock_close,
            ):
                async with _app(client).run_test() as pilot:
                    await _settle(pilot)
                    app = _the_app(pilot)
                    await pilot.press("b")
                    await _settle(pilot)
                    await pilot.press("x")
                    await _settle(pilot)
                    assert "comment rejected" in _status_text(app)
                    assert "Closed #20" not in _status_text(app)
            mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_opens_issue_in_browser(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_BROWSER) as mock_browser,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("t")
                await _settle(pilot)
                mock_browser.assert_called_once_with(
                    "https://github.com/example-org/example-repo/issues/20"
                )
                assert "Opened #20 in browser" in _status_text(app)

    @pytest.mark.asyncio
    async def test_already_done_close_lands_on_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch(
                _PATCH_CLOSE,
                side_effect=AlreadyDoneError("Issue #20 is already closed"),
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("x")
                await _settle(pilot)
                assert "already closed" in _status_text(app)


class TestReservedKeys:
    def test_covers_every_builtin_binding(self) -> None:
        # reserved_keys() filters BINDINGS by isinstance(Binding); if a
        # binding were ever written in Textual's tuple form it would
        # drop out silently and that key would become claimable by a
        # config, shadowing a built-in.
        reserved = OrbitApp.reserved_keys()
        declared = {
            binding.key for binding in OrbitApp.BINDINGS if isinstance(binding, Binding)
        }
        assert declared <= reserved
        assert len(declared) == len(OrbitApp.BINDINGS)
        # The exit keys are the ones a config most plausibly reaches for.
        assert {"q", "escape"} <= reserved


class TestCustomCommands:
    @pytest.mark.asyncio
    async def test_spawn_mode_runs_the_rendered_command(self) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                mock_spawn.assert_called_once_with("vwt 20")
                assert "Worktree" in _status_text(app)

    @pytest.mark.asyncio
    async def test_branch_placeholder_prompts_then_runs_with_the_typed_value(
        self,
    ) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_BRANCH_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                assert isinstance(app.screen, BranchPromptScreen)
                mock_spawn.assert_not_called()
                await pilot.press("f", "i", "x")
                await pilot.press("enter")
                await _settle(pilot)
                mock_spawn.assert_called_once_with("vwt fix 20")

    @pytest.mark.asyncio
    async def test_branch_name_may_contain_the_close_key(self) -> None:
        # q closes any other screen, but the focused Input must see
        # printable keys first — otherwise "quick-fix" is untypeable.
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_BRANCH_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                await pilot.press("q", "u", "i", "c", "k")
                await _settle(pilot)
                assert isinstance(app.screen, BranchPromptScreen)
                await pilot.press("enter")
                await _settle(pilot)
                mock_spawn.assert_called_once_with("vwt quick 20")

    @pytest.mark.asyncio
    async def test_cancelling_the_branch_prompt_runs_nothing(self) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_BRANCH_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                await pilot.press("escape")
                await _settle(pilot)
                mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_help_lists_the_custom_key_and_label(self) -> None:
        with _patched_github() as client:
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                panel = app.screen.query_one("#help-panel", Static)
                rendered = str(panel.content)
                assert "Worktree" in rendered
                assert "w" in rendered

    @pytest.mark.asyncio
    async def test_custom_keys_fire_over_the_detail_screen(self) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("w")
                await _settle(pilot)
                mock_spawn.assert_called_once_with("vwt 905")
                assert not isinstance(app.screen, DetailScreen)
                assert "Ran Worktree on #905" in _status_text(app)

    @pytest.mark.asyncio
    async def test_branch_command_over_detail_prompts_on_main_screen(self) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN):
            async with _app(client, commands=[_BRANCH_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("w")
                await _settle(pilot)
                assert isinstance(app.screen, BranchPromptScreen)
                assert not any(
                    isinstance(screen, DetailScreen) for screen in app.screen_stack
                )

    @pytest.mark.asyncio
    async def test_custom_keys_do_not_fire_over_a_modal(self) -> None:
        with _patched_github() as client, patch(_PATCH_SPAWN) as mock_spawn:
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                assert isinstance(app.screen, HelpScreen)
                await pilot.press("w")
                await _settle(pilot)
                mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_suspend_mode_runs_attached_while_the_app_is_suspended(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_RUN_ATTACHED) as mock_run,
            patch.object(OrbitApp, "suspend") as mock_suspend,
        ):
            async with _app(client, commands=[_SUSPEND_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("v")
                await _settle(pilot)
                mock_run.assert_called_once_with("vim 20")
                mock_suspend.assert_called_once()

    @pytest.mark.asyncio
    async def test_suspend_mode_reports_a_failing_exit_code(self) -> None:
        # We waited for this one, so we know it failed; saying "Ran Edit
        # on #20" would be a lie the user acts on.
        with (
            _patched_github() as client,
            patch(_PATCH_RUN_ATTACHED, return_value=127),
            patch.object(OrbitApp, "suspend"),
        ):
            async with _app(client, commands=[_SUSPEND_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("v")
                await _settle(pilot)
                assert "exited with status 127" in _status_text(app)

    @pytest.mark.asyncio
    async def test_launch_failure_lands_on_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch(_PATCH_SPAWN, side_effect=OSError("No such file or directory")),
        ):
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                assert "No such file or directory" in _status_text(app)
                assert app.is_running

    @pytest.mark.asyncio
    async def test_no_selected_issue_runs_nothing(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "list_issues_by_milestone", return_value=[]),
            patch(_PATCH_SPAWN) as mock_spawn,
        ):
            async with _app(client, commands=[_SPAWN_COMMAND]).run_test() as pilot:
                await _settle(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("w")
                await _settle(pilot)
                mock_spawn.assert_not_called()


class TestGotoIssue:
    @pytest.mark.asyncio
    async def test_jumps_within_the_visible_list_without_a_lookup(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("2", "1", "enter")
                await _settle(pilot)
                issue_list = app.query_one("#backlog-list", IssueList)
                assert issue_list.selected_issue_number == 21
                assert "Jumped to #21" in _status_text(app)
                mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_jumps_into_a_collapsed_epic_and_switches_to_epics(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.display
                assert tree.root.children[0].is_expanded
                assert tree.selected_issue_number == 912
                assert "Jumped to #912 (switched to epics)" in _status_text(app)

    @pytest.mark.asyncio
    async def test_jumps_within_the_loaded_tree_without_a_lookup(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS
            ) as mock_subs,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                assert mock_subs.call_count == 1
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 912
                mock_parent.assert_not_called()
                assert mock_subs.call_count == 1
                assert "Jumped to #912" in _status_text(app)
                assert "switched" not in _status_text(app)

    @pytest.mark.asyncio
    async def test_jumps_to_an_epic_itself(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("8", "5", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 852
                parent.assert_not_called()
                assert "Jumped to #852" in _status_text(app)

    @pytest.mark.asyncio
    async def test_jumps_to_a_closed_issue(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "1", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 911
                assert "Jumped to #911 (switched to epics)" in _status_text(app)

    @pytest.mark.asyncio
    async def test_the_walk_stops_at_the_first_epic_in_the_tree(self) -> None:
        # 910's parent 905 is a root epic, so the walk must stop there
        # rather than continuing up to whatever 905 itself hangs from.
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "0", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 910
                parent.assert_called_once_with(910)

    @pytest.mark.asyncio
    async def test_an_unreachable_issue_is_reported_and_the_view_stays(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("7", "7", "7", "7", "enter")
                await _settle(pilot)
                assert "#7777 is not in the current milestone" in _status_text(app)
                assert app.query_one("#backlog-list", IssueList).display
                assert not app.query_one(IssueTree).display

    @pytest.mark.asyncio
    async def test_an_epic_outside_the_milestone_is_reported(self) -> None:
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("8", "8", "8", "8", "enter")
                await _settle(pilot)
                assert "#8888 is not in the current milestone" in _status_text(app)
                assert app.query_one("#backlog-list", IssueList).display
                assert not app.query_one(IssueTree).display

    @pytest.mark.asyncio
    async def test_a_lookup_failure_lands_on_the_status_line(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=RuntimeError("boom")
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "2", "enter")
                await _settle(pilot)
                assert "Error: boom" in _status_text(app)
                assert app.is_running

    @pytest.mark.asyncio
    async def test_a_missing_issue_is_reported_and_the_app_survives(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=IssueNotFoundError(9999)
            ),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "9", "9", "9", "enter")
                await _settle(pilot)
                assert "#9999 does not exist" in _status_text(app)
                assert app.is_running

    def test_the_jump_key_is_reserved(self) -> None:
        assert "g" in OrbitApp.reserved_keys()

    @pytest.mark.asyncio
    async def test_does_not_fire_over_the_detail_screen(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)
                await pilot.press("g")
                await _settle(pilot)
                assert isinstance(app.screen, DetailScreen)

    @pytest.mark.asyncio
    async def test_does_not_fire_over_a_modal(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("question_mark")
                await _settle(pilot)
                assert isinstance(app.screen, HelpScreen)
                await pilot.press("g")
                await _settle(pilot)
                assert isinstance(app.screen, HelpScreen)

    @pytest.mark.asyncio
    async def test_a_hash_prefix_and_whitespace_are_accepted(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("space", "#", "2", "1", "space", "enter")
                await _settle(pilot)
                issue_list = app.query_one("#backlog-list", IssueList)
                assert issue_list.selected_issue_number == 21
                assert "Jumped to #21" in _status_text(app)
                mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_non_number_keeps_the_prompt_open_with_an_error(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("a", "b", "c", "enter")
                await _settle(pilot)
                assert isinstance(app.screen, IssueNumberPromptScreen)
                label = app.screen.query_one("#issue-number-prompt-label", Static)
                assert "Not an issue number" in str(label.content)
                mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_number_outside_the_list_leaves_the_highlight_alone(self) -> None:
        with _patched_github() as client:
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("down")
                await _settle(pilot)
                issue_list = app.query_one("#backlog-list", IssueList)
                assert issue_list.selected_issue_number == 21
                assert not issue_list.highlight_issue(9999)
                assert issue_list.selected_issue_number == 21

    @pytest.mark.asyncio
    async def test_loads_the_epics_when_the_tree_is_empty(self) -> None:
        # The initial load failed, so the tree has no epics to search;
        # the jump must fetch them rather than report a missing epic.
        with (
            _patched_github() as client,
            patch.object(
                client,
                "list_epics_by_milestone",
                side_effect=[RuntimeError("boom"), _EPICS],
            ) as mock_epics,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert not app.query_one(IssueTree).root.children
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "0", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 910
                assert mock_epics.call_count == 2
                assert "Jumped to #910" in _status_text(app)

    @pytest.mark.asyncio
    async def test_jumps_to_an_epic_after_loading_an_empty_tree(self) -> None:
        # The freshly loaded tree holds 852 as a root epic, so the jump
        # must land on it rather than asking who its parent is.
        with (
            _patched_github() as client,
            patch.object(
                client,
                "list_epics_by_milestone",
                side_effect=[RuntimeError("boom"), _EPICS],
            ),
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert not app.query_one(IssueTree).root.children
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("8", "5", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 852
                mock_parent.assert_not_called()
                assert "Jumped to #852" in _status_text(app)

    @pytest.mark.asyncio
    async def test_empty_input_keeps_the_prompt_open(self) -> None:
        # The sibling BranchPromptScreen dismisses with None on empty
        # input; here that would read as a cancel and lose the jump.
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("enter")
                await _settle(pilot)
                assert isinstance(app.screen, IssueNumberPromptScreen)
                label = app.screen.query_one("#issue-number-prompt-label", Static)
                assert "Not an issue number" in str(label.content)

    @pytest.mark.asyncio
    async def test_a_loaded_target_reports_the_switch_to_epics(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS
            ) as mock_subs,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.display
                assert tree.selected_issue_number == 912
                assert mock_subs.call_count == 1
                mock_parent.assert_not_called()
                assert "Jumped to #912 (switched to epics)" in _status_text(app)

    @pytest.mark.asyncio
    async def test_a_superscript_digit_is_rejected_not_crashed(self) -> None:
        # str.isdigit() accepts these but int() does not, so the guard
        # has to be isdecimal or the submit handler raises.
        with (
            _patched_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("²", "enter")
                await _settle(pilot)
                assert app.is_running
                assert isinstance(app.screen, IssueNumberPromptScreen)

    @pytest.mark.asyncio
    async def test_scrolls_a_target_below_the_fold_into_view(self) -> None:
        many = [
            Epic(number=n, state="OPEN", title=f"epic {n}", open_count=0, total_count=0)
            for n in range(900, 940)
        ]
        with (
            _patched_github() as client,
            patch.object(client, "list_epics_by_milestone", return_value=many),
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test(size=(80, 12)) as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "3", "8", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 938
                top = tree.scroll_offset.y
                assert top <= tree.cursor_line < top + tree.size.height


class TestGotoStandalone:
    @pytest.mark.asyncio
    async def test_jumps_to_a_standalone_issue_in_the_milestone(self) -> None:
        with (
            _standalone_milestone_github() as client,
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("b")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("3", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.display
                assert tree.selected_issue_number == 32
                assert "Jumped to #32 (switched to epics)" in _status_text(app)
                mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_jumps_to_a_standalone_issue_with_an_empty_tree(self) -> None:
        with (
            _standalone_milestone_github() as client,
            patch.object(
                client,
                "list_epics_by_milestone",
                side_effect=[RuntimeError("boom"), _EPICS],
            ),
            patch.object(
                client, "fetch_parent_issue", side_effect=_parent_of
            ) as mock_parent,
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                assert not app.query_one(IssueTree).root.children
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("3", "2", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 32
                assert "Jumped to #32" in _status_text(app)
                mock_parent.assert_not_called()

    @pytest.mark.asyncio
    async def test_jumps_to_a_filtered_standalone_issue_without_unfiltering(
        self,
    ) -> None:
        with (
            _standalone_milestone_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("3", "1", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 31
                assert tree.hide_closed
                assert "Jumped to #31" in _status_text(app)

    @pytest.mark.asyncio
    async def test_the_siblings_of_a_filtered_target_are_visible_after_the_jump(
        self,
    ) -> None:
        with (
            _standalone_milestone_github(_MILESTONE_ISSUES_CLOSED_PAIR) as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("3", "1", "enter")
                await _settle(pilot)
                labels = _visible_labels(app.query_one(IssueTree))
                assert "#31 solo b" in labels
                assert "#33 solo d" in labels
                assert "#30 solo a" in labels
                assert "#32 solo c" in labels

    @pytest.mark.asyncio
    async def test_jumps_to_a_filtered_epic_child_the_same_way(self) -> None:
        with (
            _patched_github() as client,
            patch.object(
                client, "fetch_sub_issue_tree", return_value=_SUBS_905_WITH_DEAD_RUN
            ),
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("right")
                await _settle(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("9", "1", "3", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 913
                assert tree.hide_closed
                labels = _visible_labels(tree)
                assert "#914 0/1 dead parent" in labels
                assert "#910 leaf a" in labels

    @pytest.mark.asyncio
    async def test_a_parentless_issue_outside_the_milestone_is_reported_as_such(
        self,
    ) -> None:
        with (
            _standalone_milestone_github() as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("7", "7", "7", "7", "enter")
                await _settle(pilot)
                status = _status_text(app)
                assert "#7777 is not in the current milestone" in status
                assert "has no epic" not in status

    @pytest.mark.asyncio
    async def test_jumps_to_a_standalone_issue_whose_whole_section_is_filtered(
        self,
    ) -> None:
        with (
            _standalone_milestone_github(_CLOSED_STANDALONE) as client,
            patch.object(client, "fetch_parent_issue", side_effect=_parent_of),
        ):
            async with _app(client).run_test() as pilot:
                await _settle(pilot)
                app = _the_app(pilot)
                await pilot.press("f")
                await _settle(pilot)
                await pilot.press("g")
                await _settle(pilot)
                await pilot.press("3", "0", "enter")
                await _settle(pilot)
                tree = app.query_one(IssueTree)
                assert tree.selected_issue_number == 30
                assert tree.hide_closed
                assert "Jumped to #30" in _status_text(app)
