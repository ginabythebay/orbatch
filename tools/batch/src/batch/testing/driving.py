"""Fakes standing in for the collaborators a dashboard drives."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import override

from batch.dashboard import Driving, Keying
from batch.models import (
    Batch,
    DashboardRow,
    DebugEntry,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    RunResult,
)
from ghgql.labels import BatchLabel
from ghgql.transport import RateLimit

BOOT = ("dtach", "-n", "/sockets/issue.sock", "vibe")


class FakeDriver(Driving):
    """`run` blocks until `released` is set, so a test can hold a run live."""

    def __init__(
        self,
        *rows: DashboardRow,
        live: Sequence[int] = (),
        refused: DebugEntry | None = None,
        result: RunResult | None = None,
        rate_limit: RateLimit | None = None,
    ) -> None:
        self.rows: tuple[DashboardRow, ...] = rows
        self.rate_limit: RateLimit | None = rate_limit
        self.live: set[int] = set(live)
        self.refused: DebugEntry | None = refused
        self.booted: list[int] = []
        self.result: RunResult | None = result
        self.released: threading.Event = threading.Event()
        self.running: threading.Event = threading.Event()
        self.runs: int = 0
        self.asked: list[tuple[int, ...]] = []
        self.asked_to_enter: list[int] = []
        self.selected: list[int | None] = []

    @override
    def run(self, targets: Sequence[int]) -> RunResult:
        self.runs += 1
        self.running.set()
        if self.result is None:
            _ = self.released.wait(timeout=10.0)
            return RunResult(targets=tuple(targets), outcomes=())
        return self.result

    @override
    def fetch(self, targets: Sequence[int]) -> Batch:
        self.asked.append(tuple(targets))
        return Batch(targets=tuple(targets), issues=(), rate_limit=self.rate_limit)

    @override
    def render(
        self, batch: Batch, selected: int | None = None
    ) -> tuple[DashboardRow, ...]:
        self.selected.append(selected)
        return self.rows

    @override
    def enter(self, issue_number: int) -> DebugEntry:
        self.asked_to_enter.append(issue_number)
        if self.refused is not None:
            return self.refused
        attach = ("dtach", "-a", f"/sockets/issue-{issue_number}.sock", "-r", "none")
        if issue_number in self.live:
            return DebugEntry(number=issue_number, command=attach)
        self.booted.append(issue_number)
        return DebugEntry(number=issue_number, command=attach, boot=BOOT)


class FakeVerbs(Keying):
    def __init__(self, refusal: RecoveryRefusal | None = None) -> None:
        self.refusal: RecoveryRefusal | None = refusal
        self.calls: list[tuple[str, int]] = []

    def _result(self, action: RecoveryAction, issue: int) -> RecoveryResult:
        self.calls.append((action, issue))
        return RecoveryResult(
            number=issue,
            action=action,
            found=BatchLabel.STUCK,
            refusal=self.refusal,
        )

    @override
    def rework(self, issue: int) -> RecoveryResult:
        return self._result(RecoveryAction.REWORK, issue)

    @override
    def skip(self, issue: int) -> RecoveryResult:
        return self._result(RecoveryAction.SKIP, issue)

    @override
    def relaunch(self, issue: int) -> RecoveryResult:
        return self._result(RecoveryAction.RELAUNCH, issue)
