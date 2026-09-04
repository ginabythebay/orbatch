from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from batch.models import (
    ReclaimOutcome,
    ReclaimResult,
    RemoveResult,
    TeardownSkip,
    UnsafeRemovalError,
    VmStatus,
)
from batch.occupancy import occupied_slots
from batch.order import MAIN

PLAN_PREFIX = "plan-"


def planning_pid(branch: str) -> int | None:
    """The process a planning slot is named for, or None for any other slot."""
    rest = branch.removeprefix(PLAN_PREFIX)
    if branch == rest or not rest.isdigit():
        return None
    return int(rest)


def _alive(pid: int) -> bool:
    """Signal 0 probes without delivering; another user's process is still live."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Slots(Protocol):
    @property
    def worktree_root(self) -> Path: ...
    def slot_names(self) -> tuple[str, ...]: ...
    def merged_into(self, branch: str, base: str) -> bool: ...
    def dirty(self, branch: str) -> bool: ...
    def checked_out(self, branch: str) -> str | None: ...
    def remove_branch(self, branch: str, *, force: bool = False) -> RemoveResult: ...


class Vms(Protocol):
    def status_branch(self, branch: str) -> VmStatus: ...
    def claim_pid(self, branch: str) -> int | None: ...
    def release_claim(self, branch: str) -> None: ...


class Reclaimer:
    """Reclaims slots from the disk rather than from an epic's children.

    Every other sweep starts at a GitHub issue, so a slot cut from an ad-hoc
    branch, a spent planning worktree, or one whose batch label was cleared, is
    unreachable forever. This pass asks git, the run root, and the process
    table instead, and so makes no GitHub call at all.
    """

    def __init__(
        self,
        stack: Slots,
        runner: Vms,
        *,
        base: str = MAIN,
        alive: Callable[[int], bool] = _alive,
        occupied: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._stack: Slots = stack
        self._runner: Vms = runner
        self._base: str = base
        self._alive: Callable[[int], bool] = alive
        self._occupied: Callable[[], frozenset[str]] = occupied or (
            lambda: occupied_slots(stack.worktree_root)
        )

    def collect(self, *, dry_run: bool = False) -> ReclaimResult:
        occupied = self._occupied()
        outcomes = [
            self._reclaim(branch, occupied, dry_run=dry_run)
            for branch in self._stack.slot_names()
        ]
        return ReclaimResult(outcomes=tuple(outcomes), dry_run=dry_run)

    def _reclaim(
        self, branch: str, occupied: frozenset[str], *, dry_run: bool
    ) -> ReclaimOutcome:
        skip = self._refuse(branch, occupied)
        if skip is not None or dry_run:
            return ReclaimOutcome(branch=branch, skip=skip)
        try:
            _ = self._stack.remove_branch(branch)
        except UnsafeRemovalError as exc:
            return ReclaimOutcome(branch=branch, skip=exc.skip)
        self._runner.release_claim(branch)
        return ReclaimOutcome(branch=branch)

    def _refuse(self, branch: str, occupied: frozenset[str]) -> TeardownSkip | None:
        # Before anything else: a slot whose worktree holds some other branch is
        # not the slot the checks below reason about. Every one of them asks git
        # about the branch the *directory* is named for.
        occupant = self._stack.checked_out(branch)
        if occupant is not None and occupant != branch:
            return TeardownSkip.BRANCH_SWITCHED
        if not self._stack.merged_into(branch, self._base):
            return TeardownSkip.NOT_MERGED
        if self._claimed(branch):
            return TeardownSkip.CLAIMED
        if self._live(branch):
            return TeardownSkip.VM_LIVE
        if branch in occupied:
            return TeardownSkip.OCCUPIED
        if self._stack.dirty(branch):
            return TeardownSkip.DIRTY_WORKTREE
        return None

    def _claimed(self, branch: str) -> bool:
        """Covers the window a process probe cannot: a slot claimed before its VM
        boots, or between a crash and the next launch, is held all the same."""
        pid = self._runner.claim_pid(branch)
        return pid is not None and self._alive(pid)

    def _live(self, branch: str) -> bool:
        """A planning VM boots attached and so has no dtach socket; the pid its
        slot is named for is the only thing that says the session is still up."""
        pid = planning_pid(branch)
        if pid is not None:
            return self._alive(pid)
        return self._runner.status_branch(branch) is VmStatus.RUNNING
