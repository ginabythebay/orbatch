from __future__ import annotations

from collections.abc import Callable, Sequence

from batch.body import has_test_plan, with_guidance
from batch.github.client import BatchGitHub
from batch.models import (
    ApproveResult,
    Batch,
    BatchIssue,
    BatchLabel,
    ChildIssue,
    ConflictingLabelsError,
    DroppedChild,
    Epic,
    LabelState,
    NotAChildError,
    NoTargetsError,
    QueueResult,
    SkippedIssue,
    Target,
)
from batch.polling import SettledTargets

_BATCH_LABELS = frozenset(label.value for label in BatchLabel)


def _batch_labels(child: ChildIssue) -> list[BatchLabel]:
    return [BatchLabel(name) for name in child.labels if name in _BATCH_LABELS]


def _skip_reason(
    child: ChildIssue,
    labels: list[BatchLabel],
    want: BatchLabel | None,
) -> str | None:
    """Why this child is not eligible, or None when it is.

    `want` is the label the verb requires; None means the verb requires
    no batch label at all.
    """
    if child.state != "OPEN":
        return "closed"
    if labels == ([] if want is None else [want]):
        return None
    if labels:
        return f"already {labels[0]}"
    return f"not {want}"


def _approve_reason(child: ChildIssue, labels: list[BatchLabel]) -> str | None:
    return _skip_reason(child, labels, BatchLabel.QUEUED)


def _fast_track_reason(child: ChildIssue, labels: list[BatchLabel]) -> str | None:
    """Eligible from either state the shortcut collapses: unlabelled or queued."""
    if child.state != "OPEN":
        return "closed"
    if labels and labels != [BatchLabel.QUEUED]:
        return f"already {labels[0]}"
    return None


def _status_reason(child: ChildIssue, labels: list[BatchLabel]) -> str | None:
    """Why `status` dropped this child, or None when it belongs in the batch.

    Distinct from `_skip_reason`, whose wording answers "not the label this
    verb wanted"; status wants any batch label at all.
    """
    if child.state == "OPEN":
        return None if labels else "no batch label"
    if labels:
        return f"closed, labelled {', '.join(labels)}"
    return "closed"


class BatchState:
    def __init__(self, client: BatchGitHub) -> None:
        self._client: BatchGitHub = client

    def _selected(
        self,
        epic_number: int | None,
        targets: Sequence[int],
    ) -> tuple[Epic | None, list[ChildIssue]]:
        """The children a verb acts on, resolved in one place.

        With no `--epic` the targets are the membership: each expands to an
        epic's children or contributes itself. With an epic every listed
        number is checked against it before any caller writes a label, so a
        typo cannot half-apply a batch.
        """
        if epic_number is None:
            epic, children = None, self._members(targets)
        else:
            epic, children = self._under_epic(epic_number, targets)
        for child in children:
            labels = _batch_labels(child)
            if child.state == "OPEN" and len(labels) > 1:
                raise ConflictingLabelsError(child.number, labels)
        return epic, children

    def _under_epic(
        self, epic_number: int, issue_numbers: Sequence[int]
    ) -> tuple[Epic, list[ChildIssue]]:
        target = self._client.fetch_targets((epic_number,))[0]
        epic = Epic(number=target.number, title=target.title, state=target.state)
        children = list(target.members) if target.epic else []
        if not issue_numbers:
            return epic, children
        by_number = {child.number: child for child in children}
        for number in issue_numbers:
            if number not in by_number:
                raise NotAChildError(number, epic_number)
        wanted = set(issue_numbers)
        return epic, [child for child in children if child.number in wanted]

    def _targets(
        self, targets: Sequence[int], settled: SettledTargets | None = None
    ) -> list[Target]:
        if not targets:
            raise NoTargetsError
        if settled is None:
            return self._client.fetch_targets(targets)
        wanted = settled.unsettled(targets)
        fetched = self._client.fetch_targets(wanted) if wanted else []
        return settled.merge(targets, fetched)

    def _members(
        self, targets: Sequence[int], settled: SettledTargets | None = None
    ) -> list[ChildIssue]:
        """Every target's contribution, concatenated in argument order.

        An issue reachable twice keeps its earliest position: the stack order
        a caller wrote down is what it gets.
        """
        seen: dict[int, ChildIssue] = {}
        for target in self._targets(targets, settled):
            for child in target.members:
                seen.setdefault(child.number, child)
        return list(seen.values())

    def queue(
        self,
        epic_number: int | None = None,
        targets: Sequence[int] = (),
    ) -> QueueResult:
        epic, children = self._selected(epic_number, targets)
        labeled: list[int] = []
        skipped: list[SkippedIssue] = []
        for child in children:
            reason = _skip_reason(child, _batch_labels(child), None)
            if reason is not None:
                skipped.append(SkippedIssue(number=child.number, reason=reason))
                continue
            self._client.add_label(child.node_id, BatchLabel.QUEUED)
            labeled.append(child.number)
        return QueueResult(epic=epic, labeled=tuple(labeled), skipped=tuple(skipped))

    def unqueue(
        self,
        epic_number: int | None = None,
        targets: Sequence[int] = (),
    ) -> QueueResult:
        epic, children = self._selected(epic_number, targets)
        labeled: list[int] = []
        skipped: list[SkippedIssue] = []
        for child in children:
            reason = _skip_reason(child, _batch_labels(child), BatchLabel.QUEUED)
            if reason is not None:
                skipped.append(SkippedIssue(number=child.number, reason=reason))
                continue
            self._client.remove_label(child.node_id, BatchLabel.QUEUED)
            labeled.append(child.number)
        return QueueResult(epic=epic, labeled=tuple(labeled), skipped=tuple(skipped))

    def approve(
        self,
        epic_number: int | None = None,
        targets: Sequence[int] = (),
        guidance: str | None = None,
    ) -> ApproveResult:
        return self._plan(epic_number, targets, guidance, _approve_reason)

    def fast_track(
        self,
        epic_number: int | None = None,
        targets: Sequence[int] = (),
        guidance: str | None = None,
    ) -> ApproveResult:
        """queue and approve in one call: an unlabelled child lands on 'planned'."""
        return self._plan(epic_number, targets, guidance, _fast_track_reason)

    def _plan(
        self,
        epic_number: int | None,
        targets: Sequence[int],
        guidance: str | None,
        reason_of: Callable[[ChildIssue, list[BatchLabel]], str | None],
    ) -> ApproveResult:
        epic, children = self._selected(epic_number, targets)
        approved: list[int] = []
        skipped: list[SkippedIssue] = []
        refused: list[int] = []
        for child in children:
            labels = _batch_labels(child)
            reason = reason_of(child, labels)
            if reason is not None:
                skipped.append(SkippedIssue(number=child.number, reason=reason))
                continue
            if guidance is not None:
                if has_test_plan(child.body):
                    refused.append(child.number)
                else:
                    self._client.set_issue_body(
                        child.node_id, with_guidance(child.body, guidance)
                    )
            self._client.add_label(child.node_id, BatchLabel.PLANNED)
            if BatchLabel.QUEUED in labels:
                self._client.remove_label(child.node_id, BatchLabel.QUEUED)
            approved.append(child.number)
        return ApproveResult(
            epic=epic,
            approved=tuple(approved),
            skipped=tuple(skipped),
            guidance_refused=tuple(refused),
        )

    def set_state(self, issue_number: int, label: BatchLabel) -> None:
        """Add the new label before dropping the old one.

        An interrupted transition then leaves two labels, which every
        read path rejects loudly, rather than none, which would drop the
        issue out of the batch silently.
        """
        child = self._client.fetch_issue(issue_number)
        current = _batch_labels(child)
        if len(current) > 1:
            raise ConflictingLabelsError(child.number, current)
        if current == [label]:
            return
        self._client.add_label(child.node_id, label)
        for old in current:
            self._client.remove_label(child.node_id, old)

    def _sole_label(self, issue_number: int) -> tuple[ChildIssue, BatchLabel | None]:
        child = self._client.fetch_issue(issue_number)
        labels = _batch_labels(child)
        if len(labels) > 1:
            raise ConflictingLabelsError(child.number, labels)
        return child, labels[0] if labels else None

    def label_state(self, issue_number: int) -> LabelState:
        """Label and openness from one read: recovery needs both, and the
        dashboard's poll already presses the same rate limit."""
        child, label = self._sole_label(issue_number)
        return LabelState(
            label=label,
            closed=child.state != "OPEN",
            closed_by_merge=child.closed_by_merge,
        )

    def clear_state(self, issue_number: int) -> None:
        """Drop the batch label entirely: a later batch() no longer sees the issue."""
        child, label = self._sole_label(issue_number)
        if label is not None:
            self._client.remove_label(child.node_id, label)

    def batch(
        self, targets: Sequence[int], *, settled: SettledTargets | None = None
    ) -> Batch:
        issues: list[BatchIssue] = []
        dropped: list[DroppedChild] = []
        for child in self._members(targets, settled):
            labels = _batch_labels(child)
            reason = _status_reason(child, labels)
            if reason is not None:
                dropped.append(
                    DroppedChild(
                        number=child.number,
                        title=child.title,
                        state=child.state,
                        labels=tuple(labels),
                        reason=reason,
                        closed_by_merge=child.closed_by_merge,
                    )
                )
                continue
            if len(labels) > 1:
                raise ConflictingLabelsError(child.number, labels)
            issues.append(
                BatchIssue(
                    number=child.number,
                    title=child.title,
                    state=labels[0],
                    body=child.body,
                )
            )
        return Batch(
            targets=tuple(targets),
            issues=tuple(issues),
            dropped=tuple(dropped),
            rate_limit=self._client.rate_limit,
        )

    def finished(self, targets: Sequence[int]) -> tuple[BatchIssue, ...]:
        """Closed members still carrying a batch label: merging closes the issue
        but leaves the label, so this is what teardown has left to clean."""
        return self._labeled(self._members(targets), "CLOSED")

    def waiting_targets(self, targets: Sequence[int]) -> tuple[int, ...]:
        """The targets that still hold a queued issue, so a run knows whether
        more planned work is coming and can name what it waits on."""
        return tuple(
            target.number
            for target in self._targets(targets)
            if any(
                child.state == "OPEN" and BatchLabel.QUEUED in _batch_labels(child)
                for child in target.members
            )
        )

    def _labeled(
        self, children: Sequence[ChildIssue], issue_state: str
    ) -> tuple[BatchIssue, ...]:
        issues: list[BatchIssue] = []
        for child in children:
            labels = _batch_labels(child)
            if not labels or child.state != issue_state:
                continue
            if len(labels) > 1:
                raise ConflictingLabelsError(child.number, labels)
            issues.append(
                BatchIssue(
                    number=child.number,
                    title=child.title,
                    state=labels[0],
                    body=child.body,
                )
            )
        return tuple(issues)
