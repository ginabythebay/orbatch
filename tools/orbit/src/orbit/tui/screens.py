"""Screens pushed over the orbit TUI's main view.

The issue detail view (`DetailScreen`), the keybinding help overlay
(`HelpScreen`), and the move target picker (`EpicPickerScreen`).
Unlike the widgets in orbit.tui.widgets, these own their data: each
fetches what it needs on mount because that data is private to the
screen's lifetime. Screens communicate back to the app only through
dismiss results and injected callbacks — never by importing the app,
which would be a circular import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import ClassVar, final, override

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen, ScreenResultType
from textual.widgets import Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from orbit.config import CustomCommand
from orbit.github.client import GitHubClient
from orbit.github.models import IssueDetail
from orbit.tui.widgets import Palette, issue_text

_KEYBINDINGS = [
    ("up/down", "Move cursor"),
    ("right", "Expand epic"),
    ("left", "Collapse epic"),
    ("enter", "Open detail view"),
    ("escape", "Close (quit on main screen)"),
    ("(detail)", "m/s/x/t act on the open issue"),
    ("e", "Epics view (tree)"),
    ("c", "Current sprint view (flat)"),
    ("b", "Backlog view"),
    ("n", "Toggle 'soon' filter (backlog)"),
    ("f", "Toggle hide-closed"),
    ("m", "Move issue to an epic"),
    ("s", "Schedule issue to a milestone"),
    ("x", "Close issue"),
    ("t", "Edit issue in browser"),
    ("g", "Go to issue number"),
    ("r", "Refresh current view"),
    ("?", "Toggle this help"),
    ("q", "Close (quit on main screen)"),
]


# Both exit keys, defined once. Screens inherit them via the Closable
# bases below rather than each restating them, so a new screen cannot
# forget and fall through to the app's quit.
_CLOSE_BINDINGS: list[BindingType] = [
    Binding("escape", "close_screen", "Close", show=False),
    Binding("q", "close_screen", "Close", show=False),
]


class ClosableScreen(Screen[ScreenResultType]):
    """Screen that q and escape close.

    Textual merges BINDINGS along the MRO but skips non-DOMNode bases,
    so this must subclass Screen to hand its keys down. Override
    action_close_screen to do more than dismiss.
    """

    BINDINGS: ClassVar[list[BindingType]] = _CLOSE_BINDINGS

    def action_close_screen(self) -> None:
        self.dismiss(None)


class ClosableModalScreen(ModalScreen[ScreenResultType]):
    """Modal screen that q and escape close.

    Parallel to ClosableScreen, not a subclass of it: Screen and
    ModalScreen are separate Textual bases.
    """

    BINDINGS: ClassVar[list[BindingType]] = _CLOSE_BINDINGS

    def action_close_screen(self) -> None:
        self.dismiss(None)


@final
class HelpScreen(ClosableModalScreen[None]):
    """Centered overlay listing all keybindings.

    Takes the project's custom commands rather than reading them from
    config: the app owns them, and a screen that imported the app would
    be a circular import.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("question_mark", "close_screen", "Dismiss", show=False),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen #help-panel {
        width: 48;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    """

    def __init__(self, commands: Sequence[CustomCommand] = ()) -> None:
        super().__init__()
        self._commands = tuple(commands)

    @override
    def compose(self) -> ComposeResult:
        text = Text()
        text.append("orbit keybindings\n\n", Palette.EMPHASIS)
        for key, description in _KEYBINDINGS:
            text.append(f"{key:>8}", Palette.KEY)
            text.append(f"  {description}\n")
        if self._commands:
            text.append("\nproject commands\n\n", Palette.EMPHASIS)
            for command in self._commands:
                text.append(f"{command.key:>8}", Palette.KEY)
                text.append(f"  {command.label}\n")
        text.append("\ncolors\n\n", Palette.EMPHASIS)
        # Rendered with issue_text so the legend cannot drift from
        # the styling actually used in the tree and lists.
        for sample in (
            issue_text(42, "OPEN", "An open issue"),
            issue_text(43, "OPEN", "An epic, 3 of 5 sub-issues open", 3, 5),
            issue_text(44, "CLOSED", "A closed issue or epic"),
        ):
            text.append_text(sample)
            text.append("\n")
        yield Static(text, id="help-panel")


def _header_text(detail: IssueDetail) -> Text:
    text = Text()
    text.append(f"#{detail.number} {detail.title}\n\n", Palette.EMPHASIS)
    state_style = Palette.CLOSED if detail.state == "CLOSED" else Palette.OPEN
    text.append("State: ", Palette.EMPHASIS)
    text.append(f"{detail.state}\n", state_style)
    text.append("Labels: ", Palette.EMPHASIS)
    text.append(f"{', '.join(detail.labels) if detail.labels else '—'}\n")
    text.append("Milestone: ", Palette.EMPHASIS)
    text.append(f"{detail.milestone_title or '—'}\n")
    text.append("Parent epic: ", Palette.EMPHASIS)
    if detail.parent_number is not None:
        title = f" {detail.parent_title}" if detail.parent_title else ""
        text.append(f"#{detail.parent_number}{title}")
    else:
        text.append("—")
    return text


@final
class DetailScreen(ClosableScreen[None]):
    """Metadata header plus the issue body rendered as markdown."""

    DEFAULT_CSS = """
    DetailScreen #detail-header {
        height: auto;
        padding: 1 2;
        background: $panel;
    }
    DetailScreen #detail-body-container {
        height: 1fr;
    }
    """

    def __init__(self, client: GitHubClient, issue_number: int) -> None:
        super().__init__()
        self._client = client
        self._issue_number = issue_number

    @property
    def issue_number(self) -> int:
        """The issue this screen displays; issue-targeted app actions
        (close, schedule, move, edit) operate on it while it's on top."""
        return self._issue_number

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="detail-header")
        yield VerticalScroll(Markdown(id="detail-body"), id="detail-body-container")

    def on_mount(self) -> None:
        self._load_detail()

    @work(exclusive=True)
    async def _load_detail(self) -> None:
        body_container = self.query_one("#detail-body-container", VerticalScroll)
        body_container.loading = True
        try:
            detail = await asyncio.to_thread(
                self._client.fetch_issue_detail, self._issue_number
            )
        except RuntimeError as exc:
            header_error = Text(
                f"Error loading #{self._issue_number}: {exc}", Palette.ERROR
            )
            self.query_one("#detail-header", Static).update(header_error)
            return
        finally:
            body_container.loading = False
        self.query_one("#detail-header", Static).update(_header_text(detail))
        await self.query_one("#detail-body", Markdown).update(detail.body)


@final
class EpicPickerScreen(ClosableModalScreen[int | None]):
    """Pick a target epic for a move; dismisses with its number or None."""

    DEFAULT_CSS = """
    EpicPickerScreen {
        align: center middle;
    }
    EpicPickerScreen #epic-picker {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
    }
    """

    def __init__(
        self,
        client: GitHubClient,
        milestone: str,
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._client = client
        self._milestone = milestone
        self._on_error = on_error

    @override
    def compose(self) -> ComposeResult:
        yield OptionList(id="epic-picker")

    def on_mount(self) -> None:
        self._load_epics()

    @work(exclusive=True)
    async def _load_epics(self) -> None:
        picker = self.query_one(OptionList)
        picker.loading = True
        try:
            epics = await asyncio.to_thread(
                self._client.list_epics_by_milestone, self._milestone
            )
        except RuntimeError as exc:
            self._on_error(f"Error loading epics: {exc}")
            self.dismiss(None)
            return
        finally:
            picker.loading = False
        for epic in epics:
            if epic.state != "OPEN":
                continue
            picker.add_option(
                Option(
                    issue_text(epic.number, epic.state, epic.title),
                    id=str(epic.number),
                )
            )
        if picker.option_count > 0:
            picker.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.dismiss(int(event.option.id))


@final
class BranchPromptScreen(ClosableModalScreen[str | None]):
    """Ask for the branch name a custom command's `{branch}` needs.

    Dismisses with what the user typed, or None if it was closed or
    left empty. Unlike its sibling pickers there is nothing to fetch:
    a branch name exists only in the user's head.

    The inherited q/escape close this screen, but the focused Input
    sees printable keys first and consumes them, so a branch name may
    still contain a "q".
    """

    DEFAULT_CSS = """
    BranchPromptScreen {
        align: center middle;
    }
    BranchPromptScreen #branch-prompt {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    BranchPromptScreen #branch-prompt-label {
        padding-bottom: 1;
    }
    """

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="branch-prompt"):
            yield Static(
                Text(f"{self._label}: branch name", Palette.EMPHASIS),
                id="branch-prompt-label",
            )
            yield Input(id="branch-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value or None)


def _parse_issue_number(raw: str) -> int | None:
    text = raw.strip().removeprefix("#").strip()
    return int(text) if text.isdecimal() else None


@final
class IssueNumberPromptScreen(ClosableModalScreen[int | None]):
    """Ask which issue to jump to; dismisses with its number or None.

    Input that is not an issue number leaves the prompt open with an
    error, so a typo cannot be mistaken for a cancel.
    """

    DEFAULT_CSS = """
    IssueNumberPromptScreen {
        align: center middle;
    }
    IssueNumberPromptScreen #issue-number-prompt {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    IssueNumberPromptScreen #issue-number-prompt-label {
        padding-bottom: 1;
    }
    """

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="issue-number-prompt"):
            yield Static(
                Text("Go to issue number", Palette.EMPHASIS),
                id="issue-number-prompt-label",
            )
            yield Input(id="issue-number-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        number = _parse_issue_number(event.value)
        if number is None:
            self.query_one("#issue-number-prompt-label", Static).update(
                Text("Not an issue number", Palette.ERROR)
            )
            self.query_one(Input).focus()
            return
        self.dismiss(number)


@final
class MilestonePickerScreen(ClosableModalScreen[str | None]):
    """Pick a target milestone for a schedule; dismisses with its
    title or None. Mirrors EpicPickerScreen, but milestones are keyed
    by title (there is no stable numeric id to pass to schedule_issue)."""

    DEFAULT_CSS = """
    MilestonePickerScreen {
        align: center middle;
    }
    MilestonePickerScreen #milestone-picker {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
    }
    """

    def __init__(self, client: GitHubClient, on_error: Callable[[str], None]) -> None:
        super().__init__()
        self._client = client
        self._on_error = on_error

    @override
    def compose(self) -> ComposeResult:
        yield OptionList(id="milestone-picker")

    def on_mount(self) -> None:
        self._load_milestones()

    @work(exclusive=True)
    async def _load_milestones(self) -> None:
        picker = self.query_one(OptionList)
        picker.loading = True
        try:
            milestones = await asyncio.to_thread(self._client.list_milestones)
        except RuntimeError as exc:
            self._on_error(f"Error loading milestones: {exc}")
            self.dismiss(None)
            return
        finally:
            picker.loading = False
        for milestone in milestones:
            picker.add_option(Option(milestone.title, id=milestone.title))
        if picker.option_count > 0:
            picker.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.dismiss(event.option.id)
