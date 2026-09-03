from __future__ import annotations

from typing import Protocol

from batch.models import (
    BatchLabel,
    LabelState,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    VmStatus,
)

SKIPPABLE = frozenset(
    {BatchLabel.QUEUED, BatchLabel.PLANNED, BatchLabel.STUCK, BatchLabel.IMPLEMENTING}
)


class Labels(Protocol):
    def label_state(self, issue_number: int) -> LabelState: ...
    def clear_state(self, issue_number: int) -> None: ...
    def set_state(self, issue_number: int, label: BatchLabel) -> None: ...


class Live(Protocol):
    def status(self, issue: int) -> VmStatus: ...


class Recovery:
    """The two levers a halted batch offers: drop an issue, or try it again.

    Both read the issue's VM liveness as well as its label, so neither is a
    pure GitHub write.

    `skip` accepts a closed issue as long as no merged PR closed it: such a
    label marks work `Teardown` refuses to do, so clearing it strands nothing.
    A merged child's label is exactly what teardown reads, so that one is
    refused. `relaunch` refuses every closed issue.
    """

    def __init__(self, state: Labels, runner: Live) -> None:
        self._state: Labels = state
        self._runner: Live = runner

    def skip(self, issue: int) -> RecoveryResult:
        state = self._state.label_state(issue)
        refusal = self._refuse_skip(issue, state)
        if refusal is None:
            self._state.clear_state(issue)
        return RecoveryResult(
            number=issue, action=RecoveryAction.SKIP, found=state.label, refusal=refusal
        )

    def relaunch(self, issue: int) -> RecoveryResult:
        state = self._state.label_state(issue)
        refusal = self._refuse_relaunch(issue, state)
        if refusal is None:
            self._state.set_state(issue, BatchLabel.PLANNED)
        return RecoveryResult(
            number=issue,
            action=RecoveryAction.RELAUNCH,
            found=state.label,
            refusal=refusal,
        )

    def _refuse_skip(self, issue: int, state: LabelState) -> RecoveryRefusal | None:
        if state.label is None:
            return RecoveryRefusal.NOT_IN_BATCH
        if state.closed and state.closed_by_merge:
            return RecoveryRefusal.MERGED
        if not state.closed and state.label not in SKIPPABLE:
            return RecoveryRefusal.WRONG_STATE
        if self._live(issue):
            return RecoveryRefusal.VM_LIVE
        return None

    def _refuse_relaunch(self, issue: int, state: LabelState) -> RecoveryRefusal | None:
        if state.label is None:
            return RecoveryRefusal.NOT_IN_BATCH
        if state.closed:
            return RecoveryRefusal.CLOSED
        if state.label is not BatchLabel.STUCK:
            return RecoveryRefusal.WRONG_STATE
        if self._live(issue):
            return RecoveryRefusal.VM_LIVE
        return None

    def _live(self, issue: int) -> bool:
        return self._runner.status(issue) is VmStatus.RUNNING
