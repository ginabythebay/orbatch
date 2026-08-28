from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO

from rich.console import Console
from rich.table import Table

from orbit.github.models import Epic, Issue, IssueDetail
from orbit.tree import FilteredRun, TreeItem


def filtered_run_label(count: int) -> str:
    noun = "issue" if count == 1 else "issues"
    return f"<{count} {noun} filtered>"


def print_issue_table(issues: Sequence[Issue | FilteredRun], out: TextIO) -> None:
    if not issues:
        out.write("No issues found.\n")
        return
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    for issue in issues:
        if isinstance(issue, FilteredRun):
            table.add_row("", "", filtered_run_label(issue.count))
        else:
            table.add_row(f"#{issue.number}", issue.state, issue.title)
    console = Console(file=out, highlight=False)
    console.print(table)


def print_standalone_section(
    issues: Sequence[Issue | FilteredRun], out: TextIO
) -> None:
    out.write("\nSTANDALONE\n")
    print_issue_table(issues, out)


def print_epic_table(epics: Sequence[Epic | FilteredRun], out: TextIO) -> None:
    if not epics:
        out.write("No epics found.\n")
        return
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    for epic in epics:
        if isinstance(epic, FilteredRun):
            table.add_row("", "", "", filtered_run_label(epic.count))
            continue
        table.add_row(
            f"#{epic.number}",
            epic.state,
            f"{epic.open_count}/{epic.total_count}",
            epic.title,
        )
    console = Console(file=out, highlight=False)
    console.print(table)


def print_sub_issue_tree(nodes: Sequence[TreeItem], out: TextIO) -> None:
    if not nodes:
        out.write("No sub-issues found.\n")
        return
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    _add_tree_rows(table, nodes, depth=0)
    console = Console(file=out, highlight=False)
    console.print(table)


def _add_tree_rows(
    table: Table,
    nodes: Sequence[TreeItem],
    depth: int,
) -> None:
    for node in nodes:
        indent = "  " * depth
        count = (
            f"{node.open_count}/{node.total_count}"
            if node.open_count is not None
            else ""
        )
        if isinstance(node, FilteredRun):
            table.add_row("", "", count, f"{indent}{filtered_run_label(node.count)}")
        else:
            table.add_row(
                f"{indent}#{node.number}",
                node.state,
                count,
                node.title,
            )
        _add_tree_rows(table, node.children, depth + 1)


def print_issue_detail(detail: IssueDetail, out: TextIO) -> None:
    """Print an issue's metadata header followed by its body."""
    console = Console(file=out, highlight=False)
    console.print(f"#{detail.number} {detail.title}", style="bold")
    console.print(f"State:     {detail.state}")
    labels = ", ".join(detail.labels) if detail.labels else "—"
    console.print(f"Labels:    {labels}")
    console.print(f"Milestone: {detail.milestone_title or '—'}")
    if detail.parent_number is not None:
        suffix = f" {detail.parent_title}" if detail.parent_title else ""
        console.print(f"Parent:    #{detail.parent_number}{suffix}")
    else:
        console.print("Parent:    —")
    if detail.body.strip():
        console.print()
        console.print(detail.body)


def print_parent_issue(issue: Issue | None, out: TextIO) -> None:
    if issue is None:
        out.write("No parent issue.\n")
        return
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column()
    table.add_row(f"#{issue.number}", issue.state, issue.title)
    console = Console(file=out, highlight=False)
    console.print(table)
