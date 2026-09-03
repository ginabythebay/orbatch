from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from batch.config import BatchConfig
from batch.models import (
    Batch,
    BatchIssue,
    BatchLabel,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    Slot,
    VmSession,
    VmStatus,
)
from batch.order import UNSTARTED, base_under
from batch.vm import agent_command, session_for

REWORKABLE = frozenset({BatchLabel.READY_FOR_REVIEW, BatchLabel.STUCK})


class State(Protocol):
    def batch(self, targets: Sequence[int]) -> Batch: ...


class Stack(Protocol):
    @property
    def mount_root(self) -> Path: ...
    def ensure(self, issue: int, base: str) -> Slot: ...


class Runner(Protocol):
    def config_dir(self, issue: int) -> Path: ...
    def write_config(self, config_dir: Path, *, headless: bool = False) -> None: ...
    def launch(self, issue: int, session: VmSession) -> None: ...
    def status(self, issue: int) -> VmStatus: ...


class Recovering(Protocol):
    def skip(self, issue: int) -> RecoveryResult: ...
    def relaunch(self, issue: int) -> RecoveryResult: ...


class Verbs:
    """The three levers the dashboard offers on one issue: rework, skip, relaunch."""

    def __init__(
        self,
        targets: Sequence[int],
        state: State,
        stack: Stack,
        runner: Runner,
        recovery: Recovering,
        *,
        config: BatchConfig,
        model: str | None = None,
    ) -> None:
        self._targets: tuple[int, ...] = tuple(targets)
        self._state: State = state
        self._stack: Stack = stack
        self._runner: Runner = runner
        self._recovery: Recovering = recovery
        self._config: BatchConfig = config
        self._model: str | None = model

    def skip(self, issue: int) -> RecoveryResult:
        return self._recovery.skip(issue)

    def relaunch(self, issue: int) -> RecoveryResult:
        return self._recovery.relaunch(issue)

    def rework(self, issue: int) -> RecoveryResult:
        batch = self._state.batch(self._targets)
        found = _find(batch, issue)
        refusal = self._refuse(issue, found)
        if refusal is None and found is not None:
            self._launch(batch, found)
        return RecoveryResult(
            number=issue,
            action=RecoveryAction.REWORK,
            found=None if found is None else found.state,
            refusal=refusal,
        )

    def _refuse(self, issue: int, found: BatchIssue | None) -> RecoveryRefusal | None:
        if found is None:
            return RecoveryRefusal.NOT_IN_BATCH
        if found.state in UNSTARTED:
            return RecoveryRefusal.NO_BRANCH
        if found.state not in REWORKABLE:
            return RecoveryRefusal.WRONG_STATE
        if self._runner.status(issue) is VmStatus.RUNNING:
            return RecoveryRefusal.VM_LIVE
        return None

    def _launch(self, batch: Batch, issue: BatchIssue) -> None:
        base = base_under(batch, issue)
        slot = self._stack.ensure(issue.number, base)
        config_dir = self._runner.config_dir(issue.number)
        self._runner.write_config(config_dir)
        self._runner.launch(
            issue.number,
            session_for(
                slot,
                mount_root=self._stack.mount_root,
                config_dir=config_dir,
                agent=agent_command(
                    self._config,
                    issue=issue.number,
                    base=base,
                    rework=True,
                    model=self._model,
                ),
            ),
        )


def _find(batch: Batch, issue: int) -> BatchIssue | None:
    return next((other for other in batch.issues if other.number == issue), None)
