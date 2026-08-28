from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from orbit.github.models import SubIssueData


class TreeNode(BaseModel, frozen=True):
    number: int
    state: str
    title: str
    open_count: int | None
    total_count: int | None
    children: tuple[TreeItem, ...]


class FilteredRun(BaseModel, frozen=True):
    count: int
    numbers: tuple[int, ...]
    open_count: int | None = None
    total_count: int | None = None
    children: tuple[TreeItem, ...] = ()


type TreeItem = TreeNode | FilteredRun

TreeNode.model_rebuild()
FilteredRun.model_rebuild()


type _FilterType = Callable[[SubIssueData], bool]


def build_tree(
    sub_issues: list[SubIssueData], node_filter: _FilterType
) -> list[TreeItem]:
    return _build_level(sub_issues, node_filter)


def _build_level(
    sub_issues: tuple[SubIssueData, ...] | list[SubIssueData],
    node_filter: _FilterType,
) -> list[TreeItem]:
    items: list[TreeItem] = []
    run: list[int] = []

    def flush() -> None:
        if run:
            items.append(FilteredRun(count=len(run), numbers=tuple(run)))
            run.clear()

    for data in sub_issues:
        if node_filter(data):
            flush()
            items.append(_build_node(data, node_filter))
        elif _has_surviving_descendant(data, node_filter):
            flush()
            open_count, total_count = _counts(data)
            items.append(
                FilteredRun(
                    count=1,
                    numbers=(data.number,),
                    open_count=open_count,
                    total_count=total_count,
                    children=tuple(_build_level(data.children, node_filter)),
                )
            )
        else:
            run.append(data.number)
    flush()
    return items


def _has_surviving_descendant(data: SubIssueData, node_filter: _FilterType) -> bool:
    return any(
        node_filter(c) or _has_surviving_descendant(c, node_filter)
        for c in data.children
    )


def _counts(data: SubIssueData) -> tuple[int, int]:
    return sum(1 for c in data.children if c.state == "OPEN"), len(data.children)


def _build_node(data: SubIssueData, node_filter: _FilterType) -> TreeNode:
    children = tuple(_build_level(data.children, node_filter))
    if children:
        open_count, total_count = _counts(data)
    else:
        open_count = None
        total_count = None
    return TreeNode(
        number=data.number,
        state=data.state,
        title=data.title,
        open_count=open_count,
        total_count=total_count,
        children=children,
    )
