from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from batch.body import DEFAULT_GUIDANCE, guidance, has_test_plan
from batch.config import BatchConfig
from batch.dashboard import rows
from batch.models import (
    DEFAULT_RAM,
    AlreadyRunningError,
    Batch,
    BatchIssue,
    BatchLabel,
    DashboardRow,
    DebugEntry,
    DebugRefusal,
    HaltReason,
    IssueOutcome,
    LabelState,
    RunResult,
    Slot,
    TeardownResult,
    Verdict,
    VmSession,
    VmStatus,
)
from batch.order import MAIN, base_for, stacked
from batch.polling import SettledTargets
from batch.recovery import Recovery
from batch.text_output import teardown_line
from batch.timings import Timings
from batch.verbs import Verbs
from batch.vm import agent_command, debug_agent_command, session_for

DEFAULT_TIMEOUT = 2 * 60 * 60.0
DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_VERIFY_WAIT = 45 * 60.0


class State(Protocol):
    def batch(
        self, targets: Sequence[int], *, settled: SettledTargets | None = None
    ) -> Batch: ...
    def waiting_targets(self, targets: Sequence[int]) -> tuple[int, ...]: ...
    def set_state(self, issue_number: int, label: BatchLabel) -> None: ...
    def label_state(self, issue_number: int) -> LabelState: ...
    def clear_state(self, issue_number: int) -> None: ...


class Sweeping(Protocol):
    def sweep(self, targets: Sequence[int]) -> TeardownResult: ...


class Stack(Protocol):
    @property
    def mount_root(self) -> Path: ...
    def ensure(self, issue: int, base: str) -> Slot: ...
    def find(self, branch: str, base: str) -> Slot | None: ...
    def missing(self, branch: str) -> tuple[str, ...]: ...


class Runner(Protocol):
    def config_dir(self, issue: int) -> Path: ...
    def write_config(self, config_dir: Path, *, headless: bool = False) -> None: ...
    def launch(self, issue: int, session: VmSession) -> None: ...
    def status(self, issue: int) -> VmStatus: ...
    def log(self, issue: int) -> Path: ...
    def attach_command(self, issue: int) -> tuple[str, ...]: ...
    def debug_command(self, issue: int, session: VmSession) -> tuple[str, ...]: ...


class Verifying(Protocol):
    def verify(
        self, issue_number: int, bases: tuple[str, ...], wait: timedelta | None = None
    ) -> Verdict: ...


def _silent(_message: str) -> None:
    return None


def _spawn(command: Sequence[str], cwd: Path | None) -> int:
    return subprocess.run(command, check=False, cwd=cwd).returncode


class Debugger:
    """Getting into an issue's VM, from the disk alone: no GitHub, so a debug
    session needs neither a token nor a remote."""

    def __init__(
        self,
        stack: Stack,
        runner: Runner,
        *,
        config: BatchConfig,
        model: str | None = None,
        ram: int = DEFAULT_RAM,
        spawn: Callable[[Sequence[str], Path | None], int] = _spawn,
    ) -> None:
        self._stack: Stack = stack
        self._runner: Runner = runner
        self._config: BatchConfig = config
        self._model: str | None = model
        self._ram: int = ram
        self._spawn: Callable[[Sequence[str], Path | None], int] = spawn

    def enter(
        self, issue_number: int, *, fresh: bool = False, dry_run: bool = False
    ) -> DebugEntry:
        """Attach to the issue's VM, booting it first when it has exited.

        Liveness is re-read here, not taken from the rendered row: a frame is
        already stale by the time a key reaches it, and it is read before the
        slot: a live VM is attached to whatever its worktree looks like now.
        The boot is `debug_command`, never `launch_command` — tee would
        truncate the log of the run being debugged. Staging and spawning fail
        as `BOOT_FAILED`: this runs under a key handler, which has nowhere to
        raise.
        """
        attach = self._runner.attach_command(issue_number)
        if self._runner.status(issue_number) is VmStatus.RUNNING:
            return DebugEntry(number=issue_number, command=attach)
        branch = f"issue-{issue_number}"
        slot = self._stack.find(branch, MAIN)
        if slot is None:
            return DebugEntry(
                number=issue_number,
                refusal=DebugRefusal.NO_SLOT,
                missing=self._stack.missing(branch),
            )
        config_dir = self._runner.config_dir(issue_number)
        session = session_for(
            slot,
            mount_root=self._stack.mount_root,
            config_dir=config_dir,
            agent=agent_command(self._config, model=self._model)
            if fresh
            else debug_agent_command(self._config, issue_number, model=self._model),
            ram=self._ram,
        )
        boot = self._runner.debug_command(issue_number, session)
        if dry_run:
            return DebugEntry(number=issue_number, command=attach, boot=boot)
        try:
            self._runner.write_config(config_dir)
            code = self._spawn(boot, session.cwd)
        except OSError:
            code = 1
        if code != 0:
            return DebugEntry(
                number=issue_number, refusal=DebugRefusal.BOOT_FAILED, boot=boot
            )
        return DebugEntry(number=issue_number, command=attach, boot=boot)


class Orchestrator:
    def __init__(
        self,
        state: State,
        stack: Stack,
        runner: Runner,
        verifier: Verifying,
        teardown: Sweeping,
        *,
        config: BatchConfig,
        report: Callable[[str], None] = _silent,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        verify_wait: float = DEFAULT_VERIFY_WAIT,
        model: str | None = None,
        ram: int = DEFAULT_RAM,
        spawn: Callable[[Sequence[str], Path | None], int] = _spawn,
    ) -> None:
        self._state: State = state
        self._stack: Stack = stack
        self._runner: Runner = runner
        self._verifier: Verifying = verifier
        self._teardown: Sweeping = teardown
        self._config: BatchConfig = config
        self.report: Callable[[str], None] = report
        self._sleep: Callable[[float], None] = sleep
        self._monotonic: Callable[[], float] = monotonic
        self._timeout: float = timeout
        self._poll_interval: float = poll_interval
        self._verify_wait: float = verify_wait
        self._model: str | None = model
        self._debugger: Debugger = Debugger(
            stack, runner, config=config, model=model, ram=ram, spawn=spawn
        )
        self.timings: Timings = Timings(monotonic)
        self._settled: SettledTargets = SettledTargets()

    def run(self, targets: Sequence[int]) -> RunResult:
        """Whatever merges during the last iteration is swept on the way out; a
        finished batch is never run again, so no later pass would reclaim it."""
        outcomes: list[IssueOutcome] = []
        while True:
            self._sweep(targets)
            batch = self._state.batch(targets)
            outcome = self._step(batch)
            if outcome is not None:
                outcomes.append(outcome)
            if outcome is None or outcome.halt is not None:
                self._sweep(targets)
                return RunResult(
                    targets=tuple(targets),
                    outcomes=tuple(outcomes),
                    anomalies=batch.anomalies,
                )

    def fetch(self, targets: Sequence[int]) -> Batch:
        """The only part of a dashboard frame that costs GitHub API points."""
        return self._state.batch(targets, settled=self._settled)

    def render(
        self, batch: Batch, selected: int | None = None
    ) -> tuple[DashboardRow, ...]:
        return rows(batch, self._runner, self.timings, selected)

    def snapshot(
        self, targets: Sequence[int], selected: int | None = None
    ) -> tuple[DashboardRow, ...]:
        return self.render(self.fetch(targets), selected)

    def waiting_targets(self, targets: Sequence[int]) -> tuple[int, ...]:
        """Which targets still hold a queued issue: what a watching run waits on."""
        return self._state.waiting_targets(targets)

    def verbs(self, targets: Sequence[int]) -> Verbs:
        """The dashboard's keys, over the same collaborators this run drives, so a
        skip the developer types and a label the run reads cannot disagree."""
        return Verbs(
            targets,
            self._state,
            self._stack,
            self._runner,
            Recovery(self._state, self._runner),
            config=self._config,
            model=self._model,
        )

    def enter(
        self, issue_number: int, *, fresh: bool = False, dry_run: bool = False
    ) -> DebugEntry:
        return self._debugger.enter(issue_number, fresh=fresh, dry_run=dry_run)

    def _sweep(self, targets: Sequence[int]) -> None:
        for outcome in self._teardown.sweep(targets).outcomes:
            self.report(teardown_line(outcome))

    def _step(self, batch: Batch) -> IssueOutcome | None:
        stuck = _first(batch, BatchLabel.STUCK)
        if stuck is not None:
            return self._blocked(batch, stuck)
        started = _first(batch, BatchLabel.IMPLEMENTING)
        if started is not None:
            return self._adopt(batch, started)
        nxt = _next_planned(batch)
        return None if nxt is None else self._drive(batch, nxt)

    def _drive(self, batch: Batch, issue: BatchIssue) -> IssueOutcome:
        base = base_for(batch, issue)
        slot = self._stack.ensure(issue.number, base)
        self.report(f"#{issue.number} {slot.branch} on {base}")
        config_dir = self._runner.config_dir(issue.number)
        self._runner.write_config(config_dir, headless=True)
        plan = has_test_plan(issue.body)
        session = session_for(
            slot,
            mount_root=self._stack.mount_root,
            config_dir=config_dir,
            agent=agent_command(
                self._config,
                issue=issue.number,
                guidance=None if plan else guidance(issue.body) or DEFAULT_GUIDANCE,
                base=base,
                model=self._model,
                impl_only=plan,
                headless=True,
                predecessors=[other.number for other in stacked(batch, issue)],
            ),
        )
        self._state.set_state(issue.number, BatchLabel.IMPLEMENTING)
        try:
            self._runner.launch(issue.number, session)
        except AlreadyRunningError:
            return self._halt(issue, base, HaltReason.VM_ALREADY_RUNNING)
        self.timings.start(issue.number)
        return self._finish(issue, base, batch.targets)

    def _adopt(self, batch: Batch, issue: BatchIssue) -> IssueOutcome:
        """A live VM is still doing the work, so wait it out rather than relaunch.

        An orphan's branch, worktree, and disk are left untouched for inspection.
        """
        base = base_for(batch, issue)
        if self._runner.status(issue.number) is not VmStatus.RUNNING:
            return self._halt(issue, base, HaltReason.ORPHANED_VM)
        self.report(f"#{issue.number} already implementing; waiting for its VM")
        self.timings.start(issue.number)
        return self._finish(issue, base, batch.targets)

    def _finish(
        self, issue: BatchIssue, base: str, targets: Sequence[int]
    ) -> IssueOutcome:
        exited = self._await_exit(issue.number)
        self.timings.finish(issue.number)
        if not exited:
            return self._halt(issue, base, HaltReason.TIMED_OUT)
        verdict = self._verifier.verify(
            issue.number,
            self._accepted_bases(issue, base, targets),
            timedelta(seconds=self._verify_wait),
        )
        if not verdict.ok:
            return self._halt(issue, base, HaltReason.VERIFICATION_FAILED, verdict)
        self._state.set_state(issue.number, BatchLabel.READY_FOR_REVIEW)
        self.report(f"#{issue.number} ready for review")
        return IssueOutcome(
            number=issue.number,
            base=base,
            state=BatchLabel.READY_FOR_REVIEW,
            verdict=verdict,
        )

    def _accepted_bases(
        self, issue: BatchIssue, base: str, targets: Sequence[int]
    ) -> tuple[str, ...]:
        """A predecessor merging while this issue ran retargets its PR, so the base
        derived after the wait is as legitimate as the one it launched on."""
        current = base_for(self._state.batch(targets), issue)
        return (base,) if current == base else (base, current)

    def _blocked(self, batch: Batch, issue: BatchIssue) -> IssueOutcome:
        """Advancing would cut the next issue from work that was never verified."""
        self.report(f"#{issue.number} is stuck; skip or relaunch it before rerunning")
        return IssueOutcome(
            number=issue.number,
            base=base_for(batch, issue),
            state=BatchLabel.STUCK,
            halt=HaltReason.STUCK_ISSUE,
        )

    def _halt(
        self,
        issue: BatchIssue,
        base: str,
        reason: HaltReason,
        verdict: Verdict | None = None,
    ) -> IssueOutcome:
        self._state.set_state(issue.number, BatchLabel.STUCK)
        self.report(f"#{issue.number} stuck: {reason}; halting the batch")
        return IssueOutcome(
            number=issue.number,
            base=base,
            state=BatchLabel.STUCK,
            verdict=verdict,
            halt=reason,
        )

    def _await_exit(self, issue: int) -> bool:
        deadline = self._monotonic() + self._timeout
        while self._runner.status(issue) is VmStatus.RUNNING:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(self._poll_interval, remaining))
        return True


def _first(batch: Batch, label: BatchLabel) -> BatchIssue | None:
    return next((issue for issue in batch.issues if issue.state is label), None)


def _next_planned(batch: Batch) -> BatchIssue | None:
    return _first(batch, BatchLabel.PLANNED)
