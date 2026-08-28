"""Tests for ghgql.repo.

These drive real `git` against a throwaway repo rather than mocking
subprocess: the contract being checked is "what git actually says".
"""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ghgql.repo import _parse_github_url, repo_root


def _git_init(path: Path) -> None:
    _ = subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)


class TestRepoRoot:
    def test_returns_the_repository_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _git_init(tmp_path)
        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        # Found from a subdirectory: a user runs orbit from anywhere in
        # the tree, and .orbit.toml lives at the root.
        assert repo_root().resolve() == tmp_path.resolve()

    def test_outside_a_repository_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="Failed to determine repository root"):
            _ = repo_root()


class TestParseGithubUrl:
    def test_https_url(self) -> None:
        assert _parse_github_url("https://github.com/example-org/example-repo.git") == (
            "example-org",
            "example-repo",
        )

    def test_https_url_without_suffix(self) -> None:
        assert _parse_github_url("https://github.com/example-org/example-repo") == (
            "example-org",
            "example-repo",
        )

    def test_ssh_url(self) -> None:
        assert _parse_github_url("git@github.com:example-org/example-repo.git") == (
            "example-org",
            "example-repo",
        )

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(RuntimeError, match="Cannot parse"):
            _parse_github_url("/local/path/to/repo")
