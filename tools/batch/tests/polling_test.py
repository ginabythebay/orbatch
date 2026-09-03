from __future__ import annotations

from collections.abc import Sequence

from batch.models import ChildIssue, Target
from batch.polling import SettledTargets


def _child(number: int, state: str) -> ChildIssue:
    return ChildIssue(
        node_id=f"I_{number}",
        number=number,
        state=state,
        title=f"Issue {number}",
        body="",
        labels=("planned",),
        closed_by_merge=False,
    )


def _target(number: int, *states: str) -> Target:
    return Target(
        number=number,
        title=f"Epic {number}",
        state="OPEN",
        members=tuple(_child(number * 10 + i, s) for i, s in enumerate(states)),
        epic=True,
    )


def _round(
    cache: SettledTargets, targets: Sequence[int], *fetched: Target
) -> list[int]:
    wanted = cache.unsettled(targets)
    _ = cache.merge(targets, [t for t in fetched if t.number in set(wanted)])
    return wanted


class TestSettledTargets:
    def test_an_open_target_is_asked_for_every_time(self) -> None:
        cache = SettledTargets()
        live = _target(1, "OPEN", "CLOSED")
        assert _round(cache, (1,), live) == [1]
        assert _round(cache, (1,), live) == [1]

    def test_a_target_whose_members_have_all_closed_is_asked_for_once(self) -> None:
        cache = SettledTargets()
        done = _target(1, "CLOSED", "CLOSED")
        assert _round(cache, (1,), done) == [1]
        assert _round(cache, (1,), done) == []

    def test_a_settled_target_still_contributes_its_members(self) -> None:
        cache = SettledTargets()
        done = _target(1, "CLOSED")
        _ = cache.merge((1,), [done])
        assert cache.merge((1,), []) == [done]

    def test_targets_come_back_in_the_order_asked_for(self) -> None:
        cache = SettledTargets()
        done, live = _target(1, "CLOSED"), _target(2, "OPEN")
        _ = cache.merge((1, 2), [done, live])
        assert [t.number for t in cache.merge((2, 1), [live])] == [2, 1]

    def test_only_the_unsettled_half_of_a_mixed_batch_is_asked_for(self) -> None:
        cache = SettledTargets()
        done, live = _target(1, "CLOSED"), _target(2, "OPEN")
        assert _round(cache, (1, 2), done, live) == [1, 2]
        assert _round(cache, (1, 2), done, live) == [2]
