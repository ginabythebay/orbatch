from __future__ import annotations

import subprocess
from pathlib import Path

from batch.runtime import Runtime
from batch.testing.payloads import write_config
from orbit.batching import load_batching


def _checkout(path: Path) -> Path:
    path.mkdir()
    _ = subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


class TestLoadBatching:
    def test_a_path_that_is_no_checkout_is_not_configured(self, tmp_path: Path) -> None:
        assert load_batching(tmp_path / "missing") is None
        assert load_batching(_checkout(tmp_path / "plain")) is None

    def test_a_checkout_without_batch_toml_is_not_configured(
        self, tmp_path: Path
    ) -> None:
        repo = _checkout(tmp_path / "repo")

        assert load_batching(repo) is None

    def test_a_checkout_with_batch_toml_yields_a_runtime(self, tmp_path: Path) -> None:
        repo = write_config(_checkout(tmp_path / "repo"))

        loaded = load_batching(repo)

        assert isinstance(loaded, Runtime)
        assert loaded.repo == repo.resolve()
