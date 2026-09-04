from __future__ import annotations

from pathlib import Path

import pytest

from batch.lock import BatchInProgressError, run_lock
from batch.vm import scoped_run_root


class TestRunLock:
    def test_a_second_run_is_refused_while_the_first_holds_it(
        self, tmp_path: Path
    ) -> None:
        with (
            run_lock(tmp_path),
            pytest.raises(BatchInProgressError),
            run_lock(tmp_path),
        ):
            pass

    def test_the_lock_is_released_when_the_run_finishes(self, tmp_path: Path) -> None:
        with run_lock(tmp_path):
            pass

        with run_lock(tmp_path) as path:
            assert path.exists()

    def test_a_run_that_raises_still_releases_the_lock(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="halted"), run_lock(tmp_path):
            raise RuntimeError("halted")

        with run_lock(tmp_path):
            pass

    def test_the_run_root_is_created_on_demand(self, tmp_path: Path) -> None:
        root = tmp_path / "cache" / "batch"

        with run_lock(root) as path:
            assert path.parent == root

    def test_two_repositories_hold_their_own_run_at_the_same_time(
        self, tmp_path: Path
    ) -> None:
        acme = scoped_run_root(tmp_path, "acme/widgets")
        other = scoped_run_root(tmp_path, "other/widgets")

        with run_lock(acme), run_lock(other):
            pass

    def test_a_second_run_in_the_same_repository_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        root = scoped_run_root(tmp_path, "acme/widgets")

        with (
            run_lock(root),
            pytest.raises(BatchInProgressError),
            run_lock(scoped_run_root(tmp_path, "acme/widgets")),
        ):
            pass
