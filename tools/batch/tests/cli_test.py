from __future__ import annotations

import errno
import subprocess
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess
from typing import cast

import click
import pytest
from click.testing import CliRunner, Result

from batch.agent import PlanningAgent
from batch.cli import (
    EMPTY_QUEUE_EXIT,
    _watch_passes,  # pyright: ignore[reportPrivateUsage]
    cli,
    main,
)
from batch.config import BatchConfig
from batch.lock import BatchInProgressError, run_lock
from batch.models import (
    AccountCheckError,
    Alignment,
    BatchIssue,
    BatchLabel,
    DebugEntry,
    DebugRefusal,
    EmptyTokenError,
    HaltReason,
    IssueOutcome,
    KeychainError,
    OccupancyError,
    RunResult,
    WrongAccountError,
)
from batch.orchestrator import Orchestrator
from batch.order import MAIN
from batch.reclaim import Reclaimer
from batch.recovery import Recovery
from batch.stack import StackManager
from batch.teardown import Teardown
from batch.testing.payloads import (
    EPIC,
    EPIC_TITLE,
    TEST_COMMANDS,
    TEST_COMMANDS_TOML,
    TEST_REPO_TOML,
    TEST_SEED,
    TEST_SLUG,
    FakeClock,
    FakeRunner,
    FakeStack,
    FakeState,
    FakeVerifier,
    FakeVms,
    batch_config,
    batch_issue,
    body_writes,
    child,
    children,
    closed_child,
    config_at,
    epic,
    label_ids,
    label_writes,
    no_config_at,
    no_git_remote,
    outside_a_checkout,
    pull_request,
    pull_requests,
    standalone,
    state,
    state_over,
    target,
    targets,
    transport,
    verifier,
    verifier_over,
    write_config,
)
from batch.testing.scratch import Scratch, scratch
from batch.text_output import debug_line
from batch.verbs import Verbs
from batch.vm import PS, GuestAccount, VmRunner


@contextmanager
def _no_assertion(_report: Callable[[str], None], **_kwargs: object) -> Generator[None]:
    yield


def _never_caffeinate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("batch.cli.awake", _no_assertion)


_ = pytest.fixture(autouse=True)(_never_caffeinate)


class TestStatus:
    def test_renders_a_row_per_batch_issue_in_order(self) -> None:
        response = children(
            child(70, labels=["planned"], title="Stack manager"),
            child(12, labels=["queued"], title="Verifier"),
        )

        result = CliRunner().invoke(cli, ["status", str(EPIC)], obj=state(response))

        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == 2
        assert "#70" in lines[0]
        assert "planned" in lines[0]
        assert "Stack manager" in lines[0]
        assert "#12" in lines[1]
        assert "queued" in lines[1]

    def test_every_batch_state_renders(self) -> None:
        response = children(
            *(
                child(index, labels=[label.value])
                for index, label in enumerate(BatchLabel, start=1)
            )
        )

        result = CliRunner().invoke(cli, ["status", str(EPIC)], obj=state(response))

        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == len(BatchLabel)
        for line, label in zip(lines, BatchLabel, strict=True):
            assert label.value in line

    def test_an_empty_batch_says_so(self) -> None:
        response = children(child(1), child(2))

        result = CliRunner().invoke(cli, ["status", str(EPIC)], obj=state(response))

        assert result.exit_code == 0
        assert "No batch issues under #1492" in result.output


class TestStatusVerbose:
    def _response(self) -> Mapping[str, object]:
        return children(
            child(70, labels=["implementing"], title="Stack manager"),
            child(12, labels=["queued"], title="Verifier"),
            child(9),
            child(1503, state="CLOSED", labels=["stuck"], title="Teardown on merge"),
        )

    def _root(self, tmp_path: Path) -> Path:
        runner = VmRunner(tmp_path, config=batch_config)
        runner.socket(70).touch()
        runner.log(70).touch()
        runner.config_dir(70).mkdir()
        return tmp_path

    def _invoke(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str
    ) -> Result:
        _ = config_at(monkeypatch, tmp_path / "repo")
        return CliRunner().invoke(
            cli,
            ["status", str(EPIC), "--run-root", str(self._root(tmp_path)), *flags],
            obj=state(self._response()),
        )

    def test_without_verbose_nothing_probes_the_run_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch)

        assert result.exit_code == 0
        assert str(tmp_path) not in result.output
        assert "socket live" not in result.output
        assert "Dropped children:" not in result.output
        assert "1 open child with no batch label.\n" in result.output
        assert (
            "  #1503 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
            in result.output
        )

    @pytest.mark.parametrize("flag", ["-v", "--verbose"])
    def test_verbose_surfaces_the_vm_facts_of_the_live_issue(
        self, tmp_path: Path, flag: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = VmRunner(tmp_path, config=batch_config)

        result = self._invoke(tmp_path, monkeypatch, flag)

        assert result.exit_code == 0
        assert (
            f"  #70 socket live, log {runner.log(70)}, config {runner.config_dir(70)}"
            in result.output
        )
        assert "#12 socket" not in result.output
        assert "  #9 no batch label — Issue 9" in result.output
        assert "  #1503 closed, labelled stuck — Teardown on merge" in result.output


class TestStatusMergedChild:
    def _invoke(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str
    ) -> Result:
        _ = config_at(monkeypatch, tmp_path / "repo")
        response = children(
            child(70, labels=["implementing"], title="Stack manager"),
            child(
                1600,
                state="CLOSED",
                labels=["ready-for-review"],
                title="Teardown on merge",
                closing_prs=[True],
            ),
        )
        return CliRunner().invoke(
            cli,
            ["status", str(EPIC), "--run-root", str(tmp_path), *flags],
            obj=state(response),
        )

    def test_a_merged_child_raises_no_anomaly_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch)

        assert result.exit_code == 0
        assert "reopen it or `bin/acme skip` it" not in result.output

    def test_verbose_still_lists_the_merged_child_as_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch, "-v")

        assert result.exit_code == 0
        assert (
            "  #1600 closed, labelled ready-for-review — Teardown on merge"
            in result.output
        )


EPIC_LINE = f"Epic #{EPIC} {EPIC_TITLE}"


class TestQueue:
    def test_reports_what_it_labeled_and_skipped(self) -> None:
        fake = transport(
            children(child(1), child(2, labels=["planned"])),
            label_ids(),
            {},
        )

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Queued #1" in result.output
        assert "Skipped #2 (already planned)" in result.output

    def test_the_epic_sweep_labels_every_unlabelled_open_child(self) -> None:
        fake = transport(
            children(child(1), child(2, state="CLOSED"), child(3)),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert EPIC_LINE in result.output
        assert label_writes(fake) == [
            ("add", "I_1", "LA_queued"),
            ("add", "I_3", "LA_queued"),
        ]

    def test_passes_listed_issue_numbers_through(self) -> None:
        fake = transport(children(child(1), child(2)), label_ids(), {})

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC), "2"], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert label_writes(fake) == [("add", "I_2", "LA_queued")]

    def test_a_number_outside_the_epic_is_a_usage_error(self) -> None:
        fake = transport(children(child(1)))

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC), "999"], obj=state_over(fake)
        )

        assert result.exit_code != 0
        assert "#999 is not a child of #1492" in result.output
        assert label_writes(fake) == []

    def test_named_issues_carry_no_epic_header(self) -> None:
        fake = transport(standalone(child(1601), child(1602)), label_ids(), {}, {})

        result = CliRunner().invoke(
            cli, ["queue", "1601", "1602"], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == ["Queued #1601, #1602"]

    def test_the_epic_line_prints_even_when_nothing_was_eligible(self) -> None:
        fake = transport(children(child(1, labels=["queued"])))

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            "Nothing to queue.",
            "Skipped #1 (already queued)",
        ]

    def test_an_epic_positional_now_queues_its_children(self) -> None:
        fake = transport(
            targets(epic(child(1)), target(child(1601))), label_ids(), {}, {}
        )

        result = CliRunner().invoke(
            cli, ["queue", str(EPIC), "1601"], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == ["Queued #1, #1601"]

    def test_naming_nothing_at_all_is_an_error(self) -> None:
        fake = transport()

        result = CliRunner().invoke(cli, ["queue"], obj=state_over(fake))

        assert result.exit_code != 0
        assert "name at least one epic or issue number" in result.output


class TestUnqueue:
    def test_removes_the_label_and_reports(self) -> None:
        fake = transport(children(child(1, labels=["queued"])), label_ids(), {})

        result = CliRunner().invoke(
            cli, ["unqueue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Unqueued #1" in result.output
        assert label_writes(fake) == [("remove", "I_1", "LA_queued")]

    def test_a_standalone_issue_needs_no_epic(self) -> None:
        fake = transport(standalone(child(1601, labels=["queued"])), label_ids(), {})

        result = CliRunner().invoke(cli, ["unqueue", "1601"], obj=state_over(fake))

        assert result.exit_code == 0
        assert result.output.splitlines() == ["Unqueued #1601"]
        assert label_writes(fake) == [("remove", "I_1601", "LA_queued")]


class TestApprove:
    def test_moves_queued_children_to_planned(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["planned"])),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli, ["approve", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Approved #1" in result.output
        assert "Skipped #2 (already planned)" in result.output
        assert label_writes(fake) == [
            ("add", "I_1", "LA_planned"),
            ("remove", "I_1", "LA_queued"),
        ]

    def test_guidance_is_written_to_the_body(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"], body="Intro")),
            {},
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["approve", "--epic", str(EPIC), "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert body_writes(fake) == [
            ("I_1", "Intro\n\n## Test Guidance\n\nNo new tests.")
        ]

    def test_a_standalone_issue_still_takes_guidance(self) -> None:
        fake = transport(
            standalone(child(1601, labels=["queued"], body="Intro")),
            {},
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["approve", "1601", "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == ["Approved #1601"]
        assert body_writes(fake) == [
            ("I_1601", "Intro\n\n## Test Guidance\n\nNo new tests.")
        ]

    def test_a_refused_guidance_write_is_reported(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"], body="## Test Plan\n\n1. A test")),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["approve", "--epic", str(EPIC), "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert "Approved #1" in result.output
        assert "#1 already has a Test Plan" in result.output
        assert body_writes(fake) == []


class TestFastTrack:
    def test_moves_unlabelled_children_to_planned_in_one_call(self) -> None:
        fake = transport(
            standalone(child(1601, body="Intro"), child(1602, body="Intro")),
            {},
            label_ids(),
            {},
            {},
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["fast-track", "1601", "1602", "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == ["Approved #1601, #1602"]
        assert label_writes(fake) == [
            ("add", "I_1601", "LA_planned"),
            ("add", "I_1602", "LA_planned"),
        ]
        assert body_writes(fake) == [
            ("I_1601", "Intro\n\n## Test Guidance\n\nNo new tests."),
            ("I_1602", "Intro\n\n## Test Guidance\n\nNo new tests."),
        ]

    def test_a_queued_child_is_carried_the_rest_of_the_way(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"])),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli, ["fast-track", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert label_writes(fake) == [
            ("add", "I_1", "LA_planned"),
            ("remove", "I_1", "LA_queued"),
        ]

    def test_guidance_is_refused_on_an_issue_that_already_has_a_test_plan(self) -> None:
        fake = transport(
            standalone(child(1601, body="## Test Plan\n\n1. A test")),
            label_ids(),
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["fast-track", "1601", "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert "Approved #1601" in result.output
        assert "#1601 already has a Test Plan" in result.output
        assert body_writes(fake) == []

    def test_a_child_past_queued_is_skipped_with_a_reason(self) -> None:
        fake = transport(standalone(child(1601, labels=["implementing"])))

        result = CliRunner().invoke(cli, ["fast-track", "1601"], obj=state_over(fake))

        assert result.exit_code == 0
        assert "Nothing to approve." in result.output
        assert "Skipped #1601 (already implementing)" in result.output
        assert label_writes(fake) == []


class TestNothingToDo:
    def test_queue_with_no_eligible_children_says_so(self) -> None:
        fake = transport(children(child(1, labels=["queued"])))

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Nothing to queue." in result.output
        assert label_writes(fake) == []

    def test_unqueue_with_no_eligible_children_says_so(self) -> None:
        fake = transport(children(child(1, labels=["planned"])))

        result = CliRunner().invoke(
            cli, ["unqueue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Nothing to unqueue." in result.output
        assert label_writes(fake) == []


@pytest.fixture(name="sc")
def scratch_repo(tmp_path: Path) -> Scratch:
    return scratch(tmp_path)


class TestStack:
    def test_ensure_reports_the_slot(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        result = CliRunner().invoke(
            cli, ["stack", "ensure", "9", "--base", "main"], obj=manager
        )

        assert result.exit_code == 0
        assert "issue-9" in result.output
        assert str(sc.trees / "issue-9.raw") in result.output
        assert "aligned" in result.output

    def test_ensure_flags_a_drifted_base(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        sc.branch_at("issue-8", "HEAD")
        sc.checkout("issue-8")
        _ = manager.ensure(9, "issue-8")
        _ = sc.commit("rework lands on the predecessor")

        result = CliRunner().invoke(
            cli, ["stack", "ensure", "9", "--base", "issue-8"], obj=manager
        )

        assert result.exit_code == 0
        assert "behind" in result.output

    def test_ensure_reads_as_english_for_an_unrelated_base(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(9, "main")
        _ = sc.orphan("elsewhere", "unrelated root")

        result = CliRunner().invoke(
            cli, ["stack", "ensure", "9", "--base", "elsewhere"], obj=manager
        )

        assert result.exit_code == 0
        assert "issue-9 is unrelated to elsewhere" in result.output

    def test_remove_reports_what_it_removed(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(9, "main")

        result = CliRunner().invoke(cli, ["stack", "remove", "9"], obj=manager)

        assert result.exit_code == 0
        assert "worktree" in result.output
        assert "branch" in result.output
        assert "disk" in result.output

    def test_an_unsafe_removal_exits_nonzero_with_the_reason(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        result = CliRunner().invoke(cli, ["stack", "remove", "9"], obj=manager)

        assert result.exit_code != 0
        assert "local changes" in result.output
        assert slot.worktree.is_dir()

    def test_a_squash_landed_branch_is_still_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, "feature.txt", "the work\n", "agent work")
        sc.push("issue-9", slot.worktree)
        _ = sc.land({"feature.txt": "the work\n"}, "agent work (#9)")
        sc.unpublish("issue-9")

        result = CliRunner().invoke(cli, ["stack", "remove", "9"], obj=manager)

        assert result.exit_code != 0
        assert "unpushed commits" in result.output
        assert slot.worktree.is_dir()

    def test_force_removes_what_the_check_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        result = CliRunner().invoke(
            cli, ["stack", "remove", "9", "--force"], obj=manager
        )

        assert result.exit_code == 0
        assert not slot.worktree.exists()


class TestVerify:
    def test_a_clean_verdict_exits_zero(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="SUCCESS")
        )

        result = CliRunner().invoke(
            cli, ["verify", "9", "--base", "issue-8"], obj=verifier(response)
        )

        assert result.exit_code == 0
        assert "#101" in result.output
        assert "green" in result.output

    def test_every_problem_is_printed_and_the_exit_code_is_nonzero(self) -> None:
        response = pull_requests(
            pull_request(101, base="main", body="Fixes #7", ci="SUCCESS")
        )

        result = CliRunner().invoke(
            cli, ["verify", "9", "--base", "issue-8"], obj=verifier(response)
        )

        assert result.exit_code != 0
        assert "wrong-base" in result.output
        assert "missing-issue-reference" in result.output

    def test_a_branch_with_no_pr_at_all_says_so(self) -> None:
        result = CliRunner().invoke(
            cli, ["verify", "9", "--base", "issue-8"], obj=verifier(pull_requests())
        )

        assert result.exit_code != 0
        assert "no PR for issue-9" in result.output

    def test_a_closed_pr_is_named_as_closed(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", state="CLOSED", body="Fixes #9", ci="SUCCESS"
            )
        )

        result = CliRunner().invoke(
            cli, ["verify", "9", "--base", "issue-8"], obj=verifier(response)
        )

        assert result.exit_code != 0
        assert "closed without merging" in result.output

    def test_the_other_prs_on_the_branch_are_listed(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", body="Fixes #9", created_at="2026-08-01T00:00:00Z"
            ),
            pull_request(
                102,
                base="issue-8",
                body="Fixes #9",
                ci="SUCCESS",
                created_at="2026-08-06T00:00:00Z",
            ),
        )

        result = CliRunner().invoke(
            cli, ["verify", "9", "--base", "issue-8"], obj=verifier(response)
        )

        assert result.exit_code != 0
        assert "extra-prs" in result.output
        assert "#101" in result.output

    def test_the_wait_option_reaches_the_poll_loop(self) -> None:
        clock = FakeClock()
        pending = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="PENDING")
        )
        green = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="SUCCESS")
        )

        result = CliRunner().invoke(
            cli,
            ["verify", "9", "--base", "issue-8", "--wait", "300"],
            obj=verifier_over(transport(pending, green), clock),
        )

        assert result.exit_code == 0
        assert clock.sleeps == [30.0]


class TestVm:
    def _runner(self, tmp_path: Path) -> VmRunner:
        return VmRunner(tmp_path, environ={}, config=batch_config, disks=frozenset)

    def test_console_previews_a_foreground_vibe_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "console",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--issue",
                "1499",
                "--dry-run",
            ],
            obj=self._runner(tmp_path),
        )

        assert result.exit_code == 0
        assert result.output.startswith("vibe ")
        assert "poweroff" not in result.output
        assert "tools/drive 1499" in result.output

    def test_a_relative_worktree_still_claims_the_bare_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`gc` looks a claim up by branch; a nested claim path reads as free."""
        _ = config_at(monkeypatch, tmp_path / "repo")
        runner = self._runner(tmp_path)
        held: list[Path] = []

        def record(
            command: Sequence[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            del command, kwargs
            held.extend(tmp_path.rglob("*.claim"))
            return subprocess.CompletedProcess([], 0)

        def staged(_self: VmRunner, _config_dir: Path) -> None:
            return None

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        monkeypatch.setattr("batch.cli.VmRunner.write_config", staged)

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "console",
                "--worktree",
                "widgets/worktrees/issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--issue",
                "1499",
            ],
            obj=runner,
        )

        assert result.exit_code == 0, result.output
        assert held == [runner.claim_path("issue-1499")]

    def test_launch_previews_a_detached_dtach_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "launch",
                "1499",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--dry-run",
            ],
            obj=self._runner(tmp_path),
        )

        assert result.exit_code == 0
        assert result.output.startswith("dtach -n ")
        assert str(tmp_path / "issue-1499.sock") in result.output
        assert str(tmp_path / "issue-1499.log") in result.output
        assert "poweroff" in result.output
        assert "tools/drive 1499" in result.output

    def test_a_dry_run_launch_resolves_no_token_and_checks_no_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        def _refuse(_value: str) -> str:
            raise AssertionError("a dry run must reach neither keychain nor network")

        runner = VmRunner(
            tmp_path,
            environ={},
            config=batch_config,
            disks=frozenset,
            token=_refuse,
            account=GuestAccount(login=_refuse),
        )

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "launch",
                "1499",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--dry-run",
            ],
            obj=runner,
        )

        assert result.exit_code == 0
        assert result.output.startswith("dtach -n ")

    def test_status_reads_the_socket(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)

        gone = CliRunner().invoke(cli, ["vm", "status", "1499"], obj=runner)
        runner.socket(1499).touch()
        live = CliRunner().invoke(cli, ["vm", "status", "1499"], obj=runner)

        assert gone.exit_code == 1
        assert "exited" in gone.output
        assert live.exit_code == 0
        assert "running" in live.output

    def test_status_reads_the_socket_without_a_batch_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = no_config_at(monkeypatch, tmp_path / "repo")
        (tmp_path / "issue-1499.sock").touch()

        result = CliRunner().invoke(
            cli, ["vm", "--run-root", str(tmp_path), "status", "1499"]
        )

        assert result.exit_code == 0, result.output
        assert "running" in result.output
        assert str(tmp_path / "issue-1499.log") in result.output

    def test_status_of_an_exited_vm_outside_a_checkout_still_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside_a_checkout(monkeypatch)

        result = CliRunner().invoke(
            cli, ["vm", "--run-root", str(tmp_path), "status", "1499"]
        )

        assert result.exit_code == 1
        assert "exited" in result.output

    def test_console_still_reports_every_problem_in_a_malformed_batch_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo", "[vm]\nseed_image = 7\n")

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "--run-root",
                str(tmp_path),
                "console",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--issue",
                "1499",
                "--dry-run",
            ],
        )

        assert result.exit_code != 0
        assert "seed_image" in result.output
        assert "slug" in result.output

    def test_a_malformed_batch_toml_stages_no_config_before_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo", "[vm]\nseed_image = 7\n")
        runner = self._runner(tmp_path)

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "launch",
                "1499",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
            ],
            obj=runner,
        )

        assert result.exit_code != 0
        assert "seed_image" in result.output
        assert not runner.config_dir(1499).exists()

    def test_an_explicit_issue_overrides_the_launched_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "launch",
                "1499",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--issue",
                "42",
                "--dry-run",
            ],
            obj=self._runner(tmp_path),
        )

        assert result.exit_code == 0
        assert "tools/drive 42" in result.output
        assert str(tmp_path / "issue-1499.sock") in result.output

    def test_the_run_root_option_places_the_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")
        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "--run-root",
                str(tmp_path),
                "launch",
                "1499",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0
        assert str(tmp_path / "issue-1499.sock") in result.output

    def test_console_needs_a_config_dir(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "console",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--dry-run",
            ],
            obj=self._runner(tmp_path),
        )

        assert result.exit_code != 0
        assert "--config-dir is required" in result.output

    def test_guidance_without_an_issue_is_refused(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "console",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--guidance",
                "no new tests",
                "--dry-run",
            ],
            obj=self._runner(tmp_path),
        )

        assert result.exit_code != 0
        assert "need an --issue" in result.output

    def test_cleaning_a_live_vm_is_refused(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        runner.socket(1499).touch()

        result = CliRunner().invoke(cli, ["vm", "clean", "1499"], obj=runner)

        assert result.exit_code != 0
        assert "still running" in result.output


def _orchestrator(
    state: FakeState,
    root: Path,
    failing: tuple[int, ...] = (),
    report: Callable[[str], None] = lambda _line: None,
    polls: Mapping[int, int] | None = None,
) -> Orchestrator:
    clock = FakeClock()
    stack = FakeStack(root)
    runner = FakeRunner(root, polls=polls)
    verifier = FakeVerifier(failing)
    return Orchestrator(
        state,
        stack,
        runner,
        verifier,
        Teardown(state, stack, runner, verifier),
        config=batch_config(),
        report=report,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


class TestRun:
    def test_reports_each_issue_it_advanced(self, tmp_path: Path) -> None:
        state = FakeState(batch_issue(10), batch_issue(11))

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(state, tmp_path),
        )

        assert result.exit_code == 0
        assert "#10 ready-for-review on main PR #110" in result.output
        assert "#11 ready-for-review on issue-10 PR #111" in result.output

    def test_a_halt_is_reported_and_exits_nonzero(self, tmp_path: Path) -> None:
        state = FakeState(batch_issue(10), batch_issue(11))

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(state, tmp_path, failing=(10,)),
        )

        assert result.exit_code == 1
        assert "#10 stuck (verification-failed) on main" in result.output
        assert "Batch halted at #10." in result.output

    def test_an_empty_batch_says_so(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(), tmp_path),
        )

        assert result.exit_code == 0
        assert f"Nothing planned under #{EPIC}." in result.output

    def test_a_concurrent_run_is_refused(self, tmp_path: Path) -> None:
        with run_lock(tmp_path):
            result = CliRunner().invoke(
                cli,
                ["run", str(EPIC), "--run-root", str(tmp_path)],
                obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
            )

        assert result.exit_code == 1
        assert "another batch run holds" in result.output

    def test_the_lock_is_released_after_a_run(self, tmp_path: Path) -> None:
        args = ["run", str(EPIC), "--run-root", str(tmp_path)]
        _ = CliRunner().invoke(
            cli, args, obj=_orchestrator(FakeState(batch_issue(10)), tmp_path)
        )

        result = CliRunner().invoke(
            cli, args, obj=_orchestrator(FakeState(batch_issue(11)), tmp_path)
        )

        assert result.exit_code == 0
        assert "#11 ready-for-review on main" in result.output


class TestRunWatch:
    def _sleeper(
        self, monkeypatch: pytest.MonkeyPatch, body: Callable[[], None] = lambda: None
    ) -> list[float]:
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            body()

        monkeypatch.setattr("batch.cli.sleep", sleeper)
        return slept

    def _invoke(self, tmp_path: Path, state: FakeState, *extra: str) -> Result:
        return CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path), *extra],
            obj=_orchestrator(state, tmp_path),
        )

    def test_an_issue_planned_mid_wait_is_picked_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))

        def plan_one() -> None:
            state.issues.append(batch_issue(10))
            state.queued_targets = ()

        _ = self._sleeper(monkeypatch, plan_one)

        result = self._invoke(tmp_path, state)

        assert result.exit_code == 0, result.output
        assert result.output == (
            f"Waiting for queued issues under #{EPIC}.\n"
            "#10 ready-for-review on main PR #110\n"
        )

    def test_the_wait_names_every_target_still_holding_queued_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC, 1769))

        def stop_waiting() -> None:
            state.queued_targets = ()

        _ = self._sleeper(monkeypatch, stop_waiting)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "1769", "--run-root", str(tmp_path)],
            obj=_orchestrator(state, tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert f"Waiting for queued issues under #{EPIC}, #1769.\n" in result.output

    def test_a_halt_stops_the_watch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never() -> None:
            raise AssertionError("a halt must not wait")

        slept = self._sleeper(monkeypatch, never)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(
                FakeState(batch_issue(10), queued_targets=(EPIC,)),
                tmp_path,
                failing=(10,),
            ),
        )

        assert result.exit_code == 1
        assert slept == []
        assert "Batch halted at #10." in result.output

    def test_a_sweep_that_keeps_refusing_says_so_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))
        state.closed.append(batch_issue(9))
        polls = 0

        def third_poll_ends_planning() -> None:
            nonlocal polls
            polls += 1
            if polls == 3:
                state.queued_targets = ()

        _ = self._sleeper(monkeypatch, third_poll_ends_planning)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(state, tmp_path, report=click.echo),
        )

        assert result.exit_code == 0, result.output
        assert result.output.count("#9 left alone (not-merged)") == 1

    def test_a_watch_that_only_idled_says_nothing_was_planned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))
        slept = self._sleeper(monkeypatch, lambda: setattr(state, "queued_targets", ()))

        result = self._invoke(tmp_path, state)

        assert result.exit_code == 0, result.output
        assert len(slept) == 1
        assert result.output == (
            f"Waiting for queued issues under #{EPIC}.\n"
            f"Nothing planned under #{EPIC}.\n"
        )

    def test_a_queued_issue_outside_the_targets_is_not_worth_waiting_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never() -> None:
            raise AssertionError("another target's queue must not hold the watch")

        slept = self._sleeper(monkeypatch, never)

        result = self._invoke(tmp_path, FakeState(queued_targets=(EPIC + 1,)))

        assert result.exit_code == 0, result.output
        assert slept == []
        assert result.output == f"Nothing planned under #{EPIC}.\n"

    def test_the_watch_interval_reaches_the_sleeper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))
        slept = self._sleeper(monkeypatch, lambda: setattr(state, "queued_targets", ()))

        result = self._invoke(tmp_path, state, "--watch-interval", "5")

        assert result.exit_code == 0, result.output
        assert slept == [5.0]

    def test_the_watch_interval_defaults_to_a_minute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))
        slept = self._sleeper(monkeypatch, lambda: setattr(state, "queued_targets", ()))

        result = self._invoke(tmp_path, state)

        assert result.exit_code == 0, result.output
        assert slept == [60.0]

    def test_watch_is_no_longer_a_flag(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path), "--watch"],
            obj=_orchestrator(FakeState(), tmp_path),
        )

        assert result.exit_code != 0
        assert "no such option" in result.output.lower()

    def test_ctrl_c_mid_wait_leaves_no_lock_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupt() -> None:
            raise KeyboardInterrupt

        _ = self._sleeper(monkeypatch, interrupt)

        result = self._invoke(tmp_path, FakeState(queued_targets=(EPIC,)))

        assert result.exit_code == 130
        assert result.output == f"Waiting for queued issues under #{EPIC}.\n"

        with run_lock(tmp_path):
            pass

    def test_ctrl_c_mid_pass_exits_the_same_way_as_mid_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every pass runs under the watch now, so there is one interrupt path."""

        def interrupted(_self: Orchestrator, _targets: Sequence[int]) -> RunResult:
            raise KeyboardInterrupt

        monkeypatch.setattr(Orchestrator, "run", interrupted)

        result = self._invoke(tmp_path, FakeState())

        assert result.exit_code == 130


class TestWatchPassesWrapper:
    def _drive(self, orchestrator: Orchestrator) -> RunResult:
        return _watch_passes(
            orchestrator,
            (EPIC,),
            0.0,
            prog="batch",
            report=lambda _line: None,
            echo=False,
        )

    def _refused(self) -> FakeState:
        state = FakeState()
        state.closed.append(batch_issue(9))
        return state

    def test_a_line_from_the_first_call_is_said_again_on_the_second(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []
        orchestrator = _orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)
        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"] * 2

    def test_the_original_report_is_back_once_the_call_returns(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []
        orchestrator = _orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)

        assert orchestrator.report == said.append

    def test_the_original_report_is_back_after_an_interrupt(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []

        def interrupt(_state: FakeState) -> None:
            raise KeyboardInterrupt

        state = self._refused()
        state.on_fetch = interrupt
        orchestrator = _orchestrator(state, tmp_path, report=said.append)

        with pytest.raises(SystemExit) as exit_code:
            _ = self._drive(orchestrator)

        assert exit_code.value.code == 130
        assert orchestrator.report == said.append

        state.on_fetch = None
        said.clear()
        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"]

    def test_a_line_repeated_within_one_pass_is_said_once(self, tmp_path: Path) -> None:
        said: list[str] = []
        orchestrator = _orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"]

    def test_a_line_repeated_across_passes_is_said_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        state = FakeState(batch_issue(10), queued_targets=(EPIC,))
        state.closed.append(batch_issue(9))

        def stop_waiting(_seconds: float) -> None:
            state.queued_targets = ()

        monkeypatch.setattr("batch.cli.sleep", stop_waiting)

        _ = self._drive(_orchestrator(state, tmp_path, report=said.append))

        assert said.count("#9 left alone (not-merged)") == 2


def _planning_stack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    absent: Sequence[str] = (),
    dirty: Sequence[str] = (),
) -> FakeStack:
    built = FakeStack(tmp_path, absent=absent, dirty=dirty)

    def stack_at(_path: object, *, seed_image: Path) -> FakeStack:
        built.seed_image = seed_image
        return built

    _ = config_at(monkeypatch, tmp_path)
    monkeypatch.setattr("batch.cli.StackManager", stack_at)
    return built


PLAN_PID = 4321


def _plan_pid(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("batch.cli.os.getpid", lambda: PLAN_PID)
    return f"plan-{PLAN_PID}"


class TestPlan:
    def _invoke(self, tmp_path: Path, *extra: str) -> Result:
        return CliRunner().invoke(
            cli,
            ["plan", str(EPIC), "--run-root", str(tmp_path), "--dry-run", *extra],
        )

    def test_boots_a_foreground_vm_on_its_own_planning_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        branch = _plan_pid(monkeypatch)

        result = self._invoke(tmp_path, "--model", "opus")

        assert result.exit_code == 0, result.output
        assert stack.currented == [(branch, "main")]
        assert result.output.startswith("vibe ")
        assert "poweroff" not in result.output
        assert f"--send 'cd widgets/worktrees/{branch}'" in result.output
        assert str(stack.worktree_root / f"{branch}.raw") in result.output
        assert f"tools/plan {EPIC} --model opus" in result.output

    def test_every_target_named_reaches_the_planning_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _planning_stack(monkeypatch, tmp_path)
        _ = _plan_pid(monkeypatch)

        result = CliRunner().invoke(
            cli,
            ["plan", str(EPIC), "1769", "--run-root", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert f"tools/plan {EPIC} 1769" in result.output

    def test_vibe_runs_from_the_mount_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _planning_stack(monkeypatch, tmp_path)
        _ = _plan_pid(monkeypatch)
        seen: list[Path | None] = []

        def record(
            command: Sequence[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == PS[0]:
                return subprocess.CompletedProcess([], 0, stdout="")
            seen.append(cast("Path | None", kwargs.get("cwd")))
            return subprocess.CompletedProcess([], 0)

        def staged(_self: VmRunner, _config_dir: Path) -> None:
            return None

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        monkeypatch.setattr("batch.cli.VmRunner.write_config", staged)

        result = CliRunner().invoke(
            cli, ["plan", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert seen == [tmp_path]

    def test_planning_runs_alongside_a_batch_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _planning_stack(monkeypatch, tmp_path)
        _ = _plan_pid(monkeypatch)

        with run_lock(tmp_path):
            result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output

    def test_a_slot_behind_main_boots_without_a_drift_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        branch = _plan_pid(monkeypatch)
        stack.alignment = Alignment.BEHIND

        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert stack.currented == [(branch, "main")]
        assert "is behind main" not in result.output

    def test_a_refused_slot_aborts_before_any_vm_boots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        branch = _plan_pid(monkeypatch)
        stack.stale = "has commits main does not; nothing should commit there"

        result = self._invoke(tmp_path)

        assert result.exit_code == 1
        assert "vibe " not in result.output
        assert f"{branch} has commits main does not" in result.output
        assert f"{TEST_COMMANDS.cli} gc" in result.output
        assert stack.removed_branches == []

    def test_the_slot_and_its_config_go_when_the_session_ends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        branch = _plan_pid(monkeypatch)
        staged = VmRunner(tmp_path, environ={}, config=batch_config).named_config_dir(
            branch
        )
        staged.mkdir(parents=True)

        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert stack.removed_branches == [branch]
        assert not staged.exists()

    def test_a_worktree_the_session_left_dirty_is_kept_and_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path, dirty=(f"plan-{PLAN_PID}",))
        branch = _plan_pid(monkeypatch)

        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert stack.removed_branches == []
        assert f"{branch} is not safe to remove" in result.output
        assert f"{TEST_COMMANDS.cli} gc" in result.output

    def test_the_slot_goes_even_when_the_vm_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        branch = _plan_pid(monkeypatch)
        staged = VmRunner(tmp_path, environ={}, config=batch_config).named_config_dir(
            branch
        )

        def failed(
            _command: Sequence[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1)

        monkeypatch.setattr("batch.cli.subprocess.run", failed)

        result = CliRunner().invoke(
            cli, ["plan", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 1
        assert stack.removed_branches == [branch]
        assert not staged.exists()

    def test_a_second_planning_session_takes_a_slot_of_its_own(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is shared between two sessions now, so nothing has to lock."""
        stack = _planning_stack(monkeypatch, tmp_path)
        outer = _plan_pid(monkeypatch)
        inner: list[Result] = []

        def spawn_a_second_session(
            _command: Sequence[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            monkeypatch.setattr("batch.cli.os.getpid", lambda: PLAN_PID + 1)
            inner.append(
                CliRunner().invoke(
                    cli, ["plan", "1769", "--run-root", str(tmp_path), "--dry-run"]
                )
            )
            return subprocess.CompletedProcess([], 0)

        monkeypatch.setattr("batch.cli.subprocess.run", spawn_a_second_session)

        def unstaged(_self: VmRunner, _config_dir: Path) -> None:
            return None

        monkeypatch.setattr("batch.cli.VmRunner.write_config", unstaged)

        result = CliRunner().invoke(
            cli, ["plan", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert inner[0].exit_code == 0, inner[0].output
        assert stack.currented == [(outer, "main"), (f"plan-{PLAN_PID + 1}", "main")]
        assert f"plan-{PLAN_PID + 1}" in inner[0].output


class TestAgentVerbs:
    @pytest.fixture(autouse=True)
    def _wrapper_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every verb here prints a remedy, and a remedy names the wrapper."""
        _ = config_at(monkeypatch, tmp_path / "repo")

    def test_next_issue_ends_by_teaching_the_verb_that_closes_the_iteration(
        self,
    ) -> None:
        state = FakeState(batch_issue(10, BatchLabel.QUEUED, title="Stack manager"))

        result = CliRunner().invoke(
            cli, ["agent", "next-issue", str(EPIC)], obj=PlanningAgent(state)
        )

        assert result.exit_code == 0, result.output
        assert result.output.rstrip().endswith(
            f"{TEST_COMMANDS.cli} agent plan-written 10 {EPIC}"
        )

    def test_next_issue_falls_back_when_the_config_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = no_config_at(monkeypatch, tmp_path / "bare")
        state = FakeState(batch_issue(10, BatchLabel.QUEUED, title="Stack manager"))

        result = CliRunner().invoke(
            cli, ["agent", "next-issue", str(EPIC)], obj=PlanningAgent(state)
        )

        assert result.exit_code == 0, result.output
        assert result.output.rstrip().endswith(f"\nbatch agent plan-written 10 {EPIC}")
        assert "dev/batch" not in result.output

    def test_an_empty_queue_exits_apart_from_every_other_failure(self) -> None:
        """The driver session ends the batch on this code, so a token or label
        error must not share it."""
        state = FakeState(batch_issue(10, BatchLabel.PLANNED))

        result = CliRunner().invoke(
            cli, ["agent", "next-issue", str(EPIC)], obj=PlanningAgent(state)
        )

        assert result.exit_code == EMPTY_QUEUE_EXIT
        assert f"Nothing queued under #{EPIC}." in result.output

    def test_an_empty_queue_still_names_the_closed_but_labeled_child(self) -> None:
        state = FakeState(
            batch_issue(10, BatchLabel.PLANNED),
            dropped=(closed_child(1503, BatchLabel.QUEUED),),
        )

        result = CliRunner().invoke(
            cli, ["agent", "next-issue", str(EPIC)], obj=PlanningAgent(state)
        )

        assert result.exit_code == EMPTY_QUEUE_EXIT
        assert result.output == (
            f"Nothing queued under #{EPIC}.\n"
            "  #1503 is closed but labeled queued — reopen it or `bin/acme skip` it\n"
        )

    def test_a_refused_claim_exits_non_zero_saying_what_is_missing(self) -> None:
        state = FakeState(batch_issue(10, BatchLabel.QUEUED))

        result = CliRunner().invoke(
            cli, ["agent", "plan-written", "10", str(EPIC)], obj=PlanningAgent(state)
        )

        assert result.exit_code == 1
        assert "Write the plan to #10 before marking it done" in result.output
        assert state.transitions == []


@dataclass(frozen=True)
class Built:
    state: FakeState
    runner: FakeRunner
    verifier: FakeVerifier
    roots: list[Path]
    stacks: list[FakeStack]
    configs: list[BatchConfig]


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *issues: BatchIssue, polls: int = 1
) -> Built:
    """Let `run` build its own orchestrator, over fakes instead of the real world."""
    built = Built(
        FakeState(*issues),
        FakeRunner(tmp_path, polls={i.number: polls for i in issues}),
        FakeVerifier(),
        [],
        [],
        [],
    )

    def unused(*_args: object, **_kwargs: object) -> None:
        return None

    def state_over_nothing(_client: object) -> FakeState:
        return built.state

    def stack_at(_path: object, *, seed_image: Path) -> FakeStack:
        stack = FakeStack(tmp_path, seed_image=seed_image)
        built.stacks.append(stack)
        return stack

    def runner_at(root: Path, *, config: Callable[[], BatchConfig]) -> FakeRunner:
        built.roots.append(root)
        built.configs.append(config())
        return built.runner

    def verifier_over_nothing(_client: object) -> FakeVerifier:
        return built.verifier

    for name in ("BatchGitHub", "GitHubGraphQL", "GitHubTransport", "repo"):
        monkeypatch.setattr(f"batch.cli.{name}", unused)
    _ = config_at(monkeypatch, tmp_path)
    monkeypatch.setattr("batch.cli.BatchState", state_over_nothing)
    monkeypatch.setattr("batch.cli.StackManager", stack_at)
    monkeypatch.setattr("batch.cli.VmRunner", runner_at)
    monkeypatch.setattr("batch.cli.Verifier", verifier_over_nothing)
    return built


class TestRunConstruction:
    """The `obj=` tests bypass construction, so the options need their own."""

    def test_the_model_reaches_the_agent_it_launches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10))

        result = CliRunner().invoke(
            cli,
            [
                "run",
                str(EPIC),
                "--run-root",
                str(tmp_path),
                "--poll-interval",
                "0",
                "--model",
                "opus",
            ],
        )

        assert result.exit_code == 0, result.output
        expected = (
            "tools/drive 10 'Do not add new tests. The existing test suite and "
            "lint must stay green.' --headless --base main --model opus"
        )
        assert built.runner.agents() == [expected]

    def test_the_verify_wait_reaches_the_verifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10))

        result = CliRunner().invoke(
            cli,
            [
                "run",
                str(EPIC),
                "--run-root",
                str(tmp_path),
                "--poll-interval",
                "0",
                "--verify-wait",
                "600",
            ],
        )

        assert result.exit_code == 0, result.output
        assert built.verifier.waits == [timedelta(seconds=600.0)]

    def test_the_timeout_reaches_the_poll_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10), polls=1000)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path), "--timeout", "0"],
        )

        assert result.exit_code == 1
        assert "timed-out" in result.output
        assert built.verifier.asked == []

    def test_the_run_root_reaches_the_vm_runner_and_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path)

        result = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert built.roots == [tmp_path]
        assert (tmp_path / "run.lock").exists()


class TestSkipAndRelaunch:
    @pytest.fixture(autouse=True)
    def _wrapper_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every verb here prints a remedy, and a remedy names the wrapper."""
        _ = config_at(monkeypatch, tmp_path / "repo")

    def _recovery(self, *issues: BatchIssue, live: tuple[int, ...] = ()) -> Recovery:
        return Recovery(FakeState(*issues), FakeVms(live))

    def test_skip_reports_the_dropped_issue(self) -> None:
        recovery = self._recovery(batch_issue(10, BatchLabel.STUCK))

        result = CliRunner().invoke(cli, ["skip", "10"], obj=recovery)

        assert result.exit_code == 0, result.output
        assert "#10 skipped (was stuck)" in result.output

    def test_skip_exits_nonzero_when_refused(self) -> None:
        recovery = self._recovery(batch_issue(10, BatchLabel.READY_FOR_REVIEW))

        result = CliRunner().invoke(cli, ["skip", "10"], obj=recovery)

        assert result.exit_code == 1
        assert "Cannot skip #10: it is ready-for-review" in result.output

    def test_relaunch_reports_the_issue_it_re_planned(self) -> None:
        recovery = self._recovery(batch_issue(10, BatchLabel.STUCK))

        result = CliRunner().invoke(cli, ["relaunch", "10"], obj=recovery)

        assert result.exit_code == 0, result.output
        assert "#10 planned (was stuck)" in result.output

    def test_relaunch_exits_nonzero_while_the_vm_is_live(self) -> None:
        recovery = self._recovery(batch_issue(10, BatchLabel.STUCK), live=(10,))

        result = CliRunner().invoke(cli, ["relaunch", "10"], obj=recovery)

        assert result.exit_code == 1
        assert "Cannot relaunch #10: its VM is still running" in result.output

    def test_an_issue_outside_the_batch_is_refused(self) -> None:
        result = CliRunner().invoke(cli, ["skip", "10"], obj=self._recovery())

        assert result.exit_code == 1
        assert "Cannot skip #10: it carries no batch label" in result.output

    def test_skip_clears_the_label_of_a_hand_closed_issue(self) -> None:
        state = FakeState(batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        state.close(10)

        result = CliRunner().invoke(cli, ["skip", "10"], obj=Recovery(state, FakeVms()))

        assert result.exit_code == 0, result.output
        assert "#10 skipped (was ready-for-review)" in result.output
        assert state.closed == []

    def test_skip_leaves_a_merged_issue_to_cleanup(self) -> None:
        state = FakeState(batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        state.close(10, merged=True)

        result = CliRunner().invoke(cli, ["skip", "10"], obj=Recovery(state, FakeVms()))

        assert result.exit_code == 1
        assert "Cannot skip #10: it merged — run `bin/acme cleanup <target>`" in (
            result.output
        )
        assert [issue.number for issue in state.closed] == [10]


class TestReworkCommand:
    def _verbs(self, tmp_path: Path, *issues: BatchIssue) -> Verbs:
        state = FakeState(*issues)
        runner = FakeRunner(tmp_path, polls={issue.number: 0 for issue in issues})
        return Verbs(
            (EPIC,),
            state,
            FakeStack(tmp_path),
            runner,
            Recovery(state, runner),
            config=batch_config(),
        )

    def test_it_reports_the_vm_it_launched(self, tmp_path: Path) -> None:
        verbs = self._verbs(tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW))

        result = CliRunner().invoke(cli, ["rework", "10", str(EPIC)], obj=verbs)

        assert result.exit_code == 0, result.output
        assert "#10 reworking (was ready-for-review)" in result.output

    def test_it_exits_nonzero_when_refused(self, tmp_path: Path) -> None:
        verbs = self._verbs(tmp_path, batch_issue(10, BatchLabel.PLANNED))

        result = CliRunner().invoke(cli, ["rework", "10", str(EPIC)], obj=verbs)

        assert result.exit_code == 1
        assert "Cannot rework #10: it is planned and has no branch yet" in result.output


class TestReworkConstruction:
    """The `obj=` tests bypass construction, so the run root needs its own."""

    def test_the_run_root_reaches_the_vm_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(
            monkeypatch, tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW), polls=0
        )

        result = CliRunner().invoke(
            cli, ["rework", "10", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert built.roots == [tmp_path]
        assert [stack.seed_image for stack in built.stacks] == [Path(TEST_SEED)]


class TestRecoveryConstruction:
    """The `obj=` tests bypass construction, so the run root needs its own."""

    def test_the_run_root_reaches_the_vm_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10, BatchLabel.STUCK), polls=0)

        result = CliRunner().invoke(
            cli, ["relaunch", "10", "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert built.roots == [tmp_path]
        assert built.state.states()[10] is BatchLabel.PLANNED


def _teardown(
    state: FakeState,
    root: Path,
    *,
    merged: tuple[int, ...] = (),
    dirty: tuple[str, ...] = (),
) -> Teardown:
    return Teardown(
        state,
        FakeStack(root, dirty=dirty),
        FakeRunner(root, polls=dict.fromkeys(merged, 0)),
        FakeVerifier(merged=merged),
    )


class TestCleanup:
    def test_reports_an_outcome_per_candidate(self, tmp_path: Path) -> None:
        state = FakeState(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.READY_FOR_REVIEW),
            batch_issue(12, BatchLabel.READY_FOR_REVIEW),
        )
        for number in (10, 11, 12):
            state.close(number, merged=number in (10, 11))

        result = CliRunner().invoke(
            cli,
            ["cleanup", str(EPIC)],
            obj=_teardown(state, tmp_path, merged=(10, 11), dirty=("issue-11",)),
        )

        assert result.exit_code == 0
        assert "#10 cleaned up" in result.output
        assert "#11 left alone (dirty-worktree)" in result.output
        assert "#12 left alone (not-merged)" in result.output

    def test_every_target_named_is_cleaned(self, tmp_path: Path) -> None:
        state = FakeState(batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        state.close(10, merged=True)

        result = CliRunner().invoke(
            cli,
            ["cleanup", str(EPIC), "1769"],
            obj=_teardown(state, tmp_path, merged=(10,)),
        )

        assert result.exit_code == 0, result.output
        assert state.swept == [EPIC, 1769]
        assert "#10 cleaned up" in result.output

    def test_nothing_to_clean_says_so(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli, ["cleanup", str(EPIC)], obj=_teardown(FakeState(), tmp_path)
        )

        assert result.exit_code == 0
        assert "Nothing to clean up under #1492." in result.output


def unoccupied(monkeypatch: pytest.MonkeyPatch) -> None:
    def nobody(_root: Path) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr("batch.reclaim.occupied_slots", nobody)


def _reclaimer(
    root: Path,
    *slots: str,
    unmerged: tuple[str, ...] = (),
    dirty: tuple[str, ...] = (),
    live: tuple[str, ...] = (),
) -> Reclaimer:
    return Reclaimer(
        FakeStack(root, slots=slots, unmerged=unmerged, dirty=dirty),
        FakeRunner(root, live_branches=live),
        occupied=frozenset,
    )


class TestGc:
    def test_it_reports_one_line_per_slot_keyed_by_branch(self, tmp_path: Path) -> None:
        journal: list[str] = []
        stack = FakeStack(
            tmp_path,
            slots=("issue-9", "openfix", "split"),
            unmerged=("openfix",),
            dirty=("split",),
            journal=journal,
        )

        result = CliRunner().invoke(
            cli,
            ["gc"],
            obj=Reclaimer(stack, FakeRunner(tmp_path), occupied=frozenset),
        )

        assert result.exit_code == 0, result.output
        assert journal == ["remove issue-9"]
        assert stack.bases == ["main", "main", "main"]
        assert result.output.splitlines() == [
            "issue-9 cleaned up",
            "openfix left alone (not-merged)",
            "split left alone (dirty-worktree)",
        ]

    def test_an_empty_scan_says_there_is_nothing_to_reclaim(
        self, tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(cli, ["gc"], obj=_reclaimer(tmp_path))

        assert result.exit_code == 0, result.output
        assert result.output == "Nothing to reclaim.\n"

    def test_a_dry_run_says_what_would_go_and_removes_nothing(
        self, tmp_path: Path
    ) -> None:
        stack = FakeStack(tmp_path, slots=("issue-9", "openfix"), unmerged=("openfix",))

        result = CliRunner().invoke(
            cli,
            ["gc", "--dry-run"],
            obj=Reclaimer(stack, FakeRunner(tmp_path), occupied=frozenset),
        )

        assert result.exit_code == 0, result.output
        assert result.output.splitlines() == [
            "issue-9 would be cleaned up",
            "openfix left alone (not-merged)",
        ]
        assert stack.removed_branches == []

    def test_the_run_root_reaches_the_vm_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(monkeypatch, tmp_path)
        unoccupied(monkeypatch)

        result = CliRunner().invoke(cli, ["gc", "--run-root", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert built.roots == [tmp_path]
        assert result.output == "Nothing to reclaim.\n"

    def test_a_live_slot_is_named_as_such(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli, ["gc"], obj=_reclaimer(tmp_path, "issue-9", live=("issue-9",))
        )

        assert result.exit_code == 0, result.output
        assert result.output == "issue-9 left alone (vm-live)\n"

    def test_a_failing_probe_refuses_the_sweep_instead_of_reporting_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack = FakeStack(tmp_path, slots=("issue-9",))

        def refuse(_root: Path) -> frozenset[str]:
            raise OccupancyError("lsof exited 1: no such file")

        monkeypatch.setattr("batch.reclaim.occupied_slots", refuse)

        result = CliRunner().invoke(
            cli, ["gc"], obj=Reclaimer(stack, FakeRunner(tmp_path))
        )

        assert result.exit_code != 0
        assert "lsof exited 1" in result.output
        assert stack.removed_branches == []

    def test_a_slot_someone_is_standing_in_survives_a_dry_run(
        self, tmp_path: Path
    ) -> None:
        stack = FakeStack(tmp_path, slots=("issue-9",))

        result = CliRunner().invoke(
            cli,
            ["gc", "--dry-run"],
            obj=Reclaimer(
                stack, FakeRunner(tmp_path), occupied=lambda: frozenset({"issue-9"})
            ),
        )

        assert result.exit_code == 0, result.output
        assert result.output == "issue-9 left alone (occupied)\n"
        assert stack.removed_branches == []


class TestCleanupConstruction:
    """The `obj=` tests bypass construction, so the run root needs its own."""

    def test_the_run_root_reaches_the_vm_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built = _wire(
            monkeypatch, tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW), polls=0
        )
        built.state.close(10)

        result = CliRunner().invoke(
            cli, ["cleanup", str(EPIC), "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert built.roots == [tmp_path]
        assert "#10 left alone (not-merged)" in result.output


class TestOutsideACheckout:
    @pytest.mark.parametrize(
        ("argv", "takes_a_run_root"),
        [
            (["run", str(EPIC)], True),
            (["stack", "remove", "9"], False),
            (["cleanup", str(EPIC)], True),
            (["gc"], True),
            (["rework", "10", str(EPIC)], True),
        ],
    )
    def test_a_command_that_needs_the_repo_root_is_a_click_error(
        self,
        argv: list[str],
        takes_a_run_root: bool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outside_a_checkout(monkeypatch)
        if takes_a_run_root:
            argv = [*argv, "--run-root", str(tmp_path)]

        result = CliRunner().invoke(cli, argv)

        assert result.exit_code != 0
        assert "Not inside a git checkout" in result.output
        assert "pass --repo" in result.output
        assert "Failed to determine repository" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_a_command_that_needs_only_a_remote_reports_the_remote_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside_a_checkout(monkeypatch)

        result = CliRunner().invoke(cli, ["agent", "next-issue", str(EPIC)])

        assert result.exit_code != 0
        assert "Failed to determine repository" in result.output

    @pytest.mark.parametrize(
        "argv",
        [
            ["run", str(EPIC)],
            ["cleanup", str(EPIC)],
            ["rework", "10", str(EPIC)],
        ],
    )
    def test_a_reordered_resolver_still_reports_a_missing_remote(
        self, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")
        no_git_remote(monkeypatch)

        result = CliRunner().invoke(cli, [*argv, "--run-root", str(tmp_path / "run")])

        assert result.exit_code != 0
        assert "Failed to determine repository" in result.output

    def test_the_checkout_error_beats_a_held_run_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside_a_checkout(monkeypatch)

        with run_lock(tmp_path):
            result = CliRunner().invoke(
                cli, ["run", str(EPIC), "--run-root", str(tmp_path)]
            )

        assert result.exit_code != 0
        assert "Not inside a git checkout" in result.output
        assert "another batch run holds" not in result.output


class TestTheRepoOption:
    @pytest.fixture(name="sc")
    def scratch_repo(self, tmp_path: Path) -> Scratch:
        sc = scratch(tmp_path)
        _ = write_config(
            sc.repo,
            f'[vm]\nseed_image = "{sc.seed}"\n' + TEST_REPO_TOML + TEST_COMMANDS_TOML,
        )
        return sc

    def test_a_worktree_lets_a_stack_command_run_from_outside_a_checkout(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")
        monkeypatch.chdir(sc.root)

        result = CliRunner().invoke(
            cli,
            ["--repo", str(slot.worktree), "stack", "ensure", "10", "--base", "main"],
        )

        assert result.exit_code == 0, result.output
        assert str(sc.trees / "issue-10") in result.output

    def test_the_stack_a_command_builds_is_rooted_at_the_main_checkout(
        self, sc: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")
        monkeypatch.chdir(sc.root)
        seen: list[Path] = []

        def stack_at(repo: Path, *, seed_image: Path) -> FakeStack:
            seen.append(repo)
            return FakeStack(tmp_path, seed_image=seed_image)

        monkeypatch.setattr("batch.cli.StackManager", stack_at)

        result = CliRunner().invoke(
            cli,
            [
                "--repo",
                str(slot.worktree),
                "plan",
                str(EPIC),
                "--run-root",
                str(tmp_path),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert seen == [sc.repo.resolve()]

    def test_help_needs_no_checkout_and_no_repo(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(sc.root)

        result = CliRunner().invoke(cli, ["stack", "ensure", "--help"])

        assert result.exit_code == 0, result.output

    def test_a_repo_that_is_no_checkout_is_reported(
        self, sc: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(sc.root)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        result = CliRunner().invoke(
            cli, ["--repo", str(elsewhere), "stack", "ensure", "10", "--base", "main"]
        )

        assert result.exit_code != 0
        assert "Not inside a git checkout" in result.output

    def test_a_usage_error_from_outside_a_checkout_names_the_script(
        self,
        sc: Scratch,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(sc.root)

        with pytest.raises(SystemExit) as caught:
            main(["--repo", str(sc.repo), "stack", "ensure", "10"])

        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert err.startswith("Usage: batch stack ensure")
        assert TEST_COMMANDS.cli not in err


class TestTheDefaultRunRoot:
    def test_run_without_a_run_root_fails_instead_of_locking_the_real_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `--run-root` is deliberately omitted here.
        _ = _wire(monkeypatch, tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        home = Path.home()

        result = CliRunner().invoke(cli, ["run", str(EPIC)])

        assert result.exit_code != 0
        assert isinstance(result.exception, OSError)
        assert result.exception.errno == errno.ENOTDIR
        assert str(home) in str(result.exception)
        assert home.is_file()


class TestAttach:
    def test_attaching_to_a_dead_vm_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")
        result = CliRunner().invoke(
            cli, ["attach", "1499", "--run-root", str(tmp_path)]
        )

        assert result.exit_code != 0
        assert "No live VM" in result.output

    def test_a_live_vm_is_attached_from_its_run_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "issue-1499.sock").touch()
        seen: list[Sequence[str]] = []

        def record(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            seen.append(command)
            return CompletedProcess(list(command), 0)

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        _ = config_at(monkeypatch, tmp_path / "repo")
        result = CliRunner().invoke(
            cli, ["attach", "1499", "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0
        assert seen == [
            ("dtach", "-a", str(tmp_path / "issue-1499.sock"), "-r", "none")
        ]

    def test_a_missing_dtach_is_reported_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "issue-1499.sock").touch()

        def absent(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            raise FileNotFoundError(command)

        monkeypatch.setattr("batch.cli.subprocess.run", absent)
        _ = config_at(monkeypatch, tmp_path / "repo")
        result = CliRunner().invoke(
            cli, ["attach", "1499", "--run-root", str(tmp_path)]
        )

        assert result.exit_code != 0
        assert "dtach is not installed" in result.output

    def _attached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Result, list[Sequence[str]]]:
        (tmp_path / "issue-1499.sock").touch()
        seen: list[Sequence[str]] = []

        def record(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            seen.append(command)
            return CompletedProcess(list(command), 0)

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        return (
            CliRunner().invoke(cli, ["attach", "1499", "--run-root", str(tmp_path)]),
            seen,
        )

    def test_a_live_vm_is_attached_without_a_batch_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = no_config_at(monkeypatch, tmp_path / "repo")

        result, seen = self._attached(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        assert seen == [
            ("dtach", "-a", str(tmp_path / "issue-1499.sock"), "-r", "none")
        ]

    def test_a_live_vm_is_attached_despite_a_malformed_batch_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo", 'seed_image = 7\nslug = "x"\n')

        result, seen = self._attached(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        assert seen == [
            ("dtach", "-a", str(tmp_path / "issue-1499.sock"), "-r", "none")
        ]

    def test_a_live_vm_is_attached_from_outside_a_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside_a_checkout(monkeypatch)

        result, seen = self._attached(tmp_path, monkeypatch)

        assert result.exit_code == 0, result.output
        assert seen == [
            ("dtach", "-a", str(tmp_path / "issue-1499.sock"), "-r", "none")
        ]

    def test_the_vm_subgroup_no_longer_owns_attach(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            ["vm", "attach", "1499"],
            obj=VmRunner(tmp_path, environ={}, config=batch_config),
        )

        assert result.exit_code != 0
        assert "No such command" in result.output


@dataclass(frozen=True)
class Rendered:
    calls: list[tuple[tuple[int, ...], object]]
    verbs: list[object]
    progs: list[str]


class TestRunSurface:
    def _dashboard(
        self, monkeypatch: pytest.MonkeyPatch, result: RunResult | None
    ) -> Rendered:
        rendered = Rendered(calls=[], verbs=[], progs=[])

        def fake(
            targets: Sequence[int],
            orchestrator: object,
            _narration: Sequence[str] = (),
            _drive: Callable[[], RunResult] | None = None,
            verbs: object = None,
            *,
            prog: str,
        ) -> RunResult | None:
            rendered.calls.append((tuple(targets), orchestrator))
            rendered.verbs.append(verbs)
            rendered.progs.append(prog)
            return result

        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)
        return rendered

    def test_a_terminal_gets_the_dashboard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        core = _orchestrator(FakeState(batch_issue(10)), tmp_path)
        seen = self._dashboard(monkeypatch, RunResult(targets=(EPIC,), outcomes=()))

        result = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path)], obj=core
        )

        assert result.exit_code == 0
        assert seen.calls == [((EPIC,), core)]

    def test_the_dashboard_is_told_the_configured_wrapper_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")
        core = _orchestrator(FakeState(batch_issue(10)), tmp_path)
        seen = self._dashboard(monkeypatch, RunResult(targets=(EPIC,), outcomes=()))

        result = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path)], obj=core
        )

        assert result.exit_code == 0
        assert seen.progs == [TEST_COMMANDS.cli]

    def test_the_dashboard_gets_verbs_over_the_running_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(batch_issue(10, BatchLabel.STUCK))
        core = _orchestrator(state, tmp_path, polls={10: 0})
        seen = self._dashboard(monkeypatch, RunResult(targets=(EPIC,), outcomes=()))

        _ = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path)], obj=core
        )

        verbs = seen.verbs[0]
        assert isinstance(verbs, Verbs)
        assert verbs.relaunch(10).refusal is None
        assert state.states()[10] is BatchLabel.PLANNED

    def test_cli_mode_streams_instead_of_rendering(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._dashboard(monkeypatch, None)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--cli", "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert seen.calls == []
        assert "#10 ready-for-review on main PR #110" in result.output

    def test_a_pipe_falls_back_to_streaming_without_the_flag(
        self, tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert "#10 ready-for-review on main PR #110" in result.output

    def test_a_halt_under_the_dashboard_still_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        halted = RunResult(
            targets=(EPIC,),
            outcomes=(
                IssueOutcome(
                    number=10,
                    base=MAIN,
                    state=BatchLabel.STUCK,
                    halt=HaltReason.VERIFICATION_FAILED,
                ),
            ),
        )
        _ = self._dashboard(monkeypatch, halted)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 1
        assert "#10 stuck (verification-failed) on main" in result.output
        assert "Batch halted at #10." in result.output

    def test_quitting_before_the_batch_finishes_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = self._dashboard(monkeypatch, None)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert "left running" in result.output

    def test_the_lock_is_held_for_as_long_as_the_dashboard_is_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        held: list[bool] = []

        def fake(
            _targets: Sequence[int],
            _orchestrator: object,
            _narration: Sequence[str] = (),
            _drive: Callable[[], RunResult] | None = None,
            _verbs: object = None,
            **_extra: object,
        ) -> RunResult:
            try:
                with run_lock(tmp_path):
                    held.append(False)
            except BatchInProgressError:
                held.append(True)
            return RunResult(targets=(EPIC,), outcomes=())

        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)
        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert held == [True]

    def test_a_second_run_is_refused_while_one_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = self._dashboard(monkeypatch, RunResult(targets=(EPIC,), outcomes=()))

        with run_lock(tmp_path):
            result = CliRunner().invoke(
                cli,
                ["run", str(EPIC), "--run-root", str(tmp_path)],
                obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
            )

        assert result.exit_code == 1
        assert "another batch run holds" in result.output


class TestNarration:
    def test_the_dashboard_receives_the_orchestrators_progress_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _wire(monkeypatch, tmp_path, batch_issue(10))
        captured: list[Sequence[str]] = []

        def fake(
            _epic_number: int,
            _orchestrator: Orchestrator,
            narration: Sequence[str] = (),
            drive: Callable[[], RunResult] | None = None,
            _verbs: object = None,
            **_extra: object,
        ) -> RunResult:
            captured.append(narration)
            assert drive is not None
            return drive()

        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)
        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path), "--poll-interval", "0"],
        )

        assert result.exit_code == 0, result.output
        assert captured and "#10 ready for review" in captured[0]
        assert "#10 ready for review" not in result.output

    def test_the_wait_is_narrated_rather_than_written_over_the_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = FakeState(queued_targets=(EPIC,))
        narrated: list[Sequence[str]] = []

        def fake(
            _targets: Sequence[int],
            _orchestrator: object,
            narration: Sequence[str] = (),
            drive: Callable[[], RunResult] | None = None,
            _verbs: object = None,
            **_extra: object,
        ) -> RunResult:
            narrated.append(narration)
            assert drive is not None
            return drive()

        def sleeper(_seconds: float) -> None:
            state.queued_targets = ()

        monkeypatch.setattr("batch.cli.sleep", sleeper)
        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(state, tmp_path),
        )

        waiting = f"Waiting for queued issues under #{EPIC}."
        assert result.exit_code == 0, result.output
        assert narrated and waiting in narrated[0]
        assert waiting not in result.output


class TestWatchUnderTheDashboard:
    def test_the_dashboard_drives_the_watch_and_reports_each_pass_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake(
            _targets: Sequence[int],
            _orchestrator: object,
            _narration: Sequence[str] = (),
            drive: Callable[[], RunResult] | None = None,
            _verbs: object = None,
            **_extra: object,
        ) -> RunResult:
            assert drive is not None
            return drive()

        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert result.output.count("#10 ready-for-review on main PR #110") == 1


class TestDebug:
    def _roots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *absent: str
    ) -> tuple[FakeStack, Path]:
        return (
            _planning_stack(monkeypatch, tmp_path / "trees", absent=absent),
            tmp_path / "run",
        )

    def _invoke(self, run: Path, *extra: str) -> Result:
        return CliRunner().invoke(
            cli, ["debug", "1597", "--run-root", str(run), *extra]
        )

    def _journalled(
        self, monkeypatch: pytest.MonkeyPatch, journal: list[str], code: int = 0
    ) -> None:
        def record(
            command: Sequence[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == PS[0]:
                return subprocess.CompletedProcess([], 0, stdout="")
            journal.append(f"ran {command[0]} {command[1]}")
            return subprocess.CompletedProcess([], code)

        def staged(_self: VmRunner, config_dir: Path) -> None:
            journal.append(f"staged {config_dir}")

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        monkeypatch.setattr("batch.cli.VmRunner.write_config", staged)

    def test_derives_every_path_from_the_issue_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--dry-run")

        assert result.exit_code == 0, result.output
        assert stack.found == [("issue-1597", MAIN)]
        assert "--send 'cd widgets/worktrees/issue-1597'" in result.output
        assert str(stack.worktree_root / "issue-1597.raw") in result.output
        assert f"{run / 'issue-1597.config'}:/mnt/claude-config" in result.output

    def test_looks_the_slot_up_without_creating_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--dry-run")

        assert result.exit_code == 0, result.output
        assert (stack.ensured, stack.branches, stack.currented) == ([], [], [])

    def test_resumes_the_issues_own_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--dry-run")

        assert result.exit_code == 0, result.output
        assert "tools/drive" not in result.output
        flags = "--allow-dangerously-skip-permissions --dangerously-skip-permissions"
        helper = f"tools/session 1597 --debug -- {flags}"
        assert f"--send 'tools/prepare && {helper}'" in result.output

    def test_fresh_boots_a_bare_interactive_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--fresh", "--dry-run")

        assert result.exit_code == 0, result.output
        assert "tools/session" not in result.output
        flags = "--allow-dangerously-skip-permissions --dangerously-skip-permissions"
        assert f"--send 'tools/prepare && claude {flags}'" in result.output

    def test_the_boot_is_detached_so_the_socket_guards_the_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)
        socket = run / "issue-1597.sock"

        result = self._invoke(run, "--dry-run")

        boot, attach = result.output.splitlines()
        assert boot.startswith(f"dtach -n {socket} vibe ")
        assert attach == f"dtach -a {socket} -r none"
        assert "tee" not in boot

    def test_vibe_runs_from_the_mount_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)
        seen: list[Path | None] = []

        def record(
            command: Sequence[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[0] == PS[0]:
                return subprocess.CompletedProcess([], 0, stdout="")
            seen.append(cast("Path | None", kwargs.get("cwd")))
            return subprocess.CompletedProcess([], 0)

        def staged(_self: VmRunner, _config_dir: Path) -> None:
            return None

        monkeypatch.setattr("batch.cli.subprocess.run", record)
        monkeypatch.setattr("batch.cli.VmRunner.write_config", staged)

        result = self._invoke(run)

        assert result.exit_code == 0, result.output
        assert seen[0] == tmp_path / "trees"

    def test_a_missing_slot_aborts_and_boots_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, run = self._roots(tmp_path, monkeypatch, "issue-1597")
        journal: list[str] = []
        self._journalled(monkeypatch, journal)

        result = self._invoke(run)

        assert result.exit_code == 1
        assert journal == []
        assert "No slot for issue-1597" in result.output
        assert str(stack.worktree_root / "issue-1597.raw") in result.output

    def test_a_missing_slot_is_worded_the_way_the_dashboard_words_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stack, run = self._roots(tmp_path, monkeypatch, "issue-1597")

        result = self._invoke(run)

        refusal = DebugEntry(
            number=1597,
            refusal=DebugRefusal.NO_SLOT,
            missing=stack.missing("issue-1597"),
        )
        assert debug_line(refusal) in result.output

    def test_a_boot_that_fails_is_reported_and_attaches_to_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)
        journal: list[str] = []
        self._journalled(monkeypatch, journal, code=3)

        result = self._invoke(run)

        assert result.exit_code == 1
        assert journal == [f"staged {run / 'issue-1597.config'}", "ran dtach -n"]
        assert (
            debug_line(DebugEntry(number=1597, refusal=DebugRefusal.BOOT_FAILED))
            in result.output
        )

    def test_a_running_vm_is_attached_to_rather_than_booted_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)
        socket = run / "issue-1597.sock"
        socket.parent.mkdir(parents=True)
        _ = socket.write_text("")

        result = self._invoke(run, "--dry-run")

        assert result.exit_code == 0, result.output
        assert "vibe " not in result.output
        assert "already running; attaching" in result.output
        assert result.output.splitlines()[-1] == f"dtach -a {socket} -r none"

    def test_a_real_boot_stages_the_config_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)
        journal: list[str] = []
        self._journalled(monkeypatch, journal)

        result = self._invoke(run)

        assert result.exit_code == 0, result.output
        assert journal == [
            f"staged {run / 'issue-1597.config'}",
            "ran dtach -n",
            "ran dtach -a",
        ]

    def test_a_dry_run_stages_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--dry-run")

        assert result.exit_code == 0, result.output
        assert not (run / "issue-1597.config").exists()

    def test_the_model_and_ram_reach_the_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, run = self._roots(tmp_path, monkeypatch)

        result = self._invoke(run, "--dry-run", "--model", "opus", "--ram", "9001")

        assert result.exit_code == 0, result.output
        assert "vibe --ram 9001" in result.output
        assert "tools/session 1597 --debug -- --model opus" in result.output


class TestRunStaysAwake:
    def _events(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        events: list[str] = []

        @contextmanager
        def fake_awake(
            report: Callable[[str], None], **_kwargs: object
        ) -> Generator[None]:
            events.append("enter")
            report("caffeinate did not start")
            try:
                yield
            finally:
                events.append("exit")
                report("caffeinate did not exit")

        def fake_run(_self: Orchestrator, targets: Sequence[int]) -> RunResult:
            events.append("drive")
            return RunResult(targets=tuple(targets), outcomes=())

        monkeypatch.setattr("batch.cli.awake", fake_awake)
        monkeypatch.setattr(Orchestrator, "run", fake_run)
        return events

    def _rendering(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        seen: list[str] = []

        def fake(
            _targets: Sequence[int],
            _orchestrator: object,
            narration: Sequence[str] = (),
            drive: Callable[[], RunResult] | None = None,
            _verbs: object = None,
            **_extra: object,
        ) -> RunResult | None:
            assert drive is not None
            result = drive()
            seen.extend(narration)
            return result

        monkeypatch.setattr("batch.cli.run_dashboard", fake)
        monkeypatch.setattr("batch.cli._interactive", lambda: True)
        return seen

    def test_the_dashboard_drive_is_wrapped_in_the_assertion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._events(monkeypatch)
        rendered = self._rendering(monkeypatch)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert events == ["enter", "drive", "exit"]
        assert "caffeinate did not start" in rendered
        assert "caffeinate did not exit" in result.output

    def test_the_cli_drive_is_wrapped_in_the_assertion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._events(monkeypatch)

        result = CliRunner().invoke(
            cli,
            ["run", str(EPIC), "--cli", "--run-root", str(tmp_path)],
            obj=_orchestrator(FakeState(batch_issue(10)), tmp_path),
        )

        assert result.exit_code == 0
        assert events == ["enter", "drive", "exit"]


class TestClosedSkips:
    def test_a_mix_of_skips_prints_the_closed_count_last(self) -> None:
        fake = transport(
            children(
                child(1),
                child(2, labels=["planned"]),
                child(3, state="CLOSED"),
                child(4, state="CLOSED"),
            ),
            label_ids(),
            {},
        )

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            "Queued #1",
            "Skipped #2 (already planned)",
            "Skipped 2 closed issues.",
        ]

    def test_a_single_closed_child_reads_singular(self) -> None:
        fake = transport(children(child(1), child(2, state="CLOSED")), label_ids(), {})

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            "Queued #1",
            "Skipped 1 closed issue.",
        ]

    def test_all_closed_children_still_print_the_nothing_line(self) -> None:
        fake = transport(
            children(child(1, state="CLOSED"), child(2, state="CLOSED")),
        )

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            "Nothing to queue.",
            "Skipped 2 closed issues.",
        ]

    def test_approve_puts_the_count_before_the_guidance_refusal(self) -> None:
        fake = transport(
            children(
                child(1, labels=["queued"], body="## Test Plan\n\n1. A test"),
                child(2, labels=["implementing"]),
                child(3, state="CLOSED"),
            ),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli,
            ["approve", "--epic", str(EPIC), "--guidance", "No new tests."],
            obj=state_over(fake),
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            "Approved #1",
            "Skipped #2 (already implementing)",
            "Skipped 1 closed issue.",
            "#1 already has a Test Plan; guidance not written",
        ]

    def test_the_count_line_goes_to_stderr(self) -> None:
        fake = transport(children(child(1), child(2, state="CLOSED")), label_ids(), {})

        result = CliRunner().invoke(
            cli, ["queue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.stdout.splitlines() == [EPIC_LINE, "Queued #1"]
        assert result.stderr.splitlines() == ["Skipped 1 closed issue."]

    @pytest.mark.parametrize(
        ("verb", "labels", "done"),
        [
            ("unqueue", ["queued"], "Unqueued #1"),
            ("fast-track", [], "Approved #1"),
        ],
    )
    def test_the_other_two_verbs_collapse_the_same_way(
        self, verb: str, labels: list[str], done: str
    ) -> None:
        fake = transport(
            children(child(1, labels=labels), child(2, state="CLOSED")),
            label_ids(),
            {},
            {},
        )

        result = CliRunner().invoke(
            cli, [verb, "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert result.output.splitlines() == [
            EPIC_LINE,
            done,
            "Skipped 1 closed issue.",
        ]


class TestBatchToml:
    def test_the_seed_image_comes_from_the_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sc: Scratch
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")
        seeds: list[Path] = []

        def record(_repo: Path, *, seed_image: Path) -> StackManager:
            seeds.append(seed_image)
            return StackManager(sc.repo, seed_image=sc.seed)

        monkeypatch.setattr("batch.cli.StackManager", record)
        unoccupied(monkeypatch)

        result = CliRunner().invoke(
            cli, ["gc", "--dry-run", "--run-root", str(tmp_path)]
        )

        assert result.exit_code == 0
        assert seeds == [Path(TEST_SEED)]

    def test_a_broken_config_lists_its_problems_instead_of_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo", "[vm]\nram = 8\n")

        result = CliRunner().invoke(
            cli, ["gc", "--dry-run", "--run-root", str(tmp_path)]
        )

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert 'missing required key "seed_image"' in result.output
        assert "unknown key(s) ram" in result.output

    def test_an_absent_config_is_reported_not_defaulted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _ = no_config_at(monkeypatch, tmp_path)

        result = CliRunner().invoke(
            cli, ["gc", "--dry-run", "--run-root", str(tmp_path)]
        )

        assert result.exit_code != 0
        assert "batch.toml" in result.output
        assert "does not exist" in result.output

    def test_the_seed_image_reaches_the_planning_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stack = _planning_stack(monkeypatch, tmp_path)
        _ = _plan_pid(monkeypatch)

        result = CliRunner().invoke(
            cli,
            ["plan", str(EPIC), "--run-root", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert stack.seed_image == Path(TEST_SEED)

    def test_the_seed_image_reaches_the_stack_a_run_builds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10))

        result = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path), "--poll-interval", "0"]
        )

        assert result.exit_code == 0, result.output
        assert [stack.seed_image for stack in built.stacks] == [Path(TEST_SEED)]

    def test_the_slug_reaches_the_runner_a_run_builds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        built = _wire(monkeypatch, tmp_path, batch_issue(10))

        result = CliRunner().invoke(
            cli, ["run", str(EPIC), "--run-root", str(tmp_path), "--poll-interval", "0"]
        )

        assert result.exit_code == 0, result.output
        assert [config.slug for config in built.configs] == [TEST_SLUG]

    def test_the_slug_reaches_the_sends_of_a_vm_the_cli_builds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No obj=, so the vm group builds the real runner.
        _ = config_at(monkeypatch, tmp_path / "repo")

        result = CliRunner().invoke(
            cli,
            [
                "vm",
                "--run-root",
                str(tmp_path),
                "console",
                "--worktree",
                "issue-1499",
                "--disk",
                str(tmp_path / "issue-1499.raw"),
                "--config-dir",
                str(tmp_path / "config"),
                "--issue",
                "1499",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert f"export GH_REPO={TEST_SLUG}" in result.output

    def test_a_command_that_builds_no_stack_needs_no_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The load stays where a seed image or a runner is wanted;
        # putting it in _pass_state would make every stackless command
        # need a file it never reads.
        _ = no_config_at(monkeypatch, tmp_path)
        fake = transport(children(child(1, labels=["queued"])), label_ids(), {}, {})

        result = CliRunner().invoke(
            cli, ["unqueue", "--epic", str(EPIC)], obj=state_over(fake)
        )

        assert result.exit_code == 0
        assert "Unqueued #1" in result.output


class TestKeychainFailureRendering:
    def test_a_keychain_error_prints_one_line_and_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        def _refuse(**_kwargs: object) -> None:
            raise KeychainError("acme-guest-token")

        monkeypatch.setattr("batch.cli.cli", _refuse)

        with pytest.raises(SystemExit) as caught:
            main([])

        assert caught.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: no keychain item named acme-guest-token")
        assert "Traceback" not in err

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (EmptyTokenError("acme-guest-token"), "empty password"),
            (
                WrongAccountError(
                    item="acme-guest-token", login="mallory", owner="acme"
                ),
                "mallory",
            ),
            (AccountCheckError("connection refused"), "connection refused"),
        ],
    )
    def test_a_guest_token_error_prints_one_line_and_exits_nonzero(
        self,
        error: RuntimeError,
        expected: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _ = config_at(monkeypatch, tmp_path / "repo")

        def _refuse(**_kwargs: object) -> None:
            raise error

        monkeypatch.setattr("batch.cli.cli", _refuse)

        with pytest.raises(SystemExit) as caught:
            main([])

        assert caught.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("Error: ")
        assert expected in err
        assert "Traceback" not in err
        assert err.count("\n") == 1
