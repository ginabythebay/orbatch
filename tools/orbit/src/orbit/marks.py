from __future__ import annotations

from typing import final


@final
class Marks:
    """Dired-style marks, keyed by issue number so they outlive any
    one rendering of the rows."""

    def __init__(self) -> None:
        self._numbers: set[int] = set()

    def toggle(self, number: int) -> None:
        self._numbers ^= {number}

    def unmark(self, number: int) -> None:
        self._numbers.discard(number)

    def clear(self) -> None:
        self._numbers.clear()

    @property
    def count(self) -> int:
        return len(self._numbers)

    def __contains__(self, number: int) -> bool:
        return number in self._numbers
