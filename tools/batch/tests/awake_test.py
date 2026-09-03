from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import override

import pytest

from batch.awake import CAFFEINATE, awake


class FakeChild:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        self.timeouts.append(timeout)
        return 0


class FakeSpawn:
    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self.children: list[FakeChild] = []

    def __call__(self, argv: Sequence[str]) -> FakeChild:
        self.argvs.append(tuple(argv))
        child = FakeChild()
        self.children.append(child)
        return child


class TestAwake:
    def test_darwin_asserts_idle_sleep_against_our_own_pid(self) -> None:
        spawn = FakeSpawn()

        with awake(lambda _line: None, spawn=spawn, platform="darwin"):
            pass

        assert spawn.argvs == [("caffeinate", "-i", "-w", str(os.getpid()))]

    def test_off_darwin_nothing_is_spawned_and_nothing_is_reported(self) -> None:
        spawn = FakeSpawn()
        reported: list[str] = []

        with awake(reported.append, spawn=spawn, platform="linux"):
            pass

        assert spawn.argvs == []
        assert reported == []

    def test_the_child_is_terminated_when_the_block_exits(self) -> None:
        spawn = FakeSpawn()

        with awake(lambda _line: None, spawn=spawn, platform="darwin"):
            pass

        (child,) = spawn.children
        assert child.calls == ["terminate", "wait"]
        assert child.timeouts == [5.0]

    @pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
    def test_a_raising_body_still_releases_the_assertion(
        self, failure: type[BaseException]
    ) -> None:
        spawn = FakeSpawn()

        with (
            pytest.raises(failure),
            awake(lambda _line: None, spawn=spawn, platform="darwin"),
        ):
            raise failure

        (child,) = spawn.children
        assert child.calls == ["terminate", "wait"]

    def test_a_missing_caffeinate_warns_and_the_run_continues(self) -> None:
        def spawn(_argv: Sequence[str]) -> FakeChild:
            raise FileNotFoundError("caffeinate")

        reported: list[str] = []
        entered = False

        with awake(reported.append, spawn=spawn, platform="darwin"):
            entered = True

        assert entered
        assert len(reported) == 1
        assert "caffeinate" in reported[0]

    def test_a_child_that_refuses_to_die_does_not_hang_the_exit(self) -> None:
        class StuckChild(FakeChild):
            @override
            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(CAFFEINATE[0], timeout or 0.0)

        stuck = StuckChild()
        reported: list[str] = []

        with awake(reported.append, spawn=lambda _argv: stuck, platform="darwin"):
            pass

        assert stuck.calls == ["terminate"]
        assert len(reported) == 1
        assert "caffeinate" in reported[0]
