from __future__ import annotations

from collections.abc import Callable

from orbit.github.models import Epic, Issue

ISSUE_SORT_KEYS = ("number", "state", "title")
EPIC_SORT_KEYS = (*ISSUE_SORT_KEYS, "progress")

_ISSUE_KEY_FUNCS: dict[str, Callable[[Issue], int | str]] = {
    "number": lambda i: i.number,
    "state": lambda i: i.state,
    "title": lambda i: i.title,
}


def _progress_ratio(epic: Epic) -> float:
    if epic.total_count == 0:
        return float("inf")
    return -(epic.open_count / epic.total_count)


_EPIC_KEY_FUNCS: dict[str, Callable[[Epic], int | str | float]] = {
    "number": lambda e: e.number,
    "state": lambda e: e.state,
    "title": lambda e: e.title,
    "progress": _progress_ratio,
}


def sort_issues[T: Issue](
    issues: list[T],
    key: str | None,
    reverse: bool,
) -> list[T]:
    if key is None:
        return list(issues)
    if key not in ISSUE_SORT_KEYS:
        valid = ", ".join(ISSUE_SORT_KEYS)
        raise ValueError(f"Invalid sort key {key!r}. Valid keys: {valid}")
    result = sorted(issues, key=_ISSUE_KEY_FUNCS[key])
    if reverse:
        result.reverse()
    return result


def sort_epics(
    epics: list[Epic],
    key: str | None,
    reverse: bool,
) -> list[Epic]:
    if key is None:
        return list(epics)
    if key not in EPIC_SORT_KEYS:
        valid = ", ".join(EPIC_SORT_KEYS)
        raise ValueError(f"Invalid sort key {key!r}. Valid keys: {valid}")
    result = sorted(epics, key=_EPIC_KEY_FUNCS[key])
    if reverse:
        result.reverse()
    return result
