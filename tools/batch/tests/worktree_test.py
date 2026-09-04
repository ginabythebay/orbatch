from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from batch.models import Slot
from batch.stack import StackManager
from batch.testing.payloads import TEST_COMMANDS, batch_config
from batch.testing.scratch import SEED_CONTENT, TRACKED_FILE, Scratch, git, scratch
from batch.worktree import (
    CONFIRM,
    CliConsole,
    Console,
    WorktreeSession,
    agent_flags,
)


@pytest.fixture(name="sc")
def scratch_repo(tmp_path: Path) -> Scratch:
    return scratch(tmp_path)


@dataclass(frozen=True)
class Boot:
    slot: Slot
    config_dir: Path
    disk: bytes | None
    staged: bool


class FakeConsole(Console):
    def __init__(self, *acts: Callable[[Slot], None], code: int = 0) -> None:
        self.boots: list[Boot] = []
        self._acts: list[Callable[[Slot], None]] = list(acts)
        self._code: int = code

    @override
    def boot(self, slot: Slot, config_dir: Path) -> int:
        self.boots.append(
            Boot(
                slot=slot,
                config_dir=config_dir,
                disk=slot.disk.read_bytes() if slot.disk.is_file() else None,
                staged=config_dir.is_dir(),
            )
        )
        if self._acts:
            self._acts.pop(0)(slot)
        return self._code


def commit(slot: Slot) -> None:
    _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "solo work")


def commit_and_push(slot: Slot) -> None:
    commit(slot)
    _ = git(slot.worktree, "push", "-q", "origin", slot.branch)


def soil(slot: Slot) -> None:
    _ = (slot.worktree / TRACKED_FILE).write_text("edited\n")


@dataclass
class Asked:
    answers: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


@dataclass(frozen=True)
class Harness:
    sc: Scratch
    stack: StackManager
    console: FakeConsole
    asked: Asked
    said: list[str]

    def run(self, branch: str = "fix-thing") -> int:
        return WorktreeSession(
            self.stack, self.console, ask=self.asked, echo=self.said.append
        ).run(branch)

    def standing(self, branch: str = "fix-thing") -> tuple[bool, bool, bool]:
        branches = git(self.sc.repo, "branch", "--format=%(refname:short)").split()
        return (
            (self.stack.worktree_root / branch).is_dir(),
            branch in branches,
            (self.stack.worktree_root / f"{branch}.raw").is_file(),
        )


def harness(sc: Scratch, console: FakeConsole, *answers: str) -> Harness:
    return Harness(
        sc=sc,
        stack=StackManager(sc.repo, seed_image=sc.seed),
        console=console,
        asked=Asked(answers=list(answers)),
        said=[],
    )


class TestCleanRun:
    def test_sets_up_and_tears_down(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(), "")
        h.run()

        [boot] = h.console.boots
        assert boot.slot.worktree == sc.trees / "fix-thing"
        assert boot.disk == SEED_CONTENT
        assert boot.staged
        assert not boot.config_dir.exists()
        assert h.standing() == (False, False, False)

    def test_pushed_work_tears_down_without_a_prompt(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(commit_and_push), "")
        h.run()

        assert h.asked.prompts == [CONFIRM]
        assert h.standing() == (False, False, False)


class TestRisks:
    def test_an_unpushed_commit_prompts_and_r_re_enters_the_console(
        self, sc: Scratch
    ) -> None:
        h = harness(sc, FakeConsole(commit), "r", "q")
        h.run()

        first, second = h.console.boots
        assert second.slot == first.slot
        assert second.config_dir != first.config_dir
        assert h.said == ["fix-thing has commits no remote has."] * 2

    def test_d_deletes_anyway(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(commit), "d")
        h.run()

        assert h.standing() == (False, False, False)

    def test_q_leaves_the_slot_standing(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(commit), "q")
        h.run()

        assert h.standing() == (True, True, True)

    def test_a_dirty_worktree_prompts_and_q_leaves_it(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(soil), "q")
        h.run()

        assert h.said == ["fix-thing has uncommitted changes."]
        assert h.standing() == (True, True, True)

    def test_both_risks_name_themselves_in_one_prompt(self, sc: Scratch) -> None:
        def work(slot: Slot) -> None:
            commit(slot)
            soil(slot)

        h = harness(sc, FakeConsole(work), "q")
        h.run()

        assert h.said == [
            "fix-thing has uncommitted changes and commits no remote has."
        ]
        assert len(h.asked.prompts) == 1

    def test_d_on_a_dirty_worktree_removes_it(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(soil), "d")
        h.run()

        assert h.standing() == (False, False, False)

    def test_an_unrecognised_answer_re_asks(self, sc: Scratch) -> None:
        h = harness(sc, FakeConsole(commit), "x", "q")
        h.run()

        assert "Answer r, d, or q." in h.said
        assert len(h.asked.prompts) == 2
        assert len(h.console.boots) == 1
        assert h.standing() == (True, True, True)


class TestFailedBoot:
    def test_a_console_exiting_non_zero_still_reaches_teardown(
        self, sc: Scratch
    ) -> None:
        h = harness(sc, FakeConsole(code=3), "")

        assert h.run() == 3
        assert h.standing() == (False, False, False)


class TestConsoleCommand:
    def test_the_options_reach_the_argv(self, sc: Scratch) -> None:
        stack = StackManager(sc.repo, seed_image=sc.seed)
        slot = stack.ensure_branch("fix-thing", "HEAD")
        console = CliConsole(
            config=batch_config(),
            mount_root=stack.mount_root,
            flags=agent_flags(
                issue=42,
                guidance="write it twice",
                model="opus",
                base="release",
                max_tests=3,
                plan_guidance="stay small",
            ),
        )

        command = console.command(slot, Path("/tmp/staged"))

        relative = str(slot.worktree.relative_to(stack.mount_root))
        assert command == (
            TEST_COMMANDS.cli,
            "--repo",
            relative,
            "vm",
            "console",
            "--worktree",
            relative,
            "--disk",
            str(slot.disk),
            "--config-dir",
            "/tmp/staged",
            "--issue",
            "42",
            "--guidance",
            "write it twice",
            "--model",
            "opus",
            "--base",
            "release",
            "--max-tests",
            "3",
            "--plan-guidance",
            "stay small",
        )

    def test_options_left_alone_stay_off_the_argv(self) -> None:
        assert agent_flags(
            issue=None,
            guidance=None,
            model="opus",
            base=None,
            max_tests=None,
            plan_guidance=None,
        ) == ("--model", "opus")
