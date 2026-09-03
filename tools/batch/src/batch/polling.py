from __future__ import annotations

from collections.abc import Sequence

from batch.models import Target


def _settled(target: Target) -> bool:
    return all(member.state != "OPEN" for member in target.members)


class SettledTargets:
    """Targets whose members have all closed, remembered across polls.

    Nothing a run does reopens an issue, so re-reading a settled target buys
    the same answer at the same price. Only the dashboard's poll consults
    this; every read a run acts on goes to GitHub.
    """

    def __init__(self) -> None:
        self._known: dict[int, Target] = {}

    def unsettled(self, targets: Sequence[int]) -> list[int]:
        return [number for number in targets if number not in self._known]

    def merge(self, targets: Sequence[int], fetched: Sequence[Target]) -> list[Target]:
        for target in fetched:
            if _settled(target):
                self._known[target.number] = target
        by_number = {target.number: target for target in fetched}
        return [
            by_number[number] if number in by_number else self._known[number]
            for number in targets
        ]
