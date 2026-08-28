"""The Textual application for the orbit TUI.

`OrbitApp` is the orchestration layer: it composes the widgets (one
per view: epics tree, sprint list, backlog list), owns the view
state, declares the keybindings, and runs every GitHub fetch and
mutation in background workers so the UI never blocks. Widgets
(orbit.tui.widgets) only render data this module hands them; overlay
screens (orbit.tui.screens) are pushed from here and report back via
dismiss results or callbacks. `run_tui` is the CLI entry point.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from enum import Enum, auto
from typing import ClassVar, final, override

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import OptionList, Tree
from textual.widgets.tree import TreeNode

from ghgql.errors import IssueNotFoundError
from orbit.config import CommandMode, CustomCommand, Milestones, ProjectConfig
from orbit.core import open_url, run_attached, spawn
from orbit.filtering import partition_standalone
from orbit.github.client import GitHubClient
from orbit.github.models import (
    AlreadyDoneError,
    CloseReason,
    Epic,
    MilestoneIssue,
    Surface,
)
from orbit.github.orchestrators import close_issue, move_issue, schedule_issue
from orbit.tui.screens import (
    BranchPromptScreen,
    DetailScreen,
    EpicPickerScreen,
    HelpScreen,
    IssueNumberPromptScreen,
    MilestonePickerScreen,
)
from orbit.tui.widgets import (
    IssueList,
    IssueNodeData,
    IssueTree,
    StatusBar,
    TreeItemData,
)

# App actions blocked while a modal or detail screen is on the stack
# (see check_action). New view/mutation bindings must be added here,
# or they will fire through modals against a hidden widget. "help" and
# "quit" are deliberately absent: help may be opened from any screen,
# and quit is shadowed per-screen instead (see BINDINGS).
_MAIN_SCREEN_ACTIONS = frozenset(
    {
        "show_epics",
        "show_sprint",
        "show_backlog",
        "toggle_soon",
        "toggle_hide_closed",
        "refresh",
        "move",
        "schedule",
        "close_issue",
        "edit",
        "custom_command",
        "goto_issue",
    }
)

# Issue-targeted actions allowed from the detail screen; each dismisses it
# before acting on the issue it displays. View switches, refresh, and the
# filter toggles remain blocked there — they have no meaning without the
# main view.
_DETAIL_SCREEN_ACTIONS = frozenset(
    {"move", "schedule", "close_issue", "edit", "custom_command"}
)


class _View(Enum):
    EPICS = auto()
    SPRINT = auto()
    BACKLOG = auto()


@final
class OrbitApp(App[None]):
    """Interactive navigator for sprint epics and backlog issues."""

    TITLE = "orbit"

    CSS = """
    IssueTree, IssueList {
        height: 1fr;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("e", "show_epics", "Epics"),
        Binding("c", "show_sprint", "Sprint"),
        Binding("b", "show_backlog", "Backlog"),
        Binding("n", "toggle_soon", "Soon filter"),
        Binding("f", "toggle_hide_closed", "Hide closed"),
        Binding("r", "refresh", "Refresh"),
        Binding("m", "move", "Move"),
        Binding("s", "schedule", "Schedule"),
        Binding("x", "close_issue", "Close"),
        Binding("t", "edit", "Edit in browser"),
        Binding("g", "goto_issue", "Go to issue"),
        Binding("question_mark", "help", "Help", key_display="?"),
        # Both only fire on the main screen: every other screen inherits
        # ClosableScreen, whose q/escape close it and shadow these. Both
        # keys mean "close what I'm in"; on the main screen that's the app.
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
    ]

    @classmethod
    def reserved_keys(cls) -> frozenset[str]:
        """Keys a project's `.orbit.toml` may not claim.

        Derived from BINDINGS rather than listed by hand so it cannot
        drift as bindings change.
        """
        return frozenset(
            binding.key for binding in cls.BINDINGS if isinstance(binding, Binding)
        )

    def __init__(
        self,
        client: GitHubClient,
        milestones: Milestones,
        commands: Sequence[CustomCommand] = (),
    ) -> None:
        super().__init__()
        self._client = client
        self._milestones = milestones
        # Bound by index: the action string is built here, and an index
        # sidesteps quoting a key that might itself be a quote character.
        self._commands = tuple(commands)
        for index, command in enumerate(self._commands):
            self._bindings.bind(command.key, f"custom_command({index})", command.label)
        self._tree = IssueTree(id="epic-tree")
        self._sprint_list = IssueList(
            id="sprint-list",
            milestone=milestones.current,
            item_name="sprint issues",
        )
        self._backlog_list = IssueList(
            id="backlog-list",
            milestone=milestones.backlog,
            item_name="backlog issues",
            soon_filterable=True,
        )
        self._view_widgets: dict[_View, IssueTree | IssueList] = {
            _View.EPICS: self._tree,
            _View.SPRINT: self._sprint_list,
            _View.BACKLOG: self._backlog_list,
        }
        self._view = _View.EPICS
        self._hide_closed = False

    @override
    def compose(self) -> ComposeResult:
        yield self._tree
        yield self._sprint_list
        yield self._backlog_list
        yield StatusBar()

    def on_mount(self) -> None:
        self._show_view(_View.EPICS)
        self._load_epics()

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in _MAIN_SCREEN_ACTIONS and len(self.screen_stack) > 1:
            return (
                action in _DETAIL_SCREEN_ACTIONS and self._detail_screen() is not None
            )
        return True

    def _detail_screen(self) -> DetailScreen | None:
        """The detail screen if it's the active (top) screen, else None."""
        top = self.screen_stack[-1]
        return top if isinstance(top, DetailScreen) else None

    def _dismiss_detail(self) -> None:
        """Pop the detail screen (if active) so the refreshed main view
        and its status line are visible after a mutation."""
        detail = self._detail_screen()
        if detail is not None:
            detail.dismiss(None)

    def _set_status(self, message: str) -> None:
        self.query_one(StatusBar).set_status(message)

    def _visible_issue_list(self) -> IssueList | None:
        """The flat list of the current view, or None in the epics view."""
        widget = self._view_widgets[self._view]
        return widget if isinstance(widget, IssueList) else None

    def _show_view(self, view: _View) -> None:
        self._view = view
        for widget_view, widget in self._view_widgets.items():
            widget.display = widget_view is view
        self._view_widgets[view].focus()

    def _selected_issue_number(self) -> int | None:
        # When the detail screen is up, actions target the issue it shows.
        detail = self._detail_screen()
        if detail is not None:
            return detail.issue_number
        issue_list = self._visible_issue_list()
        if issue_list is None:
            return self._tree.selected_issue_number
        return issue_list.selected_issue_number

    # --- Data loading ---

    def _refresh(self, final_status: str | None = None) -> None:
        """Re-fetch the visible view, preserving the cursor and (for the
        tree) which epics are expanded; show `final_status` instead of
        the load message when given (so action results aren't overwritten)."""
        issue_list = self._visible_issue_list()
        if issue_list is None:
            self._load_epics(final_status, restore=True)
        else:
            self._load_issue_list(issue_list, final_status, preserve_selection=True)

    def _cancel_expand_workers(self) -> None:
        """Cancel in-flight epic expansions.

        The sole caller of Textual's cancel_group, whose stub returns
        list[Worker[Unknown]]; the imprecision is corralled here.
        """
        self.workers.cancel_group(  # pyright: ignore[reportUnknownMemberType]
            self, "expand"
        )

    async def _fetch_tree(self) -> tuple[list[Epic], list[MilestoneIssue]]:
        """The epics and the milestone's standalone issues, fetched
        concurrently — the tree shows both, and one round trip's
        latency covers the other."""
        epics, issues = await asyncio.gather(
            asyncio.to_thread(
                self._client.list_epics_by_milestone, self._milestones.current
            ),
            asyncio.to_thread(
                self._client.list_issues_by_milestone, self._milestones.current
            ),
        )
        _, standalone = partition_standalone(issues)
        return epics, standalone

    @work(exclusive=True, group="load")
    async def _load_epics(
        self, final_status: str | None = None, restore: bool = False
    ) -> None:
        # In-flight expansions hold references to nodes that
        # load_epics is about to clear; let them die first.
        self._cancel_expand_workers()
        tree = self._tree
        # Snapshot before the await so the restore reflects the view as
        # the user left it, not any intervening change.
        saved = tree.capture_state() if restore else None
        tree.loading = True
        try:
            epics, standalone = await self._fetch_tree()
        except RuntimeError as exc:
            self._set_status(f"Error: {exc}")
            return
        finally:
            tree.loading = False
        tree.load_epics(epics, standalone, restore=saved)
        total = sum(epic.total_count for epic in epics) + len(standalone)
        self._set_status(final_status or f"Loaded {len(epics)} epics, {total} issues")

    @work(exclusive=True, group="load")
    async def _load_issue_list(
        self,
        issue_list: IssueList,
        final_status: str | None = None,
        preserve_selection: bool = False,
    ) -> None:
        select = issue_list.selected_issue_number if preserve_selection else None
        issue_list.loading = True
        try:
            issues = await asyncio.to_thread(
                self._client.list_issues_by_milestone,
                issue_list.milestone,
                label=issue_list.label_filter,
            )
        except RuntimeError as exc:
            self._set_status(f"Error: {exc}")
            return
        finally:
            issue_list.loading = False
        issue_list.load_issues(issues, select=select)
        suffix = " (soon only)" if issue_list.soon_only else ""
        self._set_status(
            final_status or f"Loaded {len(issues)} {issue_list.item_name}{suffix}"
        )

    @work(group="expand")
    async def _load_sub_issues(self, node: TreeNode[TreeItemData]) -> None:
        data = node.data
        if data is None:
            return
        tree = self._tree
        tree.loading = True
        try:
            children = await asyncio.to_thread(
                self._client.fetch_sub_issue_tree, data.fetch_number
            )
        except RuntimeError as exc:
            tree.loading = False
            self._set_status(f"Error: {exc}")
            return
        # Deliberately NOT a finally, unlike the "load" workers.
        # Expand workers are cancelled from *inside* a running load
        # worker, which sets tree.loading = True and only then yields
        # at its await — so our CancelledError arrives after the
        # reload has claimed the indicator, and a finally here would
        # switch it off while that reload is still fetching. The
        # cancelled path must leave the flag to its canceller; the
        # success and error paths reset it explicitly.
        tree.loading = False
        tree.populate_children(node, children)

    # --- Events ---

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[TreeItemData]) -> None:
        data = event.node.data
        if data is None or data.children_loaded:
            return
        self._load_sub_issues(event.node)

    def on_tree_node_selected(self, event: Tree.NodeSelected[TreeItemData]) -> None:
        data = event.node.data
        if isinstance(event.control, IssueTree) and isinstance(data, IssueNodeData):
            self.push_screen(DetailScreen(self._client, data.number))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, IssueList) and event.option.id is not None:
            self.push_screen(DetailScreen(self._client, int(event.option.id)))

    # --- Actions ---

    def action_show_epics(self) -> None:
        self._show_view(_View.EPICS)
        self._load_epics()

    def action_show_sprint(self) -> None:
        self._show_view(_View.SPRINT)
        self._load_issue_list(self._sprint_list)

    def action_show_backlog(self) -> None:
        self._show_view(_View.BACKLOG)
        self._load_issue_list(self._backlog_list)

    def action_toggle_soon(self) -> None:
        issue_list = self._visible_issue_list()
        if issue_list is None or not issue_list.soon_filterable:
            return
        issue_list.soon_only = not issue_list.soon_only
        self._load_issue_list(issue_list)

    def action_toggle_hide_closed(self) -> None:
        self._hide_closed = not self._hide_closed
        for widget in self._view_widgets.values():
            widget.hide_closed = self._hide_closed
        self._refresh(f"Hide closed: {'on' if self._hide_closed else 'off'}")

    def action_refresh(self) -> None:
        self._refresh()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self._commands))

    def action_move(self) -> None:
        issue_number = self._selected_issue_number()
        if issue_number is None:
            return
        self._dismiss_detail()

        def _on_pick(epic_number: int | None) -> None:
            if epic_number is not None:
                self._do_move(issue_number, epic_number)

        self.push_screen(
            EpicPickerScreen(self._client, self._milestones.current, self._set_status),
            _on_pick,
        )

    def action_schedule(self) -> None:
        issue_number = self._selected_issue_number()
        if issue_number is None:
            return
        self._dismiss_detail()

        def _on_pick(milestone: str | None) -> None:
            if milestone is not None:
                self._do_schedule(issue_number, milestone)

        self.push_screen(
            MilestonePickerScreen(self._client, self._set_status), _on_pick
        )

    def action_close_issue(self) -> None:
        issue_number = self._selected_issue_number()
        if issue_number is not None:
            self._dismiss_detail()
            self._do_close(issue_number)

    def action_edit(self) -> None:
        issue_number = self._selected_issue_number()
        if issue_number is None:
            return
        self._dismiss_detail()
        try:
            owner, name = self._client.repo
        except RuntimeError as exc:
            self._set_status(f"Error: {exc}")
            return
        open_url(f"https://github.com/{owner}/{name}/issues/{issue_number}")
        self._set_status(f"Opened #{issue_number} in browser")

    def action_custom_command(self, index: int) -> None:
        command = self._commands[index]
        issue_number = self._selected_issue_number()
        if issue_number is None:
            return
        self._dismiss_detail()
        if not command.needs_branch:
            self._launch(command, issue_number)
            return

        def _on_branch(branch: str | None) -> None:
            if branch is not None:
                self._launch(command, issue_number, branch)

        self.push_screen(BranchPromptScreen(command.label), _on_branch)

    def action_goto_issue(self) -> None:
        def _on_number(number: int | None) -> None:
            if number is not None:
                self._goto_issue(number)

        self.push_screen(IssueNumberPromptScreen(), _on_number)

    def _launch(
        self, command: CustomCommand, issue_number: int, branch: str | None = None
    ) -> None:
        rendered = command.render(issue_number, branch)
        try:
            if command.mode is CommandMode.SUSPEND:
                with self.suspend():
                    returncode = run_attached(rendered)
            else:
                # Spawned commands are detached: orbit is back before
                # they could fail, so launching IS the whole outcome.
                returncode = 0
                spawn(rendered)
        except OSError as exc:
            self._set_status(f"Error: {exc}")
            return
        if returncode != 0:
            self._set_status(f"{command.label} exited with status {returncode}")
            return
        self._set_status(f"Ran {command.label} on #{issue_number}")

    @work(group="goto")
    async def _goto_issue(self, number: int) -> None:
        issue_list = self._visible_issue_list()
        if issue_list is not None and issue_list.highlight_issue(number):
            self._set_status(f"Jumped to #{number}")
            return
        tree = self._tree
        suffix = "" if self._view is _View.EPICS else " (switched to epics)"
        if self._land_on(number, suffix):
            return
        self._set_status(f"Finding #{number}...")
        try:
            if not tree.root.children:
                epics, standalone = await self._fetch_tree()
                tree.load_epics(epics, standalone)
                if self._land_on(number, suffix):
                    return
            ancestor = await self._root_ancestor(number)
        except IssueNotFoundError as exc:
            self._set_status(str(exc))
            return
        except RuntimeError as exc:
            self._set_status(f"Error: {exc}")
            return
        if ancestor is None:
            return
        self._show_view(_View.EPICS)
        tree.reveal(number, ancestor)
        self._set_status(f"Jumped to #{number}{suffix}")

    def _land_on(self, number: int, suffix: str) -> bool:
        """Show and cursor the epics view if the tree already holds
        `number`; False when it does not, so the caller keeps looking."""
        if not self._tree.reveal_loaded(number):
            return False
        self._show_view(_View.EPICS)
        self._set_status(f"Jumped to #{number}{suffix}")
        return True

    async def _root_ancestor(self, number: int) -> int | None:
        """Walk parents up to the first top-level epic in the tree.

        None when the chain ends without reaching one. A parentless
        issue in the milestone is already in the tree's STANDALONE
        section, so reaching here means the issue is outside it either
        way.
        """
        roots = self._tree.root_numbers()
        current = number
        while True:
            parent = await asyncio.to_thread(self._client.fetch_parent_issue, current)
            if parent is None:
                self._set_status(f"#{number} is not in the current milestone")
                return None
            if parent.number in roots:
                return parent.number
            current = parent.number

    # --- Mutations ---

    @work(group="action")
    async def _do_move(self, issue_number: int, epic_number: int) -> None:
        try:
            result = await asyncio.to_thread(
                move_issue, self._client, issue_number, epic_number
            )
        except (AlreadyDoneError, RuntimeError) as exc:
            self._set_status(str(exc))
            return
        reopened = (
            "; reopened " + ", ".join(f"#{number}" for number in result.reopened)
            if result.reopened
            else ""
        )
        if result.already_done:
            self._refresh(
                f"#{result.issue_number} is already under"
                + f" epic #{result.epic_number}{reopened}"
            )
            return
        note = " (promoted to epic)" if result.converted_dest_to_epic else ""
        self._refresh(
            f"Moved #{result.issue_number} → epic"
            + f" #{result.epic_number} ({result.epic_title}){note}{reopened}"
        )

    @work(group="action")
    async def _do_schedule(self, issue_number: int, milestone: str) -> None:
        try:
            result = await asyncio.to_thread(
                schedule_issue, self._client, issue_number, milestone
            )
        except (AlreadyDoneError, RuntimeError) as exc:
            self._set_status(str(exc))
            return
        base = f"Scheduled #{result.issue_number} → {result.milestone}"
        if result.old_epic_number is not None:
            self._refresh(base + f" (detached from epic #{result.old_epic_number})")
        else:
            self._refresh(base)

    @work(group="action")
    async def _do_close(self, issue_number: int) -> None:
        try:
            await asyncio.to_thread(
                close_issue,
                self._client,
                issue_number,
                CloseReason.COMPLETED,
                Surface.TUI,
            )
        except (AlreadyDoneError, RuntimeError) as exc:
            self._set_status(str(exc))
            return
        self._refresh(f"Closed #{issue_number}")


def run_tui(client: GitHubClient, config: ProjectConfig) -> None:
    """Launch the interactive orbit TUI against the project's config."""
    OrbitApp(client, config.milestones, config.commands).run()
