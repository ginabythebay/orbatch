"""The solo entry point: one branch, one worktree, one VM, then teardown.

`batch` drives a stack of issues; `vwt` drives a single branch through the
same `StackManager` slot and the same `vm console`, and asks before it takes
anything away.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, override

import click

from batch.config import BatchConfig, ConfigError, load_config
from batch.models import Slot
from batch.stack import StackManager, main_repo
from batch.vm import relative_worktree

PROG_NAME = "vwt"
DEFAULT_MODEL = "opus"
START_POINT = "HEAD"
CONFIRM = (
    "Press Enter to remove the worktree, branch, and VM disk (Ctrl-C to keep them) "
)
CHOICE = "[r]eturn to the VM, [d]elete anyway, [q]uit and deal with it yourself? "


class Console(Protocol):
    def boot(self, slot: Slot, config_dir: Path) -> int: ...


@dataclass(frozen=True)
class CliConsole(Console):
    """Boots through the configured CLI, so the console inherits `vm console`'s
    claim locking, config staging and mount-root cwd rather than repeating them.
    """

    config: BatchConfig
    mount_root: Path
    flags: tuple[str, ...]

    def command(self, slot: Slot, config_dir: Path) -> tuple[str, ...]:
        worktree = str(relative_worktree(slot.worktree, self.mount_root))
        return (
            self.config.commands.cli,
            "--repo",
            worktree,
            "vm",
            "console",
            "--worktree",
            worktree,
            "--disk",
            str(slot.disk),
            "--config-dir",
            str(config_dir),
            *self.flags,
        )

    @override
    def boot(self, slot: Slot, config_dir: Path) -> int:
        command = self.command(slot, config_dir)
        try:
            return subprocess.run(command, check=False, cwd=self.mount_root).returncode
        except FileNotFoundError as exc:
            raise click.ClickException(f"{command[0]} is not installed.") from exc


def agent_flags(
    *,
    issue: int | None,
    guidance: str | None,
    model: str | None,
    base: str | None,
    max_tests: int | None,
    plan_guidance: str | None,
) -> tuple[str, ...]:
    pairs = (
        ("--issue", None if issue is None else str(issue)),
        ("--guidance", guidance),
        ("--model", model),
        ("--base", base),
        ("--max-tests", None if max_tests is None else str(max_tests)),
        ("--plan-guidance", plan_guidance),
    )
    return tuple(
        part for flag, value in pairs if value is not None for part in (flag, value)
    )


class WorktreeSession:
    def __init__(
        self,
        stack: StackManager,
        console: Console,
        *,
        ask: Callable[[str], str],
        echo: Callable[[str], None],
    ) -> None:
        self._stack: StackManager = stack
        self._console: Console = console
        self._ask: Callable[[str], str] = ask
        self._echo: Callable[[str], None] = echo

    def run(self, branch: str) -> int:
        slot = self._stack.ensure_branch(branch, START_POINT)
        code = self._boot(slot)
        self._teardown(slot)
        return code

    def _boot(self, slot: Slot) -> int:
        """A fresh config dir per boot, removed with it: `vm console` stages the
        claude login and the host secrets there, and they must not outlive the
        session.
        """
        with TemporaryDirectory(prefix=f"{PROG_NAME}-") as staged:
            return self._console.boot(slot, Path(staged))

    def _teardown(self, slot: Slot) -> None:
        while risks := _at_risk(self._stack, slot.branch):
            self._echo(f"{slot.branch} has {' and '.join(risks)}.")
            answer = self._ask(CHOICE)
            if answer == "r":
                _ = self._boot(slot)
            elif answer == "d":
                self._remove(slot)
                return
            elif answer == "q":
                return
            else:
                self._echo("Answer r, d, or q.")
        _ = self._ask(CONFIRM)
        self._remove(slot)

    def _remove(self, slot: Slot) -> None:
        """Forced past `_refuse_if_unsafe`: the loop above is the safety check,
        and a refusal here would leave the disk behind with nothing to remove it.
        """
        _ = self._stack.remove_branch(slot.branch, force=True)


def _at_risk(stack: StackManager, branch: str) -> tuple[str, ...]:
    return tuple(
        risk
        for risk, present in (
            ("uncommitted changes", stack.dirty(branch)),
            ("commits no remote has", stack.unpushed(branch)),
        )
        if present
    )


def _refuse_impossible_options(
    issue: int | None,
    guidance: str | None,
    base: str | None,
    max_tests: int | None,
    plan_guidance: str | None,
) -> None:
    """The same combinations `vm console` refuses, refused before a slot exists.

    Left to the callee they surface as a usage error from another program,
    after the branch, worktree and disk have been made.
    """
    if issue is None and (guidance is not None or base is not None):
        raise click.UsageError("GUIDANCE and --base need an ISSUE to work on.")
    if max_tests is None and plan_guidance is None:
        return
    if issue is None:
        raise click.UsageError("--max-tests and --plan-guidance need an ISSUE.")
    if guidance is not None:
        raise click.UsageError(
            "--max-tests and --plan-guidance steer a plan phase GUIDANCE skips."
        )


def _repo() -> Path:
    try:
        return main_repo()
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            f"Not inside a git checkout; run {PROG_NAME} from the repository."
        ) from exc


@click.command()
@click.argument("branch")
@click.argument("issue", type=int, required=False)
@click.argument("guidance", required=False)
@click.option("--model", default=DEFAULT_MODEL, help="Model for the claude session.")
@click.option("--base", default=None, help="Branch the PR is based on.")
@click.option(
    "-n",
    "--max-tests",
    type=click.IntRange(min=0),
    default=None,
    help="Cap the test cases the plan phase proposes.",
)
@click.option(
    "-g", "--plan-guidance", default=None, help="Free text steering the plan."
)
@click.pass_context
def cli(
    ctx: click.Context,
    branch: str,
    issue: int | None,
    guidance: str | None,
    model: str,
    base: str | None,
    max_tests: int | None,
    plan_guidance: str | None,
) -> None:
    """Give BRANCH a worktree and a VM, boot a console in it, then tear the
    three down once you say so.

    With no ISSUE the guest gets a bare claude session.
    """
    _refuse_impossible_options(issue, guidance, base, max_tests, plan_guidance)
    repo = _repo()
    try:
        config = load_config(repo)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    stack = StackManager(repo, seed_image=config.seed_image)
    console = CliConsole(
        config=config,
        mount_root=stack.mount_root,
        flags=agent_flags(
            issue=issue,
            guidance=guidance,
            model=model,
            base=base,
            max_tests=max_tests,
            plan_guidance=plan_guidance,
        ),
    )
    session = WorktreeSession(stack, console, ask=input, echo=click.echo)
    ctx.exit(session.run(branch))


def main(args: Sequence[str] | None = None) -> None:
    cli(args=args, prog_name=PROG_NAME)


if __name__ == "__main__":
    main()
