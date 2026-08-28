from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple


class Repo(NamedTuple):
    owner: str
    name: str


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to determine repository root: {result.stderr.strip()}"
        )
    return Path(result.stdout.strip())


def repo() -> Repo:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to determine repository: {result.stderr.strip()}")
    return _parse_github_url(result.stdout.strip())


def _parse_github_url(url: str) -> Repo:
    url = url.removesuffix(".git")
    if url.startswith("https://"):
        parts = url.split("/")
        return Repo(parts[-2], parts[-1])
    if ":" in url:
        path = url.split(":", 1)[1]
        if "/" not in path:
            raise RuntimeError(f"Cannot parse GitHub remote URL: {url}")
        owner, name = path.split("/", 1)
        return Repo(owner, name)
    raise RuntimeError(f"Cannot parse GitHub remote URL: {url}")
