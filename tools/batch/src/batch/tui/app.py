from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import ClassVar, Protocol, override

from rich.console import RenderableType
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Static

from batch.dashboard import Selection
from batch.models import (
    Batch,
    DashboardRow,
    DebugEntry,
    RecoveryAction,
    RecoveryResult,
    RunResult,
)
from batch.text_output import (
    dashboard_view,
    debug_line,
    recovery_line,
    run_banner,
    status_line,
)

REFRESH_INTERVAL = 2.0
FETCH_INTERVAL = 30.0


class Driving(Protocol):
    def run(self, targets: Sequence[int]) -> RunResult: ...
    def fetch(self, targets: Sequence[int]) -> Batch: ...
    def render(
        self, batch: Batch, selected: int | None = None
    ) -> tuple[DashboardRow, ...]: ...
    def enter(self, issue_number: int) -> DebugEntry: ...


class Keying(Protocol):
    def rework(self, issue: int) -> RecoveryResult: ...
    def skip(self, issue: int) -> RecoveryResult: ...
    def relaunch(self, issue: int) -> RecoveryResult: ...


def _apply(verbs: Keying, action: RecoveryAction, issue: int) -> RecoveryResult:
    match action:
        case RecoveryAction.REWORK:
            return verbs.rework(issue)
        case RecoveryAction.SKIP:
            return verbs.skip(issue)
        case RecoveryAction.RELAUNCH:
            return verbs.relaunch(issue)


class DashboardApp(App[None]):
    CSS: ClassVar[str] = """
    #rows { height: 1fr; padding: 1 1 0 1; }
    #footing { dock: bottom; height: 2; }
    #status { height: 1; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("down,j", "move(1)", "Down"),
        Binding("up,k", "move(-1)", "Up"),
        Binding("enter", "debug", "Debug"),
        Binding("f", "verb('rework')", "Rework"),
        Binding("s", "verb('skip')", "Skip"),
        Binding("r", "verb('relaunch')", "Relaunch"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        targets: Sequence[int],
        orchestrator: Driving,
        *,
        prog: str,
        verbs: Keying | None = None,
        narration: Sequence[str] = (),
        drive: Callable[[], RunResult] | None = None,
        interval: float = REFRESH_INTERVAL,
        fetch_interval: float = FETCH_INTERVAL,
    ) -> None:
        super().__init__()
        self._targets: tuple[int, ...] = tuple(targets)
        self._orchestrator: Driving = orchestrator
        self._prog: str = prog
        self._verbs: Keying | None = verbs
        self._drive_batch: Callable[[], RunResult] = drive or self._run_targets
        self._narration: Sequence[str] = narration
        self._interval: float = interval
        self._fetch_interval: float = fetch_interval
        self._batch: Batch | None = None
        self._rows: tuple[DashboardRow, ...] = ()
        self._selection: Selection = Selection()
        self._finished: threading.Event = threading.Event()
        self._polling: bool = False
        self._message: str | None = None
        self._banner: str | None = None
        self.result: RunResult | None = None
        self.failure: BaseException | None = None

    @override
    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(id="rows"))
        with Vertical(id="footing"):
            yield Static(id="status")
            yield Footer()

    def on_mount(self) -> None:
        self._drive()
        _ = self.set_interval(self._interval, self._tick)
        _ = self.set_interval(self._fetch_interval, self._refresh)
        self._tick()
        self._refresh()

    def _drive(self) -> None:
        """A daemon thread, not a Textual worker: quitting the dashboard must leave
        an in-flight VM running rather than block the exit on `Orchestrator.run`."""
        threading.Thread(target=self._consume, name="batch-run", daemon=True).start()

    def _run_targets(self) -> RunResult:
        return self._orchestrator.run(self._targets)

    def _consume(self) -> None:
        try:
            self.result = self._drive_batch()
        except BaseException as exc:  # noqa: BLE001 — a daemon thread has nowhere to raise; an escape loses the failure
            self.failure = exc
        finally:
            self._finished.set()

    def _tick(self) -> None:
        """A halt ends the run thread at exactly the moment the verbs are wanted,
        so the dashboard outlives its batch and says why it stopped."""
        if self._finished.is_set() and self._banner is None:
            self._banner = self._ending()
            self._paint_status()
        self._repaint()

    def _ending(self) -> str:
        if self.failure is not None:
            return f"Run failed: {self.failure}. q to quit."
        return "Batch over." if self.result is None else run_banner(self.result)

    def _repaint(self) -> None:
        """Rows off the cached batch. The VM status and log tail move every frame
        and cost nothing; the labels behind them cost API points, so they don't."""
        if self._batch is None:
            return
        self.show(self._orchestrator.render(self._batch, self._selection.number))

    @work(group="refresh", exit_on_error=False)
    async def _refresh(self) -> None:
        """A slow fetch must delay the next poll, never cancel this one, and a
        failed one must not end the run: the batch outlives its viewer."""
        if self._polling:
            return
        self._polling = True
        try:
            self._batch = await asyncio.to_thread(
                self._orchestrator.fetch, self._targets
            )
        except Exception as exc:  # noqa: BLE001
            self.status(f"Refresh failed: {exc}")
        else:
            self._repaint()
        finally:
            self._polling = False

    def show(self, rows: tuple[DashboardRow, ...]) -> None:
        self._rows = rows
        self._selection.sync(rows)
        self._paint()
        self._paint_status()

    def status(self, message: str) -> None:
        """Held until the next keypress, so a refresh cannot erase it."""
        self._message = message
        self._write(message)

    def _paint_status(self) -> None:
        """The banner outlives a keypress; a transient message outranks it."""
        narration = self._narration[-1] if self._narration else ""
        self._write(self._message or self._banner or narration)

    def _write(self, message: str) -> None:
        limit = self._batch.rate_limit if self._batch else None
        self._update("#status", status_line(message, limit))

    def _paint(self) -> None:
        self._update("#rows", dashboard_view(self._rows, self._selection.number))

    def _update(self, selector: str, content: RenderableType) -> None:
        """A poll can land after the widgets are gone; a torn-down app has nothing
        to say and must not raise out of a timer callback."""
        for widget in self.query(selector).results(Static):
            widget.update(content)

    def action_move(self, delta: int) -> None:
        self._forget()
        self._selection.move(self._rows, delta)
        self._paint()

    def action_verb(self, name: str) -> None:
        self._forget()
        row = self._selection.row(self._rows)
        if row is None or self._verbs is None:
            return
        action = RecoveryAction(name)
        with self._safely(action.value.capitalize()):
            result = _apply(self._verbs, action, row.number)
            self.status(recovery_line(result, prog=self._prog))
            if result.refusal is None and action is not RecoveryAction.REWORK:
                self._resume()
            self._refresh()

    def _resume(self) -> None:
        """Recovering an issue is what un-halts a batch, so the run thread the halt
        ended starts again here; a live run already covers the issue."""
        if not self._finished.is_set():
            return
        self.result = None
        self.failure = None
        self._banner = None
        self._finished.clear()
        self._drive()

    def _forget(self) -> None:
        self._message = None
        self._paint_status()

    @contextmanager
    def _safely(self, label: str) -> Generator[None]:
        """A key handler reaches git and GitHub; an escape out of one propagates
        through `App.run()` and takes the batch down with the dashboard."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001
            self.status(f"{label} failed: {exc}")

    def action_debug(self) -> None:
        self._forget()
        with self._safely("Debug"):
            row = self._selection.row(self._rows)
            if row is None:
                return
            entry = self._orchestrator.enter(row.number)
            if entry.refusal is not None:
                self.status(debug_line(entry))
                return
            with self.suspend():
                try:
                    _ = subprocess.run(entry.command, check=False)
                except FileNotFoundError:
                    self.status("dtach is not installed.")


def run_dashboard(
    targets: Sequence[int],
    orchestrator: Driving,
    narration: Sequence[str] = (),
    drive: Callable[[], RunResult] | None = None,
    verbs: Keying | None = None,
    *,
    prog: str,
) -> RunResult | None:
    """None means the developer quit before the batch finished."""
    app = DashboardApp(
        targets, orchestrator, prog=prog, verbs=verbs, narration=narration, drive=drive
    )
    app.run()
    if app.failure is not None:
        raise app.failure
    return app.result
