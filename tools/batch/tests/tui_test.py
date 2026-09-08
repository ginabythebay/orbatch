# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from subprocess import CalledProcessError, CompletedProcess
from typing import override

import pytest
from textual.app import App
from textual.binding import Binding
from textual.pilot import Pilot

from batch.models import (
    Batch,
    DashboardRow,
    DebugEntry,
    DebugRefusal,
    HaltReason,
    IssueOutcome,
    RecoveryAction,
    RecoveryRefusal,
    RecoveryResult,
    RunResult,
)
from batch.testing.driving import BOOT, FakeDriver, FakeVerbs
from batch.text_output import debug_line
from batch.tui.app import DashboardApp
from batch.tui.screen import DashboardScreen
from ghgql.labels import BatchLabel
from ghgql.transport import RateLimit

pytestmark = pytest.mark.slow

EPIC = 1492
_PROG = "bin/acme"


def _row(number: int, state: BatchLabel = BatchLabel.PLANNED) -> DashboardRow:
    return DashboardRow(number=number, title=f"Issue {number}", state=state)


def _no_slot(issue_number: int) -> DebugEntry:
    return DebugEntry(
        number=issue_number,
        refusal=DebugRefusal.NO_SLOT,
        missing=(f"no disk at /trees/issue-{issue_number}.raw",),
    )


_REFUSED = (
    _no_slot(10),
    DebugEntry(number=10, refusal=DebugRefusal.BOOT_FAILED, boot=BOOT),
)


def _refusal_id(entry: DebugEntry) -> str:
    return str(entry.refusal)


def _app(driver: FakeDriver) -> DashboardApp:
    return DashboardApp((EPIC,), driver, prog=_PROG, interval=0.05, fetch_interval=0.05)


def _attaches(monkeypatch: pytest.MonkeyPatch) -> list[Sequence[str]]:
    calls: list[Sequence[str]] = []

    def record(command: Sequence[str], **_kwargs: object) -> CompletedProcess[bytes]:
        calls.append(command)
        return CompletedProcess(list(command), 0)

    monkeypatch.setattr("batch.tui.screen.subprocess.run", record)
    return calls


def _trace(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real suspend needs a tty; how the status write interleaves with entering
    and leaving one is what says which guard sits outside which."""
    log: list[str] = []

    @contextmanager
    def logged(_app: DashboardApp) -> Generator[None]:
        log.append("enter")
        try:
            yield
        finally:
            log.append("exit")

    written = DashboardScreen.status

    def recorded(screen: DashboardScreen, message: str) -> None:
        log.append("status")
        written(screen, message)

    monkeypatch.setattr(DashboardApp, "suspend", logged)
    monkeypatch.setattr(DashboardScreen, "status", recorded)
    return log


def _screen(app: App[None]) -> list[str]:
    """Read the composited screen, not the widgets: a widget can render text
    perfectly onto a region another widget is sitting on top of."""
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _rendered(app: App[None]) -> str:
    return "\n".join(_screen(app))


def _status(app: App[None]) -> str:
    return _screen(app)[-2]


async def _until(pilot: Pilot[None], ready: Callable[[], bool], what: str) -> None:
    """Poll for the frame instead of sleeping a fixed slice: a loaded machine can
    miss a 0.2s window, and then a keypress lands on rows that are not there."""
    for _ in range(200):
        if ready():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"the dashboard never {what}")


async def _settle(app: DashboardApp, pilot: Pilot[None], count: int) -> None:
    await _shown(app.dashboard, pilot, count)


async def _shown(screen: DashboardScreen, pilot: Pilot[None], count: int) -> None:
    await _until(pilot, lambda: len(screen._rows) == count, f"showed {count} rows")


async def _ended(app: DashboardApp, pilot: Pilot[None]) -> None:
    await _until(
        pilot, lambda: app.dashboard._banner is not None, "reported its run ending"
    )


async def _said(app: App[None], pilot: Pilot[None], message: str) -> None:
    await _until(pilot, lambda: message in _status(app), f"said {message!r}")


def _table(app: App[None]) -> str:
    """The rows alone: the status line names issues too."""
    return "\n".join(_screen(app)[:-2])


class TestDashboard:
    @pytest.mark.asyncio
    async def test_it_renders_a_row_per_issue(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.IMPLEMENTING))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)

            assert "#10" in _rendered(app)
            assert "#11" in _rendered(app)
            assert set(driver.asked) == {(EPIC,)}
            driver.released.set()

    @pytest.mark.asyncio
    async def test_frames_are_redrawn_far_more_often_than_github_is_asked(self) -> None:
        """The GraphQL budget is what stands between a long batch and a dead
        dashboard, and only the fetch spends it."""
        driver = FakeDriver(_row(10))
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, interval=0.01, fetch_interval=30.0
        )

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            renders = len(driver.selected)
            await _until(
                pilot, lambda: len(driver.selected) > renders + 5, "redrew its rows"
            )

            assert driver.asked == [(EPIC,)]
            driver.released.set()

    @pytest.mark.asyncio
    async def test_the_status_line_carries_the_graphql_budget(self) -> None:
        budget = RateLimit(cost=2, remaining=4998, limit=5000, reset_at="later")
        driver = FakeDriver(_row(10), rate_limit=budget)
        app = _app(driver)

        async with app.run_test() as pilot:
            await _said(app, pilot, "GraphQL 4998/5000")
            driver.released.set()

    @pytest.mark.asyncio
    async def test_j_and_k_move_the_selection(self) -> None:
        driver = FakeDriver(_row(10), _row(11), _row(12))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 3)
            await pilot.press("j", "j")
            assert "▶ #12" in _rendered(app)

            await pilot.press("k")
            assert "▶ #11" in _rendered(app)
            driver.released.set()

    @pytest.mark.asyncio
    async def test_enter_on_a_live_vm_attaches_without_booting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10), _row(11), live=(11,))
        app = _app(driver)
        calls = _attaches(monkeypatch)
        trace = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)
            await pilot.press("j")
            await pilot.press("enter")

            assert calls == [("dtach", "-a", "/sockets/issue-11.sock", "-r", "none")]
            assert driver.booted == []
            assert trace == ["enter", "exit"]
            driver.released.set()

    @pytest.mark.asyncio
    async def test_enter_on_an_exited_vm_boots_it_and_then_attaches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10))
        app = _app(driver)
        calls = _attaches(monkeypatch)
        _ = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")

            assert driver.booted == [10]
            assert calls == [("dtach", "-a", "/sockets/issue-10.sock", "-r", "none")]
            driver.released.set()

    def test_every_refusal_the_orchestrator_can_give_has_a_case(self) -> None:
        assert {entry.refusal for entry in _REFUSED} == set(DebugRefusal)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refused", _REFUSED, ids=_refusal_id)
    async def test_a_refused_entry_says_why_and_never_suspends(
        self, monkeypatch: pytest.MonkeyPatch, refused: DebugEntry
    ) -> None:
        driver = FakeDriver(_row(10), refused=refused)
        app = _app(driver)
        calls = _attaches(monkeypatch)
        trace = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")

            await _said(app, pilot, debug_line(refused))
            assert calls == []
            assert trace == ["status"]
            driver.released.set()

    @pytest.mark.asyncio
    async def test_enter_with_no_row_to_stand_on_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver()
        app = _app(driver)
        calls = _attaches(monkeypatch)
        trace = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _until(pilot, lambda: bool(driver.asked), "polled for rows")
            await pilot.press("enter")
            await pilot.pause(0.1)

            assert driver.asked_to_enter == []
            assert (calls, trace) == ([], [])
            driver.released.set()

    def test_the_footer_names_the_enter_key_debug(self) -> None:
        keyed = [
            binding
            for binding in DashboardScreen.BINDINGS
            if isinstance(binding, Binding) and binding.key == "enter"
        ]

        assert [(one.action, one.description) for one in keyed] == [("debug", "Debug")]

    @pytest.mark.asyncio
    async def test_the_dashboard_outlives_a_finished_batch(self) -> None:
        driver = FakeDriver(_row(10), result=RunResult(targets=(EPIC,), outcomes=()))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _ended(app, pilot)

            assert app.is_running
            assert app.result is not None
            await _said(app, pilot, "Batch finished under #1492.")

    @pytest.mark.asyncio
    async def test_quitting_leaves_the_run_in_flight(self) -> None:
        driver = FakeDriver(_row(10))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("q")
            await pilot.pause(0.1)

            assert not app.is_running
            assert driver.running.is_set()
            assert not driver.released.is_set()
            assert app.result is None
        driver.released.set()

    @pytest.mark.asyncio
    async def test_the_status_line_carries_the_latest_narration(self) -> None:
        driver = FakeDriver(_row(10))
        narration = ["#10 issue-10 on main"]
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, narration=narration, interval=0.05
        )

        async with app.run_test() as pilot:
            await _said(app, pilot, "#10 issue-10 on main")

            narration.append("#10 ready for review")
            await _said(app, pilot, "#10 ready for review")
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_refusal_outlives_the_next_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10), refused=_no_slot(10))
        narration = ["#10 issue-10 on main"]
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, narration=narration, interval=0.05
        )

        def unreached(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            raise AssertionError(f"attached to a slotless issue: {command}")

        monkeypatch.setattr("batch.tui.screen.subprocess.run", unreached)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")
            await pilot.pause(0.2)

            await _said(app, pilot, debug_line(_no_slot(10)))
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_missing_dtach_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10), live=(10,))
        app = _app(driver)

        def absent(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            raise FileNotFoundError(command)

        monkeypatch.setattr("batch.tui.screen.subprocess.run", absent)
        trace = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")

            await _said(app, pilot, "dtach is not installed.")
            assert trace == ["enter", "status", "exit"]
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_failed_poll_is_reported_and_the_run_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10))

        def broken(_targets: Sequence[int]) -> Batch:
            raise RuntimeError("GitHub said 502")

        monkeypatch.setattr(driver, "fetch", broken)
        app = _app(driver)

        async with app.run_test() as pilot:
            await _said(app, pilot, "GitHub said 502")

            assert app.is_running
            driver.released.set()


class TestVerbs:
    @pytest.mark.asyncio
    async def test_f_reworks_the_selected_issue(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.READY_FOR_REVIEW))
        verbs = FakeVerbs()
        app = DashboardApp((EPIC,), driver, prog=_PROG, verbs=verbs, interval=0.05)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)
            await pilot.press("j")
            await pilot.press("f")

            assert verbs.calls == [(RecoveryAction.REWORK, 11)]
            await _said(app, pilot, "#11 reworking (was stuck)")
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_halt_leaves_the_dashboard_up_with_a_sticky_banner(self) -> None:
        halted = RunResult(
            targets=(EPIC,),
            outcomes=(
                IssueOutcome(
                    number=10,
                    base="main",
                    state=BatchLabel.STUCK,
                    halt=HaltReason.TIMED_OUT,
                ),
            ),
        )
        driver = FakeDriver(_row(10, BatchLabel.STUCK), result=halted)
        app = DashboardApp(
            (EPIC,),
            driver,
            prog=_PROG,
            narration=["#10 issue-10 on main"],
            interval=0.05,
        )

        async with app.run_test() as pilot:
            await _ended(app, pilot)

            assert app.is_running
            await _said(app, pilot, "halted at #10 (timed-out)")

            await pilot.press("j")
            await pilot.pause(0.2)

            await _said(app, pilot, "halted at #10 (timed-out)")

    @pytest.mark.asyncio
    async def test_relaunching_a_halted_batch_restarts_the_run(self) -> None:
        halted = RunResult(
            targets=(EPIC,),
            outcomes=(
                IssueOutcome(
                    number=10,
                    base="main",
                    state=BatchLabel.STUCK,
                    halt=HaltReason.STUCK_ISSUE,
                ),
            ),
        )
        driver = FakeDriver(_row(10, BatchLabel.STUCK), result=halted)
        verbs = FakeVerbs()
        app = DashboardApp((EPIC,), driver, prog=_PROG, verbs=verbs, interval=0.05)

        async with app.run_test() as pilot:
            await _ended(app, pilot)
            assert driver.runs == 1

            await pilot.press("r")
            await _until(pilot, lambda: driver.runs == 2, "restarted its run")

    @pytest.mark.asyncio
    async def test_a_rework_never_restarts_the_run(self) -> None:
        halted = RunResult(
            targets=(EPIC,),
            outcomes=(
                IssueOutcome(
                    number=10,
                    base="main",
                    state=BatchLabel.STUCK,
                    halt=HaltReason.STUCK_ISSUE,
                ),
            ),
        )
        driver = FakeDriver(_row(10, BatchLabel.STUCK), result=halted)
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, verbs=FakeVerbs(), interval=0.05
        )

        async with app.run_test() as pilot:
            await _ended(app, pilot)
            await pilot.press("f")
            await pilot.pause(0.3)

            assert driver.runs == 1

    @pytest.mark.asyncio
    async def test_a_verb_during_a_live_run_starts_no_second_run(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.STUCK))
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, verbs=FakeVerbs(), interval=0.05
        )

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)
            await _until(pilot, driver.running.is_set, "started its run")
            await pilot.press("j")
            await pilot.press("s")
            await pilot.pause(0.2)

            assert driver.runs == 1
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_refused_verb_changes_nothing(self) -> None:
        halted = RunResult(
            targets=(EPIC,),
            outcomes=(
                IssueOutcome(
                    number=10,
                    base="main",
                    state=BatchLabel.STUCK,
                    halt=HaltReason.STUCK_ISSUE,
                ),
            ),
        )
        driver = FakeDriver(_row(10, BatchLabel.STUCK), result=halted)
        app = DashboardApp(
            (EPIC,),
            driver,
            prog=_PROG,
            verbs=FakeVerbs(RecoveryRefusal.VM_LIVE),
            interval=0.05,
        )

        async with app.run_test() as pilot:
            await _ended(app, pilot)
            await pilot.press("s")
            await pilot.pause(0.3)

            assert driver.runs == 1
            await _said(app, pilot, "Cannot skip #10: its VM is still running")
            assert "failed:" not in _status(app)

    @pytest.mark.asyncio
    async def test_a_refusal_remedy_names_the_configured_wrapper(self) -> None:
        driver = FakeDriver(_row(10, BatchLabel.STUCK))
        verbs = FakeVerbs(RecoveryRefusal.MERGED)
        app = DashboardApp((EPIC,), driver, prog=_PROG, verbs=verbs, interval=0.05)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("s")

            await _said(app, pilot, "run `bin/acme cleanup <target>`")
            assert "dev/batch" not in _status(app)
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_verb_follows_the_selected_issue_across_a_refresh(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.STUCK))
        verbs = FakeVerbs()
        app = DashboardApp((EPIC,), driver, prog=_PROG, verbs=verbs, interval=0.05)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)
            await pilot.press("j")
            driver.rows = (_row(8), _row(9), _row(10), _row(11, BatchLabel.STUCK))
            await _settle(app, pilot, 4)
            await pilot.press("s")

            assert verbs.calls == [(RecoveryAction.SKIP, 11)]
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_skipped_issue_leaves_the_next_frame(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.STUCK))

        class Dropping(FakeVerbs):
            @override
            def skip(self, issue: int) -> RecoveryResult:
                driver.rows = tuple(row for row in driver.rows if row.number != issue)
                return super().skip(issue)

        app = DashboardApp((EPIC,), driver, prog=_PROG, verbs=Dropping(), interval=0.05)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 2)
            await pilot.press("j")
            await pilot.press("s")
            await _settle(app, pilot, 1)

            assert "#11" not in _table(app)
            assert "#10" in _table(app)
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_poll_after_teardown_is_silent(self) -> None:
        driver = FakeDriver(_row(10))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            driver.released.set()

        app.dashboard._tick()
        app.dashboard.status("still here")

    @pytest.mark.asyncio
    async def test_a_crashed_run_is_reported_and_still_raised_on_quit(self) -> None:
        driver = FakeDriver(_row(10))

        def broken() -> RunResult:
            raise RuntimeError("git said no")

        app = DashboardApp((EPIC,), driver, prog=_PROG, drive=broken, interval=0.05)

        async with app.run_test() as pilot:
            await _said(app, pilot, "Run failed: git said no.")

            assert app.is_running
            assert isinstance(app.failure, RuntimeError)


class _Exploding(FakeVerbs):
    def __init__(self, action: RecoveryAction) -> None:
        super().__init__()
        self.action: RecoveryAction = action

    @override
    def _result(self, action: RecoveryAction, issue: int) -> RecoveryResult:
        if action is self.action:
            raise RuntimeError("GitHub said 503")
        return super()._result(action, issue)


_HALTED = RunResult(
    targets=(EPIC,),
    outcomes=(
        IssueOutcome(
            number=10,
            base="main",
            state=BatchLabel.STUCK,
            halt=HaltReason.STUCK_ISSUE,
        ),
    ),
)


class TestKeyHandlersNeverCrash:
    @pytest.mark.asyncio
    async def test_an_entry_that_raises_leaves_the_dashboard_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10))

        def broken(_issue_number: int) -> DebugEntry:
            raise CalledProcessError(128, ["git", "worktree", "list"])

        monkeypatch.setattr(driver, "enter", broken)
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")

            await _said(app, pilot, "Debug failed: Command '['git', 'worktree'")
            assert app.is_running
            assert app.failure is None
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_verb_that_raises_leaves_the_dashboard_up(self) -> None:
        driver = FakeDriver(_row(10))
        app = DashboardApp(
            (EPIC,),
            driver,
            prog=_PROG,
            verbs=_Exploding(RecoveryAction.REWORK),
            interval=0.05,
        )

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("f")

            await _said(app, pilot, "Rework failed: GitHub said 503")
            assert app.is_running
            assert app.failure is None
            driver.released.set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("key", "action"),
        [("s", RecoveryAction.SKIP), ("r", RecoveryAction.RELAUNCH)],
    )
    async def test_a_failed_verb_is_labelled_by_its_own_name(
        self, key: str, action: RecoveryAction
    ) -> None:
        driver = FakeDriver(_row(10))
        app = DashboardApp(
            (EPIC,), driver, prog=_PROG, verbs=_Exploding(action), interval=0.05
        )

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press(key)

            await _said(app, pilot, f"{action.value.capitalize()} failed:")
            driver.released.set()

    @pytest.mark.asyncio
    async def test_a_failed_verb_does_not_restart_a_halted_run(self) -> None:
        driver = FakeDriver(_row(10, BatchLabel.STUCK), result=_HALTED)
        app = DashboardApp(
            (EPIC,),
            driver,
            prog=_PROG,
            verbs=_Exploding(RecoveryAction.SKIP),
            interval=0.05,
        )

        async with app.run_test() as pilot:
            await _ended(app, pilot)
            await pilot.press("s")
            await _said(app, pilot, "Skip failed:")
            await pilot.pause(0.3)

            assert driver.runs == 1

    @pytest.mark.asyncio
    async def test_a_spawn_that_is_not_a_missing_dtach_is_reported_generically(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = FakeDriver(_row(10), live=(10,))
        app = _app(driver)

        def denied(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            raise PermissionError(command)

        monkeypatch.setattr("batch.tui.screen.subprocess.run", denied)
        trace = _trace(monkeypatch)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("enter")

            await _said(app, pilot, "Debug failed:")
            assert app.is_running
            assert trace == ["enter", "exit", "status"]
            driver.released.set()


RUN = "run"


class _Host(App[None]):
    """A bare app standing in for orbit: it installs the screen and pushes it."""

    def __init__(self, screen: DashboardScreen) -> None:
        super().__init__()
        self.dashboard: DashboardScreen = screen

    def on_mount(self) -> None:
        self.dashboard.install_on(self, RUN)
        self.push_screen(RUN)


class TestHostedScreen:
    @pytest.mark.asyncio
    async def test_rows_status_keys_and_verbs_work_under_another_app(self) -> None:
        driver = FakeDriver(_row(10), _row(11, BatchLabel.STUCK))
        verbs = FakeVerbs()
        screen = DashboardScreen(
            (EPIC,), driver, prog=_PROG, verbs=verbs, interval=0.05, fetch_interval=0.05
        )
        app = _Host(screen)

        async with app.run_test() as pilot:
            await _shown(screen, pilot, 2)
            assert "#10" in _table(app)
            await pilot.press("j")
            assert "▶ #11" in _rendered(app)
            await pilot.press("k")
            assert "▶ #10" in _rendered(app)
            await pilot.press("j")
            await pilot.press("s")

            assert verbs.calls == [(RecoveryAction.SKIP, 11)]
            await _said(app, pilot, "#11 skipped (was stuck)")
            driver.released.set()

    @pytest.mark.asyncio
    async def test_an_installed_screen_keeps_its_state_across_a_pop(self) -> None:
        driver = FakeDriver(_row(10), _row(11))
        narration = ["#10 issue-10 on main"]
        screen = DashboardScreen(
            (EPIC,), driver, prog=_PROG, narration=narration, interval=0.05
        )
        app = _Host(screen)

        async with app.run_test() as pilot:
            await _shown(screen, pilot, 2)
            await pilot.press("j")
            await _said(app, pilot, "#10 issue-10 on main")
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.screen is not screen
            assert screen.live

            app.push_screen(RUN)
            await pilot.pause(0.1)

            assert app.screen is screen
            assert "▶ #11" in _rendered(app)
            assert "#10 issue-10 on main" in _status(app)
            driver.released.set()

    @pytest.mark.asyncio
    async def test_q_on_the_base_screen_quits_the_app(self) -> None:
        driver = FakeDriver(_row(10))
        app = _app(driver)

        async with app.run_test() as pilot:
            await _settle(app, pilot, 1)
            await pilot.press("escape")
            await pilot.pause(0.1)

            assert not app.is_running
        driver.released.set()
