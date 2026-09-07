from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from ghgql.labels import CONFLICT, BatchLabel, glyph


class Palette(StrEnum):
    """Every rich style orbit uses, named by role.

    One palette so meaning-bearing styles (closed-issue dimming,
    error red, ...) cannot drift between the tree, the list, and the
    detail/help screens, or between the TUI and the CLI listings.
    Members are strings and pass directly to `Text.append`.
    """

    CLOSED = "dim green"
    OPEN = "green"
    EMPHASIS = "bold"
    COUNT = "cyan"
    KEY = "bold cyan"
    ERROR = "red"
    WARNING = "yellow"


_WARNING_GLYPHS = frozenset({glyph((BatchLabel.STUCK,)), CONFLICT})


def glyph_span(labels: Sequence[str]) -> tuple[str, Palette | str]:
    mark = glyph(labels)
    return mark, Palette.WARNING if mark in _WARNING_GLYPHS else ""
