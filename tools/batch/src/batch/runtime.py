"""Everything a batch verb needs, resolved from a repo path instead of a click
context, so a host other than the CLI can drive a run."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from time import sleep

from batch.config import BatchConfig, load_config
from batch.dashboard import Driving, Keying
from batch.github.client import BatchGitHub
from batch.models import (
    DEFAULT_RAM,
    ApproveResult,
    QueueResult,
    RunResult,
    UnsafeRemovalError,
)
from batch.orchestrator import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_WAIT,
    Orchestrator,
)
from batch.order import MAIN
from batch.reclaim import Reclaimer
from batch.recovery import Recovery
from batch.stack import StackManager, worktree_root
from batch.state import BatchState
from batch.teardown import Teardown
from batch.text_output import print_run_result, targets_line
from batch.verbs import Verbs
from batch.verify import Verifier
from batch.vm import (
    DEFAULT_RUN_ROOT,
    VmRunner,
    plan_batch_command,
    plan_slot_branch,
    scoped_run_root,
    session_for,
)
from batch.watch import DEFAULT_WATCH_INTERVAL
from batch.watch import watch as watch_passes
from ghgql.repo import repo
from ghgql.transport import GitHubGraphQL, GitHubTransport


def _silent(_line: str) -> None:
    return None


def _spawn(command: Sequence[str], cwd: Path | None) -> int:
    return subprocess.run(command, check=False, cwd=cwd).returncode


@dataclass(frozen=True)
class PlanOutcome:
    command: tuple[str, ...]
    returncode: int
    refusal: str | None = None


@dataclass(frozen=True)
class Drive:
    """One run's collaborators, as a dashboard wants them."""

    orchestrator: Driving
    verbs: Keying
    run: Callable[[], RunResult]


class Runtime:
    def __init__(self, repo: Path, config: BatchConfig, run_root: Path) -> None:
        self.repo: Path = repo
        self.config: BatchConfig = config
        self.run_root: Path = run_root
        self.prog: str = config.commands.cli

    @classmethod
    def load(cls, repo: Path, run_root: Path = DEFAULT_RUN_ROOT) -> Runtime:
        """Raises `ConfigError` when the repo carries no usable `batch.toml`."""
        config = load_config(repo)
        return cls(repo, config, scoped_run_root(run_root, config.slug).expanduser())

    @cached_property
    def _client(self) -> BatchGitHub:
        return BatchGitHub(GitHubGraphQL(GitHubTransport()), repo(self.repo))

    def state(self) -> BatchState:
        return BatchState(self._client)

    def stack(self) -> StackManager:
        return StackManager(self.repo, seed_image=self.config.seed_image)

    def runner(self) -> VmRunner:
        return VmRunner(
            self.run_root,
            worktree_root=lambda: worktree_root(self.repo),
            config=lambda: self.config,
        )

    def orchestrator(
        self,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        verify_wait: float = DEFAULT_VERIFY_WAIT,
        report: Callable[[str], None] = _silent,
    ) -> Orchestrator:
        state = self.state()
        stack = self.stack()
        runner = self.runner()
        verifier = Verifier(self._client)
        return Orchestrator(
            state,
            stack,
            runner,
            verifier,
            Teardown(state, stack, runner, verifier),
            config=self.config,
            report=report,
            timeout=timeout,
            poll_interval=poll_interval,
            verify_wait=verify_wait,
            model=model,
        )

    def teardown(self) -> Teardown:
        state = self.state()
        verifier = Verifier(self._client)
        return Teardown(state, self.stack(), self.runner(), verifier)

    def reclaimer(self) -> Reclaimer:
        return Reclaimer(self.stack(), self.runner())

    def recovery(self) -> Recovery:
        return Recovery(self.state(), self.runner())

    def verbs(self, targets: Sequence[int], model: str | None = None) -> Verbs:
        state = self.state()
        runner = self.runner()
        return Verbs(
            targets,
            state,
            self.stack(),
            runner,
            Recovery(state, runner),
            config=self.config,
            model=model,
        )

    def queue(self, targets: Sequence[int]) -> QueueResult:
        return self.state().queue(None, targets)

    def unqueue(self, targets: Sequence[int]) -> QueueResult:
        return self.state().unqueue(None, targets)

    def approve(self, targets: Sequence[int]) -> ApproveResult:
        return self.state().approve(None, targets)

    def fast_track(self, targets: Sequence[int]) -> ApproveResult:
        return self.state().fast_track(None, targets)

    def drive(self, targets: Sequence[int], report: Callable[[str], None]) -> Drive:
        """A run over every `batch run` default; `report` sees the narration."""
        orchestrator = self.orchestrator(report=report)
        return Drive(
            orchestrator,
            orchestrator.verbs(targets),
            lambda: watch(
                orchestrator,
                targets,
                DEFAULT_WATCH_INTERVAL,
                prog=self.prog,
                report=report,
                echo=False,
            ),
        )

    def plan_session(
        self,
        targets: Sequence[int],
        *,
        model: str | None = None,
        ram: int = DEFAULT_RAM,
        dry_run: bool = False,
        spawn: Callable[[Sequence[str], Path | None], int] = _spawn,
    ) -> PlanOutcome:
        """Boot a planning VM on a slot of this process's own, and reclaim it
        after. Raises `StaleSlotError` when the slot holds commits.

        The slot is scratch: a session that left work in it is not reclaimed,
        and the refusal comes back in the outcome for the caller to say."""
        branch = plan_slot_branch(os.getpid())
        manager = self.stack()
        runner = self.runner()
        config_dir = runner.named_config_dir(branch)
        slot = manager.ensure_current(branch, MAIN)
        try:
            if not dry_run:
                runner.write_config(config_dir)
            session = session_for(
                slot,
                mount_root=manager.mount_root,
                config_dir=config_dir,
                agent=plan_batch_command(self.config, targets, model),
                ram=ram,
            )
            command = runner.vibe_command(session)
            returncode = 0 if dry_run else spawn(command, session.cwd)
        finally:
            _ = runner.clean_config(config_dir)
            refusal = self._reclaim_plan_slot(manager, branch)
        return PlanOutcome(command, returncode, refusal)

    def _reclaim_plan_slot(self, manager: StackManager, branch: str) -> str | None:
        try:
            _ = manager.remove_branch(branch)
        except UnsafeRemovalError as exc:
            return f"{exc}; `{self.prog} gc` once you are done with it."
        return None


class _QuietRepeats:
    """A sweep re-reports every issue it refused to clean, once per pass."""

    def __init__(self, echo: Callable[[str], None]) -> None:
        self._echo: Callable[[str], None] = echo
        self._said: set[str] = set()

    def __call__(self, line: str) -> None:
        if line in self._said:
            return
        self._said.add(line)
        self._echo(line)

    def reset(self) -> None:
        self._said.clear()


def watch(
    orchestrator: Orchestrator,
    targets: Sequence[int],
    interval: float,
    *,
    prog: str,
    report: Callable[[str], None],
    echo: bool = True,
) -> RunResult:
    """`echo` is off under a dashboard, which renders the passes itself; the
    wait goes through `report`, which the dashboard buffers as narration."""
    original = orchestrator.report
    quiet = _QuietRepeats(original)
    orchestrator.report = quiet

    def show(one_pass: RunResult) -> None:
        quiet.reset()
        if echo:
            print_run_result(one_pass, sys.stdout, prog=prog)

    try:
        return watch_passes(
            lambda: orchestrator.run(targets),
            lambda: orchestrator.waiting_targets(targets),
            sleep=sleep,
            report=show,
            announce=lambda pending: report(
                f"Waiting for queued issues under {targets_line(pending)}."
            ),
            interval=interval,
        )
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    finally:
        orchestrator.report = original
