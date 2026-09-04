from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from batch.models import (
    BatchIssue,
    RemoveResult,
    TeardownOutcome,
    TeardownResult,
    TeardownSkip,
    UnsafeRemovalError,
    VmStatus,
)


class Labels(Protocol):
    def finished(self, targets: Sequence[int]) -> tuple[BatchIssue, ...]: ...
    def clear_state(self, issue_number: int) -> None: ...


class Slots(Protocol):
    def remove(self, issue: int, *, force: bool = False) -> RemoveResult: ...


class Configs(Protocol):
    def clean(self, issue: int) -> bool: ...
    def status(self, issue: int) -> VmStatus: ...


class Merges(Protocol):
    def merged(self, issue_number: int) -> bool: ...


class Teardown:
    """Reclaims what a merged issue leaves behind: worktree, branch, disk, config.

    The batch label is what marks an issue as not-yet-cleaned, so it is cleared
    last and an interrupted sweep simply retries on the next pass.
    """

    def __init__(
        self, state: Labels, stack: Slots, runner: Configs, merges: Merges
    ) -> None:
        self._state: Labels = state
        self._stack: Slots = stack
        self._runner: Configs = runner
        self._merges: Merges = merges

    def sweep(self, targets: Sequence[int]) -> TeardownResult:
        outcomes = [
            self._clean(issue.number) for issue in self._state.finished(targets)
        ]
        return TeardownResult(targets=tuple(targets), outcomes=tuple(outcomes))

    def _clean(self, issue_number: int) -> TeardownOutcome:
        skip = self._refuse(issue_number)
        if skip is not None:
            return TeardownOutcome(number=issue_number, skip=skip)
        try:
            _ = self._stack.remove(issue_number)
        except UnsafeRemovalError as exc:
            return TeardownOutcome(number=issue_number, skip=exc.skip)
        _ = self._runner.clean(issue_number)
        self._state.clear_state(issue_number)
        return TeardownOutcome(number=issue_number)

    def _refuse(self, issue_number: int) -> TeardownSkip | None:
        if not self._merges.merged(issue_number):
            return TeardownSkip.NOT_MERGED
        if self._runner.status(issue_number) is VmStatus.RUNNING:
            return TeardownSkip.VM_LIVE
        return None
