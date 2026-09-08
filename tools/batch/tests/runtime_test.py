from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from batch.config import ConfigError
from batch.models import RunResult
from batch.orchestrator import Orchestrator
from batch.runtime import Runtime, watch
from batch.state import BatchState
from batch.testing.payloads import (
    EPIC,
    REPO,
    TEST_COMMANDS,
    TEST_SLUG,
    FakeRunner,
    FakeStack,
    FakeState,
    batch_config,
    batch_issue,
    fake_orchestrator,
    write_config,
)
from batch.verbs import Verbs
from batch.vm import DEFAULT_RUN_ROOT, scoped_run_root
from ghgql.repo import Repo


class TestLoad:
    def test_it_reads_the_repo_s_config_and_scopes_the_run_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        repo = write_config(tmp_path / "repo")

        runtime = Runtime.load(repo)

        assert runtime.repo == repo
        assert runtime.config.slug == TEST_SLUG
        assert (
            runtime.run_root
            == scoped_run_root(DEFAULT_RUN_ROOT, TEST_SLUG).expanduser()
        )
        assert (
            runtime.run_root
            == tmp_path / "home" / ".cache" / "batch" / "acme" / "widgets"
        )

    def test_a_repo_without_a_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            _ = Runtime.load(tmp_path / "repo")

        assert "batch.toml" in str(caught.value)


class TestWatch:
    def _drive(self, orchestrator: Orchestrator) -> RunResult:
        return watch(
            orchestrator,
            (EPIC,),
            0.0,
            prog="batch",
            report=lambda _line: None,
            echo=False,
        )

    def _refused(self) -> FakeState:
        state = FakeState()
        state.closed.append(batch_issue(9))
        return state

    def test_a_line_from_the_first_call_is_said_again_on_the_second(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []
        orchestrator = fake_orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)
        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"] * 2

    def test_the_original_report_is_back_once_the_call_returns(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []
        orchestrator = fake_orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)

        assert orchestrator.report == said.append

    def test_the_original_report_is_back_after_an_interrupt(
        self, tmp_path: Path
    ) -> None:
        said: list[str] = []

        def interrupt(_state: FakeState) -> None:
            raise KeyboardInterrupt

        state = self._refused()
        state.on_fetch = interrupt
        orchestrator = fake_orchestrator(state, tmp_path, report=said.append)

        with pytest.raises(SystemExit) as exit_code:
            _ = self._drive(orchestrator)

        assert exit_code.value.code == 130
        assert orchestrator.report == said.append

        state.on_fetch = None
        said.clear()
        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"]

    def test_a_line_repeated_within_one_pass_is_said_once(self, tmp_path: Path) -> None:
        said: list[str] = []
        orchestrator = fake_orchestrator(self._refused(), tmp_path, report=said.append)

        _ = self._drive(orchestrator)

        assert said == ["#9 left alone (not-merged)"]

    def test_a_line_repeated_across_passes_is_said_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        state = FakeState(batch_issue(10), queued_targets=(EPIC,))
        state.closed.append(batch_issue(9))

        def stop_waiting(_seconds: float) -> None:
            state.queued_targets = ()

        monkeypatch.setattr("batch.runtime.sleep", stop_waiting)

        _ = self._drive(fake_orchestrator(state, tmp_path, report=said.append))

        assert said.count("#9 left alone (not-merged)") == 2


PLAN_PID = 4321
PLAN_BRANCH = f"plan-{PLAN_PID}"


class Faked:
    """The real session over fake collaborators sharing one journal."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, root: Path, *, dirty: Sequence[str] = ()
    ) -> None:
        self.journal: list[str] = []
        self.stack: FakeStack = FakeStack(root, dirty=dirty, journal=self.journal)
        self.runner: FakeRunner = FakeRunner(root, journal=self.journal)
        monkeypatch.setattr("batch.runtime.StackManager", self._stack_at)
        monkeypatch.setattr("batch.runtime.VmRunner", self._runner_at)
        self.runtime: Runtime = Runtime(root / "repo", batch_config(), root)

    def _stack_at(self, _repo: Path, *, seed_image: Path) -> FakeStack:
        self.stack.seed_image = seed_image
        return self.stack

    def _runner_at(self, _root: Path, **_kwargs: object) -> FakeRunner:
        return self.runner


class TestPlanSession:
    @pytest.fixture(autouse=True)
    def _pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("batch.runtime.os.getpid", lambda: PLAN_PID)

    def test_it_stages_boots_cleans_and_reclaims_in_that_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        faked = Faked(monkeypatch, tmp_path)

        def boot(command: Sequence[str], cwd: Path | None) -> int:
            faked.journal.append(f"boot {command[0]} from {cwd}")
            return 3

        outcome = faked.runtime.plan_session((EPIC,), spawn=boot)

        assert faked.journal == [
            f"stage {PLAN_BRANCH}.config",
            f"boot vibe from {tmp_path}",
            f"clean {PLAN_BRANCH}.config",
            f"remove {PLAN_BRANCH}",
        ]
        assert outcome.returncode == 3
        assert outcome.refusal is None
        assert faked.stack.currented == [(PLAN_BRANCH, "main")]
        assert faked.stack.seed_image == batch_config().seed_image

    def test_a_reclaim_refusal_is_returned_not_printed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        faked = Faked(monkeypatch, tmp_path, dirty=(PLAN_BRANCH,))

        outcome = faked.runtime.plan_session((EPIC,), spawn=lambda _command, _cwd: 0)

        assert outcome.refusal == (
            f"{PLAN_BRANCH} is not safe to remove: the worktree has local changes; "
            f"`{TEST_COMMANDS.cli} gc` once you are done with it."
        )
        assert capsys.readouterr() == ("", "")
        assert faked.stack.removed_branches == []

    def test_a_dry_run_boots_nothing_and_hands_back_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        faked = Faked(monkeypatch, tmp_path)

        def never(_command: Sequence[str], _cwd: Path | None) -> int:
            raise AssertionError("booted")

        outcome = faked.runtime.plan_session((EPIC, 1769), dry_run=True, spawn=never)

        assert outcome.command[0] == "vibe"
        assert f"tools/plan {EPIC} 1769" in " ".join(outcome.command)
        assert outcome.returncode == 0
        assert faked.journal == [
            f"clean {PLAN_BRANCH}.config",
            f"remove {PLAN_BRANCH}",
        ]


class TestDrive:
    def _runtime(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> Runtime:
        state = FakeState(batch_issue(10))
        state.closed.append(batch_issue(9))

        def built(
            _self: Runtime, *, report: Callable[[str], None], **_kwargs: object
        ) -> Orchestrator:
            return fake_orchestrator(state, root, report=report, polls={10: 0})

        monkeypatch.setattr(Runtime, "orchestrator", built)
        return Runtime(root / "repo", batch_config(), root)

    def test_the_run_narrates_through_report_and_prints_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        said: list[str] = []
        drive = self._runtime(monkeypatch, tmp_path).drive((EPIC,), said.append)

        result = drive.run()

        assert [outcome.number for outcome in result.outcomes] == [10]
        assert "#9 left alone (not-merged)" in said
        assert capsys.readouterr() == ("", "")

    def test_the_verbs_are_scoped_to_the_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drive = self._runtime(monkeypatch, tmp_path).drive((EPIC,), lambda _l: None)

        assert isinstance(drive.verbs, Verbs)
        assert drive.verbs.skip(10).refusal is None
        assert drive.verbs.skip(999).refusal is not None


def _origin(_path: Path) -> Repo:
    return REPO


class TestLabellingVerbs:
    @pytest.mark.parametrize(
        ("verb", "method"),
        [
            (Runtime.queue, "queue"),
            (Runtime.unqueue, "unqueue"),
            (Runtime.approve, "approve"),
            (Runtime.fast_track, "fast_track"),
        ],
        ids=["queue", "unqueue", "approve", "fast_track"],
    )
    def test_each_reaches_the_state_over_the_bare_targets(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verb: Callable[[Runtime, Sequence[int]], object],
        method: str,
    ) -> None:
        monkeypatch.setattr("batch.runtime.repo", _origin)
        runtime = Runtime(tmp_path, batch_config(), tmp_path)

        with patch.object(BatchState, method) as called:
            _ = verb(runtime, (10, 11))

        called.assert_called_once_with(None, (10, 11))
