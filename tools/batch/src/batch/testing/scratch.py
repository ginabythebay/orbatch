from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

SEED_CONTENT = b"prebaked-image\n"
TRACKED_FILE = "README.md"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class Scratch:
    root: Path
    repo: Path
    seed: Path

    @property
    def trees(self) -> Path:
        return self.repo.parent / "worktrees"

    def commit(self, message: str) -> str:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return self.tip("HEAD")

    def tip(self, ref: str) -> str:
        return git(self.repo, "rev-parse", ref)

    def branch_at(self, name: str, start: str) -> None:
        git(self.repo, "branch", name, start)

    def checkout(self, ref: str) -> None:
        _ = git(self.repo, "checkout", "-q", ref)

    def orphan(self, name: str, message: str) -> str:
        _ = git(self.repo, "checkout", "-q", "--orphan", name)
        return self.commit(message)


def scratch(root: Path) -> Scratch:
    repo = root / "widgets"
    _ = git(root, "init", "-q", "-b", "main", "widgets")
    _ = git(repo, "config", "user.email", "scratch@example.com")
    _ = git(repo, "config", "user.name", "Scratch")
    _ = git(repo, "config", "commit.gpgsign", "false")
    seed = root / "seed.raw"
    _ = seed.write_bytes(SEED_CONTENT)
    made = Scratch(root=root, repo=repo, seed=seed)
    made.trees.mkdir()
    _ = (repo / TRACKED_FILE).write_text("hello\n")
    _ = git(repo, "add", TRACKED_FILE)
    _ = made.commit("initial")
    origin = root / "origin.git"
    _ = git(root, "init", "-q", "--bare", str(origin))
    _ = git(repo, "remote", "add", "origin", str(origin))
    _ = git(repo, "push", "-q", "-u", "origin", "main")
    return made
