from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from batch.body import DEFAULT_GUIDANCE
from batch.models import (
    DEFAULT_RAM,
    BatchIssue,
    BatchLabel,
    DebugRefusal,
    DroppedChild,
    HaltReason,
    Problem,
)
from batch.orchestrator import Orchestrator
from batch.teardown import Teardown
from batch.testing.payloads import (
    EPIC,
    FakeClock,
    FakeRunner,
    FakeStack,
    FakeState,
    FakeVerifier,
    batch_config,
    batch_issue,
    closed_child,
    unlabeled_child,
)


@dataclass(frozen=True)
class Boot:
    command: tuple[str, ...]
    cwd: Path | None
    staged: tuple[tuple[Path, bool], ...]


@dataclass
class FakeBoot:
    runner: FakeRunner
    code: int = 0
    absent: bool = False
    calls: list[Boot] = field(default_factory=list)

    def __call__(self, command: Sequence[str], cwd: Path | None) -> int:
        if self.absent:
            raise FileNotFoundError(command[0])
        self.calls.append(Boot(tuple(command), cwd, tuple(self.runner.staged)))
        return self.code


@dataclass(frozen=True)
class Harness:
    core: Orchestrator
    state: FakeState
    stack: FakeStack
    runner: FakeRunner
    verifier: FakeVerifier
    journal: list[str]
    reported: list[str]
    boot: FakeBoot


def harness(
    *issues: BatchIssue,
    root: Path,
    polls: dict[int, int] | None = None,
    live: tuple[int, ...] = (),
    failing: tuple[int, ...] = (),
    pending: tuple[int, ...] = (),
    merged: tuple[int, ...] = (),
    dirty: tuple[str, ...] = (),
    absent: tuple[str, ...] = (),
    timeout: float = 7200.0,
    verify_wait: float = 2700.0,
    model: str | None = None,
    ram: int = DEFAULT_RAM,
    boot_code: int = 0,
    dtach_missing: bool = False,
    staging_error: OSError | None = None,
    dropped: tuple[DroppedChild, ...] = (),
    queued_targets: tuple[int, ...] = (),
) -> Harness:
    journal: list[str] = []
    state = FakeState(
        *issues, journal=journal, dropped=dropped, queued_targets=queued_targets
    )
    stack = FakeStack(root, dirty=dirty, absent=absent, journal=journal)
    runner = FakeRunner(
        root, polls=polls, live=live, staging_error=staging_error, journal=journal
    )
    verifier = FakeVerifier(failing, pending, merged)
    clock = FakeClock()
    reported: list[str] = []
    boot = FakeBoot(runner, code=boot_code, absent=dtach_missing)
    core = Orchestrator(
        state,
        stack,
        runner,
        verifier,
        Teardown(state, stack, runner, verifier),
        config=batch_config(),
        report=reported.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        timeout=timeout,
        verify_wait=verify_wait,
        model=model,
        ram=ram,
        spawn=boot,
    )
    return Harness(core, state, stack, runner, verifier, journal, reported, boot)


class TestStackingAndAdvance:
    def test_one_planned_issue_runs_end_to_end(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path)

        result = h.core.run((EPIC,))

        assert h.stack.ensured == [(10, "main")]
        assert [issue for issue, _ in h.runner.launched] == [10]
        assert h.verifier.asked == [(10, ("main",))]
        assert h.journal == [
            "label #10 implementing",
            "launch #10",
            "label #10 ready-for-review",
        ]
        assert not result.halted

    def test_the_vm_boots_the_slot_the_stack_returned_with_staged_credentials(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path)

        _ = h.core.run((EPIC,))

        _, session = h.runner.launched[0]
        assert h.runner.staged == [(h.runner.config_dir(10), True)]
        assert session.config_dir == h.runner.config_dir(10)
        assert session.worktree == "widgets/worktrees/issue-10"
        assert session.cwd == h.stack.mount_root
        assert session.disk == h.stack.worktree_root / "issue-10.raw"

    def test_the_second_issue_is_cut_from_the_first_issues_branch(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(10, "main"), (11, "issue-10")]

    def test_the_verifier_is_asked_for_the_base_the_branch_was_cut_from(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), batch_issue(12), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert h.verifier.asked == [(issue, (base,)) for issue, base in h.stack.ensured]

    def test_issues_run_in_sub_issue_order_not_numeric_order(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(70), batch_issue(12), batch_issue(40), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(70, "main"), (12, "issue-70"), (40, "issue-12")]

    def test_a_ready_predecessor_from_an_earlier_run_is_the_base(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(11, "issue-10")]

    def test_a_queued_predecessor_is_not_a_base_(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, BatchLabel.QUEUED), batch_issue(11), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(11, "main")]


PLAN_BODY = "Some preamble\n\n## Test Plan\n\n1. A test\n"
GUIDANCE_BODY = "Some preamble\n\n## Test Guidance\n\nPort the callers, no new tests.\n"


class TestPromptAssembly:
    def test_a_test_plan_selects_the_impl_only_template(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, body=PLAN_BODY), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert h.runner.agents() == [
            "tools/drive 10 --impl-only --headless --base main"
        ]

    def test_test_guidance_is_passed_verbatim_as_the_positional(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10, body=GUIDANCE_BODY), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert shlex.split(h.runner.agents()[0]) == [
            "tools/drive",
            "10",
            "Port the callers, no new tests.",
            "--headless",
            "--base",
            "main",
        ]

    def test_a_body_with_neither_section_gets_the_default_guidance(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10, body="Just a description."), root=tmp_path)

        _ = h.core.run((EPIC,))

        assert shlex.split(h.runner.agents()[0])[2] == DEFAULT_GUIDANCE

    def test_predecessors_list_the_issues_the_branch_is_stacked_on(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, body=PLAN_BODY),
            batch_issue(11, BatchLabel.QUEUED),
            batch_issue(12, body=PLAN_BODY),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.runner.agents() == [
            "tools/drive 10 --impl-only --headless --base main",
            "tools/drive 12 --impl-only --headless --base issue-10 --predecessors 10",
        ]

    def test_the_model_reaches_the_agent_command(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, body=PLAN_BODY), root=tmp_path, model="opus")

        _ = h.core.run((EPIC,))

        assert h.runner.agents() == [
            "tools/drive 10 --impl-only --headless --base main --model opus"
        ]


class TestHalt:
    def test_a_failed_verdict_marks_the_issue_stuck_and_stops_the_batch(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, failing=(10,))

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.VERIFICATION_FAILED
        assert h.state.states()[10] is BatchLabel.STUCK
        assert [issue for issue, _ in h.runner.launched] == [10]
        assert h.stack.ensured == [(10, "main")]

    def test_a_vm_that_never_exits_times_out_into_stuck(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10),
            batch_issue(11),
            root=tmp_path,
            polls={10: 1000},
            timeout=90.0,
        )

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.TIMED_OUT
        assert h.state.states()[10] is BatchLabel.STUCK
        assert h.verifier.asked == []
        assert [issue for issue, _ in h.runner.launched] == [10]

    def test_a_vm_already_live_for_the_issue_marks_it_stuck(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, live=(10,))

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.VM_ALREADY_RUNNING
        assert h.state.states()[10] is BatchLabel.STUCK
        assert h.runner.launched == []

    def test_the_failed_issues_verdict_survives_on_the_outcome(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, failing=(10,))

        result = h.core.run((EPIC,))

        verdict = result.outcomes[-1].verdict
        assert verdict is not None
        assert verdict.problems == (Problem.NO_PR,)


class TestPickupAndResume:
    def test_an_issue_planned_mid_run_is_picked_up(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path)

        def plan_eleven(state: FakeState) -> None:
            if state.fetches == 2:
                state.issues.append(batch_issue(11))

        h.state.on_fetch = plan_eleven
        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(10, "main"), (11, "issue-10")]
        assert h.state.states()[11] is BatchLabel.READY_FOR_REVIEW

    def test_a_resumed_run_stacks_on_the_last_ready_issue(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.READY_FOR_REVIEW),
            batch_issue(12),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(12, "issue-11")]

    def test_a_resumed_run_relaunches_nothing_already_ready(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.READY_FOR_REVIEW),
            batch_issue(12),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert [issue for issue, _ in h.runner.launched] == [12]
        assert h.verifier.asked == [(12, ("issue-11",))]

    def test_a_batch_with_nothing_planned_is_a_clean_no_op(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.QUEUED),
            root=tmp_path,
        )

        result = h.core.run((EPIC,))

        assert result.outcomes == ()
        assert not result.halted
        assert h.runner.launched == []
        assert h.state.transitions == []

    def test_a_run_with_nothing_to_do_still_reports_the_anomaly(
        self, tmp_path: Path
    ) -> None:
        h = harness(root=tmp_path, dropped=(unlabeled_child(3), closed_child(1503)))

        result = h.core.run((EPIC,))

        assert result.outcomes == ()
        assert result.anomalies == (closed_child(1503),)


class TestVerificationWait:
    def test_the_verifier_is_given_a_budget_to_wait_out_pending_ci(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, verify_wait=1800.0)

        _ = h.core.run((EPIC,))

        assert h.verifier.waits == [timedelta(seconds=1800.0)]

    def test_ci_still_pending_when_the_budget_runs_out_is_a_halt(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, pending=(10,))

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.VERIFICATION_FAILED
        assert h.state.states()[10] is BatchLabel.STUCK
        assert [issue for issue, _ in h.runner.launched] == [10]


class TestStackingOnTheTip:
    def test_a_re_added_issue_stacks_on_the_tip_not_on_its_neighbour(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11),
            batch_issue(12, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(11, "issue-12")]

    def test_a_re_added_issue_is_told_about_the_plans_it_sits_on(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW, body=PLAN_BODY),
            batch_issue(11, body=PLAN_BODY),
            batch_issue(12, BatchLabel.READY_FOR_REVIEW, body=PLAN_BODY),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.runner.agents() == [
            "tools/drive 11 --impl-only --headless --base issue-12 --predecessors 10,12"
        ]

    def test_the_issue_after_a_skipped_one_stacks_on_its_predecessor(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11),
            batch_issue(12),
            root=tmp_path,
        )
        h.state.clear_state(11)

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(12, "issue-10")]

    def test_the_tip_ignores_unstarted_issues_between_started_ones(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.QUEUED),
            batch_issue(12, BatchLabel.READY_FOR_REVIEW),
            batch_issue(13),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(13, "issue-12")]


class TestHaltAtStuck:
    def test_a_restart_with_an_earlier_stuck_issue_launches_nothing(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.STUCK),
            batch_issue(11),
            root=tmp_path,
        )

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.STUCK_ISSUE
        assert h.runner.launched == []
        assert h.stack.ensured == []
        assert h.state.transitions == []

    def test_once_the_stuck_issue_is_skipped_the_next_run_proceeds(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.STUCK),
            batch_issue(11),
            root=tmp_path,
        )

        halted = h.core.run((EPIC,))
        h.state.clear_state(10)
        result = h.core.run((EPIC,))

        assert halted.halted is HaltReason.STUCK_ISSUE
        assert not result.halted
        assert h.stack.ensured == [(11, "main")]


class TestCrashRecovery:
    def test_implementing_with_no_live_vm_becomes_stuck_and_halts(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING),
            batch_issue(11),
            root=tmp_path,
            polls={10: 0},
        )

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.ORPHANED_VM
        assert h.state.states()[10] is BatchLabel.STUCK
        assert h.runner.launched == []

    def test_an_orphans_branch_and_disk_are_left_for_inspection(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING), root=tmp_path, polls={10: 0}
        )

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == []
        assert h.stack.branches == []
        assert h.verifier.asked == []

    def test_implementing_with_a_live_vm_is_adopted_and_advances(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING),
            batch_issue(11),
            root=tmp_path,
            polls={10: 3},
        )

        result = h.core.run((EPIC,))

        assert not result.halted
        assert [issue for issue, _ in h.runner.launched] == [11]
        assert h.stack.ensured == [(11, "issue-10")]
        assert h.verifier.asked == [(10, ("main",)), (11, ("issue-10",))]
        assert h.state.states()[10] is BatchLabel.READY_FOR_REVIEW

    def test_an_adopted_vm_that_fails_verification_goes_stuck(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING),
            batch_issue(11),
            root=tmp_path,
            polls={10: 3},
            failing=(10,),
        )

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.VERIFICATION_FAILED
        assert h.state.states()[10] is BatchLabel.STUCK
        assert h.runner.launched == []

    def test_the_issue_this_run_is_driving_is_not_mistaken_for_a_crash(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path)

        result = h.core.run((EPIC,))

        assert not result.halted
        assert [issue for issue, _ in h.runner.launched] == [10, 11]


def _close_once(
    number: int, trigger: int, label: BatchLabel = BatchLabel.READY_FOR_REVIEW
) -> Callable[[FakeState], None]:
    def close(state: FakeState) -> None:
        if state.states().get(trigger) is label:
            state.close(number, merged=True)

    return close


class TestTeardownDuringARun:
    def test_an_issue_merged_mid_batch_is_cleaned_while_later_issues_wait(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10),
            batch_issue(11),
            batch_issue(12),
            root=tmp_path,
            merged=(10,),
        )
        h.state.on_fetch = _close_once(10, 10)

        result = h.core.run((EPIC,))

        assert not result.halted
        assert h.stack.removed == [10]
        assert h.journal.index("remove #10") < h.journal.index("launch #12")
        assert "#10 cleaned up" in h.reported

    def test_the_successor_of_a_merged_issue_is_cut_from_main(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, merged=(10,))
        h.state.on_fetch = _close_once(10, 10)

        _ = h.core.run((EPIC,))

        assert h.stack.ensured == [(10, "main"), (11, "main")]

    def test_an_issue_merged_during_the_last_iteration_is_still_cleaned(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, merged=(11,))
        h.state.on_fetch = _close_once(11, 11)

        result = h.core.run((EPIC,))

        assert not result.halted
        assert h.stack.removed == [11]
        assert "#11 cleaned up" in h.reported

    def test_a_halting_run_sweeps_before_it_returns(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10),
            batch_issue(11),
            root=tmp_path,
            merged=(10,),
            failing=(11,),
        )
        h.state.on_fetch = _close_once(10, 10)

        result = h.core.run((EPIC,))

        assert result.halted is HaltReason.VERIFICATION_FAILED
        assert h.stack.removed == [10]

    def test_a_run_with_no_work_sweeps_what_an_earlier_run_left(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            polls={10: 0},
            merged=(10,),
        )
        h.state.on_fetch = _close_once(10, 10)

        result = h.core.run((EPIC,))

        assert result.outcomes == ()
        assert h.stack.removed == [10]
        assert "#10 cleaned up" in h.reported

    def test_a_sweep_with_nothing_to_clean_never_halts(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path)

        result = h.core.run((EPIC,))

        assert not result.halted
        assert h.stack.removed == []

    def test_a_sweep_that_skips_an_issue_never_halts(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10),
            batch_issue(11, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            merged=(11,),
            dirty=("issue-11",),
        )
        h.state.close(11, merged=True)

        result = h.core.run((EPIC,))

        assert not result.halted
        assert h.stack.removed == []
        assert [issue.number for issue in h.state.finished((EPIC,))] == [11]
        assert [issue for issue, _ in h.runner.launched] == [10]
        assert "#11 left alone (dirty-worktree)" in h.reported


class TestBaseRederivation:
    def test_a_predecessor_merging_mid_run_widens_the_accepted_bases(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11),
            root=tmp_path,
            merged=(10,),
        )
        h.state.on_fetch = _close_once(10, 11, BatchLabel.IMPLEMENTING)

        result = h.core.run((EPIC,))

        assert h.stack.ensured == [(11, "issue-10")]
        assert h.verifier.asked == [(11, ("issue-10", "main"))]
        assert not result.halted

    def test_an_untouched_stack_keeps_the_launch_base_as_the_only_one(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11),
            root=tmp_path,
        )

        _ = h.core.run((EPIC,))

        assert h.verifier.asked == [(11, ("issue-10",))]


class TestTimings:
    def test_a_launched_issue_is_timed_from_its_launch(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 2})

        _ = h.core.run((EPIC,))

        assert h.core.timings.elapsed(10) == 60.0

    def test_an_unstarted_issue_has_no_elapsed(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), batch_issue(11), root=tmp_path, failing=(10,))

        _ = h.core.run((EPIC,))

        assert h.core.timings.elapsed(11) is None

    def test_a_stuck_issue_keeps_the_time_it_took_to_get_there(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 1}, failing=(10,))

        _ = h.core.run((EPIC,))

        assert h.core.timings.elapsed(10) == 30.0

    def test_an_adopted_vm_is_timed_from_adoption(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING),
            root=tmp_path,
            polls={10: 3},
            live=(10,),
        )

        _ = h.core.run((EPIC,))

        assert h.core.timings.elapsed(10) == 60.0

    def test_a_timed_out_issue_stops_counting_at_the_deadline(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 100}, timeout=90.0)

        _ = h.core.run((EPIC,))

        assert h.core.timings.elapsed(10) == 90.0


class TestDashboardView:
    def test_a_snapshot_carries_the_batch_with_liveness_and_elapsed(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING),
            batch_issue(11),
            root=tmp_path,
            polls={10: 40, 11: 0},
            live=(10,),
            timeout=60.0,
        )
        _ = (tmp_path / "issue-10.log").write_text("compiling\n")

        _ = h.core.run((EPIC,))
        snapshot = h.core.snapshot((EPIC,))

        assert [row.number for row in snapshot] == [10, 11]
        assert snapshot[0].live
        assert snapshot[0].elapsed == "1m00s"
        assert snapshot[0].last_line == "compiling"
        assert not snapshot[1].live
        assert snapshot[1].elapsed == ""


class TestEntering:
    def test_a_live_vm_is_attached_to_and_never_booted(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 5})

        entry = h.core.enter(10)

        assert entry.command == (
            "dtach",
            "-a",
            str(tmp_path / "issue-10.sock"),
            "-r",
            "none",
        )
        assert entry.boot == ()
        assert entry.refusal is None
        assert h.boot.calls == []
        assert h.stack.found == []

    def test_an_exited_vm_is_booted_before_it_is_attached_to(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0})

        entry = h.core.enter(10)

        boot = h.boot.calls[0]
        assert boot.command[:3] == ("dtach", "-n", str(tmp_path / "issue-10.sock"))
        assert boot.cwd == tmp_path
        assert boot.staged == ((h.runner.config_dir(10), False),)
        assert entry.boot == boot.command
        assert entry.command == h.runner.attach_command(10)
        assert h.stack.found == [("issue-10", "main")]

    def test_the_boot_leaves_the_log_of_the_run_being_debugged_alone(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0})

        _ = h.core.enter(10)

        booted = h.boot.calls[0].command
        assert booted[3] == "vibe"
        assert str(h.runner.log(10)) not in shlex.join(booted)

    def test_the_booted_session_resumes_the_agents_own_transcript(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0})

        _ = h.core.enter(10)

        booted = shlex.join(h.boot.calls[0].command)
        assert "tools/session 10 --debug" in booted
        assert "tools/drive" not in booted

    def test_the_batchs_model_and_ram_reach_the_booted_session(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10), root=tmp_path, polls={10: 0}, model="opus", ram=9001
        )

        _ = h.core.enter(10)

        booted = shlex.join(h.boot.calls[0].command)
        assert "vibe --ram 9001" in booted
        assert "tools/session 10 --debug -- --model opus" in booted

    def test_a_missing_slot_names_the_pieces_and_boots_nothing(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0}, absent=("issue-10",))

        entry = h.core.enter(10)

        assert entry.refusal is DebugRefusal.NO_SLOT
        assert entry.command == ()
        assert entry.missing == (
            f"no worktree at {h.stack.worktree_root / 'issue-10'}",
            f"no disk at {h.stack.worktree_root / 'issue-10.raw'}",
        )
        assert h.boot.calls == []
        assert h.runner.staged == []

    def test_a_boot_that_cannot_spawn_is_a_refusal_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0}, dtach_missing=True)

        entry = h.core.enter(10)

        assert entry.refusal is DebugRefusal.BOOT_FAILED
        assert entry.command == ()

    def test_config_staging_that_fails_is_a_refusal_not_an_exception(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10),
            root=tmp_path,
            polls={10: 0},
            staging_error=PermissionError("read-only run root"),
        )

        entry = h.core.enter(10)

        assert entry.refusal is DebugRefusal.BOOT_FAILED
        assert entry.command == ()
        assert h.boot.calls == []

    def test_a_boot_that_exits_non_zero_is_a_refusal(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0}, boot_code=3)

        entry = h.core.enter(10)

        assert entry.refusal is DebugRefusal.BOOT_FAILED
        assert entry.command == ()

    def test_a_dry_run_resolves_the_boot_without_spawning_it(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0})

        entry = h.core.enter(10, dry_run=True)

        assert entry.boot[:2] == ("dtach", "-n")
        assert entry.command == h.runner.attach_command(10)
        assert h.boot.calls == []
        assert h.runner.staged == []

    def test_fresh_boots_a_bare_session_instead_of_the_agents_own(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10), root=tmp_path, polls={10: 0})

        _ = h.core.enter(10, fresh=True)

        booted = shlex.join(h.boot.calls[0].command)
        assert "tools/session" not in booted
        assert "claude --allow-dangerously-skip-permissions" in booted

    def test_liveness_is_read_at_the_keypress_not_from_the_rendered_frame(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.IMPLEMENTING), root=tmp_path, polls={10: 1}
        )

        rendered = h.core.snapshot((EPIC,))
        entry = h.core.enter(10)

        assert rendered[0].live
        assert entry.boot == h.boot.calls[0].command


class TestWaitingTargets:
    def test_a_run_asks_its_state_which_targets_still_hold_queued_work(
        self, tmp_path: Path
    ) -> None:
        h = harness(root=tmp_path, queued_targets=(EPIC,))

        assert h.core.waiting_targets((EPIC, 1769)) == (EPIC,)
        assert h.core.waiting_targets((1769,)) == ()
