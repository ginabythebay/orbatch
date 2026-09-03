from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TextIO

from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from batch.models import (
    Batch,
    BatchLabel,
    CiStatus,
    DashboardRow,
    DebugEntry,
    DebugRefusal,
    DroppedChild,
    NextIssue,
    PlanRefusal,
    PlanWritten,
    Problem,
    ReclaimOutcome,
    ReclaimResult,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    RunResult,
    TeardownOutcome,
    TeardownResult,
    Verdict,
    VmFacts,
)
from ghgql.transport import RateLimit

_STATE_STYLES = {
    "queued": "white",
    "planned": "cyan",
    "implementing": "yellow",
    "ready-for-review": "green",
    "stuck": "red",
}


_STATE_WIDTH = max(len(label) for label in BatchLabel)
_ELAPSED_WIDTH = 7
_NUMBER_WIDTH = 9
_LOG_INDENT = "  \u2514 "


def _remedy(prog: str) -> str:
    return f"reopen it or `{prog} skip` it"


def _conflict_remedy(prog: str) -> str:
    return f"clear all but one label in GitHub, then `{prog} skip` it"


def _issue_row(row: DashboardRow, chosen: bool) -> Table:
    """One grid per issue so the log line can start at the left margin; the fixed
    widths are what keep the rows aligned with each other."""
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=_NUMBER_WIDTH, no_wrap=True)
    grid.add_column(width=_STATE_WIDTH, no_wrap=True)
    grid.add_column(width=_ELAPSED_WIDTH, justify="right", no_wrap=True)
    grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    style = _STATE_STYLES[row.state]
    grid.add_row(
        f"{'▶' if chosen else ' '} [bold]#{row.number}[/bold]",
        f"[{style}]{row.state}[/{style}]",
        row.elapsed,
        escape(row.title),
        style="reverse" if chosen else "",
    )
    return grid


def dashboard_view(rows: Sequence[DashboardRow], selected: int | None) -> Group:
    """The log line gets its own line under its issue: sharing the row would let a
    long line squeeze every other column."""
    if not rows:
        return Group(Text("Nothing planned yet."))
    painted: list[RenderableType] = []
    for row in rows:
        chosen = row.number == selected
        painted.append(_issue_row(row, chosen))
        if row.last_line and (row.live or chosen):
            painted.append(
                Text(
                    f"{_LOG_INDENT}{row.last_line}",
                    style="dim",
                    no_wrap=True,
                    overflow="ellipsis",
                )
            )
    return Group(*painted)


def _budget_style(rate_limit: RateLimit) -> str:
    if not rate_limit.limit:
        return "green"
    spent = (rate_limit.limit - rate_limit.remaining) / rate_limit.limit
    if spent <= 0.4:
        return "green"
    return "yellow" if spent <= 0.8 else "red"


def status_line(message: str, rate_limit: RateLimit | None) -> Text:
    """The GraphQL budget rides along with whatever the status line already says:
    a dashboard that polls GitHub is the thing most likely to exhaust it."""
    line = Text(message)
    if rate_limit is None:
        return line
    if message:
        line.append("  ·  ")
    line.append(
        f"GraphQL {rate_limit.remaining}/{rate_limit.limit}",
        style=_budget_style(rate_limit),
    )
    return line


def _plural(count: int) -> str:
    return "child" if count == 1 else "children"


def _unlabeled_note(dropped: Sequence[DroppedChild]) -> str:
    unlabeled = sum(
        1 for child in dropped if not child.labels and child.state == "OPEN"
    )
    if not unlabeled:
        return ""
    return f"{unlabeled} open {_plural(unlabeled)} with no batch label"


def _anomaly_lines(anomalies: Sequence[DroppedChild], prog: str) -> Iterator[str]:
    for child in anomalies:
        labels = ", ".join(child.labels)
        remedy = _remedy(prog) if len(child.labels) == 1 else _conflict_remedy(prog)
        yield f"  #{child.number} is closed but labeled {labels} — {remedy}\n"


def print_anomalies(
    anomalies: Sequence[DroppedChild], out: TextIO, *, prog: str
) -> None:
    out.writelines(_anomaly_lines(anomalies, prog))


def _facts_line(number: int, facts: VmFacts) -> str:
    parts = ["socket live"] if facts.live else []
    if facts.log is not None:
        parts.append(f"log {facts.log}")
    if facts.config_dir is not None:
        parts.append(f"config {facts.config_dir}")
    return f"  #{number} {', '.join(parts)}\n"


def print_batch_table(
    batch: Batch,
    out: TextIO,
    vm_facts: Mapping[int, VmFacts] | None = None,
    *,
    prog: str,
) -> None:
    """`vm_facts` is None for a non-verbose render; a mapping turns on the detail."""
    note = _unlabeled_note(batch.dropped)
    if not batch.issues:
        tail = f" — {note}" if note else ""
        out.write(f"No batch issues under {targets_line(batch.targets)}{tail}.\n")
    else:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        for issue in batch.issues:
            style = _STATE_STYLES[issue.state]
            table.add_row(
                f"#{issue.number}", f"[{style}]{issue.state}[/{style}]", issue.title
            )
        console = Console(file=out, highlight=False)
        console.print(table)
        for issue in batch.issues:
            facts = (vm_facts or {}).get(issue.number)
            if facts is not None and facts.any:
                out.write(_facts_line(issue.number, facts))
        if note:
            out.write(f"{note}.\n")
    out.writelines(_anomaly_lines(batch.anomalies, prog))
    if vm_facts is not None and batch.dropped:
        out.write("Dropped children:\n")
        out.writelines(
            f"  #{child.number} {child.reason} — {child.title}\n"
            for child in batch.dropped
        )


def targets_line(targets: Sequence[int]) -> str:
    return ", ".join(f"#{number}" for number in targets)


def _bases(bases: tuple[str, ...]) -> str:
    if len(bases) == 1:
        return bases[0]
    return f"one of {', '.join(bases)}"


def _detail(verdict: Verdict, problem: Problem) -> str:
    match problem:
        case Problem.NO_PR:
            return f"no PR for issue-{verdict.issue_number}"
        case Problem.PR_CLOSED:
            return "the PR was closed without merging"
        case Problem.WRONG_BASE:
            return f"based on {verdict.base}, expected {_bases(verdict.expected_bases)}"
        case Problem.MISSING_ISSUE_REFERENCE:
            return f"the PR does not close #{verdict.issue_number}"
        case Problem.EXTRA_PRS:
            others = ", ".join(f"#{number}" for number in verdict.extra_pr_numbers)
            return f"other PRs on the branch: {others}"


def _problem_lines(verdict: Verdict) -> Iterator[str]:
    return (
        f"  {problem}: {_detail(verdict, problem)}\n" for problem in verdict.problems
    )


def print_verdict(verdict: Verdict, out: TextIO) -> None:
    pull = f"PR #{verdict.pr_number}" if verdict.pr_number else "no PR"
    out.write(f"#{verdict.issue_number} {pull} ci={verdict.ci}\n")
    out.writelines(_problem_lines(verdict))


def _print_shortfall(verdict: Verdict, out: TextIO) -> None:
    out.writelines(_problem_lines(verdict))
    if verdict.pr_number is not None and verdict.ci is not CiStatus.GREEN:
        out.write(f"  ci={verdict.ci}\n")


def print_next_issue(
    issue: NextIssue, targets: Sequence[int], out: TextIO, *, prog: str
) -> None:
    """Ends with the verb that closes the iteration, so a session that has lost
    its context re-learns the protocol from this output alone."""
    out.write(f"#{issue.number} {issue.title}\n")
    if issue.predecessors:
        numbers = ", ".join(f"#{number}" for number in issue.predecessors)
        out.write(f"Predecessors: {numbers}\n")
    out.write(f"\n{issue.body.rstrip()}\n")
    out.write("\nWhen the plan is approved and written to the issue body, run:\n")
    named = " ".join(str(number) for number in targets)
    out.write(f"{prog} agent plan-written {issue.number} {named}\n")


def print_plan_written(result: PlanWritten, out: TextIO) -> None:
    if result.refusal is None:
        out.write(f"#{result.number} planned\n")
        return
    out.write(f"{_REFUSALS[result.refusal].format(number=result.number)}\n")


_REFUSALS = {
    PlanRefusal.NO_PLAN: (
        "Write the plan to #{number} before marking it done: "
        "its body has no '## Test Plan' section."
    ),
    PlanRefusal.WRONG_STATE: "#{number} is past planning; nothing to mark done.",
    PlanRefusal.NOT_IN_BATCH: "#{number} is not a queued issue of this batch.",
}


_DEBUG_REFUSALS = {
    DebugRefusal.NO_SLOT: "No slot for issue-{number}: {missing}.",
    DebugRefusal.BOOT_FAILED: "Could not boot a VM for #{number}.",
}


def debug_line(entry: DebugEntry) -> str:
    """Only a refusal has a line to render; an entry that took carries an argv."""
    template = _DEBUG_REFUSALS[entry.refusal or DebugRefusal.NO_SLOT]
    return template.format(number=entry.number, missing=", ".join(entry.missing))


def print_run_result(result: RunResult, out: TextIO, *, prog: str) -> None:
    if not result.outcomes:
        out.write(f"Nothing planned under {targets_line(result.targets)}.\n")
        out.writelines(_anomaly_lines(result.anomalies, prog))
        return
    for outcome in result.outcomes:
        pull = (
            f" PR #{outcome.verdict.pr_number}"
            if outcome.verdict and outcome.verdict.pr_number
            else ""
        )
        detail = f" ({outcome.halt})" if outcome.halt else ""
        out.write(
            f"#{outcome.number} {outcome.state}{detail} on {outcome.base}{pull}\n"
        )
        if outcome.halt is not None and outcome.verdict is not None:
            _print_shortfall(outcome.verdict, out)
    if result.halted is not None:
        out.write(f"Batch halted at #{result.outcomes[-1].number}.\n")
    out.writelines(_anomaly_lines(result.anomalies, prog))


def run_banner(result: RunResult) -> str:
    halt = result.halted
    if halt is None:
        return f"Batch finished under {targets_line(result.targets)}. q to quit."
    return (
        f"Batch halted at #{result.outcomes[-1].number} ({halt}). "
        "f rework, s skip, r relaunch, q quit."
    )


def teardown_line(outcome: TeardownOutcome) -> str:
    detail = f"left alone ({outcome.skip})" if outcome.skip else "cleaned up"
    return f"#{outcome.number} {detail}"


def print_teardown_result(result: TeardownResult, out: TextIO) -> None:
    if not result.outcomes:
        out.write(f"Nothing to clean up under {targets_line(result.targets)}.\n")
        return
    out.writelines(f"{teardown_line(outcome)}\n" for outcome in result.outcomes)


def reclaim_line(outcome: ReclaimOutcome, *, dry_run: bool) -> str:
    if outcome.skip is not None:
        return f"{outcome.branch} left alone ({outcome.skip})"
    return (
        f"{outcome.branch} would be cleaned up"
        if dry_run
        else (f"{outcome.branch} cleaned up")
    )


def print_reclaim_result(result: ReclaimResult, out: TextIO) -> None:
    if not result.outcomes:
        out.write("Nothing to reclaim.\n")
        return
    out.writelines(
        f"{reclaim_line(outcome, dry_run=result.dry_run)}\n"
        for outcome in result.outcomes
    )


_DONE = {
    RecoveryAction.REWORK: "reworking",
    RecoveryAction.SKIP: "skipped",
    RecoveryAction.RELAUNCH: "planned",
}


def _refusal(result: RecoveryResult, prog: str) -> str:
    match result.refusal:
        case RecoveryRefusal.NOT_IN_BATCH | None:
            return "it carries no batch label"
        case RecoveryRefusal.WRONG_STATE:
            return f"it is {result.found}"
        case RecoveryRefusal.VM_LIVE:
            return "its VM is still running"
        case RecoveryRefusal.NO_BRANCH:
            return f"it is {result.found} and has no branch yet"
        case RecoveryRefusal.CLOSED:
            return "it is closed"
        case RecoveryRefusal.MERGED:
            return f"it merged — run `{prog} cleanup <target>`"


def recovery_line(result: RecoveryResult, *, prog: str) -> str:
    if result.refusal is None:
        return f"#{result.number} {_DONE[result.action]} (was {result.found})"
    return f"Cannot {result.action} #{result.number}: {_refusal(result, prog)}"


def print_recovery_result(result: RecoveryResult, out: TextIO, *, prog: str) -> None:
    out.write(f"{recovery_line(result, prog=prog)}\n")
