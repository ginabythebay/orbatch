from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from batch.models import (
    Batch,
    BatchIssue,
    BatchLabel,
    CiStatus,
    DashboardRow,
    DroppedChild,
    HaltReason,
    IssueOutcome,
    NextIssue,
    Problem,
    ReclaimOutcome,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    RunResult,
    TeardownResult,
    TeardownSkip,
    Verdict,
    VmFacts,
)
from batch.text_output import (
    dashboard_view,
    print_batch_table,
    print_next_issue,
    print_run_result,
    print_teardown_result,
    print_verdict,
    reclaim_line,
    recovery_line,
    run_banner,
    status_line,
)
from ghgql.transport import RateLimit


def _budget_color(remaining: int) -> str:
    budget = RateLimit(cost=2, remaining=remaining, limit=5000, reset_at="later")
    line = status_line("", budget)
    return str(line.spans[0].style)


class TestStatusLine:
    def test_a_message_alone_when_no_budget_has_been_reported(self) -> None:
        assert status_line("Rework failed: 502", None).plain == "Rework failed: 502"

    def test_the_budget_rides_alongside_the_message(self) -> None:
        budget = RateLimit(cost=2, remaining=4998, limit=5000, reset_at="later")
        line = status_line("Skipped #10", budget)
        assert line.plain == "Skipped #10  ·  GraphQL 4998/5000"

    def test_the_budget_stands_alone_when_there_is_nothing_to_say(self) -> None:
        budget = RateLimit(cost=2, remaining=12, limit=5000, reset_at="later")
        assert status_line("", budget).plain == "GraphQL 12/5000"

    @pytest.mark.parametrize(
        ("remaining", "color"),
        [
            (5000, "green"),
            (3000, "green"),
            (2999, "yellow"),
            (1000, "yellow"),
            (999, "red"),
            (0, "red"),
        ],
    )
    def test_the_budget_darkens_as_it_is_spent(
        self, remaining: int, color: str
    ) -> None:
        assert _budget_color(remaining) == color

    def test_a_limit_of_zero_is_not_a_division(self) -> None:
        budget = RateLimit(cost=0, remaining=0, limit=0, reset_at="later")
        assert status_line("", budget).plain == "GraphQL 0/0"


_PROG = "bin/acme"


def _render(result: RunResult) -> str:
    out = io.StringIO()
    print_run_result(result, out, prog=_PROG)
    return out.getvalue()


def _rendered(verdict: Verdict) -> str:
    out = io.StringIO()
    print_verdict(verdict, out)
    return out.getvalue()


def _halted(verdict: Verdict) -> RunResult:
    return RunResult(
        targets=(1492,),
        outcomes=(
            IssueOutcome(
                number=verdict.issue_number,
                base=verdict.expected_bases[0],
                state=BatchLabel.STUCK,
                verdict=verdict,
                halt=HaltReason.VERIFICATION_FAILED,
            ),
        ),
    )


def test_halted_outcome_names_the_verdicts_problems() -> None:
    output = _render(
        _halted(
            Verdict(
                issue_number=1503,
                expected_bases=("main",),
                problems=(Problem.NO_PR,),
            )
        )
    )

    assert output == (
        "#1503 stuck (verification-failed) on main\n"
        "  no-pr: no PR for issue-1503\n"
        "Batch halted at #1503.\n"
    )


def test_every_problem_gets_its_own_line_in_verdict_order() -> None:
    output = _render(
        _halted(
            Verdict(
                issue_number=1503,
                expected_bases=("main",),
                pr_number=1600,
                base="develop",
                problems=(Problem.WRONG_BASE, Problem.MISSING_ISSUE_REFERENCE),
                ci=CiStatus.GREEN,
            )
        )
    )

    assert output.splitlines()[1:3] == [
        "  wrong-base: based on develop, expected main",
        "  missing-issue-reference: the PR does not close #1503",
    ]


def test_a_ci_only_failure_still_explains_itself() -> None:
    output = _render(
        _halted(
            Verdict(
                issue_number=1503,
                expected_bases=("main",),
                pr_number=1600,
                base="main",
                ci=CiStatus.FAILED,
            )
        )
    )

    assert output.splitlines()[1] == "  ci=failed"


def test_a_clean_run_stays_one_line_per_issue() -> None:
    result = RunResult(
        targets=(1492,),
        outcomes=(
            IssueOutcome(
                number=1503,
                base="main",
                state=BatchLabel.READY_FOR_REVIEW,
                verdict=Verdict(
                    issue_number=1503,
                    expected_bases=("main",),
                    pr_number=1600,
                    base="main",
                    ci=CiStatus.GREEN,
                ),
            ),
        ),
    )

    assert _render(result) == "#1503 ready-for-review on main PR #1600\n"


def test_a_halt_without_a_verdict_adds_no_detail() -> None:
    result = RunResult(
        targets=(1492,),
        outcomes=(
            IssueOutcome(
                number=1503,
                base="main",
                state=BatchLabel.IMPLEMENTING,
                halt=HaltReason.VM_ALREADY_RUNNING,
            ),
        ),
    )

    assert _render(result) == (
        "#1503 implementing (vm-already-running) on main\nBatch halted at #1503.\n"
    )


class TestWrongBaseDetail:
    def test_several_accepted_bases_are_all_named(self) -> None:
        verdict = Verdict(
            issue_number=11,
            expected_bases=("issue-10", "main"),
            pr_number=101,
            base="issue-7",
            problems=(Problem.WRONG_BASE,),
            ci=CiStatus.GREEN,
        )

        assert "based on issue-7, expected one of issue-10, main" in _rendered(verdict)

    def test_a_single_accepted_base_is_named_alone(self) -> None:
        verdict = Verdict(
            issue_number=11,
            expected_bases=("issue-10",),
            pr_number=101,
            base="main",
            problems=(Problem.WRONG_BASE,),
            ci=CiStatus.GREEN,
        )

        assert "based on main, expected issue-10" in _rendered(verdict)


def _table(batch: Batch, vm_facts: Mapping[int, VmFacts] | None = None) -> str:
    out = io.StringIO()
    print_batch_table(batch, out, vm_facts, prog=_PROG)
    return "".join(f"{line.rstrip()}\n" for line in out.getvalue().splitlines())


def _dropped(
    number: int,
    reason: str,
    *,
    state: str = "OPEN",
    labels: tuple[BatchLabel, ...] = (),
    title: str = "",
) -> DroppedChild:
    return DroppedChild(
        number=number,
        title=title or f"Issue {number}",
        state=state,
        labels=labels,
        reason=reason,
    )


def _closed(number: int, label: BatchLabel, **kwargs: str) -> DroppedChild:
    return _dropped(
        number,
        f"closed, labelled {label}",
        state="CLOSED",
        labels=(label,),
        **kwargs,
    )


def _unlabeled(number: int) -> DroppedChild:
    return _dropped(number, "no batch label")


def test_an_idle_run_still_names_the_closed_but_labeled_child() -> None:
    result = RunResult(
        targets=(1492,),
        anomalies=(_closed(1503, BatchLabel.PLANNED),),
    )

    assert _render(result) == (
        "Nothing planned under #1492.\n"
        "  #1503 is closed but labeled planned — reopen it or `bin/acme skip` it\n"
    )


def test_a_halted_run_names_the_closed_but_labeled_child_after_the_halt() -> None:
    result = RunResult(
        targets=(1492,),
        outcomes=(
            IssueOutcome(
                number=1510,
                base="main",
                state=BatchLabel.STUCK,
                halt=HaltReason.VERIFICATION_FAILED,
            ),
        ),
        anomalies=(_closed(1503, BatchLabel.PLANNED),),
    )

    assert _render(result) == (
        "#1510 stuck (verification-failed) on main\n"
        "Batch halted at #1510.\n"
        "  #1503 is closed but labeled planned — reopen it or `bin/acme skip` it\n"
    )


def test_a_finished_run_names_the_closed_but_labeled_child_too() -> None:
    result = RunResult(
        targets=(1492,),
        outcomes=(
            IssueOutcome(number=1510, base="main", state=BatchLabel.READY_FOR_REVIEW),
        ),
        anomalies=(_closed(1503, BatchLabel.PLANNED),),
    )

    assert _render(result) == (
        "#1510 ready-for-review on main\n"
        "  #1503 is closed but labeled planned — reopen it or `bin/acme skip` it\n"
    )


class TestBatchTableAnomalies:
    def test_an_empty_batch_names_the_closed_child_and_counts_the_rest(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(
                *(_unlabeled(number) for number in range(1, 8)),
                _closed(1503, BatchLabel.STUCK),
            ),
        )

        assert _table(batch) == (
            "No batch issues under #1492 — 7 open children with no batch label.\n"
            "  #1503 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
        )

    def test_an_empty_batch_with_nothing_dropped_is_unchanged(self) -> None:
        assert _table(Batch(targets=(1492,), issues=())) == (
            "No batch issues under #1492.\n"
        )

    def test_a_multi_target_batch_names_every_target_and_no_epic(self) -> None:
        assert _table(Batch(targets=(1771, 1769), issues=())) == (
            "No batch issues under #1771, #1769.\n"
        )

    def test_unlabeled_children_alone_render_only_their_clause(self) -> None:
        batch = Batch(targets=(1492,), issues=(), dropped=(_unlabeled(3),))

        assert _table(batch) == (
            "No batch issues under #1492 — 1 open child with no batch label.\n"
        )

    def test_each_closed_child_gets_its_own_line_in_order(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(_closed(1510, BatchLabel.STUCK), _closed(1503, BatchLabel.STUCK)),
        )

        assert _table(batch) == (
            "No batch issues under #1492.\n"
            "  #1510 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
            "  #1503 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
        )

    def test_a_closed_unlabeled_child_joins_no_clause(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(_unlabeled(3), _dropped(9, "closed", state="CLOSED")),
        )

        assert _table(batch) == (
            "No batch issues under #1492 — 1 open child with no batch label.\n"
        )

    def test_a_closed_child_with_two_labels_is_one_line_naming_both(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(
                _dropped(
                    7,
                    "closed, labelled implementing, stuck",
                    state="CLOSED",
                    labels=(BatchLabel.IMPLEMENTING, BatchLabel.STUCK),
                ),
            ),
        )

        assert _table(batch) == (
            "No batch issues under #1492.\n"
            "  #7 is closed but labeled implementing, stuck"
            " — clear all but one label in GitHub, then `bin/acme skip` it\n"
        )

    def test_a_rendered_table_is_followed_by_the_note(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(number=70, title="Stack manager", state=BatchLabel.PLANNED),
            ),
            dropped=(_unlabeled(3), _closed(1503, BatchLabel.STUCK)),
        )

        assert _table(batch) == (
            "#70  planned  Stack manager\n"
            "1 open child with no batch label.\n"
            "  #1503 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
        )


class TestBatchTableVerbose:
    def test_verbose_lists_every_dropped_child_with_its_reason(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(
                _unlabeled(3),
                _closed(1503, BatchLabel.STUCK, title="Teardown on merge"),
            ),
        )

        assert _table(batch, {}) == (
            "No batch issues under #1492 — 1 open child with no batch label.\n"
            "  #1503 is closed but labeled stuck — reopen it or `bin/acme skip` it\n"
            "Dropped children:\n"
            "  #3 no batch label — Issue 3\n"
            "  #1503 closed, labelled stuck — Teardown on merge\n"
        )

    def test_verbose_renders_vm_facts_only_for_rows_that_have_them(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(number=70, title="Stack manager", state=BatchLabel.PLANNED),
                BatchIssue(number=12, title="Verifier", state=BatchLabel.QUEUED),
            ),
        )
        facts = {
            70: VmFacts(
                live=True,
                log=Path("/run/issue-70.log"),
                config_dir=Path("/run/issue-70.config"),
            )
        }

        assert _table(batch, facts) == (
            "#70  planned  Stack manager\n"
            "#12  queued   Verifier\n"
            "  #70 socket live, log /run/issue-70.log, config /run/issue-70.config\n"
        )

    @pytest.mark.parametrize(
        ("facts", "line"),
        [
            (
                VmFacts(config_dir=Path("/run/issue-70.config")),
                "  #70 config /run/issue-70.config\n",
            ),
            (
                VmFacts(log=Path("/run/issue-70.log")),
                "  #70 log /run/issue-70.log\n",
            ),
            (VmFacts(live=True), "  #70 socket live\n"),
        ],
    )
    def test_a_row_shows_only_the_facts_it_has(self, facts: VmFacts, line: str) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(number=70, title="Stack manager", state=BatchLabel.PLANNED),
            ),
        )

        assert _table(batch, {70: facts}) == f"#70  planned  Stack manager\n{line}"


def _row(
    number: int,
    state: BatchLabel = BatchLabel.PLANNED,
    *,
    live: bool = False,
    elapsed: str = "",
    last_line: str = "",
) -> DashboardRow:
    return DashboardRow(
        number=number,
        title=f"Issue {number}",
        state=state,
        live=live,
        elapsed=elapsed,
        last_line=last_line,
    )


def _dashboard(rows: tuple[DashboardRow, ...], selected: int | None) -> str:
    console = Console(file=io.StringIO(), width=120, highlight=False)
    console.print(dashboard_view(rows, selected))
    text = cast("io.StringIO", console.file).getvalue()
    return "".join(f"{line.rstrip()}\n" for line in text.splitlines())


def test_the_dashboard_lists_every_row_with_its_state_and_progress() -> None:
    output = _dashboard(
        (
            _row(10, BatchLabel.READY_FOR_REVIEW),
            _row(
                11,
                BatchLabel.IMPLEMENTING,
                live=True,
                elapsed="7m12s",
                last_line="running the suite",
            ),
        ),
        selected=11,
    )

    assert "#10" in output
    assert "ready-for-review" in output
    assert "implementing" in output
    assert "7m12s" in output
    assert "└ running the suite" in output


def test_the_selected_row_is_marked() -> None:
    rows = (_row(10), _row(11))

    assert "▶ #11" in _dashboard(rows, selected=11)
    assert "▶ #10" in _dashboard(rows, selected=10)
    assert "▶" not in _dashboard(rows, selected=None)


def test_an_empty_batch_renders_a_placeholder() -> None:
    assert "Nothing planned" in _dashboard((), selected=None)


def test_a_bracketed_title_survives_rendering() -> None:
    row = DashboardRow(number=10, title="[batch] dashboard", state=BatchLabel.PLANNED)

    assert "[batch] dashboard" in _dashboard((row,), selected=None)


def test_the_log_line_sits_under_its_issue() -> None:
    rows = (
        _row(10, BatchLabel.IMPLEMENTING, live=True, last_line="running the suite"),
        _row(11, last_line="never started"),
    )

    lines = _dashboard(rows, selected=11).splitlines()

    assert "Issue 10" in lines[0]
    assert lines[1] == "  └ running the suite"
    assert "Issue 11" in lines[2]
    assert lines[3] == "  └ never started"


def test_an_unselected_idle_row_shows_no_log_line() -> None:
    rows = (_row(10, BatchLabel.STUCK, last_line="boom"),)

    assert "boom" not in _dashboard(rows, selected=None)


def test_a_long_log_line_does_not_squeeze_the_columns() -> None:
    rows = (
        _row(
            10, BatchLabel.IMPLEMENTING, live=True, elapsed="7m12s", last_line="x" * 300
        ),
    )

    first = _dashboard(rows, selected=10).splitlines()[0]

    assert first.startswith("▶ #10")
    assert "implementing" in first
    assert "7m12s" in first
    assert "Issue 10" in first


class TestBatchTableTitleMarkup:
    def test_a_bracketed_title_renders_instead_of_raising(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(
                    number=70,
                    title="Drop the [/tmp] staging dir",
                    state=BatchLabel.PLANNED,
                ),
            ),
        )

        assert "[/tmp]" in _table(batch)

    def test_a_title_that_looks_like_a_style_is_not_swallowed(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(
                    number=70, title="Handle [ci] failures", state=BatchLabel.PLANNED
                ),
            ),
        )

        assert "Handle [ci] failures" in _table(batch)

    def test_the_state_column_keeps_its_markup(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(
                BatchIssue(
                    number=70,
                    title="Handle [ci] failures",
                    state=BatchLabel.READY_FOR_REVIEW,
                ),
            ),
        )

        rendered = _table(batch)

        assert "ready-for-review" in rendered
        assert "[green]" not in rendered

    def test_the_table_and_the_dashboard_agree_on_a_bracketed_title(self) -> None:
        title = "Drop the [/tmp] staging dir"
        batch = Batch(
            targets=(1492,),
            issues=(BatchIssue(number=70, title=title, state=BatchLabel.PLANNED),),
        )
        rows = (DashboardRow(number=70, title=title, state=BatchLabel.PLANNED),)

        assert title in _table(batch)
        assert title in _dashboard(rows, selected=None)


class TestTargetHeadings:
    def test_an_idle_run_names_every_target_rather_than_an_epic(self) -> None:
        out = io.StringIO()
        print_run_result(RunResult(targets=(1771, 1769)), out, prog=_PROG)

        assert out.getvalue() == "Nothing planned under #1771, #1769.\n"

    def test_an_idle_sweep_names_every_target(self) -> None:
        out = io.StringIO()
        print_teardown_result(TeardownResult(targets=(1771, 1769)), out)

        assert out.getvalue() == "Nothing to clean up under #1771, #1769.\n"

    def test_the_banner_names_the_targets_the_batch_ran_over(self) -> None:
        assert run_banner(RunResult(targets=(1771, 1769))) == (
            "Batch finished under #1771, #1769. q to quit."
        )

    def test_a_standalone_target_implies_no_parent(self) -> None:
        out = io.StringIO()
        print_next_issue(
            NextIssue(number=1769, title="One-off", body="Body"),
            (1769,),
            out,
            prog="bin/acme",
        )

        assert "bin/acme agent plan-written 1769 1769" in out.getvalue()
        assert "dev/batch" not in out.getvalue()
        assert "epic" not in out.getvalue().lower()


class TestConfiguredProgName:
    def test_the_skip_remedy_names_it(self) -> None:
        result = RunResult(
            targets=(1492,), anomalies=(_closed(1503, BatchLabel.PLANNED),)
        )

        rendered = _render(result)

        assert "reopen it or `bin/acme skip` it" in rendered
        assert "dev/batch" not in rendered

    def test_the_conflicting_label_remedy_names_it(self) -> None:
        batch = Batch(
            targets=(1492,),
            issues=(),
            dropped=(
                _dropped(
                    7,
                    "closed, labelled implementing, stuck",
                    state="CLOSED",
                    labels=(BatchLabel.IMPLEMENTING, BatchLabel.STUCK),
                ),
            ),
        )

        rendered = _table(batch)

        assert "then `bin/acme skip` it" in rendered
        assert "dev/batch" not in rendered

    def test_the_merged_cleanup_remedy_names_it(self) -> None:
        line = recovery_line(
            RecoveryResult(
                number=10,
                action=RecoveryAction.SKIP,
                refusal=RecoveryRefusal.MERGED,
            ),
            prog=_PROG,
        )

        assert line == "Cannot skip #10: it merged — run `bin/acme cleanup <target>`"


class TestReclaimLine:
    def test_an_occupied_slot_is_named_as_such(self) -> None:
        outcome = ReclaimOutcome(branch="issue-9", skip=TeardownSkip.OCCUPIED)

        assert reclaim_line(outcome, dry_run=False) == "issue-9 left alone (occupied)"
