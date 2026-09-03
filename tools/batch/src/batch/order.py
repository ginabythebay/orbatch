from __future__ import annotations

from batch.models import Batch, BatchIssue, BatchLabel

MAIN = "main"
UNSTARTED = frozenset({BatchLabel.QUEUED, BatchLabel.PLANNED})


def preceding(batch: Batch, issue: BatchIssue) -> tuple[BatchIssue, ...]:
    return batch.issues[: batch.issues.index(issue)]


def predecessors(batch: Batch, issue: BatchIssue) -> tuple[int, ...]:
    """Every earlier batch issue whose plan is approved; a queued one has none yet."""
    return tuple(
        other.number
        for other in preceding(batch, issue)
        if other.state is not BatchLabel.QUEUED
    )


def stacked(batch: Batch, issue: BatchIssue) -> tuple[BatchIssue, ...]:
    """Every started issue, whatever its position: a re-added issue lands at the end.

    In a normal run this is just the issues preceding it, since everything after
    the current issue is unstarted.
    """
    return tuple(
        other
        for other in batch.issues
        if other.number != issue.number and other.state not in UNSTARTED
    )


def base_for(batch: Batch, issue: BatchIssue) -> str:
    """The tip of the stack: where an issue about to start is cut from."""
    started = stacked(batch, issue)
    return f"issue-{started[-1].number}" if started else MAIN


def base_under(batch: Batch, issue: BatchIssue) -> str:
    """The branch an already-started issue sits on, which is its nearest started
    predecessor — the tip rule would name a descendant once successors exist.
    """
    earlier = [
        other for other in preceding(batch, issue) if other.state not in UNSTARTED
    ]
    return f"issue-{earlier[-1].number}" if earlier else MAIN
