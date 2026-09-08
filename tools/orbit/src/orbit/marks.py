from __future__ import annotations

from typing import final


@final
class Marks:
    """Dired-style marks, keyed by issue number so they outlive any
    one rendering of the rows. Marking order is kept: it is the order
    a batch verb receives its targets in."""

    def __init__(self) -> None:
        self._numbers: dict[int, None] = {}

    def toggle(self, number: int) -> None:
        if number in self._numbers:
            del self._numbers[number]
        else:
            self._numbers[number] = None

    def unmark(self, number: int) -> None:
        _ = self._numbers.pop(number, None)

    def clear(self) -> None:
        self._numbers.clear()

    @property
    def count(self) -> int:
        return len(self._numbers)

    @property
    def numbers(self) -> tuple[int, ...]:
        return tuple(self._numbers)

    def __contains__(self, number: int) -> bool:
        return number in self._numbers
