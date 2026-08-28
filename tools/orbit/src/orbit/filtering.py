from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from orbit.github.models import MilestoneIssue
from orbit.tree import FilteredRun


class Numbered(Protocol):
    @property
    def number(self) -> int: ...


def partition_standalone(
    issues: Iterable[MilestoneIssue],
) -> tuple[list[MilestoneIssue], list[MilestoneIssue]]:
    """Split into (structured, standalone), preserving order within each.

    A top-level epic is parentless too, so standalone means no parent
    *and* not an epic.
    """
    structured: list[MilestoneIssue] = []
    standalone: list[MilestoneIssue] = []
    for issue in issues:
        if issue.parent_number is None and not issue.is_epic:
            standalone.append(issue)
        else:
            structured.append(issue)
    return structured, standalone


def partition_filtered[T: Numbered](
    items: Iterable[T], keep: Callable[[T], bool]
) -> list[T | FilteredRun]:
    """Interleave surviving items with placeholders for adjacent dropped runs.

    Callers must sort before partitioning: a run means "adjacent in the
    order the reader sees", not "adjacent in fetch order".
    """
    rows: list[T | FilteredRun] = []
    run: list[int] = []

    def flush() -> None:
        if run:
            rows.append(FilteredRun(count=len(run), numbers=tuple(run)))
            run.clear()

    for item in items:
        if keep(item):
            flush()
            rows.append(item)
        else:
            run.append(item.number)
    flush()
    return rows
