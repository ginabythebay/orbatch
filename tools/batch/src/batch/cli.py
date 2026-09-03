from __future__ import annotations

import functools
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from time import sleep
from typing import cast

import click
from click.decorators import FC

from batch.agent import PlanningAgent
from batch.awake import awake
from batch.completion import source_with_alias
from batch.config import BatchConfig, ConfigError, load_config
from batch.github.client import BatchGitHub
from batch.lock import run_lock
from batch.models import (
    DEFAULT_RAM,
    AccountCheckError,
    Alignment,
    AlreadyRunningError,
    ApproveResult,
    Batch,
    EmptyTokenError,
    Epic,
    KeychainError,
    QueueResult,
    RecoveryResult,
    RunResult,
    SkippedIssue,
    Slot,
    StaleSlotError,
    UnsafeRemovalError,
    VmFacts,
    VmSession,
    VmStatus,
    WrongAccountError,
)
from batch.orchestrator import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_WAIT,
    Debugger,
    Orchestrator,
)
from batch.order import MAIN
from batch.reclaim import Reclaimer
from batch.recovery import Recovery
from batch.stack import StackManager, main_repo
from batch.state import BatchState
from batch.teardown import Teardown
from batch.text_output import (
    debug_line,
    print_anomalies,
    print_batch_table,
    print_next_issue,
    print_plan_written,
    print_reclaim_result,
    print_recovery_result,
    print_run_result,
    print_teardown_result,
    print_verdict,
    targets_line,
)
from batch.tui.app import run_dashboard
from batch.verbs import Verbs
from batch.verify import Verifier
from batch.vm import (
    DEFAULT_RUN_ROOT,
    VmRunner,
    agent_command,
    plan_batch_command,
    plan_slot_branch,
    session_for,
)
from batch.watch import DEFAULT_WATCH_INTERVAL
from batch.watch import watch as watch_passes
from ghgql.repo import repo
from ghgql.transport import GitHubGraphQL, GitHubTransport

SCRIPT_NAME = "batch"
FALLBACK_PROG = SCRIPT_NAME
COMPLETE_VAR = "_BATCH_COMPLETE"


def main(args: Sequence[str] | None = None) -> None:
    prog_name = _prog_name()
    source = source_with_alias(cli, prog_name, COMPLETE_VAR, SCRIPT_NAME)
    if source is not None:
        click.echo(source, nl=False)
        raise SystemExit(0)
    try:
        cli(args=args, prog_name=prog_name, complete_var=COMPLETE_VAR)
    except (
        AccountCheckError,
        EmptyTokenError,
        KeychainError,
        WrongAccountError,
    ) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


def _prog_name() -> str:
    """click wants a program name before any context exists, so this read cannot
    be the lazy one; falling back keeps an unreadable config to a generic usage
    line rather than a failure for commands that never need the config. No
    context means no `--repo` either, so a usage error from outside a checkout
    names the script rather than the configured wrapper."""
    try:
        return load_config(main_repo()).commands.cli
    except (ConfigError, subprocess.CalledProcessError):
        return FALLBACK_PROG


def _drift(slot: Slot, base: str) -> str:
    relation = (
        "unrelated to" if slot.alignment is Alignment.UNRELATED else slot.alignment
    )
    return f"{slot.branch} is {relation} {base}"


def _targets_arg(f: FC) -> FC:
    return click.argument("targets", type=int, nargs=-1, required=True)(f)


_CONFIG_KEY = "batch.config"
_REPO_KEY = "batch.repo"


def _main_repo(ctx: click.Context) -> Path:
    try:
        return main_repo(cast("Path | None", ctx.meta.get(_REPO_KEY)))
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            "Not inside a git checkout; run batch from the repository or pass --repo."
        ) from exc


def _resolve_config(ctx: click.Context) -> BatchConfig:
    """The repo's `batch.toml`, read once per invocation and cached.

    Cached on `ctx.meta` rather than `ctx.obj`, which the isinstance
    dispatch in the other resolvers already claims for injected fakes.
    """
    cached = ctx.meta.get(_CONFIG_KEY)
    if isinstance(cached, BatchConfig):
        return cached
    try:
        config = load_config(_main_repo(ctx))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.meta[_CONFIG_KEY] = config
    return config


def _prog(ctx: click.Context) -> str:
    """Falls back for the same reason `_prog_name` does: naming the wrapper in a
    remedy must not be what makes a command demand a readable config."""
    try:
        return _resolve_config(ctx).commands.cli
    except click.ClickException:
        return FALLBACK_PROG


def _runner(ctx: click.Context, root: Path) -> VmRunner:
    return VmRunner(root, config=lambda: _resolve_config(ctx))


def _resolve_state(ctx: click.Context) -> BatchState:
    state = cast("BatchState | None", ctx.obj)
    if state is None:
        try:
            state = BatchState(BatchGitHub(GitHubGraphQL(GitHubTransport()), repo()))
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        ctx.obj = state
    return state


def _pass_state(f: Callable[..., None]) -> Callable[..., None]:
    """Hand the command its batch state, built on first use.

    Resolved when the command body runs, not in the group callback, so
    `batch <command> --help` needs neither a checkout nor a token.
    """

    @click.pass_context
    @functools.wraps(f)
    def wrapper(ctx: click.Context, *args: object, **kwargs: object) -> None:
        try:
            f(_resolve_state(ctx), *args, **kwargs)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


@click.group()
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory to resolve the repository from, for callers outside a checkout.",
)
@click.pass_context
def cli(ctx: click.Context, repo: Path | None) -> None:
    """Drive a batch through the batch workflow.

    Every verb takes a list of targets, each an epic — which contributes its
    children, in sub-issue order — or a standalone issue, which contributes
    itself. Stack order follows the order the targets are named.
    """
    ctx.meta[_REPO_KEY] = repo


def _optional_targets_arg(f: FC) -> FC:
    return click.argument("targets", type=int, nargs=-1)(f)


def _epic_option(f: FC) -> FC:
    return click.option(
        "--epic",
        "epic_number",
        type=int,
        default=None,
        help="Act on the epic's children, checking each named number against it.",
    )(f)


def _echo_epic(epic: Epic | None) -> None:
    if epic is not None:
        click.echo(f"Epic #{epic.number} {epic.title}")


def _report(done: str, todo: str, result: QueueResult) -> None:
    _echo_epic(result.epic)
    if result.labeled:
        numbers = ", ".join(f"#{number}" for number in result.labeled)
        click.echo(f"{done} {numbers}")
    else:
        click.echo(f"Nothing to {todo}.")
    _report_skipped(result.skipped)


def _report_skipped(skipped: Sequence[SkippedIssue]) -> None:
    closed = 0
    for item in skipped:
        if item.reason == "closed":
            closed += 1
        else:
            click.echo(f"Skipped #{item.number} ({item.reason})", err=True)
    if closed:
        plural = "issue" if closed == 1 else "issues"
        click.echo(f"Skipped {closed} closed {plural}.", err=True)


def _vm_facts(batch: Batch, runner: VmRunner) -> dict[int, VmFacts]:
    facts: dict[int, VmFacts] = {}
    for issue in batch.issues:
        log = runner.log(issue.number)
        config_dir = runner.config_dir(issue.number)
        found = VmFacts(
            live=runner.status(issue.number) is VmStatus.RUNNING,
            log=log if log.exists() else None,
            config_dir=config_dir if config_dir.is_dir() else None,
        )
        if found.any:
            facts[issue.number] = found
    return facts


@cli.command()
@_targets_arg
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Also show every skipped child and the local VM facts.",
)
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the per-issue sockets, logs, and staged configs.",
)
@_pass_state
def status(
    state: BatchState, targets: tuple[int, ...], verbose: bool, run_root: Path
) -> None:
    """Show every batch issue the targets name, in stack order."""
    ctx = click.get_current_context()
    batch = state.batch(targets)
    facts = _vm_facts(batch, _runner(ctx, run_root.expanduser())) if verbose else None
    print_batch_table(batch, sys.stdout, facts, prog=_prog(ctx))


@cli.command()
@_optional_targets_arg
@_epic_option
@_pass_state
def queue(state: BatchState, targets: tuple[int, ...], epic_number: int | None) -> None:
    """Label open unlabeled issues 'queued'."""
    _report("Queued", "queue", state.queue(epic_number, targets))


def _guidance_option(f: FC) -> FC:
    return click.option(
        "--guidance",
        default=None,
        help="Test guidance to write to each approved issue's body.",
    )(f)


def _report_approved(result: ApproveResult) -> None:
    _echo_epic(result.epic)
    if result.approved:
        numbers = ", ".join(f"#{number}" for number in result.approved)
        click.echo(f"Approved {numbers}")
    else:
        click.echo("Nothing to approve.")
    _report_skipped(result.skipped)
    for number in result.guidance_refused:
        click.echo(f"#{number} already has a Test Plan; guidance not written", err=True)


@cli.command()
@_optional_targets_arg
@_epic_option
@_guidance_option
@_pass_state
def approve(
    state: BatchState,
    targets: tuple[int, ...],
    epic_number: int | None,
    guidance: str | None,
) -> None:
    """Move queued issues straight to 'planned'."""
    _report_approved(state.approve(epic_number, targets, guidance))


@cli.command("fast-track")
@_optional_targets_arg
@_epic_option
@_guidance_option
@_pass_state
def fast_track(
    state: BatchState,
    targets: tuple[int, ...],
    epic_number: int | None,
    guidance: str | None,
) -> None:
    """Queue and approve in one call: unlabelled issues land on 'planned'."""
    _report_approved(state.fast_track(epic_number, targets, guidance))


@cli.group()
def stack() -> None:
    """Manage the branches, worktrees, and VM disks behind a batch."""


def _resolve_stack(ctx: click.Context) -> StackManager:
    existing = cast("object", ctx.obj)
    manager = existing if isinstance(existing, StackManager) else None
    if manager is None:
        manager = StackManager(
            _main_repo(ctx), seed_image=_resolve_config(ctx).seed_image
        )
        ctx.obj = manager
    return manager


@stack.command("ensure")
@click.argument("issue_number", type=int)
@click.option("--base", required=True, help="Commit-ish the branch is cut from.")
@click.pass_context
def stack_ensure(ctx: click.Context, issue_number: int, base: str) -> None:
    """Create or adopt the branch, worktree, and disk for an issue."""
    slot = _resolve_stack(ctx).ensure(issue_number, base)
    click.echo(f"{slot.branch} {slot.worktree} {slot.disk} {slot.alignment}")
    if slot.alignment is not Alignment.ALIGNED:
        click.echo(_drift(slot, base), err=True)


@stack.command("remove")
@click.argument("issue_number", type=int)
@click.option("--force", is_flag=True, help="Remove even when work would be lost.")
@click.pass_context
def stack_remove(ctx: click.Context, issue_number: int, force: bool) -> None:
    """Remove the worktree, local branch, and disk for an issue."""
    try:
        result = _resolve_stack(ctx).remove(issue_number, force=force)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    removed = [
        name
        for name, done in (
            ("worktree", result.removed_worktree),
            ("branch", result.removed_branch),
            ("disk", result.removed_disk),
        )
        if done
    ]
    if removed:
        click.echo(f"Removed {result.branch}: {', '.join(removed)}")
    else:
        click.echo(f"Nothing to remove for {result.branch}.")


@cli.group()
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding per-issue dtach sockets and console logs.",
)
@click.pass_context
def vm(ctx: click.Context, run_root: Path) -> None:
    """Run an agent VM, attached or detached."""
    existing = cast("object", ctx.obj)
    if not isinstance(existing, VmRunner):
        ctx.obj = _runner(ctx, run_root)


def _agent_options(f: FC) -> FC:
    for option in reversed(
        [
            click.option(
                "--worktree",
                required=True,
                help="Worktree dir to cd into, relative to the mount root.",
            ),
            click.option(
                "--disk",
                required=True,
                type=click.Path(path_type=Path),
                help="VM disk image to boot.",
            ),
            click.option(
                "--config-dir",
                type=click.Path(path_type=Path),
                default=None,
                help="Directory mounted read-only as the claude config.",
            ),
            click.option("--issue", type=int, default=None, help="Issue to work."),
            click.option(
                "--guidance", default=None, help="Test guidance for the agent."
            ),
            click.option("--base", default=None, help="Branch the PR is based on."),
            click.option("--model", default=None, help="Model for the claude session."),
            click.option("--ram", type=int, default=DEFAULT_RAM, help="VM RAM in MB."),
            click.option(
                "--dry-run", is_flag=True, help="Print the command instead of running."
            ),
        ]
    ):
        f = option(f)
    return f


def _session(
    config: BatchConfig,
    *,
    worktree: str,
    disk: Path,
    config_dir: Path,
    issue: int | None,
    guidance: str | None,
    base: str | None,
    model: str | None,
    ram: int,
    max_tests: int | None = None,
    plan_guidance: str | None = None,
) -> VmSession:
    return VmSession(
        worktree=worktree,
        disk=disk,
        config_dir=config_dir,
        agent=agent_command(
            config,
            issue=issue,
            guidance=guidance,
            base=base,
            model=model,
            max_tests=max_tests,
            plan_guidance=plan_guidance,
        ),
        ram=ram,
    )


def _refuse_orphan_agent_options(
    issue: int | None, guidance: str | None, base: str | None
) -> None:
    if issue is None and (guidance is not None or base is not None):
        raise click.UsageError("--guidance and --base need an --issue to work on.")


def _refuse_unplanned_steering(
    issue: int | None,
    guidance: str | None,
    max_tests: int | None,
    plan_guidance: str | None,
) -> None:
    if max_tests is None and plan_guidance is None:
        return
    if issue is None:
        raise click.UsageError("--max-tests and --plan-guidance need an --issue.")
    if guidance is not None:
        raise click.UsageError(
            "--max-tests and --plan-guidance steer a plan phase --guidance skips."
        )


def _run(
    ctx: click.Context,
    command: Sequence[str],
    *,
    dry_run: bool,
    cwd: Path | None = None,
) -> None:
    if dry_run:
        click.echo(shlex.join(command))
        return
    ctx.exit(_spawn(command, cwd))


def _spawn(command: Sequence[str], cwd: Path | None = None) -> int:
    try:
        return subprocess.run(command, check=False, cwd=cwd).returncode
    except FileNotFoundError as exc:
        raise click.ClickException(f"{command[0]} is not installed.") from exc


@vm.command("console")
@_agent_options
@click.option(
    "--max-tests",
    type=click.IntRange(min=0),
    default=None,
    help="Cap the test cases the plan phase proposes.",
)
@click.option("--plan-guidance", default=None, help="Free text steering the plan.")
@click.pass_context
def vm_console(
    ctx: click.Context,
    worktree: str,
    disk: Path,
    config_dir: Path | None,
    issue: int | None,
    guidance: str | None,
    base: str | None,
    model: str | None,
    ram: int,
    dry_run: bool,
    max_tests: int | None,
    plan_guidance: str | None,
) -> None:
    """Boot a VM in this terminal, the way the solo flow always has."""
    if config_dir is None:
        raise click.UsageError("--config-dir is required for an attached console.")
    _refuse_orphan_agent_options(issue, guidance, base)
    _refuse_unplanned_steering(issue, guidance, max_tests, plan_guidance)
    runner = cast("VmRunner", ctx.obj)
    config = _resolve_config(ctx)
    if not dry_run:
        runner.write_config(config_dir)
    session = _session(
        config,
        worktree=worktree,
        disk=disk,
        config_dir=config_dir,
        issue=issue,
        guidance=guidance,
        base=base,
        model=model,
        ram=ram,
        max_tests=max_tests,
        plan_guidance=plan_guidance,
    )
    if dry_run:
        _run(ctx, runner.vibe_command(session), dry_run=dry_run)
        return
    # `reclaim` looks a claim up by bare branch, so the guest's relative path
    # must not reach the lock key.
    with runner.claimed(Path(worktree).name):
        _run(ctx, runner.vibe_command(session), dry_run=dry_run)


@vm.command("launch")
@click.argument("issue_number", type=int)
@_agent_options
@click.pass_context
def vm_launch(
    ctx: click.Context,
    issue_number: int,
    worktree: str,
    disk: Path,
    config_dir: Path | None,
    issue: int | None,
    guidance: str | None,
    base: str | None,
    model: str | None,
    ram: int,
    dry_run: bool,
) -> None:
    """Boot a VM detached under dtach, console tee'd to a per-issue log."""
    runner = cast("VmRunner", ctx.obj)
    config = _resolve_config(ctx)
    config_dir = config_dir or runner.config_dir(issue_number)
    if not dry_run:
        runner.write_config(config_dir)
    session = _session(
        config,
        worktree=worktree,
        disk=disk,
        config_dir=config_dir,
        issue=issue_number if issue is None else issue,
        guidance=guidance,
        base=base,
        model=model,
        ram=ram,
    )
    if dry_run:
        click.echo(shlex.join(runner.launch_command(issue_number, session)))
        return
    try:
        runner.launch(issue_number, session)
    except (AlreadyRunningError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"#{issue_number} launched, logging to {runner.log(issue_number)}")


@vm.command("clean")
@click.argument("issue_number", type=int)
@click.pass_context
def vm_clean(ctx: click.Context, issue_number: int) -> None:
    """Delete an issue's staged claude config, credentials included."""
    runner = cast("VmRunner", ctx.obj)
    if runner.status(issue_number) is VmStatus.RUNNING:
        raise click.ClickException(f"#{issue_number} is still running.")
    staged = runner.config_dir(issue_number)
    removed = runner.clean_config(staged)
    click.echo(
        f"Removed {staged}" if removed else f"Nothing staged for #{issue_number}."
    )


@vm.command("status")
@click.argument("issue_number", type=int)
@click.pass_context
def vm_status(ctx: click.Context, issue_number: int) -> None:
    """Report whether a detached VM is still running."""
    runner = cast("VmRunner", ctx.obj)
    status = runner.status(issue_number)
    click.echo(f"#{issue_number} {status} {runner.log(issue_number)}")
    if status is VmStatus.EXITED:
        ctx.exit(1)


def _resolve_verifier(ctx: click.Context) -> Verifier:
    existing = cast("object", ctx.obj)
    found = existing if isinstance(existing, Verifier) else None
    if found is None:
        try:
            found = Verifier(BatchGitHub(GitHubGraphQL(GitHubTransport()), repo()))
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        ctx.obj = found
    return found


@cli.command()
@click.argument("issue_number", type=int)
@click.option("--base", required=True, help="Branch the PR should be based on.")
@click.option(
    "--wait",
    type=float,
    default=None,
    help="Seconds to keep polling while CI is pending.",
)
@click.pass_context
def verify(
    ctx: click.Context, issue_number: int, base: str, wait: float | None
) -> None:
    """Check an issue's PR: exists, right base, closes the issue, CI green."""
    waiting = timedelta(seconds=wait) if wait else None
    try:
        verdict = _resolve_verifier(ctx).verify(issue_number, (base,), waiting)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    print_verdict(verdict, sys.stdout)
    if not verdict.ok:
        ctx.exit(1)


def _interactive() -> bool:
    return sys.stdout.isatty()


def _resolve_orchestrator(
    ctx: click.Context,
    *,
    root: Path,
    model: str | None,
    timeout: float,
    poll_interval: float,
    verify_wait: float,
    report: Callable[[str], None] = click.echo,
) -> Orchestrator:
    existing = cast("object", ctx.obj)
    if isinstance(existing, Orchestrator):
        return existing
    config = _resolve_config(ctx)
    stack = StackManager(_main_repo(ctx), seed_image=config.seed_image)
    client = BatchGitHub(GitHubGraphQL(GitHubTransport()), repo())
    state = BatchState(client)
    runner = _runner(ctx, root)
    verifier = Verifier(client)
    return Orchestrator(
        state,
        stack,
        runner,
        verifier,
        Teardown(state, stack, runner, verifier),
        config=config,
        report=report,
        timeout=timeout,
        poll_interval=poll_interval,
        verify_wait=verify_wait,
        model=model,
    )


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


def _watch_passes(
    orchestrator: Orchestrator,
    targets: Sequence[int],
    interval: float,
    *,
    prog: str,
    report: Callable[[str], None],
    echo: bool = True,
) -> RunResult:
    """`echo` is off under the dashboard, which renders the passes itself; the
    wait goes through `report`, which the dashboard buffers as narration."""
    quiet = _QuietRepeats(orchestrator.report)
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


@cli.command()
@_targets_arg
@click.option("--model", default=None, help="Model for the implementation agents.")
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    help="Seconds to wait for one issue's VM to exit before marking it stuck.",
)
@click.option(
    "--poll-interval",
    type=float,
    default=DEFAULT_POLL_INTERVAL,
    help="Seconds between VM status checks.",
)
@click.option(
    "--verify-wait",
    type=float,
    default=DEFAULT_VERIFY_WAIT,
    help="Seconds to keep polling a new PR while its CI is still pending.",
)
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the run lock, dtach sockets, and console logs.",
)
@click.option(
    "--cli",
    "cli_only",
    is_flag=True,
    help="Stream progress as lines instead of rendering the dashboard.",
)
@click.option(
    "--watch-interval",
    type=float,
    default=DEFAULT_WATCH_INTERVAL,
    help="Seconds between passes while waiting on queued issues.",
)
@click.pass_context
def run(
    ctx: click.Context,
    targets: tuple[int, ...],
    model: str | None,
    timeout: float,
    poll_interval: float,
    verify_wait: float,
    run_root: Path,
    cli_only: bool,
    watch_interval: float,
) -> None:
    """Drive the targets' planned issues through to ready-for-review.

    The run waits while any target still holds a queued issue, so planning can
    pipeline alongside it; it names what it is waiting on each time that set
    changes.

    The dashboard is the default surface; a non-terminal stdout streams instead,
    so pipes and CI need no flag.
    """
    root = run_root.expanduser()
    prog = _prog(ctx)
    narration: list[str] = []
    rendered = not cli_only and _interactive()
    live = rendered

    def report(line: str) -> None:
        if live:
            narration.append(line)
        else:
            click.echo(line)

    try:
        orchestrator = _resolve_orchestrator(
            ctx,
            root=root,
            model=model,
            timeout=timeout,
            poll_interval=poll_interval,
            verify_wait=verify_wait,
            report=report,
        )
        with run_lock(root), awake(report):

            def drive() -> RunResult:
                return _watch_passes(
                    orchestrator,
                    targets,
                    watch_interval,
                    prog=prog,
                    report=report,
                    echo=not rendered,
                )

            def render() -> RunResult | None:
                nonlocal live
                try:
                    return run_dashboard(
                        targets,
                        orchestrator,
                        narration,
                        drive,
                        orchestrator.verbs(targets),
                        prog=prog,
                    )
                finally:
                    live = False

            result = render() if rendered else drive()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if result is None:
        click.echo("Dashboard closed; any VM still in flight is left running.")
        return
    if rendered or not result.outcomes:
        print_run_result(result, sys.stdout, prog=prog)
    if result.halted is not None:
        ctx.exit(1)


def _resolve_teardown(ctx: click.Context, root: Path) -> Teardown:
    existing = cast("object", ctx.obj)
    if isinstance(existing, Teardown):
        return existing
    stack = StackManager(_main_repo(ctx), seed_image=_resolve_config(ctx).seed_image)
    client = BatchGitHub(GitHubGraphQL(GitHubTransport()), repo())
    return Teardown(
        BatchState(client),
        stack,
        _runner(ctx, root),
        Verifier(client),
    )


@cli.command()
@_targets_arg
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the per-issue staged claude configs.",
)
@click.pass_context
def cleanup(ctx: click.Context, targets: tuple[int, ...], run_root: Path) -> None:
    """Reclaim the worktree, branch, disk, and config of every merged issue the
    targets name."""
    try:
        result = _resolve_teardown(ctx, run_root.expanduser()).sweep(targets)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    print_teardown_result(result, sys.stdout)


def _resolve_reclaimer(ctx: click.Context, root: Path) -> Reclaimer:
    existing = cast("object", ctx.obj)
    if isinstance(existing, Reclaimer):
        return existing
    return Reclaimer(
        StackManager(_main_repo(ctx), seed_image=_resolve_config(ctx).seed_image),
        _runner(ctx, root),
    )


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would go, and why, without removing anything.",
)
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the per-slot dtach sockets.",
)
@click.pass_context
def gc(ctx: click.Context, dry_run: bool, run_root: Path) -> None:
    """Reclaim every merged, clean, idle slot on disk, whatever target it came
    from — the ad-hoc branches, spent planning worktrees, and label-less
    leftovers no sweep can name. Start with --dry-run."""
    try:
        result = _resolve_reclaimer(ctx, run_root.expanduser()).collect(dry_run=dry_run)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    print_reclaim_result(result, sys.stdout)


@cli.command()
@_optional_targets_arg
@_epic_option
@_pass_state
def unqueue(
    state: BatchState, targets: tuple[int, ...], epic_number: int | None
) -> None:
    """Remove 'queued' from issues that still carry it."""
    _report("Unqueued", "unqueue", state.unqueue(epic_number, targets))


def _resolve_recovery(ctx: click.Context, root: Path) -> Recovery:
    existing = cast("object", ctx.obj)
    if isinstance(existing, Recovery):
        return existing
    client = BatchGitHub(GitHubGraphQL(GitHubTransport()), repo())
    return Recovery(BatchState(client), _runner(ctx, root))


def _recover(ctx: click.Context, result: RecoveryResult) -> None:
    print_recovery_result(result, sys.stdout, prog=_prog(ctx))
    if result.refusal is not None:
        ctx.exit(1)


def _recovery_root_option(f: FC) -> FC:
    return click.option(
        "--run-root",
        type=click.Path(path_type=Path),
        default=DEFAULT_RUN_ROOT,
        help="Directory holding the per-issue dtach sockets.",
    )(f)


@cli.command()
@click.argument("issue_number", type=int)
@_recovery_root_option
@click.pass_context
def attach(ctx: click.Context, issue_number: int, run_root: Path) -> None:
    """Attach to a detached VM's console; C-\\ detaches again.

    Attaching shows nothing until the VM writes again. For history, read the
    per-issue log the run is tee'd to; `vm status` prints its path.
    """
    runner = _runner(ctx, run_root.expanduser())
    if runner.status(issue_number) is VmStatus.EXITED:
        raise click.ClickException(f"No live VM for #{issue_number}.")
    _run(ctx, runner.attach_command(issue_number), dry_run=False)


def _resolve_verbs(
    ctx: click.Context, root: Path, targets: Sequence[int], model: str | None
) -> Verbs:
    existing = cast("object", ctx.obj)
    if isinstance(existing, Verbs):
        return existing
    config = _resolve_config(ctx)
    stack = StackManager(_main_repo(ctx), seed_image=config.seed_image)
    client = BatchGitHub(GitHubGraphQL(GitHubTransport()), repo())
    state = BatchState(client)
    runner = _runner(ctx, root)
    return Verbs(
        targets,
        state,
        stack,
        runner,
        Recovery(state, runner),
        config=config,
        model=model,
    )


@cli.command()
@click.argument("issue_number", type=int)
@_targets_arg
@click.option("--model", default=None, help="Model for the rework session.")
@_recovery_root_option
@click.pass_context
def rework(
    ctx: click.Context,
    issue_number: int,
    targets: tuple[int, ...],
    model: str | None,
    run_root: Path,
) -> None:
    """Boot an interactive VM on an issue's branch to address its PR feedback.

    The targets are needed for the branch the rework stacks on, and are named
    after the issue so the list can run on; the issue's batch label is left
    exactly as found.
    """
    try:
        verbs = _resolve_verbs(ctx, run_root.expanduser(), targets, model)
        result = verbs.rework(issue_number)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _recover(ctx, result)


@cli.command()
@click.argument("number", type=int)
@click.option(
    "--fresh", is_flag=True, help="Start a bare session instead of the agent's own."
)
@click.option("--model", default=None, help="Model for the claude session.")
@click.option("--ram", type=int, default=DEFAULT_RAM, help="VM RAM in MB.")
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the dtach sockets and the staged claude config.",
)
@click.option("--dry-run", is_flag=True, help="Print the command instead of running.")
@click.pass_context
def debug(
    ctx: click.Context,
    number: int,
    fresh: bool,
    model: str | None,
    ram: int,
    run_root: Path,
    dry_run: bool,
) -> None:
    """Boot an issue's existing VM in this terminal, resuming the agent's session."""
    root = run_root.expanduser()
    config = _resolve_config(ctx)
    debugger = Debugger(
        StackManager(_main_repo(ctx), seed_image=config.seed_image),
        _runner(ctx, root),
        config=config,
        model=model,
        ram=ram,
    )
    entry = debugger.enter(number, fresh=fresh, dry_run=dry_run)
    if entry.refusal is not None:
        raise click.ClickException(debug_line(entry))
    if not entry.boot:
        click.echo(f"#{number} is already running; attaching instead.")
    elif dry_run:
        click.echo(shlex.join(entry.boot))
    _run(ctx, entry.command, dry_run=dry_run)


@cli.command()
@click.argument("issue_number", type=int)
@_recovery_root_option
@click.pass_context
def skip(ctx: click.Context, issue_number: int, run_root: Path) -> None:
    """Drop an issue from the batch, leaving its approved plan in the body."""
    recovery = _resolve_recovery(ctx, run_root.expanduser())
    try:
        result = recovery.skip(issue_number)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _recover(ctx, result)


@cli.command()
@click.argument("issue_number", type=int)
@_recovery_root_option
@click.pass_context
def relaunch(ctx: click.Context, issue_number: int, run_root: Path) -> None:
    """Send a stuck issue back to 'planned' for the next run to pick up."""
    recovery = _resolve_recovery(ctx, run_root.expanduser())
    try:
        result = recovery.relaunch(issue_number)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    _recover(ctx, result)


EMPTY_QUEUE_EXIT = 3
"""Distinct from the 1 every other failure exits with: the driver session ends
the batch on an empty queue, and must not end it on a token or label error."""


@cli.group("agent")
def agent_group() -> None:
    """Commands the planning agent calls during a session; not meant to be run
    by hand."""


def _resolve_agent(ctx: click.Context) -> PlanningAgent:
    existing = cast("object", ctx.obj)
    if isinstance(existing, PlanningAgent):
        return existing
    return PlanningAgent(
        BatchState(BatchGitHub(GitHubGraphQL(GitHubTransport()), repo()))
    )


@agent_group.command("next-issue")
@_targets_arg
@click.pass_context
def agent_next_issue(ctx: click.Context, targets: tuple[int, ...]) -> None:
    """Print the next queued issue to plan, and the verb that closes it."""
    try:
        found = _resolve_agent(ctx).next_issue(targets)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if found is None:
        click.echo(f"Nothing queued under {targets_line(targets)}.")
        print_anomalies(
            _resolve_agent(ctx).anomalies(targets), sys.stdout, prog=_prog(ctx)
        )
        ctx.exit(EMPTY_QUEUE_EXIT)
    print_next_issue(found, targets, sys.stdout, prog=_prog(ctx))


@agent_group.command("plan-written")
@click.argument("issue_number", type=int)
@_targets_arg
@click.pass_context
def agent_plan_written(
    ctx: click.Context, issue_number: int, targets: tuple[int, ...]
) -> None:
    """Check the claim that an issue's plan is written, and advance it."""
    try:
        result = _resolve_agent(ctx).plan_written(targets, issue_number)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    print_plan_written(result, sys.stdout)
    if result.refusal is not None:
        ctx.exit(1)


def _reclaim_plan_slot(manager: StackManager, branch: str, prog: str) -> None:
    """Unforced: a session that committed or left changes in its scratch worktree
    keeps them, and says where."""
    try:
        _ = manager.remove_branch(branch)
    except UnsafeRemovalError as exc:
        click.echo(f"{exc}; `{prog} gc` once you are done with it.", err=True)


@cli.command()
@_targets_arg
@click.option("--model", default=None, help="Model for the planning sessions.")
@click.option("--ram", type=int, default=DEFAULT_RAM, help="VM RAM in MB.")
@click.option(
    "--run-root",
    type=click.Path(path_type=Path),
    default=DEFAULT_RUN_ROOT,
    help="Directory holding the staged claude config.",
)
@click.option("--dry-run", is_flag=True, help="Print the command instead of running.")
@click.pass_context
def plan(
    ctx: click.Context,
    targets: tuple[int, ...],
    model: str | None,
    ram: int,
    run_root: Path,
    dry_run: bool,
) -> None:
    """Boot a planning VM in this terminal and walk the targets' queued issues.

    The worktree and config dir belong to this invocation alone and go when it
    ends, so concurrent planning sessions never contend for either. The session
    writes plans to issue bodies over the API and commits nothing, so the slot
    is scratch; `gc` reclaims one a crash left behind.
    """
    root = run_root.expanduser()
    branch = plan_slot_branch(os.getpid())
    config = _resolve_config(ctx)
    manager = StackManager(_main_repo(ctx), seed_image=config.seed_image)
    runner = _runner(ctx, root)
    config_dir = runner.named_config_dir(branch)
    try:
        slot = manager.ensure_current(branch, MAIN)
    except StaleSlotError as exc:
        raise click.ClickException(
            f"{exc} — inspect it, then `{config.commands.cli} gc`"
        ) from exc
    try:
        if not dry_run:
            runner.write_config(config_dir)
        session = session_for(
            slot,
            mount_root=manager.mount_root,
            config_dir=config_dir,
            agent=plan_batch_command(config, targets, model),
            ram=ram,
        )
        _run(ctx, runner.vibe_command(session), dry_run=dry_run, cwd=session.cwd)
    finally:
        _ = runner.clean_config(config_dir)
        _reclaim_plan_slot(manager, branch, config.commands.cli)
