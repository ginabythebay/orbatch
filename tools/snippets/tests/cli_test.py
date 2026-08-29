# pyright: reportPrivateUsage=false
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner, Result

import snippets.cli
from ghgql.repo import Repo
from orbit.github.models import MilestoneSummary, ParentRef, PeriodIssue, PeriodPR
from snippets.cli import (
    Annotation,
    Commit,
    Period,
    PeriodError,
    RepoFailure,
    RepoReport,
    Report,
    annotations_from,
    build_report,
    build_rollup,
    cli,
    count_deploys,
    direct_commits,
    doc_paths,
    fetch_commits,
    fetch_doc_paths,
    git_in,
    main,
    milestones_in,
    parse_period,
    print_report,
    resolve_repos,
)
from snippets.config import RepoSpec


def _period(start: str, end: str) -> Period:
    return Period(date.fromisoformat(start), date.fromisoformat(end))


def _parsed(text: str, today: str) -> tuple[str, str]:
    result = parse_period(text, date.fromisoformat(today))
    return result.start.isoformat(), result.end.isoformat()


class TestParsePeriod:
    def test_single_day_periods(self) -> None:
        assert _parsed("today", "2026-04-15") == ("2026-04-15", "2026-04-15")
        assert _parsed("yesterday", "2026-04-15") == ("2026-04-14", "2026-04-14")

    def test_this_week(self) -> None:
        assert _parsed("this week", "2026-04-15") == ("2026-04-13", "2026-04-15")
        assert _parsed("this week", "2026-04-13") == ("2026-04-13", "2026-04-13")

    @pytest.mark.parametrize("text", ["last week", "past week"])
    def test_last_week(self, text: str) -> None:
        assert _parsed(text, "2026-04-15") == ("2026-04-06", "2026-04-12")
        assert _parsed(text, "2026-04-13") == ("2026-04-06", "2026-04-12")

    @pytest.mark.parametrize(
        "text", ["last 7 days", "past 7 days", "7 days", "7d", "7 d"]
    )
    def test_trailing_day_window_ends_today(self, text: str) -> None:
        assert _parsed(text, "2026-04-15") == ("2026-04-09", "2026-04-15")

    def test_day_window_crosses_month_and_year_boundaries(self) -> None:
        assert _parsed("30 days", "2026-01-05") == ("2025-12-07", "2026-01-05")

    def test_one_day_window_is_today(self) -> None:
        assert _parsed("1 day", "2026-04-15") == ("2026-04-15", "2026-04-15")

    def test_this_month(self) -> None:
        assert _parsed("this month", "2026-04-15") == ("2026-04-01", "2026-04-15")

    @pytest.mark.parametrize("text", ["last month", "past month"])
    def test_last_month(self, text: str) -> None:
        assert _parsed(text, "2026-03-10") == ("2026-02-01", "2026-02-28")
        assert _parsed(text, "2024-03-10") == ("2024-02-01", "2024-02-29")
        assert _parsed(text, "2026-01-10") == ("2025-12-01", "2025-12-31")

    @pytest.mark.parametrize("text", ["February", "feb", "FEBRUARY"])
    def test_bare_month_name(self, text: str) -> None:
        assert _parsed(text, "2026-04-15") == ("2026-02-01", "2026-02-28")

    def test_future_month_rolls_back_a_year(self) -> None:
        assert _parsed("December", "2026-08-15") == ("2025-12-01", "2025-12-31")

    def test_within_month_range(self) -> None:
        assert _parsed("April 1-15", "2026-08-15") == ("2026-04-01", "2026-04-15")

    @pytest.mark.parametrize(
        "text",
        [
            "2026-03-04",
            "2026/03/04",
            "03/04/2026",
            "03/04/26",
            "4 March 2026",
            "March 4 2026",
        ],
    )
    def test_bare_date(self, text: str) -> None:
        assert _parsed(text, "2026-08-15") == ("2026-03-04", "2026-03-04")

    @pytest.mark.parametrize(
        "text", ["not a period", "Foo 1-5", "April 31-40", "0 days"]
    )
    def test_unparseable_raises(self, text: str) -> None:
        with pytest.raises(PeriodError, match=f"cannot parse period '{text}'"):
            parse_period(text, date(2026, 4, 15))


def _issue(
    number: int,
    *,
    title: str = "a title",
    state: str = "OPEN",
    created: str = "2026-04-10",
    closed: str | None = None,
    is_epic: bool = False,
    parent: tuple[int, str, str] | None = None,
    milestone: str | None = None,
) -> PeriodIssue:
    return PeriodIssue(
        number=number,
        title=title,
        state=state,
        created_at=date.fromisoformat(created),
        closed_at=date.fromisoformat(closed) if closed is not None else None,
        is_epic=is_epic,
        milestone=milestone,
        parent=(
            ParentRef(
                number=parent[0],
                title=parent[1],
                created_at=date.fromisoformat(parent[2]),
            )
            if parent is not None
            else None
        ),
    )


_APRIL = _period("2026-04-01", "2026-04-30")
_EPIC_REF = (5, "the epic", "2026-01-15")


class TestBuildRollup:
    def test_created_and_closed_counts_are_independent(self) -> None:
        issues = [
            _issue(
                21,
                created="2026-01-20",
                closed="2026-04-05",
                state="CLOSED",
                parent=(6, "other epic", "2026-01-16"),
            ),
            _issue(11, created="2026-04-02", parent=_EPIC_REF),
        ]
        rows = build_rollup(issues, _APRIL).rows
        assert [(r.number, r.subs_created, r.subs_closed) for r in rows] == [
            (5, 1, 0),
            (6, 0, 1),
        ]

    def test_old_epic_keeps_its_own_creation_date(self) -> None:
        issues = [
            _issue(21, created="2026-01-20", closed="2026-04-05", parent=_EPIC_REF)
        ]
        (row,) = build_rollup(issues, _APRIL).rows
        assert row.created == date(2026, 1, 15)
        assert row.title == "the epic"
        assert row.closed is None

    def test_child_created_and_closed_in_period_counts_twice(self) -> None:
        issues = [
            _issue(11, created="2026-04-02", closed="2026-04-09", parent=_EPIC_REF)
        ]
        (row,) = build_rollup(issues, _APRIL).rows
        assert (row.subs_created, row.subs_closed) == (1, 1)

    def test_epic_own_activity_earns_a_row(self) -> None:
        issues = [
            _issue(7, title="new epic", created="2026-04-03", is_epic=True),
            _issue(
                8,
                title="finished epic",
                created="2026-01-04",
                closed="2026-04-20",
                state="CLOSED",
                is_epic=True,
            ),
        ]
        rows = build_rollup(issues, _APRIL).rows
        assert [(r.number, r.subs_created, r.subs_closed) for r in rows] == [
            (7, 0, 0),
            (8, 0, 0),
        ]
        assert rows[0].closed is None
        assert rows[1].closed == date(2026, 4, 20)

    def test_nested_epic_counts_for_its_parent_and_keeps_its_own_row(self) -> None:
        issues = [
            _issue(
                9,
                title="sub epic",
                created="2026-04-02",
                is_epic=True,
                parent=_EPIC_REF,
            ),
            _issue(11, created="2026-04-03", parent=(9, "sub epic", "2026-04-02")),
        ]
        rows = build_rollup(issues, _APRIL).rows
        assert [(r.number, r.subs_created, r.subs_closed) for r in rows] == [
            (5, 1, 0),
            (9, 1, 0),
        ]

    def test_only_parentless_leaves_are_standalone(self) -> None:
        issues = [
            _issue(12, title="a stray leaf", created="2026-04-08"),
            _issue(7, title="top epic", created="2026-04-03", is_epic=True),
        ]
        rollup = build_rollup(issues, _APRIL)
        (standalone,) = rollup.standalone
        assert (
            standalone.number,
            standalone.date,
            standalone.state,
            standalone.title,
        ) == (
            12,
            date(2026, 4, 8),
            "OPEN",
            "a stray leaf",
        )
        assert [r.number for r in rollup.rows] == [7]

    def test_standalone_issue_closed_in_period_shows_its_close_date(self) -> None:
        issues = [
            _issue(
                12,
                title="a stray leaf",
                state="CLOSED",
                created="2026-01-20",
                closed="2026-04-05",
            )
        ]
        (standalone,) = build_rollup(issues, _APRIL).standalone
        assert (standalone.date, standalone.state) == (date(2026, 4, 5), "CLOSED")


def _pr(
    number: int,
    *,
    title: str = "a pull request",
    merged: str = "2026-04-03",
    merge_commit: str | None = None,
) -> PeriodPR:
    return PeriodPR(
        number=number,
        title=title,
        merged_at=date.fromisoformat(merged),
        merge_commit_oid=merge_commit,
    )


class _FakeGit:
    def __init__(self, output: str = "") -> None:
        self.output: str = output
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        return self.output


def _raise_not_a_repo(_cwd: Path | None = None) -> Path:
    raise RuntimeError("Failed to determine repository root: not a git repository")


class _FakeDiscovery:
    def __init__(self, root: Path, git: _FakeGit) -> None:
        self.root: Path = root
        self.git: _FakeGit = git
        self.roots: list[Path] = []

    def repo_root(self, cwd: Path | None = None) -> Path:
        return self.root if cwd is None else cwd

    def git_in(self, root: Path) -> _FakeGit:
        self.roots.append(root)
        return self.git


class TestDirectCommits:
    def test_excludes_merge_commit_subjects(self) -> None:
        commits = [Commit("aaa", "Merge pull request #180 from mc/issue-1")]
        assert direct_commits(commits, []) == ()

    def test_excludes_version_bumps(self) -> None:
        commits = [Commit("aaa", "2.37.0")]
        assert direct_commits(commits, []) == ()

    def test_excludes_squash_subject_naming_a_listed_pr(self) -> None:
        commits = [Commit("aaa", "fix: memory leak (#181)")]
        assert direct_commits(commits, [_pr(181)]) == ()

    def test_keeps_subject_naming_an_unlisted_number(self) -> None:
        commits = [Commit("aaa", "fix: memory leak (#999)")]
        assert [c.sha for c in direct_commits(commits, [_pr(181)])] == ["aaa"]

    def test_excludes_a_listed_prs_merge_commit_sha(self) -> None:
        commits = [Commit("d34db33f", "a subject naming nothing")]
        assert direct_commits(commits, [_pr(181, merge_commit="d34db33f")]) == ()

    def test_keeps_a_genuinely_direct_commit(self) -> None:
        commits = [Commit("aaa", "docs: log #1710 in progress.md")]
        kept = direct_commits(commits, [_pr(181, merge_commit="d34db33f")])
        assert [(c.sha, c.subject) for c in kept] == [
            ("aaa", "docs: log #1710 in progress.md")
        ]


class TestFetchCommits:
    def test_parses_sha_and_subject(self) -> None:
        git = _FakeGit("aaa\tdocs: a change\nbbb\t2.37.0\n")
        commits = fetch_commits(_APRIL, git)
        assert [(c.sha, c.subject) for c in commits] == [
            ("aaa", "docs: a change"),
            ("bbb", "2.37.0"),
        ]

    def test_asks_git_for_first_parent_main_within_the_period(self) -> None:
        git = _FakeGit()
        fetch_commits(_APRIL, git)
        (args,) = git.calls
        assert "--first-parent" in args
        assert "main" in args
        assert "--after=2026-04-01T00:00:00" in args
        assert "--before=2026-05-01T00:00:00" in args


class TestDocPaths:
    def test_collects_dedupes_and_sorts_doc_paths(self) -> None:
        log = (
            "docs/adr/0007-a-decision.md\n"
            "apps/example/src/example/bot/guild.py\n"
            "docs/adr/0007-a-decision.md\n"
            "docs/runbook.md\n"
            "\n"
            "CONTEXT.md\n"
        )
        assert doc_paths(log) == (
            "docs/adr/0007-a-decision.md",
            "docs/runbook.md",
        )

    def test_no_doc_changes_is_empty(self) -> None:
        assert doc_paths("apps/example/src/example/bot/guild.py\n") == ()

    def test_asks_git_for_every_commit_on_main(self) -> None:
        git = _FakeGit("docs/adr/0007-a-decision.md\n")
        assert fetch_doc_paths(_APRIL, git) == ("docs/adr/0007-a-decision.md",)
        (args,) = git.calls
        assert "--first-parent" not in args
        assert "--name-only" in args
        assert "main" in args
        assert "--after=2026-04-01T00:00:00" in args
        assert "--before=2026-05-01T00:00:00" in args


class TestMilestonesIn:
    _KNOWN: ClassVar[tuple[MilestoneSummary, ...]] = (
        MilestoneSummary(title="config v2", state="OPEN", due_on=date(2026, 4, 30)),
        MilestoneSummary(title="degraded state", state="OPEN", due_on=None),
    )

    def test_reports_every_milestone_the_period_spans(self) -> None:
        issues = [
            _issue(11, milestone="config v2"),
            _issue(12, milestone="degraded state"),
            _issue(13, milestone="config v2"),
        ]
        assert milestones_in(issues, self._KNOWN) == (
            MilestoneSummary(title="config v2", state="OPEN", due_on=date(2026, 4, 30)),
            MilestoneSummary(title="degraded state", state="OPEN", due_on=None),
        )

    def test_issues_without_a_milestone_report_nothing(self) -> None:
        assert milestones_in([_issue(11), _issue(12)], self._KNOWN) == ()

    def test_unknown_milestone_is_still_reported(self) -> None:
        (milestone,) = milestones_in([_issue(11, milestone="a closed sprint")], ())
        assert (milestone.title, milestone.state, milestone.due_on) == (
            "a closed sprint",
            "unknown",
            None,
        )


class _FakeAnnotations:
    def __init__(self, annotations: Sequence[Annotation] = ()) -> None:
        self.annotations: tuple[Annotation, ...] = tuple(annotations)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token: str, url: str) -> list[Annotation]:
        self.calls.append((token, url))
        return list(self.annotations)


def _at(text: str) -> Annotation:
    return Annotation(datetime.fromisoformat(text).replace(tzinfo=UTC))


def _epoch_ms(day: str) -> int:
    return int(
        datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000,
    )


class TestAnnotationsFrom:
    def test_maps_grafana_epoch_millis(self) -> None:
        annotations = annotations_from(
            [
                {"time": 1775030400000, "text": "v2.37.0"},
                {"text": "an annotation with no timestamp"},
            ]
        )
        assert [note.at for note in annotations] == [
            datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
        ]


class TestCountDeploys:
    def test_no_token_skips_without_reaching_the_network(self) -> None:
        fetch = _FakeAnnotations([_at("2026-04-05T12:00:00")])
        assert count_deploys(_APRIL, None, "deploy", fetch) is None
        assert fetch.calls == []

    def test_queries_the_configured_tag(self) -> None:
        fetch = _FakeAnnotations()
        _ = count_deploys(_APRIL, "a-token", "pinky deploy", fetch)
        ((_, url),) = fetch.calls
        assert "tags=pinky%20deploy" in url

    def test_counts_only_annotations_inside_the_period(self) -> None:
        fetch = _FakeAnnotations(
            [
                _at("2026-03-30T23:00:00"),
                _at("2026-04-01T00:30:00"),
                _at("2026-04-30T23:59:00"),
                _at("2026-05-01T00:30:00"),
            ]
        )
        assert count_deploys(_APRIL, "a-token", "deploy", fetch) == 2
        ((token, url),) = fetch.calls
        assert token == "a-token"
        assert "tags=deploy" in url
        assert f"from={_epoch_ms('2026-04-01')}" in url
        assert f"to={_epoch_ms('2026-05-01')}" in url

    def test_a_failing_endpoint_skips_the_section(self) -> None:
        def fetch(_token: str, _url: str) -> list[Annotation]:
            raise OSError("grafana is down")

        assert count_deploys(_APRIL, "a-token", "deploy", fetch) is None


_POPULATED_ISSUES = [
    _issue(11, title="a child", created="2026-04-02", parent=_EPIC_REF),
    _issue(
        13,
        title="another child",
        state="CLOSED",
        created="2026-01-05",
        closed="2026-04-06",
        parent=_EPIC_REF,
    ),
    _issue(14, title="a third child", created="2026-04-07", parent=_EPIC_REF),
    _issue(12, title="a stray leaf", created="2026-04-08", milestone="config v2"),
]


def _repo_report(
    issues: Sequence[PeriodIssue],
    *,
    name: str = "a-repo",
    milestones: Sequence[MilestoneSummary] = (),
    prs: Sequence[PeriodPR] = (),
    commits: Sequence[Commit] = (),
    deploy_tag: str | None = None,
    deploys: int | None = None,
    docs: Sequence[str] = (),
) -> RepoReport:
    return RepoReport(
        name=name,
        milestones=tuple(milestones),
        rollup=build_rollup(issues, _APRIL),
        prs=tuple(prs),
        commits=tuple(commits),
        deploy_tag=deploy_tag,
        deploys=deploys,
        docs=tuple(docs),
    )


def _report(*repos: RepoReport) -> Report:
    return Report(period=_APRIL, repos=repos)


class TestPrintReport:
    def _printed(self, report: Report) -> list[str]:
        out = StringIO()
        print_report(report, out)
        return [" ".join(line.split()) for line in out.getvalue().splitlines()]

    def test_populated_period(self) -> None:
        lines = self._printed(
            _report(
                _repo_report(
                    _POPULATED_ISSUES,
                    milestones=[
                        MilestoneSummary(
                            title="config v2", state="OPEN", due_on=date(2026, 4, 30)
                        )
                    ],
                    prs=[_pr(180, title="feat: a feature", merged="2026-04-03")],
                    commits=[Commit("aaaaaaaabbbb", "docs: a direct change")],
                    deploy_tag="deploy",
                    deploys=3,
                    docs=["docs/adr/0007-a-decision.md"],
                )
            )
        )
        assert lines[0] == "Period: 2026-04-01 to 2026-04-30"
        assert lines.index("MILESTONES:") < lines.index("EPIC ACTIVITY:")
        assert lines.index("EPIC ACTIVITY:") < lines.index("STANDALONE WORK:")
        assert lines.index("STANDALONE WORK:") < lines.index("MERGED PRs:")
        assert lines.index("MERGED PRs:") < lines.index("DIRECT COMMITS:")
        assert lines.index("DIRECT COMMITS:") < lines.index("DEPLOYS:")
        assert lines.index("DEPLOYS:") < lines.index("CHANGED DOCS:")
        assert "config v2 [OPEN] due 2026-04-30" in lines
        assert "EPIC CREATED CLOSED SUBS + SUBS - TITLE" in lines
        assert "#5 2026-01-15 2 1 the epic" in lines
        assert "#12 2026-04-08 [OPEN] a stray leaf" in lines
        assert "#180 2026-04-03 feat: a feature" in lines
        assert "aaaaaaa docs: a direct change" in lines
        assert "3" in lines
        assert "docs/adr/0007-a-decision.md" in lines

    def test_empty_period(self) -> None:
        lines = self._printed(_report(_repo_report([], deploy_tag="deploy")))
        assert lines[0] == "Period: 2026-04-01 to 2026-04-30"
        assert lines.count("(none)") == 6
        assert "(unavailable)" in lines

    def test_zero_deploys_is_a_count_not_a_skip(self) -> None:
        lines = self._printed(_report(_repo_report([], deploy_tag="deploy", deploys=0)))
        assert "0" in lines
        assert "(unavailable)" not in lines

    def test_an_untagged_repo_has_no_deploys_section(self) -> None:
        lines = self._printed(_report(_repo_report([])))
        assert not any(line.startswith("DEPLOYS") for line in lines)

    def test_each_repo_gets_its_own_headed_sections(self) -> None:
        lines = self._printed(
            _report(
                _repo_report(
                    [_issue(12, title="work in the first", created="2026-04-08")],
                    name="orbatch",
                ),
                _repo_report(
                    [_issue(7, title="work in the second", created="2026-04-09")],
                    name="pinky",
                ),
            )
        )
        assert lines[0] == "Period: 2026-04-01 to 2026-04-30"
        heads = [
            i for i, line in enumerate(lines) if "orbatch" in line or "pinky" in line
        ]
        assert len(heads) == 2
        assert lines.count("STANDALONE WORK:") == 2
        assert lines.index("#12 2026-04-08 [OPEN] work in the first") < heads[1]
        assert lines.index("#7 2026-04-09 [OPEN] work in the second") > heads[1]


class _FakeClient:
    def __init__(self) -> None:
        self.targets: list[Repo] = []
        self.calls: list[tuple[date, date]] = []
        self.pr_calls: list[tuple[date, date]] = []
        self.milestone_calls: list[bool] = []

    def search_period_issues(self, start: date, end: date) -> list[PeriodIssue]:
        self.calls.append((start, end))
        return [_issue(12, title="a stray leaf", created="2026-04-15")]

    def search_period_prs(self, start: date, end: date) -> list[PeriodPR]:
        self.pr_calls.append((start, end))
        return [_pr(180, title="feat: a feature", merged="2026-04-15")]

    def list_milestones(
        self, *, include_closed: bool = False
    ) -> list[MilestoneSummary]:
        self.milestone_calls.append(include_closed)
        return []


_GIT_OUTPUT = (
    "aaaaaaabbbb\tfeat: a feature (#180)\n"
    "cccccccdddd\tchore: a direct change\n"
    "docs/adr/0007-a-decision.md\n"
)


_DISCOVERED_ROOT = Path("/somewhere/a-checkout")


def _serving(client: _FakeClient) -> Callable[[Repo], _FakeClient]:
    def factory(target: Repo) -> _FakeClient:
        client.targets.append(target)
        return client

    return factory


def _fake_repo(root: Path) -> Repo:
    return Repo("an-org", root.name)


class TestCli:
    def _run(
        self,
        args: Sequence[str],
        monkeypatch: pytest.MonkeyPatch,
        *,
        config: Path = Path("/nonexistent/snippets.toml"),
        root_finder: Callable[[Path | None], Path] | None = None,
    ) -> tuple[Result, _FakeClient, _FakeDiscovery]:
        client = _FakeClient()
        discovery = _FakeDiscovery(_DISCOVERED_ROOT, _FakeGit(_GIT_OUTPUT))
        monkeypatch.setattr(snippets.cli, "github_client", _serving(client))
        monkeypatch.setattr(snippets.cli, "_today", lambda: date(2026, 4, 15))
        monkeypatch.setattr(
            snippets.cli, "repo_root", root_finder or discovery.repo_root
        )
        monkeypatch.setattr(snippets.cli, "repo", _fake_repo)
        monkeypatch.setattr(snippets.cli, "git_in", discovery.git_in)
        monkeypatch.setattr(snippets.cli, "config_path", lambda: config)
        monkeypatch.setattr(
            snippets.cli, "DEPLOY_TOKEN_FILE", Path("/nonexistent/token")
        )
        runner = CliRunner(mix_stderr=False)
        return runner.invoke(cli, list(args)), client, discovery

    def test_defaults_to_today(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, client, discovery = self._run([], monkeypatch)
        assert result.exit_code == 0
        assert client.calls == [(date(2026, 4, 15), date(2026, 4, 15))]
        assert client.pr_calls == [(date(2026, 4, 15), date(2026, 4, 15))]
        assert client.milestone_calls == [True]
        assert all(
            "--after=2026-04-15T00:00:00" in args for args in discovery.git.calls
        )
        assert "Period: 2026-04-15 to 2026-04-15" in result.stdout
        assert "#12 2026-04-15 [OPEN] a stray leaf" in result.stdout
        assert "#180 2026-04-15 feat: a feature" in result.stdout
        assert "ccccccc chore: a direct change" in result.stdout
        assert "aaaaaaa feat: a feature (#180)" not in result.stdout
        assert "docs/adr/0007-a-decision.md" in result.stdout

    def test_joins_unquoted_words(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _, client, _ = self._run(["last", "week"], monkeypatch)
        assert client.calls == [(date(2026, 4, 6), date(2026, 4, 12))]

    def test_git_targets_the_discovered_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, discovery = self._run([], monkeypatch)
        assert discovery.roots == [_DISCOVERED_ROOT]

    def test_one_git_binding_serves_every_consumer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, discovery = self._run([], monkeypatch)
        assert len(discovery.roots) == 1
        assert len(discovery.git.calls) > 1

    def test_a_repoless_directory_fails_before_any_github_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient()
        monkeypatch.setattr(snippets.cli, "github_client", _serving(client))
        monkeypatch.setattr(snippets.cli, "_today", lambda: date(2026, 4, 15))
        monkeypatch.setattr(snippets.cli, "repo_root", _raise_not_a_repo)

        report = build_report(
            _period("2026-04-15", "2026-04-15"), [RepoSpec(Path("/nowhere"))]
        )

        (failure,) = report.repos
        assert isinstance(failure, RepoFailure)
        assert "Failed to determine repository root" in failure.message
        assert client.calls == []

    def test_repo_flags_report_on_every_named_checkout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, client, discovery = self._run(
            ["--repo", "/src/orbatch", "--repo", "/src/pinky"], monkeypatch
        )
        assert result.exit_code == 0
        assert discovery.roots == [Path("/src/orbatch"), Path("/src/pinky")]
        assert len(client.calls) == 2
        assert "orbatch" in result.stdout
        assert "pinky" in result.stdout

    def test_each_repo_is_queried_against_its_own_slug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, client, _ = self._run(
            ["--repo", "/src/orbatch", "--repo", "/src/pinky"], monkeypatch
        )
        assert [target.name for target in client.targets] == ["orbatch", "pinky"]

    def test_one_unusable_repo_does_not_cost_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def sometimes_missing(cwd: Path | None = None) -> Path:
            if cwd is not None and cwd.name == "gone":
                raise RuntimeError("Failed to determine repository root: no such repo")
            return cwd or _DISCOVERED_ROOT

        result, client, _ = self._run(
            ["--repo", "/src/gone", "--repo", "/src/pinky"],
            monkeypatch,
            root_finder=sometimes_missing,
        )
        assert result.exit_code == 0
        assert "(skipped: Failed to determine repository root" in result.stdout
        assert "#180 2026-04-15 feat: a feature" in result.stdout
        assert len(client.calls) == 1

    def test_configured_repos_are_used_when_no_flag_is_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "snippets.toml"
        _ = config.write_text('[[repo]]\npath = "/src/pinky"\ndeploy_tag = "deploy"\n')
        result, _, discovery = self._run([], monkeypatch, config=config)
        assert discovery.roots == [Path("/src/pinky")]
        assert "DEPLOYS:" in result.stdout

    def test_a_flag_overrides_the_configured_repos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "snippets.toml"
        _ = config.write_text('[[repo]]\npath = "/src/pinky"\n')
        _, _, discovery = self._run(
            ["--repo", "/src/orbatch"], monkeypatch, config=config
        )
        assert discovery.roots == [Path("/src/orbatch")]

    def test_a_broken_config_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "snippets.toml"
        _ = config.write_text("[[repo]]\nname = 42\n")
        result, client, _ = self._run([], monkeypatch, config=config)
        assert result.exit_code == 1
        assert 'needs a non-empty "path"' in result.stderr
        assert client.calls == []

    def test_a_repo_without_a_deploy_tag_reports_no_deploys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, _, _ = self._run(["--repo", "/src/orbatch"], monkeypatch)
        assert "DEPLOYS" not in result.stdout

    def test_unparseable_period_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, client, _ = self._run(["not a period"], monkeypatch)
        assert result.exit_code == 1
        assert "cannot parse period 'not a period'" in result.stderr
        assert result.stdout == ""
        assert client.calls == []


class TestMain:
    def test_help_reports_the_shim_path_as_the_program_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])

        assert exit_info.value.code == 0
        assert capsys.readouterr().out.startswith("Usage: dev/snippets.py")


def _git(args: list[str], cwd: Path) -> None:
    _ = subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _checkout_with_a_commit(root: Path, subject: str) -> None:
    _git(["git", "init", "-q", "-b", "main"], root)
    _git(["git", "config", "user.email", "test@example.com"], root)
    _git(["git", "config", "user.name", "A Tester"], root)
    _git(
        ["git", "remote", "add", "origin", "git@github.com:example-org/a-repo.git"],
        root,
    )
    (root / "a-file").write_text("contents\n")
    _git(["git", "add", "a-file"], root)
    _git(["git", "commit", "-qm", subject], root)


class TestGitIn:
    def test_runs_against_the_root_it_was_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "checkout"
        elsewhere = tmp_path / "elsewhere"
        checkout.mkdir()
        elsewhere.mkdir()
        _checkout_with_a_commit(checkout, "feat: a commit")
        monkeypatch.chdir(elsewhere)

        assert git_in(checkout)(["log", "--format=%s"]) == "feat: a commit\n"

    def test_a_failing_command_raises_naming_it(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="git log --oneline failed"):
            _ = git_in(tmp_path)(["log", "--oneline"])


class TestReportingOnTheSurroundingCheckout:
    def test_reports_the_checkout_the_cwd_is_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-04-15T12:00:00")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-04-15T12:00:00")
        _checkout_with_a_commit(checkout, "chore: from the surrounding checkout")
        nested = checkout / "deep" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        monkeypatch.setattr(snippets.cli, "github_client", _serving(_FakeClient()))
        monkeypatch.setattr(
            snippets.cli, "config_path", lambda: tmp_path / "nonexistent.toml"
        )
        monkeypatch.setattr(
            snippets.cli, "DEPLOY_TOKEN_FILE", Path("/nonexistent/token")
        )

        report = build_report(_period("2026-04-15", "2026-04-15"), resolve_repos([]))

        (only,) = report.repos
        assert isinstance(only, RepoReport)
        assert only.name == "a-repo"
        assert [c.subject for c in only.commits] == [
            "chore: from the surrounding checkout"
        ]

    def test_a_nested_path_flag_resolves_to_its_checkout_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-04-15T12:00:00")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-04-15T12:00:00")
        _checkout_with_a_commit(checkout, "chore: from a named checkout")
        nested = checkout / "deep"
        nested.mkdir()
        monkeypatch.setattr(snippets.cli, "github_client", _serving(_FakeClient()))
        monkeypatch.setattr(
            snippets.cli, "DEPLOY_TOKEN_FILE", Path("/nonexistent/token")
        )

        report = build_report(
            _period("2026-04-15", "2026-04-15"), [RepoSpec(path=nested)]
        )

        (only,) = report.repos
        assert isinstance(only, RepoReport)
        assert only.name == "a-repo"
        assert [c.subject for c in only.commits] == ["chore: from a named checkout"]
