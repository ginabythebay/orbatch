from __future__ import annotations

from orbit.github.models import SubIssueData
from orbit.tree import FilteredRun, TreeNode, build_tree


def filter_nothing(_: SubIssueData) -> bool:
    return True


def filter_open(data: SubIssueData) -> bool:
    return data.state == "OPEN"


def _leaf(number: int, state: str) -> SubIssueData:
    return SubIssueData(
        number=number, state=state, title=f"Issue {number}", children=()
    )


class TestBuildTree:
    def test_epic_gets_counts_leaf_gets_none(self) -> None:
        data = [
            SubIssueData(number=1, state="OPEN", title="Leaf", children=()),
            SubIssueData(
                number=2,
                state="OPEN",
                title="Epic",
                children=(
                    SubIssueData(number=3, state="OPEN", title="A", children=()),
                    SubIssueData(number=4, state="CLOSED", title="B", children=()),
                ),
            ),
        ]
        nodes = build_tree(data, filter_nothing)
        assert len(nodes) == 2
        leaf, epic = nodes[0], nodes[1]
        assert leaf.open_count is None
        assert leaf.total_count is None
        assert epic.open_count == 1
        assert epic.total_count == 2
        assert len(epic.children) == 2


class TestFilteredRuns:
    def test_adjacent_dropped_leaves_merge_into_one_run(self) -> None:
        data = [_leaf(1, "CLOSED"), _leaf(2, "CLOSED"), _leaf(3, "CLOSED")]
        items = build_tree(data, filter_open)
        assert items == [FilteredRun(count=3, numbers=(1, 2, 3))]

    def test_survivor_breaks_a_run(self) -> None:
        data = [_leaf(1, "CLOSED"), _leaf(2, "OPEN"), _leaf(3, "CLOSED")]
        items = build_tree(data, filter_open)
        assert items[0] == FilteredRun(count=1, numbers=(1,))
        assert isinstance(items[1], TreeNode)
        assert items[1].number == 2
        assert items[2] == FilteredRun(count=1, numbers=(3,))

    def test_trailing_run_is_emitted(self) -> None:
        data = [_leaf(1, "OPEN"), _leaf(2, "CLOSED"), _leaf(3, "CLOSED")]
        items = build_tree(data, filter_open)
        assert items[-1] == FilteredRun(count=2, numbers=(2, 3))

    def test_closed_only_subtree_merges_into_the_run(self) -> None:
        middle = SubIssueData(
            number=2,
            state="CLOSED",
            title="Epic",
            children=(_leaf(20, "CLOSED"), _leaf(21, "CLOSED")),
        )
        items = build_tree(
            [_leaf(1, "CLOSED"), middle, _leaf(3, "CLOSED")], filter_open
        )
        assert items == [FilteredRun(count=3, numbers=(1, 2, 3))]

    def test_surviving_child_gives_its_parent_its_own_placeholder(self) -> None:
        middle = SubIssueData(
            number=2,
            state="CLOSED",
            title="Epic",
            children=(_leaf(20, "OPEN"), _leaf(21, "CLOSED")),
        )
        items = build_tree(
            [_leaf(1, "CLOSED"), middle, _leaf(3, "CLOSED")], filter_open
        )
        assert items[0] == FilteredRun(count=1, numbers=(1,))
        assert items[2] == FilteredRun(count=1, numbers=(3,))
        placeholder = items[1]
        assert isinstance(placeholder, FilteredRun)
        assert placeholder.count == 1
        assert placeholder.numbers == (2,)
        assert placeholder.open_count == 1
        assert placeholder.total_count == 2
        child = placeholder.children[0]
        assert isinstance(child, TreeNode)
        assert child.number == 20
        assert placeholder.children[1] == FilteredRun(count=1, numbers=(21,))

    def test_dropped_children_of_a_surviving_parent_become_a_run(self) -> None:
        epic = SubIssueData(
            number=30,
            state="OPEN",
            title="Epic",
            children=(_leaf(31, "OPEN"), _leaf(32, "CLOSED"), _leaf(33, "CLOSED")),
        )
        items = build_tree([epic], filter_open)
        assert len(items) == 1
        node = items[0]
        assert isinstance(node, TreeNode)
        assert node.open_count == 1
        assert node.total_count == 3
        first = node.children[0]
        assert isinstance(first, TreeNode)
        assert first.number == 31
        assert node.children[1] == FilteredRun(count=2, numbers=(32, 33))
