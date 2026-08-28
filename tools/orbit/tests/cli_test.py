from __future__ import annotations

import io
import json
from collections.abc import Generator
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import pytest
from click import UsageError
from click.testing import CliRunner

from ghgql.fake import FakeTransport
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL
from orbit.cli import cli, resolve_body
from orbit.config import ConfigError, CustomCommand, Milestones, ProjectConfig
from orbit.github.client import GitHubClient
from orbit.github.models import (
    AlreadyDoneError,
    CloseReason,
    CloseResult,
    CreatedIssue,
    Epic,
    Issue,
    IssueDetail,
    MilestoneIssue,
    MoveResult,
    ReorderResult,
    ScheduleResult,
    SubIssueData,
    Surface,
)

# Deliberately not this repo's own sprint name: an assertion that passes
# against a reinstated constant would prove nothing.
_CURRENT = "sprint 42"
_BACKLOG = "Icebox"
_CONFIG = ProjectConfig(milestones=Milestones(current=_CURRENT, backlog=_BACKLOG))


def _supply_config() -> Generator[None]:
    """Every command loads `.orbit.toml`; these tests supply it."""
    with patch("orbit.cli.load_config", return_value=_CONFIG):
        yield


project_config = pytest.fixture(autouse=True)(_supply_config)


def _client() -> GitHubClient:
    """A client whose GitHub calls every test patches out."""
    return GitHubClient(
        GitHubGraphQL(FakeTransport([])), Repo("example-org", "example-repo")
    )


def _runner() -> CliRunner:
    return CliRunner()


def _split_runner() -> CliRunner:
    """Runner that keeps stdout and stderr separate, so --json stdout
    can be parsed without informational stderr messages polluting it."""
    return CliRunner(mix_stderr=False)


def _fake_label_id(name: str) -> str:
    return f"LA_{name}"


class TestClientResolution:
    def test_subcommand_help_builds_no_client(self) -> None:
        # Building one shells out to git for the repo, so --help would
        # need a checkout with an origin remote.
        with patch("orbit.cli.github_client") as mock_client:
            result = _runner().invoke(cli, ["sprint", "--help"])
        assert result.exit_code == 0
        mock_client.assert_not_called()

    def test_a_command_builds_one_client(self) -> None:
        client = _client()
        with (
            patch("orbit.cli.github_client", return_value=client) as mock_client,
            patch.object(client, "list_issues_by_milestone", return_value=[]),
        ):
            result = _runner().invoke(cli, ["sprint"])
        assert result.exit_code == 0
        mock_client.assert_called_once_with()


class TestBareInvocation:
    def test_launches_tui_with_the_loaded_config(self) -> None:
        client = _client()
        config = ProjectConfig(
            milestones=_CONFIG.milestones,
            commands=(CustomCommand(key="w", label="Worktree", run="vwt {issue}"),),
        )
        with (
            patch("orbit.cli.load_config", return_value=config),
            patch("orbit.cli.run_tui") as mock_run,
        ):
            result = _runner().invoke(cli, [], obj=client)
        mock_run.assert_called_once_with(client, config)
        assert result.exit_code == 0

    def test_broken_config_refuses_to_start(self) -> None:
        client = _client()
        # A config that exists but is wrong is a startup failure: better
        # a loud error than a TUI whose keys quietly do nothing.
        error = ConfigError(Path(".orbit.toml"), ['commands[0]: key "e" is taken'])
        with (
            patch("orbit.cli.load_config", side_effect=error),
            patch("orbit.cli.run_tui") as mock_run,
        ):
            result = _runner().invoke(cli, [], obj=client)
        mock_run.assert_not_called()
        assert result.exit_code != 0
        assert 'key "e" is taken' in result.output

    def test_help_flag_prints_usage_with_examples(self) -> None:
        client = _client()
        result = _runner().invoke(cli, ["--help"], obj=client)
        assert "sprint" in result.output
        assert "backlog" in result.output
        assert "soon" in result.output
        assert "epics" in result.output
        assert "subs" in result.output
        assert "parent" in result.output
        assert "move" in result.output
        assert "schedule" in result.output
        assert "create-epic" in result.output
        assert "Create a leaf issue" in result.output
        assert result.exit_code == 0

    def test_help_flag_does_not_launch_tui(self) -> None:
        client = _client()
        with patch("orbit.cli.run_tui") as mock_run:
            result = _runner().invoke(cli, ["--help"], obj=client)
        mock_run.assert_not_called()
        assert result.exit_code == 0


class TestConfiguredMilestones:
    """Milestones come from `.orbit.toml`, and every command needs it."""

    @pytest.mark.parametrize("command", [["sprint"], ["show", "905"]])
    def test_a_broken_config_fails_every_command(self, command: list[str]) -> None:
        # Even a command that reads no milestone fails, so a broken
        # config is one uniform error rather than a puzzle about which
        # subcommands still work.
        client = _client()
        error = ConfigError(Path(".orbit.toml"), ["does not exist"])
        with patch("orbit.cli.load_config", side_effect=error):
            result = _runner().invoke(cli, command, obj=client)
        assert result.exit_code != 0
        assert "does not exist" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_schedule_help_describes_the_default_without_naming_it(self) -> None:
        result = _runner().invoke(cli, ["schedule", "--help"])
        assert "defaults to the current milestone" in result.output
        assert _CURRENT not in result.output

    def test_create_passes_the_configured_milestones(self) -> None:
        client = _client()
        with patch("orbit.cli.create_leaf") as mock_create:
            result = _runner().invoke(
                cli, ["create", "standalone", "a leaf"], obj=client
            )
        assert result.exit_code == 0
        mock_create.assert_called_once_with(
            client, _CONFIG.milestones, "standalone", "a leaf", None, ()
        )

    def test_create_epic_targets_the_configured_current_milestone(self) -> None:
        client = _client()
        with patch("orbit.cli.create_epic") as mock_create:
            result = _runner().invoke(cli, ["create-epic", "an epic"], obj=client)
        assert result.exit_code == 0
        mock_create.assert_called_once_with(client, _CURRENT, "an epic", [], None, ())


class TestUnknownCommand:
    def test_prints_usage_and_exits_nonzero(self) -> None:
        client = _client()
        result = _runner().invoke(cli, ["bogus"], obj=client)
        assert result.exit_code != 0


def _issue_numbers(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines if line.startswith("#")]


def _milestone_issue(
    number: int,
    *,
    state: str = "OPEN",
    title: str = "An issue",
    parent: int | None = None,
    is_epic: bool = False,
) -> MilestoneIssue:
    return MilestoneIssue(
        number=number,
        state=state,
        title=title,
        parent_number=parent,
        is_epic=is_epic,
    )


class TestSprint:
    def test_passes_current_milestone(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]) as mock:
            _ = _runner().invoke(cli, ["sprint"], obj=client)
        mock.assert_called_once_with(_CURRENT)

    def test_lists_issues(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(42, title="Fix the widget", parent=5),
            _milestone_issue(100, state="CLOSED", title="Add tests", parent=5),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--status", "ALL"], obj=client)
        assert "#42" in result.output
        assert "Fix the widget" in result.output
        assert "#100" in result.output
        assert "Add tests" in result.output
        assert result.exit_code == 0

    def test_json_flag(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(42, title="Fix the widget", parent=5),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 42
        assert data[0]["state"] == "OPEN"
        assert data[0]["title"] == "Fix the widget"
        assert result.exit_code == 0

    def test_json_is_a_flat_list_carrying_the_new_fields(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(5, title="An epic", is_epic=True),
            _milestone_issue(6, title="Its child", parent=5),
            _milestone_issue(7, title="(20) a one-off"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert [row["number"] for row in data] == [5, 6, 7]
        assert [row["parent_number"] for row in data] == [None, 5, None]
        assert [row["is_epic"] for row in data] == [True, False, False]

    def test_standalone_issues_print_under_their_own_heading(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(5, title="An epic", is_epic=True),
            _milestone_issue(6, title="Its child", parent=5),
            _milestone_issue(7, title="(20) a one-off"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint"], obj=client)
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        heading = lines.index("STANDALONE")
        assert _issue_numbers(lines[:heading]) == ["#5", "#6"]
        assert _issue_numbers(lines[heading:]) == ["#7"]
        assert result.exit_code == 0

    def test_no_empty_main_table_above_the_standalone_heading(self) -> None:
        client = _client()
        with patch.object(
            client, "list_issues_by_milestone", return_value=[_milestone_issue(7)]
        ):
            result = _runner().invoke(cli, ["sprint"], obj=client)
        assert "No issues found" not in result.output
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        heading = lines.index("STANDALONE")
        assert _issue_numbers(lines[heading:]) == ["#7"]
        assert result.exit_code == 0

    def test_no_standalone_heading_when_the_filter_drops_every_one(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(5, title="An epic", is_epic=True),
            _milestone_issue(7, state="CLOSED", title="(20) a one-off"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--status", "OPEN"], obj=client)
        assert "STANDALONE" not in result.output
        assert result.exit_code == 0

    def test_no_standalone_heading_when_every_issue_is_structured(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(5, title="An epic", is_epic=True),
            _milestone_issue(6, title="Its child", parent=5),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint"], obj=client)
        assert "STANDALONE" not in result.output
        assert result.exit_code == 0

    def test_empty_sprint(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]):
            result = _runner().invoke(cli, ["sprint"], obj=client)
        assert "No issues found" in result.output
        assert result.exit_code == 0

    def test_empty_sprint_json(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]):
            result = _runner().invoke(cli, ["sprint", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert data == []
        assert result.exit_code == 0


class TestBacklog:
    def test_lists_issues(self) -> None:
        client = _client()
        issues = [
            Issue(number=10, state="OPEN", title="Backlog item A"),
            Issue(number=20, state="OPEN", title="Backlog item B"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["backlog"], obj=client)
        assert "#10" in result.output
        assert "Backlog item A" in result.output
        assert "#20" in result.output
        assert "Backlog item B" in result.output
        assert result.exit_code == 0

    def test_json_flag(self) -> None:
        client = _client()
        issues = [
            Issue(number=10, state="OPEN", title="Backlog item A"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["backlog", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 10
        assert data[0]["state"] == "OPEN"
        assert data[0]["title"] == "Backlog item A"
        assert result.exit_code == 0

    def test_empty_backlog(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]):
            result = _runner().invoke(cli, ["backlog"], obj=client)
        assert "No issues found" in result.output
        assert result.exit_code == 0

    def test_passes_backlog_milestone(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]) as mock:
            _runner().invoke(cli, ["backlog"], obj=client)
        mock.assert_called_once_with(_BACKLOG)


class TestSoon:
    def test_lists_issues(self) -> None:
        client = _client()
        issues = [
            Issue(number=30, state="OPEN", title="Soon item"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["soon"], obj=client)
        assert "#30" in result.output
        assert "Soon item" in result.output
        assert result.exit_code == 0

    def test_json_flag(self) -> None:
        client = _client()
        issues = [
            Issue(number=30, state="OPEN", title="Soon item"),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["soon", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 30
        assert data[0]["state"] == "OPEN"
        assert data[0]["title"] == "Soon item"
        assert result.exit_code == 0

    def test_empty_soon(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]):
            result = _runner().invoke(cli, ["soon"], obj=client)
        assert "No issues found" in result.output
        assert result.exit_code == 0

    def test_passes_backlog_milestone_and_soon_label(self) -> None:
        client = _client()
        with patch.object(client, "list_issues_by_milestone", return_value=[]) as mock:
            _runner().invoke(cli, ["soon"], obj=client)
        mock.assert_called_once_with(_BACKLOG, label="soon")


class TestFilteredRunPlaceholders:
    def _issues(self) -> list[MilestoneIssue]:
        return [
            _milestone_issue(99, state="OPEN", title="Apple", parent=1),
            _milestone_issue(50, state="OPEN", title="Zebra", parent=1),
            _milestone_issue(3, state="CLOSED", title="Mango", parent=1),
        ]

    def test_placeholder_sits_where_the_dropped_issue_sorts(self) -> None:
        client = _client()
        with patch.object(
            client, "list_issues_by_milestone", return_value=self._issues()
        ):
            result = _runner().invoke(
                cli, ["sprint", "--status", "OPEN", "--sort", "title"], obj=client
            )
        assert result.exit_code == 0
        lines = [line for line in result.output.split("\n") if line.strip()]
        rendered = [
            line.strip().split()[0]
            for line in lines
            if line.strip().startswith(("#", "<"))
        ]
        assert rendered == ["#99", "<1", "#50"]

    def test_json_omits_placeholders(self) -> None:
        client = _client()
        with patch.object(
            client, "list_issues_by_milestone", return_value=self._issues()
        ):
            result = _split_runner().invoke(
                cli, ["sprint", "--status", "OPEN", "--json"], obj=client
            )
        assert result.exit_code == 0
        data = cast(list[dict[str, object]], json.loads(result.stdout))
        assert [i["number"] for i in data] == [99, 50]


class TestSortFlag:
    def test_sort_by_title(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(50, state="OPEN", title="Mango", parent=1),
            _milestone_issue(3, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(99, state="OPEN", title="Apple", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(
                cli, ["sprint", "--sort", "title", "--status", "ALL"], obj=client
            )
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#99", "#50", "#3"]
        assert result.exit_code == 0

    def test_sort_by_number_reverse(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(3, state="OPEN", title="Apple", parent=1),
            _milestone_issue(99, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(50, state="OPEN", title="Mango", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(
                cli,
                ["sprint", "--sort", "number", "--reverse", "--status", "ALL"],
                obj=client,
            )
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#99", "#50", "#3"]
        assert result.exit_code == 0

    def test_epics_sort_by_progress(self) -> None:
        client = _client()
        epics = [
            Epic(number=1, state="OPEN", title="Done", open_count=0, total_count=5),
            Epic(number=2, state="OPEN", title="Half", open_count=3, total_count=6),
            Epic(number=3, state="OPEN", title="All open", open_count=4, total_count=4),
            Epic(number=4, state="OPEN", title="No subs", open_count=0, total_count=0),
        ]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            result = _runner().invoke(cli, ["epics", "--sort", "progress"], obj=client)
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#3", "#2", "#1", "#4"]
        assert result.exit_code == 0

    def test_reverse_is_exact_mirror_of_ascending(self) -> None:
        client = _client()
        epics = [
            Epic(number=10, state="OPEN", title="A", open_count=1, total_count=1),
            Epic(number=20, state="OPEN", title="B", open_count=1, total_count=1),
            Epic(number=30, state="OPEN", title="C", open_count=0, total_count=1),
        ]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            asc = _runner().invoke(
                cli, ["epics", "--sort", "progress", "--json"], obj=client
            )
            desc = _runner().invoke(
                cli, ["epics", "--reverse", "progress", "--json"], obj=client
            )
        asc_nums = [
            d["number"] for d in cast(list[dict[str, object]], json.loads(asc.output))
        ]
        desc_nums = [
            d["number"] for d in cast(list[dict[str, object]], json.loads(desc.output))
        ]
        assert desc_nums == list(reversed(asc_nums))

    def test_invalid_sort_key_shows_valid_options(self) -> None:
        client = _client()
        issues = [_milestone_issue(1, state="OPEN", title="A", parent=1)]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--sort", "bogus"], obj=client)
        assert result.exit_code != 0
        assert "bogus" in result.output
        assert "number" in result.output
        assert "state" in result.output
        assert "title" in result.output

    def test_epics_sort_progress_json(self) -> None:
        client = _client()
        epics = [
            Epic(number=1, state="OPEN", title="Done", open_count=0, total_count=5),
            Epic(number=2, state="OPEN", title="All open", open_count=4, total_count=4),
        ]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            result = _runner().invoke(
                cli, ["epics", "--sort", "progress", "--json"], obj=client
            )
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert [d["number"] for d in data] == [2, 1]
        assert result.exit_code == 0

    def test_sort_title_reverse(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(50, state="OPEN", title="Mango", parent=1),
            _milestone_issue(3, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(99, state="OPEN", title="Apple", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(
                cli,
                ["sprint", "--sort", "title", "--reverse", "--status", "ALL"],
                obj=client,
            )
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#3", "#50", "#99"]
        assert result.exit_code == 0

    def test_reverse_with_key_implies_sort(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(50, state="OPEN", title="Mango", parent=1),
            _milestone_issue(3, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(99, state="OPEN", title="Apple", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(
                cli, ["sprint", "--reverse", "title", "--status", "ALL"], obj=client
            )
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#3", "#50", "#99"]
        assert result.exit_code == 0

    def test_epics_invalid_sort_key_mentions_progress(self) -> None:
        client = _client()
        epics = [Epic(number=1, state="OPEN", title="A", open_count=1, total_count=2)]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            result = _runner().invoke(cli, ["epics", "--sort", "bogus"], obj=client)
        assert result.exit_code != 0
        assert "progress" in result.output

    def test_reverse_without_sort_preserves_api_order(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(99, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(3, state="OPEN", title="Apple", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(
                cli, ["sprint", "--reverse", "--status", "ALL"], obj=client
            )
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#99", "#3"]
        assert result.exit_code == 0

    def test_no_flags_preserves_api_order(self) -> None:
        client = _client()
        issues = [
            _milestone_issue(99, state="CLOSED", title="Zebra", parent=1),
            _milestone_issue(3, state="OPEN", title="Apple", parent=1),
            _milestone_issue(50, state="OPEN", title="Mango", parent=1),
        ]
        with patch.object(client, "list_issues_by_milestone", return_value=issues):
            result = _runner().invoke(cli, ["sprint", "--status", "ALL"], obj=client)
        lines = result.output.strip().split("\n")
        nums = [
            line.strip().split()[0] for line in lines if line.strip().startswith("#")
        ]
        assert nums == ["#99", "#3", "#50"]
        assert result.exit_code == 0


class TestEpics:
    def test_lists_epics(self) -> None:
        client = _client()
        epics = [
            Epic(
                number=905,
                state="OPEN",
                title="orbit — dev tool",
                open_count=3,
                total_count=5,
            ),
            Epic(
                number=852,
                state="OPEN",
                title="Test suite speed",
                open_count=1,
                total_count=4,
            ),
        ]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            result = _runner().invoke(cli, ["epics"], obj=client)
        assert "#905" in result.output
        assert "3/5" in result.output
        assert "orbit — dev tool" in result.output
        assert "#852" in result.output
        assert "1/4" in result.output
        assert result.exit_code == 0

    def test_json_flag(self) -> None:
        client = _client()
        epics = [
            Epic(
                number=905,
                state="OPEN",
                title="orbit — dev tool",
                open_count=3,
                total_count=5,
            ),
        ]
        with patch.object(client, "list_epics_by_milestone", return_value=epics):
            result = _runner().invoke(cli, ["epics", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 905
        assert data[0]["state"] == "OPEN"
        assert data[0]["title"] == "orbit — dev tool"
        assert data[0]["open_count"] == 3
        assert data[0]["total_count"] == 5
        assert result.exit_code == 0

    def test_empty_epics(self) -> None:
        client = _client()
        with patch.object(client, "list_epics_by_milestone", return_value=[]):
            result = _runner().invoke(cli, ["epics"], obj=client)
        assert "No epics found" in result.output
        assert result.exit_code == 0

    def test_passes_current_milestone(self) -> None:
        client = _client()
        with patch.object(client, "list_epics_by_milestone", return_value=[]) as mock:
            _runner().invoke(cli, ["epics"], obj=client)
        mock.assert_called_once_with(_CURRENT)


class TestSubs:
    def test_epic_with_two_leaves(self) -> None:
        client = _client()
        tree_data = [
            SubIssueData(number=10, state="OPEN", title="Leaf one", children=()),
            SubIssueData(number=20, state="CLOSED", title="Leaf two", children=()),
            SubIssueData(
                number=30,
                state="OPEN",
                title="An epic",
                children=(
                    SubIssueData(number=31, state="OPEN", title="Child A", children=()),
                    SubIssueData(
                        number=32, state="CLOSED", title="Child B", children=()
                    ),
                ),
            ),
        ]
        with patch.object(client, "fetch_sub_issue_tree", return_value=tree_data):
            result = _runner().invoke(cli, ["subs", "1", "--status", "ALL"], obj=client)
        assert result.exit_code == 0
        assert "#10" in result.output
        assert "#20" in result.output
        assert "#30" in result.output
        assert "1/2" in result.output
        assert "#31" in result.output
        assert "#32" in result.output

    def test_json_flag(self) -> None:
        client = _client()
        tree_data = [
            SubIssueData(number=10, state="OPEN", title="Leaf", children=()),
        ]
        with patch.object(client, "fetch_sub_issue_tree", return_value=tree_data):
            result = _runner().invoke(cli, ["subs", "1", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 10
        assert data[0]["open_count"] is None
        assert result.exit_code == 0

    def test_json_flag_emits_placeholders_for_filtered_issues(self) -> None:
        client = _client()
        tree_data = [
            SubIssueData(
                number=10,
                state="OPEN",
                title="Epic",
                children=(
                    SubIssueData(number=11, state="CLOSED", title="Done", children=()),
                ),
            ),
            SubIssueData(number=20, state="CLOSED", title="Also done", children=()),
        ]
        with patch.object(client, "fetch_sub_issue_tree", return_value=tree_data):
            result = _runner().invoke(
                cli, ["subs", "1", "--status", "OPEN", "--json"], obj=client
            )
        assert result.exit_code == 0
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 2
        assert data[1]["count"] == 1
        assert data[1]["numbers"] == [20]
        children = cast(list[dict[str, object]], data[0]["children"])
        assert children[0]["count"] == 1
        assert children[0]["numbers"] == [11]


class TestParent:
    def test_shows_parent(self) -> None:
        client = _client()
        parent = Issue(number=100, state="OPEN", title="Parent epic")
        with patch.object(client, "fetch_parent_issue", return_value=parent):
            result = _runner().invoke(cli, ["parent", "42"], obj=client)
        assert result.exit_code == 0
        assert "#100" in result.output
        assert "OPEN" in result.output
        assert "Parent epic" in result.output

    def test_json_flag(self) -> None:
        client = _client()
        parent = Issue(number=100, state="OPEN", title="Parent epic")
        with patch.object(client, "fetch_parent_issue", return_value=parent):
            result = _runner().invoke(cli, ["parent", "42", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["number"] == 100
        assert result.exit_code == 0

    def test_no_parent(self) -> None:
        client = _client()
        with patch.object(client, "fetch_parent_issue", return_value=None):
            result = _runner().invoke(cli, ["parent", "42"], obj=client)
        assert "No parent issue" in result.output
        assert result.exit_code == 0


class TestShow:
    def test_shows_metadata_and_body(self) -> None:
        client = _client()
        detail = _issue_detail(
            number=905,
            state="OPEN",
            title="orbit dev tool",
            body="## Body\n\nhello world",
            labels=("epic", "soon"),
            milestone_title="developer velocity",
            parent_number=900,
            parent_title="Parent epic",
        )
        with patch.object(
            client, "fetch_issue_detail", return_value=detail
        ) as mock_show:
            result = _runner().invoke(cli, ["show", "905"], obj=client)
        mock_show.assert_called_once_with(905)
        assert result.exit_code == 0
        assert "#905" in result.output
        assert "orbit dev tool" in result.output
        assert "OPEN" in result.output
        assert "epic, soon" in result.output
        assert "developer velocity" in result.output
        assert "#900" in result.output
        assert "Parent epic" in result.output
        assert "hello world" in result.output

    def test_no_parent_and_no_labels(self) -> None:
        client = _client()
        detail = _issue_detail(number=42, labels=(), parent_number=None)
        with patch.object(client, "fetch_issue_detail", return_value=detail):
            result = _runner().invoke(cli, ["show", "42"], obj=client)
        assert result.exit_code == 0
        assert "Parent:" in result.output
        assert "—" in result.output

    def test_json_flag(self) -> None:
        client = _client()
        detail = _issue_detail(number=905, title="orbit dev tool", labels=("epic",))
        with patch.object(client, "fetch_issue_detail", return_value=detail):
            result = _runner().invoke(cli, ["show", "905", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["number"] == 905
        assert data["title"] == "orbit dev tool"
        assert data["labels"] == ["epic"]
        assert result.exit_code == 0

    def test_not_found_exits_nonzero(self) -> None:
        client = _client()
        with patch.object(
            client,
            "fetch_issue_detail",
            side_effect=RuntimeError("Issue #999 not found"),
        ):
            result = _runner().invoke(cli, ["show", "999"], obj=client)
        assert result.exit_code != 0
        assert "Issue #999 not found" in result.output


class TestFind:
    def test_lists_matches(self) -> None:
        client = _client()
        issues = [
            Issue(number=42, state="OPEN", title="guild.yaml loader"),
            Issue(number=100, state="CLOSED", title="guild.yaml schema"),
        ]
        with patch.object(client, "search_issue_titles", return_value=issues):
            result = _runner().invoke(cli, ["find", "guild.yaml"], obj=client)
        assert result.exit_code == 0
        assert "#42" in result.output
        assert "guild.yaml loader" in result.output
        assert "#100" in result.output

    def test_json_flag(self) -> None:
        client = _client()
        issues = [Issue(number=42, state="OPEN", title="guild.yaml loader")]
        with patch.object(client, "search_issue_titles", return_value=issues):
            result = _runner().invoke(cli, ["find", "guild.yaml", "--json"], obj=client)
        data = cast(list[dict[str, object]], json.loads(result.output))
        assert len(data) == 1
        assert data[0]["number"] == 42
        assert data[0]["title"] == "guild.yaml loader"
        assert result.exit_code == 0

    def test_no_matches(self) -> None:
        client = _client()
        with patch.object(client, "search_issue_titles", return_value=[]):
            result = _runner().invoke(cli, ["find", "nonexistent"], obj=client)
        assert result.exit_code == 0
        assert "No issues found" in result.output

    def test_passes_query(self) -> None:
        client = _client()
        with patch.object(client, "search_issue_titles", return_value=[]) as mock:
            _runner().invoke(cli, ["find", "guild.yaml"], obj=client)
        mock.assert_called_once_with("guild.yaml")

    def test_status_and_sort(self) -> None:
        client = _client()
        issues = [
            Issue(number=42, state="OPEN", title="Zebra loader"),
            Issue(number=100, state="CLOSED", title="Apple schema"),
            Issue(number=7, state="OPEN", title="Mango parser"),
        ]
        with patch.object(client, "search_issue_titles", return_value=issues):
            result = _runner().invoke(
                cli,
                ["find", "guild.yaml", "--status", "OPEN", "--sort", "title"],
                obj=client,
            )
        assert result.exit_code == 0
        rendered = [
            line.strip().split()[0]
            for line in result.output.split("\n")
            if line.strip().startswith(("#", "<"))
        ]
        assert rendered == ["<1", "#7", "#42"]
        assert "Status filter 'OPEN' applied" in result.output

    def test_runtime_error_exits_nonzero(self) -> None:
        client = _client()
        with patch.object(
            client, "search_issue_titles", side_effect=RuntimeError("no remote")
        ):
            result = _runner().invoke(cli, ["find", "guild.yaml"], obj=client)
        assert result.exit_code != 0
        assert "no remote" in result.output


class TestMove:
    def test_json_flag(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=700,
            old_epic_title="Old epic",
            milestone="developer velocity",
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr) as mock_move:
            result = _runner().invoke(cli, ["move", "42", "800", "--json"], obj=client)
        mock_move.assert_called_once_with(client, 42, 800)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["issue_number"] == 42
        assert data["epic_number"] == 800
        assert data["old_epic_number"] == 700
        assert data["milestone"] == "developer velocity"
        assert result.exit_code == 0

    def test_text_output_with_reparent(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=700,
            old_epic_title="Old epic",
            milestone="developer velocity",
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Detached #42 from epic #700" in result.output
        assert "Moved #42" in result.output
        assert "Sprint epic" in result.output
        assert "developer velocity" in result.output
        assert result.exit_code == 0

    def test_text_output_without_old_epic(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Detached" not in result.output
        assert "Moved #42" in result.output
        assert result.exit_code == 0

    def test_text_output_without_milestone(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone=None,
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Milestone" not in result.output
        assert result.exit_code == 0

    def test_text_output_lists_reopened_chain(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
            reopened=(800, 700),
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Reopened #800, #700" in result.output
        assert result.exit_code == 0

    def test_text_output_silent_when_nothing_reopened(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Reopened" not in result.output
        assert result.exit_code == 0

    def test_json_carries_reopened_chain(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
            reopened=(800, 700),
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["reopened"] == [800, 700]
        assert result.exit_code == 0

    def test_already_done_with_reopen_reports_both(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
            reopened=(800,),
            already_done=True,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert "Reopened #800" in result.output
        assert "already under epic #800" in result.output
        assert "Moved #42" not in result.output
        assert result.exit_code == 0

    def test_already_done_with_reopen_json(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
            reopened=(800,),
            already_done=True,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _split_runner().invoke(
                cli, ["move", "42", "800", "--json"], obj=client
            )
        data = cast(dict[str, object], json.loads(result.stdout))
        assert data["already_done"] is True
        assert data["reopened"] == [800]
        assert result.exit_code == 0

    def test_validation_error_exits_nonzero(self) -> None:
        client = _client()
        with patch("orbit.cli.move_issue", side_effect=RuntimeError("not an epic")):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert result.exit_code != 0
        assert "not an epic" in result.output

    def test_already_done_exits_zero_with_message(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.move_issue",
            side_effect=AlreadyDoneError("already under epic #800"),
        ):
            result = _runner().invoke(cli, ["move", "42", "800"], obj=client)
        assert result.exit_code == 0
        assert "already under epic #800" in result.output
        assert "Error:" not in result.output

    def test_json_already_done_reports_flag(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.move_issue",
            side_effect=AlreadyDoneError("already under epic #800"),
        ):
            result = _split_runner().invoke(
                cli, ["move", "42", "800", "--json"], obj=client
            )
        assert result.exit_code == 0
        data = cast(dict[str, object], json.loads(result.output))
        assert data["already_done"] is True

    def test_json_already_done_keeps_stdout_pure(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.move_issue",
            side_effect=AlreadyDoneError("already under epic #800"),
        ):
            result = _split_runner().invoke(
                cli, ["move", "42", "800", "--json"], obj=client
            )
        json.loads(result.output)  # stdout must be parseable JSON, nothing else
        assert "already under epic #800" in result.stderr

    def test_json_success_reports_not_already_done(self) -> None:
        client = _client()
        mr = MoveResult(
            issue_number=42,
            issue_title="Fix widget",
            epic_number=800,
            epic_title="Sprint epic",
            old_epic_number=None,
            old_epic_title=None,
            milestone="developer velocity",
            converted_dest_to_epic=False,
        )
        with patch("orbit.cli.move_issue", return_value=mr):
            result = _runner().invoke(cli, ["move", "42", "800", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["already_done"] is False


def _reorder_result(
    position: Literal["first", "after", "before"] = "after",
    reference_number: int | None = 43,
    reference_title: str | None = "Ship widget",
    already_done: bool = False,
) -> ReorderResult:
    return ReorderResult(
        issue_number=42,
        issue_title="Fix widget",
        epic_number=800,
        epic_title="Sprint epic",
        position=position,
        reference_number=reference_number,
        reference_title=reference_title,
        already_done=already_done,
    )


class TestReorder:
    def test_text_output_after(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.reorder_issue", return_value=_reorder_result()
        ) as mock_reorder:
            result = _runner().invoke(
                cli, ["reorder", "42", "--after", "43"], obj=client
            )
        mock_reorder.assert_called_once_with(
            client, 42, after_number=43, before_number=None
        )
        assert "#42" in result.output
        assert "after #43" in result.output
        assert "Sprint epic" in result.output
        assert result.exit_code == 0

    def test_text_output_first(self) -> None:
        client = _client()
        reorder = _reorder_result("first", None, None)
        with patch("orbit.cli.reorder_issue", return_value=reorder) as mock_reorder:
            result = _runner().invoke(cli, ["reorder", "42", "--first"], obj=client)
        mock_reorder.assert_called_once_with(
            client, 42, after_number=None, before_number=None
        )
        assert "Moved #42" in result.output
        assert "first" in result.output
        assert result.exit_code == 0

    def test_json_flag(self) -> None:
        client = _client()
        with patch("orbit.cli.reorder_issue", return_value=_reorder_result("before")):
            result = _runner().invoke(
                cli, ["reorder", "42", "--before", "43", "--json"], obj=client
            )
        data = cast(dict[str, object], json.loads(result.output))
        assert data["issue_number"] == 42
        assert data["epic_number"] == 800
        assert data["position"] == "before"
        assert data["reference_number"] == 43
        assert result.exit_code == 0

    def test_after_and_before_are_mutually_exclusive(self) -> None:
        client = _client()
        with patch("orbit.cli.reorder_issue") as mock_reorder:
            result = _runner().invoke(
                cli, ["reorder", "42", "--after", "43", "--before", "44"], obj=client
            )
        assert result.exit_code == 2
        mock_reorder.assert_not_called()

    def test_first_and_after_are_mutually_exclusive(self) -> None:
        client = _client()
        with patch("orbit.cli.reorder_issue") as mock_reorder:
            result = _runner().invoke(
                cli, ["reorder", "42", "--first", "--after", "43"], obj=client
            )
        assert result.exit_code == 2
        mock_reorder.assert_not_called()

    def test_requires_a_position(self) -> None:
        client = _client()
        with patch("orbit.cli.reorder_issue") as mock_reorder:
            result = _runner().invoke(cli, ["reorder", "42"], obj=client)
        assert result.exit_code == 2
        mock_reorder.assert_not_called()

    def test_already_first_reports_a_no_op(self) -> None:
        client = _client()
        reorder = _reorder_result("first", None, None, already_done=True)
        with patch("orbit.cli.reorder_issue", return_value=reorder):
            result = _runner().invoke(cli, ["reorder", "42", "--first"], obj=client)
        assert "is already first" in result.output
        assert "Moved" not in result.output
        assert result.exit_code == 0

    def test_json_carries_already_done(self) -> None:
        client = _client()
        reorder = _reorder_result("first", None, None, already_done=True)
        with patch("orbit.cli.reorder_issue", return_value=reorder):
            result = _runner().invoke(
                cli, ["reorder", "42", "--first", "--json"], obj=client
            )
        data = cast(dict[str, object], json.loads(result.output))
        assert data["already_done"] is True
        assert result.exit_code == 0

    def test_validation_error_exits_nonzero(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.reorder_issue", side_effect=RuntimeError("not in an epic")
        ):
            result = _runner().invoke(cli, ["reorder", "42", "--first"], obj=client)
        assert result.exit_code == 1
        assert "not in an epic" in result.output


class TestSchedule:
    def test_defaults_to_current_milestone(self) -> None:
        client = _client()
        sr = ScheduleResult(
            issue_number=42,
            issue_title="Fix widget",
            milestone=_CURRENT,
            old_epic_number=None,
            old_epic_title=None,
        )
        with patch("orbit.cli.schedule_issue", return_value=sr) as mock_sched:
            result = _runner().invoke(cli, ["schedule", "42"], obj=client)
        assert result.exit_code == 0
        mock_sched.assert_called_once_with(client, 42, _CURRENT)
        assert f"Scheduled #42 (Fix widget) → milestone {_CURRENT!r}" in result.output
        assert "Detached" not in result.output

    def test_backlog_milestone_replicates_shelve(self) -> None:
        client = _client()
        sr = ScheduleResult(
            issue_number=42,
            issue_title="Fix widget",
            milestone=_BACKLOG,
            old_epic_number=905,
            old_epic_title="orbit — dev tool",
        )
        with patch("orbit.cli.schedule_issue", return_value=sr) as mock_sched:
            result = _runner().invoke(
                cli, ["schedule", "42", "-m", _BACKLOG], obj=client
            )
        assert result.exit_code == 0
        mock_sched.assert_called_once_with(client, 42, _BACKLOG)
        assert "Detached #42 from epic #905" in result.output
        assert "orbit — dev tool" in result.output
        assert "Scheduled #42" in result.output
        assert _BACKLOG in result.output

    def test_passes_explicit_milestone_through(self) -> None:
        client = _client()
        sr = ScheduleResult(
            issue_number=42,
            issue_title="Fix widget",
            milestone="web frontend 1",
            old_epic_number=None,
            old_epic_title=None,
        )
        with patch("orbit.cli.schedule_issue", return_value=sr) as mock_sched:
            result = _runner().invoke(
                cli, ["schedule", "42", "--milestone", "web frontend 1"], obj=client
            )
        assert result.exit_code == 0
        mock_sched.assert_called_once_with(client, 42, "web frontend 1")
        assert "web frontend 1" in result.output

    def test_detach_line_omitted_without_parent(self) -> None:
        client = _client()
        sr = ScheduleResult(
            issue_number=42,
            issue_title="Fix widget",
            milestone=_CURRENT,
            old_epic_number=None,
            old_epic_title=None,
        )
        with patch("orbit.cli.schedule_issue", return_value=sr):
            result = _runner().invoke(cli, ["schedule", "42"], obj=client)
        assert "Detached" not in result.output

    def test_json_flag(self) -> None:
        client = _client()
        sr = ScheduleResult(
            issue_number=42,
            issue_title="Fix widget",
            milestone="Backlog",
            old_epic_number=905,
            old_epic_title="orbit — dev tool",
        )
        with patch("orbit.cli.schedule_issue", return_value=sr):
            result = _runner().invoke(
                cli, ["schedule", "42", "-m", "Backlog", "--json"], obj=client
            )
        data = cast(dict[str, object], json.loads(result.output))
        assert data["issue_number"] == 42
        assert data["issue_title"] == "Fix widget"
        assert data["milestone"] == "Backlog"
        assert data["old_epic_number"] == 905
        assert data["old_epic_title"] == "orbit — dev tool"
        assert data["already_done"] is False
        assert result.exit_code == 0

    def test_runtime_error_exits_nonzero(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.schedule_issue", side_effect=RuntimeError("milestone not found")
        ):
            result = _runner().invoke(cli, ["schedule", "42"], obj=client)
        assert result.exit_code != 0
        assert "milestone not found" in result.output

    def test_already_done_exits_zero_with_message(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.schedule_issue",
            side_effect=AlreadyDoneError("already in milestone 'degraded state'"),
        ):
            result = _runner().invoke(cli, ["schedule", "42"], obj=client)
        assert result.exit_code == 0
        assert "already in milestone" in result.output
        assert "Error:" not in result.output

    def test_json_already_done_reports_flag(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.schedule_issue",
            side_effect=AlreadyDoneError("already in milestone 'degraded state'"),
        ):
            result = _split_runner().invoke(
                cli, ["schedule", "42", "--json"], obj=client
            )
        assert result.exit_code == 0
        data = cast(dict[str, object], json.loads(result.output))
        assert data["already_done"] is True


class TestClose:
    def test_json_flag_emits_result(self) -> None:
        client = _client()
        cr = CloseResult(number=42, reason=CloseReason.COMPLETED)
        with patch("orbit.cli.close_issue", return_value=cr):
            result = _runner().invoke(cli, ["close", "42", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["number"] == 42
        assert data["reason"] == "completed"
        assert data["already_done"] is False
        assert result.exit_code == 0

    def test_text_output(self) -> None:
        client = _client()
        cr = CloseResult(number=42, reason=CloseReason.COMPLETED)
        with patch("orbit.cli.close_issue", return_value=cr):
            result = _runner().invoke(cli, ["close", "42"], obj=client)
        assert "Closed #42 (completed)" in result.output
        assert result.exit_code == 0

    def test_passes_the_cli_surface(self) -> None:
        client = _client()
        cr = CloseResult(number=42, reason=CloseReason.COMPLETED)
        with patch("orbit.cli.close_issue", return_value=cr) as mock_close:
            result = _runner().invoke(
                cli, ["close", "42", "--reason", "not_planned"], obj=client
            )
        assert result.exit_code == 0
        mock_close.assert_called_once_with(
            client, 42, CloseReason.NOT_PLANNED, Surface.CLI
        )

    def test_json_already_done_keeps_stdout_pure(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.close_issue",
            side_effect=AlreadyDoneError("Issue #42 is already closed"),
        ):
            result = _split_runner().invoke(cli, ["close", "42", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["already_done"] is True
        assert "already closed" in result.stderr
        assert result.exit_code == 0

    def test_already_done_exits_zero_with_message(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.close_issue",
            side_effect=AlreadyDoneError("Issue #42 is already closed"),
        ):
            result = _runner().invoke(cli, ["close", "42"], obj=client)
        assert result.exit_code == 0
        assert "already closed" in result.output
        assert "Error:" not in result.output

    def test_runtime_error_exits_nonzero(self) -> None:
        client = _client()
        with patch(
            "orbit.cli.close_issue", side_effect=RuntimeError("issue not found")
        ):
            result = _runner().invoke(cli, ["close", "42"], obj=client)
        assert result.exit_code != 0
        assert "issue not found" in result.output


_EDIT_URL = "https://github.com/example-org/example-repo/issues/42"


class TestEdit:
    def test_json_flag_emits_number_and_url(self) -> None:
        client = _client()
        with patch("orbit.cli.open_url"):
            result = _runner().invoke(cli, ["edit", "42", "--json"], obj=client)
        data = cast(dict[str, object], json.loads(result.output))
        assert data["number"] == 42
        assert data["url"] == _EDIT_URL
        assert result.exit_code == 0

    def test_text_output_prints_url(self) -> None:
        client = _client()
        with patch("orbit.cli.open_url") as mock_open:
            result = _runner().invoke(cli, ["edit", "42"], obj=client)
        assert _EDIT_URL in result.output
        mock_open.assert_called_once_with(_EDIT_URL)
        assert result.exit_code == 0

    def test_repo_error_exits_nonzero(self) -> None:
        with patch("orbit.cli.github_client", side_effect=RuntimeError("no remote")):
            result = _runner().invoke(cli, ["edit", "42"])
        assert result.exit_code != 0
        assert "no remote" in result.output


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


class TestCreateEpic:
    def test_creates_epic_with_sub_issues(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_950", number=950, title="New epic")
        sub_42 = _issue_detail(node_id="I_42", number=42, title="Sub A")
        sub_55 = _issue_detail(node_id="I_55", number=55, title="Sub B")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(client, "fetch_label_id", return_value="LA_epic"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
            patch.object(client, "add_label") as mock_label,
            patch.object(client, "fetch_issue_detail", side_effect=[sub_42, sub_55]),
            patch.object(client, "add_sub_issue") as mock_sub,
            patch.object(client, "set_issue_milestone") as mock_ms,
        ):
            result = _runner().invoke(
                cli, ["create-epic", "New epic", "42", "55"], obj=client
            )

        assert result.exit_code == 0
        assert "#950" in result.output
        assert "New epic" in result.output
        mock_create.assert_called_once_with("New epic", "R_123", "MI_1", None)
        mock_label.assert_called_once_with("I_950", "LA_epic")
        mock_sub.assert_any_call("I_950", "I_42")
        mock_sub.assert_any_call("I_950", "I_55")
        mock_ms.assert_any_call("I_42", "MI_1")
        mock_ms.assert_any_call("I_55", "MI_1")

    def test_creates_epic_threads_body(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_950", number=950, title="New epic")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(client, "fetch_label_id", return_value="LA_epic"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
            patch.object(client, "add_label"),
        ):
            result = _runner().invoke(
                cli, ["create-epic", "New epic", "--body", "Epic body"], obj=client
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with("New epic", "R_123", "MI_1", "Epic body")

    def test_creates_epic_with_extra_label_in_addition_to_epic(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_950", number=950, title="New epic")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(
                client, "fetch_label_id", side_effect=_fake_label_id
            ) as mock_fetch,
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "add_label") as mock_add,
        ):
            result = _runner().invoke(
                cli, ["create-epic", "New epic", "--label", "soon"], obj=client
            )

        assert result.exit_code == 0
        assert [c.args[0] for c in mock_fetch.call_args_list] == ["epic", "soon"]
        assert [c.args for c in mock_add.call_args_list] == [
            ("I_950", "LA_epic"),
            ("I_950", "LA_soon"),
        ]


class TestCreate:
    def test_create_under_epic(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_905",
            number=905,
            title="Sprint epic",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        created = CreatedIssue(node_id="I_960", number=960, title="New leaf")
        with (
            patch.object(client, "fetch_issue_detail", return_value=epic),
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "add_sub_issue") as mock_sub,
        ):
            result = _runner().invoke(cli, ["create", "905", "New leaf"], obj=client)

        assert result.exit_code == 0
        assert "#960" in result.output
        assert "New leaf" in result.output
        assert "#905" in result.output
        assert "Sprint epic" in result.output
        mock_sub.assert_called_once_with("I_905", "I_960")

    def test_create_lists_reopened_chain(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_905",
            number=905,
            title="Sprint epic",
            state="CLOSED",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        created = CreatedIssue(node_id="I_960", number=960, title="New leaf")
        with (
            patch.object(client, "fetch_issue_detail", return_value=epic),
            patch.object(client, "reopen_issue_by_id"),
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "add_sub_issue"),
        ):
            result = _runner().invoke(cli, ["create", "905", "New leaf"], obj=client)

        assert "Reopened #905" in result.output
        assert result.exit_code == 0

    def test_create_json_carries_reopened_chain(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_905",
            number=905,
            title="Sprint epic",
            state="CLOSED",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        created = CreatedIssue(node_id="I_960", number=960, title="New leaf")
        with (
            patch.object(client, "fetch_issue_detail", return_value=epic),
            patch.object(client, "reopen_issue_by_id"),
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "add_sub_issue"),
        ):
            result = _runner().invoke(
                cli, ["create", "905", "New leaf", "--json"], obj=client
            )

        data = cast(dict[str, object], json.loads(result.output))
        assert data["reopened"] == [905]
        assert result.exit_code == 0

    def test_create_with_label(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_905",
            number=905,
            title="Sprint epic",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        created = CreatedIssue(node_id="I_960", number=960, title="New leaf")
        with (
            patch.object(client, "fetch_issue_detail", return_value=epic),
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "add_sub_issue"),
            patch.object(
                client, "fetch_label_id", return_value="LA_soon"
            ) as mock_fetch,
            patch.object(client, "add_label") as mock_add,
        ):
            result = _runner().invoke(
                cli, ["create", "905", "New leaf", "--label", "soon"], obj=client
            )

        assert result.exit_code == 0
        mock_fetch.assert_called_once_with("soon")
        mock_add.assert_called_once_with("I_960", "LA_soon")

    def test_create_with_multiple_labels(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(
                client, "fetch_label_id", side_effect=["LA_a", "LA_b"]
            ) as mock_fetch,
            patch.object(client, "add_label") as mock_add,
        ):
            result = _runner().invoke(
                cli,
                ["create", "shelf", "Backlog idea", "--label", "a", "--label", "b"],
                obj=client,
            )

        assert result.exit_code == 0
        assert [c.args[0] for c in mock_fetch.call_args_list] == ["a", "b"]
        assert [c.args for c in mock_add.call_args_list] == [
            ("I_970", "LA_a"),
            ("I_970", "LA_b"),
        ]

    def test_create_without_label_adds_none(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created),
            patch.object(client, "fetch_label_id") as mock_fetch,
            patch.object(client, "add_label") as mock_add,
        ):
            result = _runner().invoke(
                cli, ["create", "shelf", "Backlog idea"], obj=client
            )

        assert result.exit_code == 0
        mock_fetch.assert_not_called()
        mock_add.assert_not_called()

    def test_create_under_epic_threads_body(self) -> None:
        client = _client()
        epic = _issue_detail(
            node_id="I_905",
            number=905,
            title="Sprint epic",
            labels=("epic",),
            milestone_id="MI_1",
            milestone_title="developer velocity",
        )
        created = CreatedIssue(node_id="I_960", number=960, title="New leaf")
        with (
            patch.object(client, "fetch_issue_detail", return_value=epic),
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
            patch.object(client, "add_sub_issue"),
        ):
            result = _runner().invoke(
                cli, ["create", "905", "New leaf", "--body", "Body text"], obj=client
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with("New leaf", "R_123", "MI_1", "Body text")

    def test_create_body_file_stdin(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
        ):
            result = _runner().invoke(
                cli,
                ["create", "shelf", "Backlog idea", "--body-file", "-"],
                input="piped body\n",
                obj=client,
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with(
            "Backlog idea", "R_123", "MI_backlog", "piped body\n"
        )

    def test_create_shelf(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created),
        ):
            result = _runner().invoke(
                cli, ["create", "shelf", "Backlog idea"], obj=client
            )

        assert result.exit_code == 0
        assert "#970" in result.output
        assert "Backlog idea" in result.output
        assert "Backlog" in result.output

    def test_create_without_body_passes_none(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
        ):
            result = _runner().invoke(
                cli, ["create", "shelf", "Backlog idea"], obj=client
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with("Backlog idea", "R_123", "MI_backlog", None)

    def test_create_shelf_threads_body(self) -> None:
        client = _client()
        created = CreatedIssue(node_id="I_970", number=970, title="Backlog idea")
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_backlog"),
            patch.object(client, "create_issue", return_value=created) as mock_create,
        ):
            result = _runner().invoke(
                cli,
                ["create", "shelf", "Backlog idea", "--body", "Shelf body"],
                obj=client,
            )

        assert result.exit_code == 0
        mock_create.assert_called_once_with(
            "Backlog idea", "R_123", "MI_backlog", "Shelf body"
        )


class TestSetBody:
    def test_sets_body_via_node_id(self) -> None:
        client = _client()
        detail = _issue_detail(node_id="I_42", number=42, title="Some issue")
        with (
            patch.object(client, "fetch_issue_detail", return_value=detail),
            patch.object(client, "set_issue_body") as mock_set,
        ):
            result = _runner().invoke(
                cli, ["set-body", "42", "--body", "new body"], obj=client
            )

        assert result.exit_code == 0
        mock_set.assert_called_once_with("I_42", "new body")

    def test_body_file_stdin(self) -> None:
        client = _client()
        detail = _issue_detail(node_id="I_42", number=42, title="Some issue")
        with (
            patch.object(client, "fetch_issue_detail", return_value=detail),
            patch.object(client, "set_issue_body") as mock_set,
        ):
            result = _runner().invoke(
                cli,
                ["set-body", "42", "--body-file", "-"],
                input="piped body\n",
                obj=client,
            )

        assert result.exit_code == 0
        mock_set.assert_called_once_with("I_42", "piped body\n")

    def test_both_options_error_without_mutating(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_issue_detail") as mock_fetch,
            patch.object(client, "set_issue_body") as mock_set,
        ):
            result = _runner().invoke(
                cli,
                ["set-body", "42", "--body", "x", "--body-file", "-"],
                input="y\n",
                obj=client,
            )

        assert result.exit_code != 0
        mock_fetch.assert_not_called()
        mock_set.assert_not_called()

    def test_no_body_errors_as_required(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_issue_detail") as mock_fetch,
            patch.object(client, "set_issue_body") as mock_set,
        ):
            result = _runner().invoke(cli, ["set-body", "42"], obj=client)

        assert result.exit_code != 0
        assert "--body" in result.output
        assert "--body-file" in result.output
        mock_fetch.assert_not_called()
        mock_set.assert_not_called()

    def test_issue_not_found_exits_nonzero(self) -> None:
        client = _client()
        with (
            patch.object(
                client,
                "fetch_issue_detail",
                side_effect=RuntimeError("Issue #42 not found"),
            ),
            patch.object(client, "set_issue_body") as mock_set,
        ):
            result = _runner().invoke(
                cli, ["set-body", "42", "--body", "x"], obj=client
            )

        assert result.exit_code != 0
        assert "not found" in result.output
        mock_set.assert_not_called()

    def test_json_flag(self) -> None:
        client = _client()
        detail = _issue_detail(node_id="I_42", number=42, title="Some issue")
        with (
            patch.object(client, "fetch_issue_detail", return_value=detail),
            patch.object(client, "set_issue_body"),
        ):
            result = _runner().invoke(
                cli, ["set-body", "42", "--body", "x", "--json"], obj=client
            )

        assert result.exit_code == 0
        payload = cast("dict[str, object]", json.loads(result.output))
        assert payload == {"number": 42, "title": "Some issue"}


class TestResolveBody:
    def test_neither_option_returns_none(self) -> None:
        assert resolve_body(None, None) is None

    def test_body_option_returned_verbatim(self) -> None:
        assert resolve_body("hello body", None) == "hello body"

    def test_body_file_handle_is_read(self) -> None:
        assert resolve_body(None, io.StringIO("from file")) == "from file"

    def test_both_options_raise_usage_error(self) -> None:
        with pytest.raises(UsageError):
            resolve_body("inline", io.StringIO("from file"))


class TestCreateStandalone:
    def _created(self) -> CreatedIssue:
        return CreatedIssue(node_id="I_980", number=980, title="(20) a one-off")

    def test_create_standalone_reports_the_current_milestone(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1") as mock_ms,
            patch.object(client, "create_issue", return_value=self._created()),
        ):
            result = _runner().invoke(
                cli, ["create", "standalone", "(20) a one-off"], obj=client
            )

        assert result.exit_code == 0
        mock_ms.assert_called_once_with(_CURRENT)
        assert "#980" in result.output
        assert _CURRENT in result.output
        assert "epic" not in result.output

    def test_create_standalone_json_reports_no_epic(self) -> None:
        client = _client()
        with (
            patch.object(client, "fetch_repository_id", return_value="R_123"),
            patch.object(client, "fetch_milestone_id", return_value="MI_1"),
            patch.object(client, "create_issue", return_value=self._created()),
        ):
            result = _split_runner().invoke(
                cli, ["create", "standalone", "(20) a one-off", "--json"], obj=client
            )

        assert result.exit_code == 0
        payload = cast("dict[str, object]", json.loads(result.stdout))
        assert payload["epic_number"] is None
        assert payload["milestone"] == _CURRENT
