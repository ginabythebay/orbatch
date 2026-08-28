"""Display widgets for the orbit TUI.

Everything visible on the main screen lives here: the epics tree
(`IssueTree`), the flat issue list shared by the sprint and backlog
views (`IssueList`), and the one-row `StatusBar`. These widgets only
render `orbit.github.models` values that the app has already fetched —
they never call the GitHub API. Data loading, keybinding policy, and
view switching live in orbit.tui.app.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, final, override

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import OptionList, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from orbit.filtering import partition_filtered
from orbit.github.models import Epic, Issue, MilestoneIssue, SubIssueData
from orbit.text_output import filtered_run_label
from orbit.tree import FilteredRun, TreeItem, build_tree


class Palette(StrEnum):
    """Every rich style the TUI uses, named by role.

    One palette so meaning-bearing styles (closed-issue dimming,
    error red, ...) cannot drift between the tree, the list, and the
    detail/help screens. Members are strings and pass directly to
    `Text.append`.
    """

    CLOSED = "dim green"
    OPEN = "green"
    EMPHASIS = "bold"
    COUNT = "cyan"
    KEY = "bold cyan"
    ERROR = "red"


def issue_text(
    number: int,
    state: str,
    title: str,
    open_count: int | None = None,
    total_count: int | None = None,
) -> Text:
    """Render an issue as a single styled line.

    Closed issues are dimmed green; epic nodes show open/total counts.
    """
    closed = state == "CLOSED"
    text = Text()
    text.append(f"#{number}", Palette.CLOSED if closed else Palette.EMPHASIS)
    if open_count is not None and total_count is not None:
        text.append(
            f" {open_count}/{total_count}",
            Palette.CLOSED if closed else Palette.COUNT,
        )
    text.append(f" {title}", Palette.CLOSED if closed else "")
    return text


def filtered_text(
    count: int,
    open_count: int | None = None,
    total_count: int | None = None,
) -> Text:
    """Render a run of filtered-out issues as a single dimmed line."""
    text = Text()
    if open_count is not None and total_count is not None:
        text.append(f"{open_count}/{total_count} ", Palette.CLOSED)
    text.append(filtered_run_label(count), Palette.CLOSED)
    return text


@dataclass
class IssueNodeData:
    """Payload attached to each tree node."""

    number: int
    state: str
    title: str
    children_loaded: bool = True

    @property
    def key(self) -> int:
        return self.number

    @property
    def fetch_number(self) -> int:
        return self.number


@dataclass
class FilteredNodeData:
    """Payload for a node standing in for issues the filter dropped.

    Keyed for capture/restore on the negated first covered number:
    expanding a run reveals a real node for that same issue, so the
    sign is what keeps the two apart.
    """

    numbers: tuple[int, ...]
    open_count: int | None = None
    total_count: int | None = None
    children_loaded: bool = True

    @property
    def key(self) -> int:
        return -self.numbers[0]

    @property
    def fetch_number(self) -> int:
        return self.numbers[0]


@dataclass
class SectionNodeData:
    """Payload for the STANDALONE section heading.

    Keyed on 0, which no issue number can take, so capture/restore
    tracks the section's expansion without colliding with either
    `IssueNodeData.key` (+number) or `FilteredNodeData.key` (-number).
    """

    children_loaded: bool = True

    @property
    def key(self) -> int:
        return 0

    @property
    def fetch_number(self) -> int:
        return 0


type TreeItemData = IssueNodeData | FilteredNodeData | SectionNodeData


@dataclass(frozen=True)
class TreeState:
    """A snapshot of the tree's cursor and expansion, by issue number.

    Captured before a refresh so `load_epics` can restore which node
    the cursor sat on and which epics were expanded — see
    `IssueTree.capture_state`.
    """

    selected: int | None
    expanded: frozenset[int] = field(default_factory=frozenset)
    had_section: bool = False


def _is_open(data: SubIssueData) -> bool:
    return data.state == "OPEN"


def _is_open_issue(issue: MilestoneIssue) -> bool:
    return issue.state == "OPEN"


def _by_number(subs: Sequence[SubIssueData]) -> dict[int, SubIssueData]:
    """Every issue in `subs` and below it, keyed by number, so a run
    placeholder can render the issues its `numbers` name."""
    found: dict[int, SubIssueData] = {}
    for sub in subs:
        found[sub.number] = sub
        found.update(_by_number(sub.children))
    return found


@dataclass(frozen=True)
class _EpicRun:
    """A placeholder over dropped epics, with the epics it reveals.

    `epics` is empty when the placeholder fetches on expand instead —
    a single closed epic whose open work lives one level down.
    """

    data: FilteredNodeData
    epics: tuple[Epic, ...] = ()


def _group_epics(epics: list[Epic], hide_closed: bool) -> list[Epic | _EpicRun]:
    """Collapse closed epics into runs, mirroring `orbit.tree.build_tree`.

    Epics arrive without their children, so `open_count > 0` stands in
    for that module's surviving-descendant test.
    """
    if not hide_closed:
        return list(epics)
    items: list[Epic | _EpicRun] = []
    run: list[Epic] = []

    def flush() -> None:
        if run:
            data = FilteredNodeData(numbers=tuple(epic.number for epic in run))
            items.append(_EpicRun(data=data, epics=tuple(run)))
            run.clear()

    for epic in epics:
        if epic.state != "CLOSED":
            flush()
            items.append(epic)
        elif epic.open_count > 0:
            flush()
            items.append(
                _EpicRun(
                    data=FilteredNodeData(
                        numbers=(epic.number,),
                        open_count=epic.open_count,
                        total_count=epic.total_count,
                        children_loaded=False,
                    )
                )
            )
        else:
            run.append(epic)
    flush()
    return items


@final
class IssueTree(Tree[TreeItemData]):
    """Sprint epics with lazily fetched sub-issue children.

    Right arrow expands the node under the cursor, left arrow collapses
    it. Expanding a node whose children have not been loaded posts
    `Tree.NodeExpanded`; the app fetches and calls `populate_children`.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("right", "expand_node", "Expand", show=False),
        Binding("left", "collapse_node", "Collapse", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("Epics", id=id)
        self.show_root = False
        self.auto_expand = False
        self.hide_closed = False
        # Restoration state, set by load_epics(restore=...) and consumed
        # incrementally as epics expand and populate. Only epics fetch
        # lazily — whether at the top level or revealed under a run
        # placeholder; their subtrees arrive whole, so nested
        # re-expansion is purely local (see populate_children).
        self._restore_expanded: frozenset[int] = frozenset()
        self._restore_cursor: int | None = None
        self._restore_pending: set[int] = set()
        # A refresh is incidental to the cursor, so a run placeholder
        # covering the saved issue wins over it; a goto asks for that
        # issue by number and must open the run instead.
        self._restore_retargets: bool = False

    def _walk(self, node: TreeNode[TreeItemData]) -> Iterator[TreeNode[TreeItemData]]:
        """Yield every descendant of `node` (depth-first, node excluded)."""
        for child in node.children:
            yield child
            yield from self._walk(child)

    def _find_node(self, number: int) -> TreeNode[TreeItemData] | None:
        for node in self._walk(self.root):
            data = node.data
            if isinstance(data, IssueNodeData) and data.number == number:
                return node
        return None

    def _node_for_key(self, key: int) -> TreeNode[TreeItemData] | None:
        for node in self._walk(self.root):
            if node.data is not None and node.data.key == key:
                return node
        return None

    def root_numbers(self) -> frozenset[int]:
        """The epics that fetch lazily, and so the valid targets for
        `reveal`. Hide-closed hands some of them to a placeholder, or
        moves them a level down under one, without making them any
        less top-level."""
        return frozenset(
            node.data.fetch_number
            for node in self._walk(self.root)
            if node.data is not None and not node.data.children_loaded
        )

    def _find_epic_node(self, number: int) -> TreeNode[TreeItemData] | None:
        """The node that fetches `number`'s children — the epic itself,
        or the placeholder standing in for it."""
        for node in self._walk(self.root):
            data = node.data
            if (
                data is not None
                and not data.children_loaded
                and data.fetch_number == number
            ):
                return node
        return self._find_node(number)

    def reveal_loaded(self, number: int) -> bool:
        """Place the cursor on `number` if the tree already holds it.

        Expanding is purely local — every ancestor of a materialised
        node is itself materialised — so this costs no fetch.
        """
        if self._find_node(number) is None:
            return False
        self._restore_cursor = number
        self._restore_retargets = False
        self._expand_ancestors(number)
        self._restore_cursor_if_present()
        if not self._restore_pending:
            self._finish_restore()
        return True

    def reveal(self, number: int, ancestor: int) -> None:
        """Place the cursor on `number`, under top-level epic `ancestor`.

        When the ancestor's children have not been fetched, expanding it
        posts `Tree.NodeExpanded` and the reveal resumes in
        `populate_children` once they arrive.
        """
        node = self._find_epic_node(ancestor)
        if node is None:
            return
        self._restore_cursor = number
        self._restore_retargets = False
        if node.data is not None and not node.data.children_loaded:
            self._restore_pending.add(node.data.key)
            node.expand()
            return
        self._expand_ancestors(number)
        self._restore_cursor_if_present()
        if not self._restore_pending:
            self._finish_restore()

    def _expand_ancestors(self, key: int) -> None:
        node = self._node_for_key(key)
        if node is None:
            return
        parent = node.parent
        while parent is not None:
            if parent.allow_expand and not parent.is_expanded:
                parent.expand()
            parent = parent.parent

    def _resolve_restore_cursor(self) -> None:
        """Re-point the saved cursor at the node the refreshed tree
        actually holds for it.

        A run that swallowed the issue takes the cursor — the outermost
        one still collapsed, so no placeholder has to open to show it.
        A run the user had open before the refresh is already expanded
        by now and keeps its hands off. Going the other way, a
        placeholder key whose run is gone falls back to the issue it
        negates, which the unfiltered tree shows again.
        """
        if self._restore_cursor is None or not self._restore_retargets:
            return
        node = self._node_for_key(self._restore_cursor)
        if node is None and self._restore_cursor < 0:
            node = self._node_for_key(-self._restore_cursor)
            if node is not None:
                self._restore_cursor = -self._restore_cursor
        if node is None:
            self._retarget_to_covering_run(self._restore_cursor)
            return
        parent = node.parent
        while parent is not None:
            if isinstance(parent.data, FilteredNodeData) and not parent.is_expanded:
                self._restore_cursor = parent.data.key
            parent = parent.parent

    def _retarget_to_covering_run(self, number: int) -> None:
        """Hand the cursor to a run that covers `number` without holding
        a node for it — a closed epic whose children have yet to be
        fetched, or one whose run kept only its surviving descendants.

        The walk is depth-first, so the first match is the outermost.
        """
        for node in self._walk(self.root):
            data = node.data
            if (
                isinstance(data, FilteredNodeData)
                and not node.is_expanded
                and number in data.numbers
            ):
                self._restore_cursor = data.key
                return

    def capture_state(self) -> TreeState:
        """Snapshot the cursor and the set of currently expanded issues."""
        expanded = frozenset(
            node.data.key
            for node in self._walk(self.root)
            if node.data is not None and node.is_expanded
        )
        cursor = self.cursor_node
        selected = (
            cursor.data.key if cursor is not None and cursor.data is not None else None
        )
        had_section = any(
            isinstance(node.data, SectionNodeData) for node in self._walk(self.root)
        )
        return TreeState(selected=selected, expanded=expanded, had_section=had_section)

    def load_epics(
        self,
        epics: list[Epic],
        standalone: Sequence[MilestoneIssue] = (),
        restore: TreeState | None = None,
    ) -> None:
        self.clear()
        for item in _group_epics(epics, self.hide_closed):
            if isinstance(item, Epic):
                self._add_epic(self.root, item)
                continue
            node = self.root.add(
                filtered_text(
                    len(item.data.numbers),
                    item.data.open_count,
                    item.data.total_count,
                ),
                data=item.data,
            )
            for epic in item.epics:
                self._add_epic(node, epic)
        self._add_standalone(
            standalone, expand=restore is None or not restore.had_section
        )
        self.root.expand()
        self._restore_pending = set()
        if restore is None:
            self._restore_expanded = frozenset()
            self._restore_cursor = None
            self._restore_retargets = False
            if epics or standalone:
                self.cursor_line = 0
            return
        self._restore_expanded = restore.expanded
        self._restore_cursor = restore.selected
        self._restore_retargets = True
        self._restore_subtree(self.root)
        self._resolve_restore_cursor()
        self._restore_cursor_if_present()
        if not self._restore_pending:
            self._finish_restore()

    def _add_standalone(self, issues: Sequence[MilestoneIssue], expand: bool) -> None:
        """Add the STANDALONE section, unless the milestone has no
        standalone issues at all.

        Hide-closed never removes the section, only collapses it into a
        run — the same way it treats the epics, and what lets goto
        reveal a closed standalone issue.

        A section already on screen keeps the expansion the user left
        it in — `_restore_subtree` re-expands it from the captured
        state. `expand` covers the other case: a section that was not
        there before starts open, like any fresh load.
        """
        if not issues:
            return
        rows = (
            partition_filtered(issues, _is_open_issue)
            if self.hide_closed
            else list(issues)
        )
        covered = {issue.number: issue for issue in issues}
        section = self.root.add(Text("STANDALONE", Palette.EMPHASIS), SectionNodeData())
        for row in rows:
            if isinstance(row, FilteredRun):
                node = section.add(
                    filtered_text(row.count),
                    data=FilteredNodeData(numbers=row.numbers),
                )
                for number in row.numbers:
                    self._add_standalone_issue(node, covered[number])
                continue
            self._add_standalone_issue(section, row)
        if expand:
            section.expand()

    def _add_standalone_issue(
        self, parent: TreeNode[TreeItemData], issue: MilestoneIssue
    ) -> None:
        parent.add_leaf(
            issue_text(issue.number, issue.state, issue.title),
            data=IssueNodeData(
                number=issue.number, state=issue.state, title=issue.title
            ),
        )

    def _add_epic(self, parent: TreeNode[TreeItemData], epic: Epic) -> None:
        data = IssueNodeData(
            number=epic.number,
            state=epic.state,
            title=epic.title,
            children_loaded=False,
        )
        label = issue_text(
            epic.number,
            epic.state,
            epic.title,
            epic.open_count,
            epic.total_count,
        )
        if epic.total_count > 0:
            parent.add(label, data=data)
        else:
            parent.add_leaf(label, data=data)

    def populate_children(
        self,
        node: TreeNode[TreeItemData],
        children: list[SubIssueData],
    ) -> None:
        if node.data is not None:
            node.data.children_loaded = True
        node.remove_children()
        if self.hide_closed:
            covered = _by_number(children)
            for item in build_tree(children, _is_open):
                self._add_tree_item(node, item, covered)
        else:
            for child in children:
                self._add_sub_issue(node, child)
        key = node.data.key if node.data is not None else None
        if key is not None and key in self._restore_pending:
            self._restore_pending.discard(key)
            self._restore_subtree(node)
            self._resolve_restore_cursor()
            if self._restore_cursor is not None:
                self._expand_ancestors(self._restore_cursor)
            self._restore_cursor_if_present()
            if not self._restore_pending:
                self._finish_restore()

    def _restore_subtree(self, node: TreeNode[TreeItemData]) -> None:
        """Re-expand descendants of `node` that were expanded before refresh.

        Descendants that have not loaded yet — epics revealed under a
        run placeholder — join `_restore_pending`, so the restore only
        finishes once their fetches land.
        """
        for descendant in self._walk(node):
            data = descendant.data
            if (
                data is None
                or data.key not in self._restore_expanded
                or not descendant.allow_expand
            ):
                continue
            if not data.children_loaded:
                self._restore_pending.add(data.key)
            if not descendant.is_expanded:
                descendant.expand()

    def _restore_cursor_if_present(self) -> None:
        """Place the cursor on the saved issue once its node is visible."""
        if self._restore_cursor is None:
            return
        # Reading last_line forces the tree to rebuild its line cache, so
        # the node.line values below reflect the expansions just applied
        # rather than the pre-refresh layout.
        _ = self.last_line
        for node in self._walk(self.root):
            if (
                node.data is not None
                and node.data.key == self._restore_cursor
                and node.line != -1
            ):
                self.cursor_line = node.line
                self.scroll_to_line(node.line, animate=False)
                return

    def _finish_restore(self) -> None:
        """Clear restore state so later user navigation isn't second-guessed."""
        self._restore_expanded = frozenset()
        self._restore_cursor = None
        self._restore_retargets = False

    def _add_tree_item(
        self,
        parent: TreeNode[TreeItemData],
        item: TreeItem,
        covered: dict[int, SubIssueData],
    ) -> None:
        if isinstance(item, FilteredRun):
            node = parent.add(
                filtered_text(item.count, item.open_count, item.total_count),
                data=FilteredNodeData(
                    numbers=item.numbers,
                    open_count=item.open_count,
                    total_count=item.total_count,
                ),
            )
            for child in item.children:
                self._add_tree_item(node, child, covered)
            # A run covers issues whose whole subtree was filtered out,
            # so expanding it is the only way back to them.
            if not item.children:
                for number in item.numbers:
                    self._add_sub_issue(node, covered[number])
            return
        data = IssueNodeData(number=item.number, state=item.state, title=item.title)
        label = issue_text(
            item.number, item.state, item.title, item.open_count, item.total_count
        )
        if not item.children:
            parent.add_leaf(label, data=data)
            return
        node = parent.add(label, data=data)
        for child in item.children:
            self._add_tree_item(node, child, covered)

    def _add_sub_issue(
        self,
        parent: TreeNode[TreeItemData],
        sub: SubIssueData,
    ) -> None:
        data = IssueNodeData(number=sub.number, state=sub.state, title=sub.title)
        if sub.children:
            open_count = sum(1 for c in sub.children if c.state == "OPEN")
            label = issue_text(
                sub.number,
                sub.state,
                sub.title,
                open_count,
                len(sub.children),
            )
            node = parent.add(label, data=data)
            for child in sub.children:
                self._add_sub_issue(node, child)
        else:
            parent.add_leaf(issue_text(sub.number, sub.state, sub.title), data=data)

    @property
    def selected_issue_number(self) -> int | None:
        """The issue under the cursor, or None on a run placeholder —
        a placeholder is not an issue, so issue actions no-op there."""
        node = self.cursor_node
        if node is None or not isinstance(node.data, IssueNodeData):
            return None
        return node.data.number

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand and not node.is_expanded:
            node.expand()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is not None and node.is_expanded:
            node.collapse()


@final
class IssueList(OptionList):
    """Flat list of issues from one milestone.

    Each instance is one view (sprint, backlog) and carries that
    view's query configuration as plain data. The app reads it to
    fetch; the widget itself never calls the GitHub API.
    """

    def __init__(
        self,
        *,
        milestone: str,
        item_name: str,
        soon_filterable: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.milestone = milestone
        self.item_name = item_name
        self.soon_filterable = soon_filterable
        self.soon_only = False
        self.hide_closed = False

    @property
    def label_filter(self) -> str | None:
        return "soon" if self.soon_only else None

    def load_issues(self, issues: Sequence[Issue], select: int | None = None) -> None:
        self.clear_options()
        rows = (
            partition_filtered(issues, lambda issue: issue.state != "CLOSED")
            if self.hide_closed
            else issues
        )
        for row in rows:
            if isinstance(row, FilteredRun):
                # Inert: an OptionList has no expansion affordance, and
                # every issue a run covers is a closed leaf.
                self.add_option(Option(filtered_text(row.count), disabled=True))
                continue
            self.add_option(
                Option(
                    issue_text(row.number, row.state, row.title),
                    id=str(row.number),
                )
            )
        if not issues:
            return
        if select is not None and self.highlight_issue(select):
            return
        self.highlighted = self._first_selectable()

    def _first_selectable(self) -> int | None:
        for index in range(self.option_count):
            if not self.get_option_at_index(index).disabled:
                return index
        return None

    def highlight_issue(self, number: int) -> bool:
        """Move the highlight to `number`; False if it isn't in the list."""
        for index in range(self.option_count):
            if self.get_option_at_index(index).id == str(number):
                self.highlighted = index
                return True
        return False

    @property
    def selected_issue_number(self) -> int | None:
        if self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        if option.id is None:
            return None
        return int(option.id)


@final
class StatusBar(Horizontal):
    """One-row bar: last action/load result on the left, help hint right."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $panel;
    }
    StatusBar #status-message {
        width: 1fr;
        padding: 0 1;
    }
    StatusBar #help-hint {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }
    """

    @override
    def compose(self) -> ComposeResult:
        yield Static("", id="status-message")
        yield Static("? help", id="help-hint")

    def set_status(self, message: str) -> None:
        self.query_one("#status-message", Static).update(message)
