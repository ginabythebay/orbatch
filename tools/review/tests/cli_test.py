# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from orbit.core import NO_BROWSER_VAR
from review.cli import (
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    LENSES,
    CommandResult,
    Deps,
    Session,
    _run_command,
    _run_session,
    all_templates,
    build_report,
    claude_argv,
    lens_detail,
    parse_pr,
    render_prompt,
    template,
)
from review.cli import cli as review_cli
from review.cli import main as review_main

_HEAD_SHA = "abc1234"
_PR_HEAD = "head1234beef"
_PR_BASE_OID = "base9999feed"
_MERGE_BASE = "mb5555cafe"

_LENS_REPORTS = {
    "correctness": (
        "## correctness\n\n"
        "### Retry loop never terminates on a 500\n"
        "- **Where:** dev/thing.py:12\n"
        "- **Severity:** blocking\n"
        "- **Failure:** a persistent 500 spins forever.\n"
        "- **Fix:** cap the retries.\n"
    ),
    "tests": (
        "## tests\n\n"
        "### The retry path has no test\n"
        "- **Where:** tests/thing_test.py:1\n"
        "- **Severity:** should-fix\n"
        "- **Failure:** a regression in the retry loop ships silently.\n"
        "- **Fix:** add a test that exhausts the retries.\n"
    ),
    "conventions": "## conventions\n\nNo findings.\n",
}

_CONSOLIDATED = (
    "## Findings\n\n"
    "### Retry loop never terminates on a 500\n"
    "- **Where:** dev/thing.py:12\n"
    "- **Severity:** blocking\n"
    "- **Lenses:** correctness, tests\n"
    "- **Failure:** a persistent 500 spins forever and nothing covers it.\n"
    "- **Fix:** cap the retries and test the exhausted path.\n"
)

_PR_METADATA = {
    "headRefOid": _PR_HEAD,
    "baseRefOid": _PR_BASE_OID,
    "baseRefName": "main",
    "title": "Cap the retries",
}


def _kind(prompt: str) -> str:
    for lens in LENSES:
        if f"Your lens for this review is: **{lens}**" in prompt:
            return lens
    return "consolidate"


@dataclass
class FakeSessions:
    failures: frozenset[str] = frozenset()
    explode: frozenset[str] = frozenset()
    barrier: threading.Barrier | None = None
    seen: list[Session] = field(default_factory=list)
    threads: set[str] = field(default_factory=set)
    max_in_flight: int = 0
    in_flight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, session: Session) -> CommandResult:
        kind = _kind(session.prompt)
        with self.lock:
            self.seen.append(session)
            self.threads.add(threading.current_thread().name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.barrier is not None and kind in LENSES:
                _ = self.barrier.wait()
            return self._reply(kind)
        finally:
            with self.lock:
                self.in_flight -= 1

    def _reply(self, kind: str) -> CommandResult:
        if kind in self.explode:
            raise RuntimeError(f"the {kind} session crashed the reviewer")
        if kind in self.failures:
            return CommandResult(3, "", f"the {kind} session exploded")
        reply = _CONSOLIDATED if kind == "consolidate" else _LENS_REPORTS[kind]
        return CommandResult(0, reply, "")

    def prompt(self, kind: str) -> str:
        return next(s.prompt for s in self.seen if _kind(s.prompt) == kind)


@dataclass
class FakeCommands:
    issue_body: str = "Make the retry loop terminate."
    merge_base_found: bool = True
    has_diff: bool = True
    fail_matching: frozenset[str] = frozenset()
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], cwd: Path) -> CommandResult:
        call = list(argv)
        self.calls.append(call)
        if any(pattern in " ".join(call) for pattern in self.fail_matching):
            return CommandResult(1, "", "the command failed")
        match call[:3]:
            case ["git", "diff", "--quiet"]:
                return CommandResult(1 if self.has_diff else 0, "", "")
            case ["git", "rev-parse", "--short"]:
                return CommandResult(0, f"{_HEAD_SHA}\n", "")
            case ["git", "merge-base", _]:
                if not self.merge_base_found:
                    return CommandResult(128, "", "not an ancestor")
                return CommandResult(0, f"{_MERGE_BASE}\n", "")
            case ["gh", "issue", "view"]:
                return CommandResult(0, f"{self.issue_body}\n", "")
            case ["gh", "pr", "view"]:
                return CommandResult(0, json.dumps(_PR_METADATA), "")
            case ["gh", "repo", "view"]:
                return CommandResult(0, "example-org/example-repo\n", "")
            case _:
                return CommandResult(0, "", "")

    def matching(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]


@dataclass(frozen=True)
class Run:
    result: Result
    cache: Path
    sessions: FakeSessions
    commands: FakeCommands

    def run_dir(self) -> Path:
        dirs = [path for path in self.cache.iterdir() if path.is_dir()]
        assert len(dirs) == 1, dirs
        return dirs[0]

    def report(self) -> str:
        return (self.run_dir() / "report.md").read_text()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("REVIEW_CACHE_DIR", str(cache))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("REVIEW_MODEL", raising=False)
    # No browser is launched by a test run, on any platform.
    monkeypatch.setenv(NO_BROWSER_VAR, "1")
    return work


def _run(
    *args: str,
    sessions: FakeSessions | None = None,
    commands: FakeCommands | None = None,
) -> Run:
    sessions = sessions or FakeSessions()
    commands = commands or FakeCommands()
    result = CliRunner().invoke(
        review_cli,
        args,
        obj=Deps(run_command=commands, run_session=sessions),
        catch_exceptions=True,
    )
    return Run(
        result=result,
        cache=Path(os.environ["REVIEW_CACHE_DIR"]),
        sessions=sessions,
        commands=commands,
    )


@pytest.mark.usefixtures("repo")
class TestSessionFanOut:
    def test_one_session_per_lens_plus_one_to_consolidate(self) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        kinds = [_kind(session.prompt) for session in run.sessions.seen]
        assert kinds[-1] == "consolidate"
        assert sorted(kinds[:-1]) == sorted(LENSES)

    def test_every_session_runs_in_the_repo_under_review(self, repo: Path) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        assert {session.cwd for session in run.sessions.seen} == {repo}

    def test_every_session_is_spawned_read_only(self) -> None:
        argv = claude_argv(None)

        assert argv[argv.index("--allowed-tools") + 1] == ALLOWED_TOOLS
        assert argv[argv.index("--disallowed-tools") + 1] == DISALLOWED_TOOLS
        for banned in ("Edit", "Write", "NotebookEdit", "Bash(*lint*)"):
            assert banned in DISALLOWED_TOOLS
        assert "dev/" not in DISALLOWED_TOOLS
        assert argv[:4] == ["claude", "-p", "--output-format", "text"]
        assert "--model" not in argv

    def test_review_model_reaches_every_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_MODEL", "claude-fable-5")

        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        assert {session.model for session in run.sessions.seen} == {"claude-fable-5"}
        assert claude_argv("claude-fable-5")[-2:] == ["--model", "claude-fable-5"]

    def test_serial_mode_runs_one_at_a_time_and_still_consolidates(self) -> None:
        run = _run("-s")

        assert run.result.exit_code == 0, run.result.stderr
        assert run.sessions.threads == {threading.current_thread().name}
        assert run.sessions.max_in_flight == 1
        assert [_kind(s.prompt) for s in run.sessions.seen] == [*LENSES, "consolidate"]
        report = run.report()
        assert report.index("## Findings") < report.index("## Per-lens reports")

    def test_the_lenses_run_concurrently_by_default(self) -> None:
        # Every lens blocks until all three have arrived, so the run only
        # finishes if they were in flight together.
        barrier = threading.Barrier(len(LENSES), timeout=30)

        run = _run(sessions=FakeSessions(barrier=barrier))

        assert run.result.exit_code == 0, run.result.stderr
        assert run.sessions.max_in_flight == len(LENSES)


@pytest.mark.usefixtures("repo")
class TestDegradedSessions:
    def test_a_failed_lens_still_reaches_the_consolidator(self) -> None:
        run = _run(sessions=FakeSessions(failures=frozenset({"tests"})))

        assert run.result.exit_code == 0, run.result.stderr
        prompt = run.sessions.prompt("consolidate")
        begin = prompt.index("--- BEGIN tests ---")
        end = prompt.index("--- END tests ---")
        assert begin < prompt.index("the tests session exploded") < end

    def test_a_failed_lens_reports_its_stderr_under_its_own_heading(self) -> None:
        run = _run(sessions=FakeSessions(failures=frozenset({"tests"})))

        assert run.result.exit_code == 0, run.result.stderr
        report = run.report()
        assert "## tests\n\nReviewer failed. stderr:" in report
        assert "the tests session exploded" in report

    def test_a_failed_consolidator_keeps_every_lens_finding(self) -> None:
        run = _run(sessions=FakeSessions(failures=frozenset({"consolidate"})))

        assert run.result.exit_code == 0, run.result.stderr
        report = run.report()
        assert "the consolidate session exploded" in report
        assert report.index("## Consolidation failed") < report.index(
            "## Per-lens reports"
        )
        for text in _LENS_REPORTS.values():
            assert text.rstrip("\n") in report


@pytest.mark.usefixtures("repo")
class TestPromptRendering:
    def test_no_placeholder_survives_rendering(self) -> None:
        run = _run("-i", "1467")

        assert run.result.exit_code == 0, run.result.stderr
        for kind in (*LENSES, "consolidate"):
            assert "{{" not in run.sessions.prompt(kind), kind

    def test_the_issue_context_is_substituted_last(self) -> None:
        prompt = render_prompt(
            "{{LENS}}/{{DIFF_SPEC}}\n{{ISSUE_CONTEXT}}\n",
            diff_spec="a..b",
            issue_context="quoting {{LENS}} and {{DIFF_SPEC}} verbatim",
            lens="tests",
        )

        assert prompt == "tests/a..b\nquoting {{LENS}} and {{DIFF_SPEC}} verbatim\n"

    def test_a_placeholder_quoted_in_the_issue_body_is_not_expanded(self) -> None:
        body = "Placeholders: {{LENS_REPORTS}}, {{DIFF_SPEC}}."

        run = _run("-i", "1467", commands=FakeCommands(issue_body=body))

        assert run.result.exit_code == 0, run.result.stderr
        prompt = run.sessions.prompt("consolidate")
        assert prompt.count("--- BEGIN correctness ---") == 1
        assert body in prompt

    def test_the_consolidator_is_given_every_lens_report(self) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        prompt = run.sessions.prompt("consolidate")
        for lens, text in _LENS_REPORTS.items():
            begin = prompt.index(f"--- BEGIN {lens} ---")
            end = prompt.index(f"--- END {lens} ---")
            assert begin < prompt.index(text.rstrip("\n")) < end

    def test_the_issue_body_reaches_every_session(self) -> None:
        run = _run("-i", "1467", commands=FakeCommands(issue_body="Cap at three."))

        assert run.result.exit_code == 0, run.result.stderr
        for kind in (*LENSES, "consolidate"):
            prompt = run.sessions.prompt(kind)
            assert "Cap at three." in prompt
            assert "--- BEGIN ISSUE #1467 ---" in prompt

    def test_an_unreadable_issue_stops_the_review(self) -> None:
        run = _run(
            "-i",
            "1467",
            commands=FakeCommands(fail_matching=frozenset({"gh issue view"})),
        )

        assert run.result.exit_code == 1
        assert "ERROR: could not read issue #1467" in run.result.stderr
        assert run.sessions.seen == []

    def test_without_an_issue_the_reviewers_are_told_so(self) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        assert "You have not been told" in run.sessions.prompt("correctness")
        assert run.commands.matching("gh", "issue") == []


@pytest.mark.usefixtures("repo")
class TestReportAssembly:
    def test_merged_findings_precede_the_per_lens_reports(self) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        report = run.report()
        assert report.index("## Findings") < report.index("## Per-lens reports")
        for lens, text in _LENS_REPORTS.items():
            assert text.rstrip("\n") in report, lens
            assert report.index("## Per-lens reports") < report.index(f"## {lens}")

    def test_the_heading_names_the_diff_spec_and_the_commit(self) -> None:
        run = _run("-i", "1467")

        assert run.result.exit_code == 0, run.result.stderr
        assert run.report().startswith(
            f"# Review of `origin/main...HEAD`\n\nCommit: `{_HEAD_SHA}`\nIssue: #1467\n"
        )

    def test_the_report_is_printed_to_stdout(self) -> None:
        run = _run()

        assert run.result.exit_code == 0, run.result.stderr
        assert run.result.stdout == run.report()
        assert "consolidating" in run.result.stderr

    def test_quiet_suppresses_only_the_progress(self) -> None:
        run = _run("-q")

        assert run.result.exit_code == 0, run.result.stderr
        assert run.result.stdout == run.report()
        assert run.result.stderr == ""

    def test_no_consolidate_produces_the_older_shape(self) -> None:
        run = _run("--no-consolidate")

        assert run.result.exit_code == 0, run.result.stderr
        assert len(run.sessions.seen) == len(LENSES)
        report = run.report()
        assert "## Per-lens reports" not in report
        assert "## Findings" not in report
        for text in _LENS_REPORTS.values():
            assert text.rstrip("\n") in report

    def test_an_empty_diff_is_refused_before_any_session(self) -> None:
        sessions = FakeSessions()
        result = CliRunner().invoke(
            review_cli,
            [],
            obj=Deps(run_command=FakeCommands(has_diff=False), run_session=sessions),
        )

        assert result.exit_code == 1
        assert "empty diff for origin/main...HEAD" in result.stderr
        assert sessions.seen == []

    def test_build_report_orders_its_sections(self) -> None:
        report = build_report(
            title="Review of `a..b`",
            commit="deadbee",
            issue="",
            consolidated="## Findings\n\nNo findings.",
            reports=dict.fromkeys(LENSES, "## lens\n\nNo findings.\n"),
        )

        assert report.startswith("# Review of `a..b`\n\nCommit: `deadbee`\n\n")
        assert "Issue:" not in report
        assert report.endswith("No findings.\n")


@pytest.mark.usefixtures("repo")
class TestHtml:
    def test_html_lands_beside_the_markdown_and_is_relinked(self) -> None:
        run = _run("--html")

        assert run.result.exit_code == 0, run.result.stderr
        page = run.run_dir() / "report.html"
        assert str(page) in run.result.stderr
        assert "Retry loop never terminates on a 500" in page.read_text()
        link = run.cache / "last-report.html"
        assert link.is_symlink()
        assert link.resolve() == page
        assert run.result.stdout == run.report()

    def test_open_implies_html(self) -> None:
        run = _run("--open")

        assert run.result.exit_code == 0, run.result.stderr
        assert (run.run_dir() / "report.html").exists()

    def test_markdown_reprints_without_spawning_a_session(self) -> None:
        first = _run()
        assert first.result.exit_code == 0, first.result.stderr
        assert not (first.run_dir() / "report.html").exists()

        run = _run("--markdown", "--html")

        assert run.result.exit_code == 0, run.result.stderr
        assert run.sessions.seen == []
        assert run.result.stdout == first.report()
        page = first.run_dir() / "report.html"
        assert "Retry loop never terminates on a 500" in page.read_text()
        assert (run.cache / "last-report.html").resolve() == page

    def test_markdown_without_a_previous_report_fails(self) -> None:
        run = _run("--markdown")

        assert run.result.exit_code == 1
        assert "no previous report at" in run.result.stderr

    def test_a_report_copied_over_the_symlink_survives_rendering(self) -> None:
        first = _run()
        assert first.result.exit_code == 0, first.result.stderr
        last = first.cache / "last-report.md"
        last.unlink()
        last.write_text(first.report())

        run = _run("--markdown", "--html")

        assert run.result.exit_code == 0, run.result.stderr
        page = run.cache / "last-report.html"
        assert not page.is_symlink()
        assert "Retry loop never terminates on a 500" in page.read_text()

    def test_the_page_survives_a_symlinked_cache_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "real-cache"
        real.mkdir()
        (tmp_path / "cache-link").symlink_to(real)
        monkeypatch.setenv("REVIEW_CACHE_DIR", str(tmp_path / "cache-link"))
        first = _run()
        assert first.result.exit_code == 0, first.result.stderr
        last = first.cache / "last-report.md"
        last.unlink()
        last.write_text(first.report())

        run = _run("--markdown", "--html")

        assert run.result.exit_code == 0, run.result.stderr
        page = real / "last-report.html"
        assert page.is_file()
        assert "Retry loop never terminates on a 500" in page.read_text()

    def test_a_failing_renderer_leaves_the_markdown_on_stdout(self) -> None:
        first = _run()
        assert first.result.exit_code == 0, first.result.stderr
        # A directory where the page goes: only the write can fail.
        (first.run_dir() / "report.html").mkdir()

        run = _run("--markdown", "--html")

        assert run.result.exit_code == 1
        assert run.result.stdout == first.report()
        assert "could not render" in run.result.stderr
        assert not (first.run_dir() / "report.html").is_file()


class TestParsePr:
    @pytest.mark.parametrize(
        ("target", "number", "repo_name"),
        [
            ("123", "123", ""),
            ("owner/repo#123", "123", "owner/repo"),
            ("https://github.com/owner/repo/pull/123", "123", "owner/repo"),
            ("https://github.com/owner/repo/pull/123/files", "123", "owner/repo"),
        ],
    )
    def test_a_pull_request_is_recognised(
        self, target: str, number: str, repo_name: str
    ) -> None:
        pr = parse_pr(target)

        assert pr is not None
        assert (pr.number, pr.repo) == (number, repo_name)

    @pytest.mark.parametrize(
        "target",
        ["origin/main...HEAD", "v1.2.3", "feature/123", "abc123", "HEAD~3..HEAD", ""],
    )
    def test_a_diff_spec_falls_through(self, target: str) -> None:
        assert parse_pr(target) is None


@pytest.mark.usefixtures("repo")
class TestPullRequestReview:
    def test_the_pr_is_fetched_and_reviewed_in_a_detached_worktree(self) -> None:
        run = _run("owner/repo#123")

        assert run.result.exit_code == 0, run.result.stderr
        remote = "https://github.com/owner/repo"
        assert run.commands.matching("git", "fetch") == [
            ["git", "fetch", "-q", remote, "refs/pull/123/head"],
            ["git", "fetch", "-q", remote, "refs/heads/main"],
        ]
        [added] = run.commands.matching("git", "worktree", "add")
        assert added[:5] == ["git", "worktree", "add", "-q", "--detach"]
        assert added[-1] == _PR_HEAD
        assert {session.cwd for session in run.sessions.seen} == {Path(added[5])}
        assert f"{_MERGE_BASE}..{_PR_HEAD}" in run.sessions.prompt("correctness")
        assert run.report().startswith("# Review of owner/repo#123 — Cap the retries")

    def test_a_bare_number_resolves_the_repo_from_the_checkout(self) -> None:
        run = _run("123")

        assert run.result.exit_code == 0, run.result.stderr
        assert run.commands.matching("gh", "repo", "view") != []
        assert run.commands.matching("git", "fetch")[0][3] == (
            "https://github.com/example-org/example-repo"
        )
        assert "example-org/example-repo#123" in run.report()

    def test_the_pinned_base_oid_is_used_when_merge_base_fails(self) -> None:
        run = _run("owner/repo#123", commands=FakeCommands(merge_base_found=False))

        assert run.result.exit_code == 0, run.result.stderr
        assert f"{_PR_BASE_OID}..{_PR_HEAD}" in run.sessions.prompt("correctness")

    def test_the_worktree_is_removed_on_success(self) -> None:
        run = _run("owner/repo#123")

        assert run.result.exit_code == 0, run.result.stderr
        [added] = run.commands.matching("git", "worktree", "add")
        work_dir = Path(added[5])
        assert run.commands.matching("git", "worktree", "remove") == [
            ["git", "worktree", "remove", "--force", str(work_dir)]
        ]
        assert not work_dir.exists()

    @pytest.mark.parametrize(
        ("target", "pattern", "message"),
        [
            ("owner/repo#123", "gh pr view", "could not read PR #123"),
            (
                "owner/repo#123",
                "refs/pull/123/head",
                "could not fetch PR #123 from https://github.com/owner/repo",
            ),
            (
                "owner/repo#123",
                "refs/heads/main",
                "could not fetch base branch main from https://github.com/owner/repo",
            ),
            (
                "owner/repo#123",
                "worktree add",
                f"could not create worktree at {_PR_HEAD}",
            ),
            ("123", "gh repo view", "could not determine the repository for PR #123"),
        ],
    )
    def test_a_failure_on_the_pr_path_names_what_failed(
        self, target: str, pattern: str, message: str
    ) -> None:
        run = _run(target, commands=FakeCommands(fail_matching=frozenset({pattern})))

        assert run.result.exit_code == 1
        assert f"ERROR: {message}" in run.result.stderr
        assert run.sessions.seen == []

    def test_the_worktree_is_removed_when_a_lens_raises(self) -> None:
        run = _run(
            "owner/repo#123",
            sessions=FakeSessions(explode=frozenset({"tests"})),
        )

        assert run.result.exit_code != 0
        [added] = run.commands.matching("git", "worktree", "add")
        work_dir = Path(added[5])
        assert run.commands.matching("git", "worktree", "remove") == [
            ["git", "worktree", "remove", "--force", str(work_dir)]
        ]
        assert not work_dir.exists()


_REPO_SPECIFIC = (
    "CONVENTIONS.md",
    "CONTEXT.md",
    "ops/",
    "apps/figaro",
    "ruff",
    "basedpyright",
    "pinky",
)


class TestTemplates:
    def test_every_lens_has_a_template(self) -> None:
        for lens in LENSES:
            assert lens_detail(lens).strip(), lens

    def test_no_template_names_a_particular_repository(self) -> None:
        for name, text in all_templates().items():
            assert text == template(*name.split("/")), name
            lowered = text.lower()
            for identifier in _REPO_SPECIFIC:
                assert identifier.lower() not in lowered, f"{name}: {identifier}"

    def test_the_enumerator_covers_every_template_on_disk(self) -> None:
        root = Path(str(resources.files("review").joinpath("templates")))

        on_disk = {str(path.relative_to(root)) for path in root.rglob("*.md")}

        assert set(all_templates()) == on_disk

    def test_templates_resolve_outside_any_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        prompt = render_prompt(
            template("review.md"),
            diff_spec="a..b",
            issue_context="none",
            lens="conventions",
        )

        assert "{{" not in prompt
        assert lens_detail("conventions") in prompt

    def test_the_conventions_lens_instructs_discovery(self) -> None:
        detail = lens_detail("conventions")

        assert "guidance" in detail
        assert "links" in detail


@dataclass
class Spawn:
    argv: list[str] = field(default_factory=list)
    stdin: str = ""
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> Spawn:
    spawn = Spawn()

    def fake_run(
        argv: Sequence[str],
        *,
        input: str = "",
        capture_output: bool = False,
        text: bool = False,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        spawn.argv = list(argv)
        spawn.stdin = input
        spawn.cwd = cwd
        spawn.env = dict(env or {})
        assert capture_output and text and not check
        return subprocess.CompletedProcess(list(argv), 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return spawn


class TestTheDefaultRunners:
    def test_a_session_gets_its_prompt_on_stdin_in_the_repo_under_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = _capture_spawn(monkeypatch)

        result = _run_session(Session("the prompt", tmp_path, "claude-fable-5"))

        assert result == CommandResult(0, "out", "err")
        assert spawn.argv == claude_argv("claude-fable-5")
        assert spawn.stdin == "the prompt"
        assert spawn.cwd == tmp_path

    def test_a_configured_diff_external_is_overridden_for_every_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawn = _capture_spawn(monkeypatch)

        _ = _run_command(["git", "status"], tmp_path)

        assert spawn.argv == ["git", "status"]
        assert spawn.cwd == tmp_path
        assert spawn.env["GIT_CONFIG_COUNT"] == "1"
        assert spawn.env["GIT_CONFIG_KEY_0"] == "diff.external"
        assert spawn.env["GIT_CONFIG_VALUE_0"] == ""
        assert spawn.env["PATH"] == os.environ["PATH"]


class TestMain:
    def test_help_names_the_console_script(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as caught:
            review_main(["--help"])

        assert caught.value.code == 0
        assert capsys.readouterr().out.startswith("Usage: review-diff")
