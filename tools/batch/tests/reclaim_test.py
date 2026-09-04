from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from batch.models import (
    OccupancyError,
    ReclaimResult,
    RemoveResult,
    TeardownSkip,
    UnsafeRemovalError,
)
from batch.occupancy import occupied_slots
from batch.reclaim import Reclaimer
from batch.stack import StackManager
from batch.testing.payloads import batch_config
from batch.testing.scratch import Scratch, git, scratch
from batch.vm import VmRunner


@pytest.fixture(name="sc")
def scratch_repo(tmp_path: Path) -> Scratch:
    """The seed image is moved off the worktree root, where a bare `.raw` is
    itself a reclaim candidate."""
    made = scratch(tmp_path)
    images = tmp_path / "images"
    images.mkdir()
    seed = images / made.seed.name
    made.seed.rename(seed)
    return replace(made, seed=seed)


def manager(sc: Scratch) -> StackManager:
    return StackManager(sc.repo, seed_image=sc.seed)


def vms(sc: Scratch, *, disks: Sequence[str] = ()) -> VmRunner:
    return VmRunner(sc.root, config=batch_config, disks=lambda: frozenset(disks))


def reclaimer(
    sc: Scratch,
    *,
    live_pids: Sequence[int] = (),
    occupied: Sequence[str] = (),
    disks: Sequence[str] = (),
) -> Reclaimer:
    return Reclaimer(
        manager(sc),
        vms(sc, disks=disks),
        alive=lambda pid: pid in set(live_pids),
        occupied=lambda: frozenset(occupied),
    )


def skips(result: ReclaimResult) -> list[tuple[str, TeardownSkip | None]]:
    return [(outcome.branch, outcome.skip) for outcome in result.outcomes]


def intact(sc: Scratch, branch: str) -> bool:
    return (sc.trees / branch).is_dir() and (sc.trees / f"{branch}.raw").is_file()


class CountingStack:
    def __init__(self, inner: StackManager) -> None:
        self._inner: StackManager = inner
        self.removals: int = 0

    @property
    def worktree_root(self) -> Path:
        return self._inner.worktree_root

    def slot_names(self) -> tuple[str, ...]:
        return self._inner.slot_names()

    def merged_into(self, branch: str, base: str) -> bool:
        return self._inner.merged_into(branch, base)

    def dirty(self, branch: str) -> bool:
        return self._inner.dirty(branch)

    def unpushed(self, branch: str) -> bool:
        return self._inner.unpushed(branch)

    def checked_out(self, branch: str) -> str | None:
        return self._inner.checked_out(branch)

    def remove_branch(self, branch: str, *, force: bool = False) -> RemoveResult:
        self.removals += 1
        return self._inner.remove_branch(branch, force=force)


class TestReclaim:
    def test_a_merged_clean_idle_slot_is_reclaimed(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", None)]
        assert not slot.worktree.exists()
        assert not slot.disk.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")

    def test_an_unmerged_branch_is_left_alone(self, sc: Scratch) -> None:
        slot = manager(sc).ensure_branch("openfix", "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "ad-hoc work")

        result = reclaimer(sc).collect()

        assert skips(result) == [("openfix", TeardownSkip.NOT_MERGED)]
        assert intact(sc, "openfix")

    def test_a_dirty_worktree_is_left_alone(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        _ = (slot.worktree / "scribble.txt").write_text("uncommitted\n")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", TeardownSkip.DIRTY_WORKTREE)]
        assert intact(sc, "issue-9")

    def test_a_branch_with_unpushed_commits_is_left_alone(self, sc: Scratch) -> None:
        _ = sc.commit("landed but never pushed")
        _ = manager(sc).ensure(9, "main")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", TeardownSkip.UNPUSHED_COMMITS)]
        assert intact(sc, "issue-9")

    def test_a_squash_landed_branch_is_left_alone(self, sc: Scratch) -> None:
        """Reclaim has no merged PR to justify the looser patch-identity check,
        so a branch whose content landed elsewhere is still its own copy."""
        slot = manager(sc).ensure(9, "main")
        _ = sc.commit_file(slot.worktree, "feature.txt", "the work\n", "agent work")
        sc.push("issue-9", slot.worktree)
        _ = sc.land("feature.txt", "the work\n", "agent work (#9)")
        sc.unpublish("issue-9")
        _ = sc.merge("issue-9")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", TeardownSkip.UNPUSHED_COMMITS)]
        assert intact(sc, "issue-9")

    def test_a_slot_whose_socket_exists_is_left_alone(self, sc: Scratch) -> None:
        _ = manager(sc).ensure_branch("subtienodm", "main")
        _ = (sc.root / "subtienodm.sock").write_text("")

        result = reclaimer(sc).collect()

        assert skips(result) == [("subtienodm", TeardownSkip.VM_LIVE)]
        assert intact(sc, "subtienodm")


class RacingStack:
    """Selects clean, then refuses removal: the slot changed under the sweep."""

    def __init__(self, skip: TeardownSkip, root: Path) -> None:
        self._skip: TeardownSkip = skip
        self._root: Path = root

    @property
    def worktree_root(self) -> Path:
        return self._root

    def slot_names(self) -> tuple[str, ...]:
        return ("issue-9",)

    def merged_into(self, branch: str, base: str) -> bool:
        del branch, base
        return True

    def dirty(self, branch: str) -> bool:
        del branch
        return False

    def unpushed(self, branch: str) -> bool:
        del branch
        return False

    def checked_out(self, branch: str) -> str | None:
        return branch

    def remove_branch(self, branch: str, *, force: bool = False) -> RemoveResult:
        del force
        raise UnsafeRemovalError(branch, self._skip, "raced")


class TestRacedRemoval:
    def test_a_refusal_after_selection_carries_the_refusal_s_own_reason(
        self, tmp_path: Path
    ) -> None:
        stack = RacingStack(TeardownSkip.UNPUSHED_COMMITS, tmp_path)

        result = Reclaimer(
            stack,
            VmRunner(tmp_path, config=batch_config, disks=frozenset),
            occupied=frozenset,
        ).collect()

        assert skips(result) == [("issue-9", TeardownSkip.UNPUSHED_COMMITS)]


class TestDryRun:
    def test_it_reports_the_same_outcomes_but_removes_nothing(
        self, sc: Scratch
    ) -> None:
        _ = manager(sc).ensure(9, "main")
        slot = manager(sc).ensure_branch("openfix", "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "ad-hoc work")

        result = reclaimer(sc).collect(dry_run=True)

        assert skips(result) == [
            ("issue-9", None),
            ("openfix", TeardownSkip.NOT_MERGED),
        ]
        assert result.dry_run
        assert intact(sc, "issue-9")
        assert intact(sc, "openfix")


class TestEnumeration:
    def test_a_disk_with_no_worktree_is_reclaimed(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        _ = git(sc.repo, "worktree", "remove", str(slot.worktree))

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", None)]
        assert not slot.disk.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")

    def test_a_worktree_with_no_disk_is_reclaimed(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        slot.disk.unlink()

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", None)]
        assert not slot.worktree.exists()

    def test_a_disk_with_no_branch_at_all_is_reclaimed(self, sc: Scratch) -> None:
        leftover = sc.trees / "issue-1602.raw"
        _ = leftover.write_bytes(b"orphaned\n")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-1602", None)]
        assert not leftover.exists()

    def test_a_spent_planning_worktree_is_reclaimed(self, sc: Scratch) -> None:
        """A planning slot is per-invocation scratch that commits nothing, so
        the pid it is named for is all that says a session still owns it."""
        _ = manager(sc).ensure_branch("plan-4321", "main")

        result = reclaimer(sc).collect()

        assert skips(result) == [("plan-4321", None)]
        assert not (sc.trees / "plan-4321").exists()

    def test_a_live_planning_session_keeps_its_worktree(self, sc: Scratch) -> None:
        """A planning VM boots attached, so it never has a dtach socket to see."""
        _ = manager(sc).ensure_branch("plan-4321", "main")

        result = reclaimer(sc, live_pids=(4321,)).collect()

        assert skips(result) == [("plan-4321", TeardownSkip.VM_LIVE)]
        assert intact(sc, "plan-4321")

    def test_a_plan_branch_with_no_pid_falls_back_to_the_socket(
        self, sc: Scratch
    ) -> None:
        _ = manager(sc).ensure_branch("plan-by-hand", "main")
        (sc.root / "plan-by-hand.sock").touch()

        result = reclaimer(sc, live_pids=(4321,)).collect()

        assert skips(result) == [("plan-by-hand", TeardownSkip.VM_LIVE)]

    def test_the_main_repo_and_the_current_branch_are_never_selected(
        self, sc: Scratch
    ) -> None:
        _ = (sc.trees / "main.raw").write_bytes(b"not a slot\n")

        result = reclaimer(sc).collect()

        assert result.outcomes == ()
        assert (sc.trees / "main.raw").exists()
        assert (sc.repo / "README.md").exists()

    def test_the_slot_the_caller_stands_in_is_left_alone(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = manager(sc).ensure(9, "main")
        monkeypatch.chdir(slot.worktree)

        result = Reclaimer(
            manager(sc),
            vms(sc),
            occupied=lambda: occupied_slots(sc.trees, cwds=lambda: (Path.cwd(),)),
        ).collect()

        assert skips(result) == [("issue-9", TeardownSkip.OCCUPIED)]
        assert intact(sc, "issue-9")

    def test_a_worktree_registered_outside_the_root_is_never_selected(
        self, sc: Scratch
    ) -> None:
        elsewhere = sc.root / "elsewhere" / "side"
        _ = git(sc.repo, "worktree", "add", "-q", "-b", "side", str(elsewhere), "main")

        result = reclaimer(sc).collect()

        assert result.outcomes == ()
        assert elsewhere.is_dir()


class TestOccupancy:
    def test_a_slot_a_live_process_stands_in_is_left_alone(self, sc: Scratch) -> None:
        _ = manager(sc).ensure(9, "main")

        result = reclaimer(sc, occupied=("issue-9",)).collect()

        assert skips(result) == [("issue-9", TeardownSkip.OCCUPIED)]
        assert intact(sc, "issue-9")

    def test_a_dry_run_reports_the_same_protection(self, sc: Scratch) -> None:
        _ = manager(sc).ensure(9, "main")

        result = reclaimer(sc, occupied=("issue-9",)).collect(dry_run=True)

        assert skips(result) == [("issue-9", TeardownSkip.OCCUPIED)]
        assert intact(sc, "issue-9")

    def test_protection_holds_when_the_sweep_runs_from_another_worktree(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = manager(sc).ensure(9, "main")
        monkeypatch.chdir(sc.repo)

        result = Reclaimer(
            manager(sc),
            vms(sc),
            occupied=lambda: occupied_slots(sc.trees, cwds=lambda: (slot.worktree,)),
        ).collect()

        assert skips(result) == [("issue-9", TeardownSkip.OCCUPIED)]
        assert intact(sc, "issue-9")

    def test_a_failing_process_table_probe_aborts_the_sweep_too(
        self, sc: Scratch
    ) -> None:
        _ = manager(sc).ensure(9, "main")
        stack = CountingStack(manager(sc))

        def refuse() -> frozenset[str]:
            raise OccupancyError("ps exited 1")

        runner = VmRunner(sc.root, config=batch_config, disks=refuse)

        with pytest.raises(OccupancyError):
            _ = Reclaimer(stack, runner, occupied=frozenset).collect()

        assert stack.removals == 0
        assert intact(sc, "issue-9")

    def test_a_failing_probe_aborts_the_sweep_before_anything_is_removed(
        self, sc: Scratch
    ) -> None:
        _ = manager(sc).ensure(9, "main")
        stack = CountingStack(manager(sc))

        def refuse() -> frozenset[str]:
            raise OccupancyError("lsof exited 1")

        with pytest.raises(OccupancyError):
            _ = Reclaimer(stack, vms(sc), occupied=refuse).collect()

        assert stack.removals == 0
        assert intact(sc, "issue-9")

    def test_an_unmerged_occupied_slot_reports_the_unmerged_refusal(
        self, sc: Scratch
    ) -> None:
        slot = manager(sc).ensure_branch("openfix", "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "ad-hoc work")

        result = reclaimer(sc, occupied=("openfix",)).collect()

        assert skips(result) == [("openfix", TeardownSkip.NOT_MERGED)]


class TestAttachedConsole:
    def test_a_live_vm_process_protects_a_socket_less_slot(self, sc: Scratch) -> None:
        # The 2026-08-22 incident: `dev/vwt` runs vibe attached, so the slot is
        # merged, clean, unclaimed and socket-less while a VM is live in it.
        _ = manager(sc).ensure(9, "main")

        result = reclaimer(sc, disks=("issue-9.raw",)).collect()

        assert skips(result) == [("issue-9", TeardownSkip.VM_LIVE)]
        assert intact(sc, "issue-9")


class TestClaimedSlot:
    def test_an_attached_console_survives_with_no_socket_and_no_proc(
        self, sc: Scratch
    ) -> None:
        slot = manager(sc).ensure(9, "main")
        runner = vms(sc)
        held = Reclaimer(
            manager(sc),
            runner,
            alive=lambda pid: pid == 4242,
            occupied=frozenset,
        )

        _ = runner.claim_path(slot.branch).write_text("4242\n")
        result = held.collect()

        assert skips(result) == [("issue-9", TeardownSkip.CLAIMED)]
        assert intact(sc, "issue-9")

    def test_a_claim_whose_process_is_gone_does_not_protect(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        runner = vms(sc)
        _ = runner.claim_path(slot.branch).write_text("4242\n")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", None)]
        assert not intact(sc, "issue-9")
        assert not runner.claim_path("issue-9").exists()

    def test_an_unreadable_claim_does_not_protect(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        _ = vms(sc).claim_path(slot.branch).write_text("not a pid\n")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", None)]

    def test_the_context_manager_writes_and_clears_the_claim(self, sc: Scratch) -> None:
        runner = vms(sc)

        with runner.claimed("issue-9"):
            assert runner.claim_pid("issue-9") == os.getpid()

        assert runner.claim_pid("issue-9") is None


class TestSwitchedBranch:
    def test_a_worktree_holding_another_branch_is_left_alone(self, sc: Scratch) -> None:
        # The failure that removed a live worktree: every other check asks git
        # about the branch the directory is named for, not the one it holds.
        slot = manager(sc).ensure(9, "main")
        git(slot.worktree, "checkout", "-q", "-b", "unrelated-work")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", TeardownSkip.BRANCH_SWITCHED)]
        assert intact(sc, "issue-9")

    def test_a_detached_head_is_left_alone(self, sc: Scratch) -> None:
        slot = manager(sc).ensure(9, "main")
        git(slot.worktree, "checkout", "-q", "--detach")

        result = reclaimer(sc).collect()

        assert skips(result) == [("issue-9", TeardownSkip.BRANCH_SWITCHED)]
        assert intact(sc, "issue-9")
