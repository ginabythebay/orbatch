from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from batch.models import (
    BatchIssue,
    BatchLabel,
    TeardownOutcome,
    TeardownSkip,
    UnsafeRemovalError,
)
from batch.teardown import Teardown
from batch.testing.payloads import (
    EPIC,
    FakeRunner,
    FakeStack,
    FakeState,
    FakeVerifier,
    batch_config,
    batch_issue,
)
from batch.vm import VmRunner


@dataclass(frozen=True)
class Harness:
    core: Teardown
    state: FakeState
    stack: FakeStack
    runner: FakeRunner
    verifier: FakeVerifier
    journal: list[str]


def harness(
    *merged_issues: BatchIssue,
    root: Path,
    unmerged: tuple[BatchIssue, ...] = (),
    dirty: tuple[str, ...] = (),
    unpushed: tuple[str, ...] = (),
    patch_unique: tuple[str, ...] = (),
    live: tuple[int, ...] = (),
) -> Harness:
    journal: list[str] = []
    state = FakeState(*merged_issues, *unmerged, journal=journal)
    for issue in merged_issues:
        state.close(issue.number, merged=True)
    for issue in unmerged:
        state.close(issue.number)
    stack = FakeStack(
        root,
        dirty=dirty,
        unpushed=unpushed,
        patch_unique=patch_unique,
        journal=journal,
    )
    polls = {issue.number: 0 for issue in (*merged_issues, *unmerged)}
    runner = FakeRunner(
        root, polls={**polls, **dict.fromkeys(live, 1)}, journal=journal
    )
    verifier = FakeVerifier(merged=[issue.number for issue in merged_issues])
    return Harness(
        Teardown(state, stack, runner, verifier),
        state,
        stack,
        runner,
        verifier,
        journal,
    )


class TestDetection:
    def test_a_merged_closed_candidate_is_cleaned(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
        )

        result = h.core.sweep((EPIC,))

        assert result.cleaned == (10,)
        assert h.state.swept == [EPIC]
        assert h.stack.removed == [10]
        assert h.runner.cleaned == [10]
        assert h.state.finished((EPIC,)) == ()

    def test_an_unmerged_candidate_is_left_entirely_alone(self, tmp_path: Path) -> None:
        h = harness(
            root=tmp_path, unmerged=(batch_issue(10, BatchLabel.READY_FOR_REVIEW),)
        )

        result = h.core.sweep((EPIC,))

        assert result.outcomes[0].skip is TeardownSkip.NOT_MERGED
        assert result.cleaned == ()
        assert h.journal == []

    def test_a_stuck_issue_that_merged_anyway_is_still_cleaned(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10, BatchLabel.STUCK), root=tmp_path)

        result = h.core.sweep((EPIC,))

        assert result.cleaned == (10,)


class TestSafety:
    def test_a_dirty_worktree_skips_the_issue_whole(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            dirty=("issue-10",),
        )

        result = h.core.sweep((EPIC,))

        assert result.outcomes[0].skip is TeardownSkip.DIRTY_WORKTREE
        assert h.stack.removed == []
        assert h.runner.cleaned == []
        assert h.journal == []

    def test_a_branch_carrying_its_own_landed_commits_is_torn_down(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            unpushed=("issue-10",),
        )

        result = h.core.sweep((EPIC,))

        assert result.cleaned == (10,)
        assert h.stack.removed == [10]
        assert h.runner.cleaned == [10]
        assert h.state.finished((EPIC,)) == ()

    def test_the_merged_base_reaches_the_removal(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path)

        _ = h.core.sweep((EPIC,))

        assert h.stack.merged_bases == ["origin/main"]

    def test_a_commit_that_never_landed_skips_the_issue_whole(
        self, tmp_path: Path
    ) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            patch_unique=("issue-10",),
        )

        result = h.core.sweep((EPIC,))

        assert result.outcomes[0].skip is TeardownSkip.UNPUSHED_COMMITS
        assert h.stack.removed == []
        assert h.runner.cleaned == []
        assert h.journal == []

    def test_a_live_vm_skips_the_issue_whole(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path, live=(10,)
        )

        result = h.core.sweep((EPIC,))

        assert result.outcomes[0].skip is TeardownSkip.VM_LIVE
        assert h.stack.removed == []
        assert h.runner.cleaned == []
        assert h.journal == []

    def test_removal_goes_through_the_stack_safety_check(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path)

        _ = h.core.sweep((EPIC,))

        assert h.journal == ["remove #10", "clean #10", "clear #10"]


class TestFakeStackHonoursUnpushed:
    def test_an_unforced_removal_raises(self, tmp_path: Path) -> None:
        stack = FakeStack(tmp_path, unpushed=("issue-10",))

        with pytest.raises(UnsafeRemovalError) as caught:
            _ = stack.remove(10)

        assert caught.value.skip is TeardownSkip.UNPUSHED_COMMITS
        assert stack.removed == []
        assert stack.journal == []

    def test_a_forced_removal_still_goes_through(self, tmp_path: Path) -> None:
        stack = FakeStack(tmp_path, unpushed=("issue-10",))

        _ = stack.remove(10, force=True)

        assert stack.removed == [10]


class TestLabelAndConfigDir:
    def test_the_config_dir_goes_and_the_log_stays(self, tmp_path: Path) -> None:
        runner = VmRunner(tmp_path, config=batch_config)
        runner.config_dir(10).mkdir(parents=True)
        _ = runner.log(10).write_text("console output\n")
        state = FakeState(batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        state.close(10, merged=True)

        _ = Teardown(
            state, FakeStack(tmp_path), runner, FakeVerifier(merged=(10,))
        ).sweep((EPIC,))

        assert not runner.config_dir(10).exists()
        assert runner.log(10).read_text() == "console output\n"

    def test_a_skipped_issue_keeps_its_batch_label(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            dirty=("issue-10",),
        )

        _ = h.core.sweep((EPIC,))

        assert [issue.number for issue in h.state.finished((EPIC,))] == [10]

    def test_an_unlanded_issue_keeps_its_batch_label(self, tmp_path: Path) -> None:
        h = harness(
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            root=tmp_path,
            patch_unique=("issue-10",),
        )

        _ = h.core.sweep((EPIC,))

        assert [issue.number for issue in h.state.finished((EPIC,))] == [10]

    def test_a_second_sweep_finds_nothing_left_to_do(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path)
        _ = h.core.sweep((EPIC,))

        again = h.core.sweep((EPIC,))

        assert again.outcomes == ()
        assert h.stack.removed == [10]


class TestNoPlanningSlotIsSwept:
    def test_a_closed_epic_sweeps_no_planning_slot(self, tmp_path: Path) -> None:
        h = harness(root=tmp_path)
        (tmp_path / f"plan-{EPIC}.config").mkdir(parents=True)

        result = h.core.sweep((EPIC,))

        assert result.outcomes == ()
        assert h.stack.removed_branches == []
        assert (tmp_path / f"plan-{EPIC}.config").is_dir()

    def test_a_closed_epic_still_cleans_its_merged_children(
        self, tmp_path: Path
    ) -> None:
        h = harness(batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path)

        result = h.core.sweep((EPIC,))

        assert result.outcomes == (TeardownOutcome(number=10),)
        assert h.stack.removed == [10]
        assert h.stack.removed_branches == []

    def test_every_target_named_is_swept(self, tmp_path: Path) -> None:
        h = harness(batch_issue(10, BatchLabel.READY_FOR_REVIEW), root=tmp_path)

        result = h.core.sweep((EPIC, 1769))

        assert result.targets == (EPIC, 1769)
        assert h.state.swept == [EPIC, 1769]
