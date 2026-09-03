from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from batch.models import Batch, BatchIssue, DashboardRow, VmStatus

LINE_LIMIT = 120
TAIL_BYTES = 8192
_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|.)")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Facts(Protocol):
    def status(self, issue: int) -> VmStatus: ...
    def log(self, issue: int) -> Path: ...


class Elapsing(Protocol):
    def elapsed(self, issue: int) -> float | None: ...


def format_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def last_line(path: Path, *, limit: int = LINE_LIMIT) -> str:
    """The console is tee'd raw, so a log line arrives wrapped in ANSI and \\r."""
    try:
        with path.open("rb") as handle:
            _ = handle.seek(0, 2)
            _ = handle.seek(max(0, handle.tell() - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return ""
    for chunk in reversed(re.split(r"[\r\n]", tail.decode("utf-8", "replace"))):
        cleaned = _CONTROL.sub("", _ESCAPE.sub("", chunk)).replace("\t", " ").strip()
        if cleaned:
            return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
    return ""


class Selection:
    """Selection follows the issue, not the row index: labels change under the
    cursor mid-run, and Enter must attach to the row the developer is looking at.
    """

    def __init__(self) -> None:
        self.number: int | None = None
        self._index: int = 0

    def sync(self, rows: Sequence[DashboardRow]) -> None:
        if not rows:
            self.number = None
            return
        numbers = [row.number for row in rows]
        if self.number in numbers:
            self._index = numbers.index(self.number)
        else:
            self._index = min(self._index, len(rows) - 1)
        self.number = numbers[self._index]

    def move(self, rows: Sequence[DashboardRow], delta: int) -> None:
        if not rows:
            return
        self.sync(rows)
        self._index = max(0, min(self._index + delta, len(rows) - 1))
        self.number = rows[self._index].number

    def row(self, rows: Sequence[DashboardRow]) -> DashboardRow | None:
        return next((row for row in rows if row.number == self.number), None)


def _row(
    issue: BatchIssue, facts: Facts, timings: Elapsing, selected: int | None
) -> DashboardRow:
    """Only a shown line is read: the log of every issue is a file read per frame."""
    live = facts.status(issue.number) is VmStatus.RUNNING
    shown = live or issue.number == selected
    return DashboardRow(
        number=issue.number,
        title=issue.title,
        state=issue.state,
        live=live,
        elapsed=format_elapsed(timings.elapsed(issue.number)),
        last_line=last_line(facts.log(issue.number)) if shown else "",
    )


def rows(
    batch: Batch, facts: Facts, timings: Elapsing, selected: int | None = None
) -> tuple[DashboardRow, ...]:
    return tuple(_row(issue, facts, timings, selected) for issue in batch.issues)
