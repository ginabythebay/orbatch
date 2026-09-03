from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from batch.models import BatchIssue, BatchLabel, RecoveryRefusal
from batch.recovery import Recovery
from batch.testing.payloads import (
    EPIC,
    FakeRunner,
    FakeStack,
    FakeState,
    batch_config,
    batch_issue,
    fake_account,
)
from batch.verbs import Verbs
from batch.vm import VmRunner


def verbs(
    tmp_path: Path,
    *issues: BatchIssue,
    live: tuple[int, ...] = (),
    model: str | None = None,
) -> tuple[Verbs, FakeState, FakeStack, FakeRunner]:
    state = FakeState(*issues)
    stack = FakeStack(tmp_path)
    polls = {issue.number: 99 if issue.number in live else 0 for issue in issues}
    runner = FakeRunner(tmp_path, polls=polls)
    return (
        Verbs(
            (EPIC,),
            state,
            stack,
            runner,
            Recovery(state, runner),
            config=batch_config(),
            model=model,
        ),
        state,
        stack,
        runner,
    )


class TestRework:
    def test_it_adopts_the_existing_branch_and_leaves_the_label_alone(
        self, tmp_path: Path
    ) -> None:
        keys, state, stack, runner = verbs(
            tmp_path,
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.PLANNED),
        )

        result = keys.rework(10)

        assert result.refusal is None
        assert stack.ensured == [(10, "main")]
        assert [number for number, _ in runner.launched] == [10]
        assert state.transitions == []

    def test_the_session_is_interactive_and_carries_the_stack_base(
        self, tmp_path: Path
    ) -> None:
        keys, _, _, runner = verbs(
            tmp_path,
            batch_issue(9, BatchLabel.READY_FOR_REVIEW),
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
        )

        _ = keys.rework(10)

        assert runner.staged == [(tmp_path / "issue-10.config", False)]
        assert runner.agents() == ["tools/drive 10 --rework --base issue-9"]

    def test_a_stuck_issue_is_reworkable_and_stays_stuck(self, tmp_path: Path) -> None:
        keys, state, stack, runner = verbs(tmp_path, batch_issue(10, BatchLabel.STUCK))

        result = keys.rework(10)

        assert result.refusal is None
        assert result.found is BatchLabel.STUCK
        assert stack.ensured == [(10, "main")]
        assert runner.launched != []
        assert state.states() == {10: BatchLabel.STUCK}

    def test_the_base_is_the_predecessor_even_once_successors_have_started(
        self, tmp_path: Path
    ) -> None:
        keys, _, stack, runner = verbs(
            tmp_path,
            batch_issue(9, BatchLabel.READY_FOR_REVIEW),
            batch_issue(10, BatchLabel.READY_FOR_REVIEW),
            batch_issue(11, BatchLabel.IMPLEMENTING),
        )

        _ = keys.rework(10)

        assert stack.ensured == [(10, "issue-9")]
        assert runner.agents() == ["tools/drive 10 --rework --base issue-9"]

    def test_the_model_reaches_the_rework_agent(self, tmp_path: Path) -> None:
        keys, _, _, runner = verbs(
            tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW), model="opus"
        )

        _ = keys.rework(10)

        assert runner.agents() == ["tools/drive 10 --rework --base main --model opus"]

    def test_an_implementing_issue_is_refused_for_what_it_is(
        self, tmp_path: Path
    ) -> None:
        keys, _, stack, _ = verbs(tmp_path, batch_issue(10, BatchLabel.IMPLEMENTING))

        result = keys.rework(10)

        assert result.refusal is RecoveryRefusal.WRONG_STATE
        assert stack.ensured == []

    @pytest.mark.parametrize("label", [BatchLabel.QUEUED, BatchLabel.PLANNED])
    def test_an_unstarted_issue_has_no_branch_to_rework(
        self, tmp_path: Path, label: BatchLabel
    ) -> None:
        keys, _, stack, runner = verbs(tmp_path, batch_issue(10, label))

        result = keys.rework(10)

        assert result.refusal is RecoveryRefusal.NO_BRANCH
        assert result.found is label
        assert stack.ensured == []
        assert runner.launched == []

    def test_a_live_vm_refuses_the_rework(self, tmp_path: Path) -> None:
        keys, _, stack, runner = verbs(
            tmp_path, batch_issue(10, BatchLabel.READY_FOR_REVIEW), live=(10,)
        )

        result = keys.rework(10)

        assert result.refusal is RecoveryRefusal.VM_LIVE
        assert stack.ensured == []
        assert runner.launched == []

    def test_the_vm_lands_on_the_issues_own_socket_and_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _ = (home / ".claude.json").write_text("{}\n")
        monkeypatch.setenv("HOME", str(home))
        launched: list[Sequence[str]] = []

        def record(
            command: Sequence[str], **_kwargs: object
        ) -> CompletedProcess[bytes]:
            launched.append(command)
            return CompletedProcess(list(command), 0)

        monkeypatch.setattr("batch.vm.subprocess.run", record)
        state = FakeState(batch_issue(10, BatchLabel.READY_FOR_REVIEW))
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=batch_config,
            disks=frozenset,
            token=lambda _item: "gh-tok",
            account=fake_account(),
        )

        result = Verbs(
            (EPIC,),
            state,
            FakeStack(tmp_path),
            runner,
            Recovery(state, runner),
            config=batch_config(),
        ).rework(10)

        assert result.refusal is None
        assert launched[0][2] == str(runner.socket(10))
        assert str(runner.log(10)) in launched[0][5]
        assert runner.attach_command(10)[2] == str(runner.socket(10))

    def test_an_issue_outside_the_batch_is_refused(self, tmp_path: Path) -> None:
        keys, _, _, runner = verbs(tmp_path, batch_issue(11, BatchLabel.PLANNED))

        result = keys.rework(10)

        assert result.refusal is RecoveryRefusal.NOT_IN_BATCH
        assert result.found is None
        assert runner.launched == []
