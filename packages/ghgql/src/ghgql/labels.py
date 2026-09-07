from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class BatchLabel(StrEnum):
    QUEUED = "queued"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    READY_FOR_REVIEW = "ready-for-review"
    STUCK = "stuck"


_NAMES = frozenset(BatchLabel)


def batch_labels(names: Iterable[str]) -> list[BatchLabel]:
    return [BatchLabel(name) for name in names if name in _NAMES]


_GLYPHS = {
    BatchLabel.QUEUED: "q",
    BatchLabel.PLANNED: "p",
    BatchLabel.IMPLEMENTING: "i",
    BatchLabel.READY_FOR_REVIEW: "r",
    BatchLabel.STUCK: "s",
}


NO_LABEL = " "
CONFLICT = "!"


def glyph(names: Iterable[str]) -> str:
    """One character standing for the issue's batch state.

    A conflict renders as `!` rather than raising: a viewer's job is to
    surface the bad issue, not to refuse to draw it.
    """
    labels = batch_labels(names)
    if not labels:
        return NO_LABEL
    if len(labels) > 1:
        return CONFLICT
    return _GLYPHS[labels[0]]
