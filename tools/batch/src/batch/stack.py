from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from batch.models import (
    Alignment,
    RemoveResult,
    Slot,
    StaleSlotError,
    TeardownSkip,
    UnsafeRemovalError,
)

_CLONE_FLAG = "-c" if sys.platform == "darwin" else "--reflink=auto"


def main_repo(repo: Path | None = None) -> Path:
    """The checkout holding the shared gitdir, even when called from a worktree.

    Resolved from `repo` when given, because the cwd is not always inside a
    checkout: vibe mounts its launch directory into the guest, so the VM
    commands run from the mount root — the directory holding every repo — which
    is inside none.
    """
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo,
    ).stdout.strip()
    return Path(common).parent


class StackManager:
    def __init__(
        self,
        repo: Path,
        *,
        worktree_root: Path | None = None,
        seed_image: Path,
    ) -> None:
        self._repo: Path = repo
        self._worktree_root: Path = worktree_root or repo.parent / "worktrees"
        self._seed_image: Path = seed_image.expanduser()
        self._worktree_root.mkdir(parents=True, exist_ok=True)

    @property
    def worktree_root(self) -> Path:
        return self._worktree_root

    @property
    def mount_root(self) -> Path:
        """The directory vibe mounts into the guest: the parent of every repo's
        tree, so a guest sees its siblings.

        Derived from the checkout rather than from `worktree_root`, which a
        caller may point anywhere; the guest's `cd <repo>/worktrees/<branch>`
        only resolves while the two agree.
        """
        return self._repo.parent.parent

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self._repo), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def _git(self, *args: str) -> str:
        return self._run(*args).stdout.strip()

    def _branch_exists(self, branch: str) -> bool:
        return (
            self._run(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
            ).returncode
            == 0
        )

    def _worktrees(self) -> set[Path]:
        listing = self._git("worktree", "list", "--porcelain")
        return {
            Path(line.removeprefix("worktree "))
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }

    def slot_names(self) -> tuple[str, ...]:
        """Every branch name with a worktree or a disk under the root, excluding
        the main repo's own worktree and the branch it has checked out.

        Whether anyone is standing in a slot is a separate question, asked of
        `lsof` rather than of git — see `batch.occupancy`.
        """
        root = self._worktree_root.resolve()
        names = {
            worktree.name
            for worktree in self._worktrees()
            if worktree.resolve().parent == root
            and worktree.resolve() != self._repo.resolve()
        }
        names |= {disk.name.removesuffix(".raw") for disk in root.glob("*.raw")}
        return tuple(sorted(names - {self.current_branch()}))

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD")

    def merged_into(self, branch: str, base: str) -> bool:
        """True for a branch that does not exist: no commit is left to lose."""
        if not self._branch_exists(branch):
            return True
        return (
            self._run(
                "merge-base", "--is-ancestor", branch, base, check=False
            ).returncode
            == 0
        )

    def _paths(self, branch: str) -> tuple[Path, Path]:
        return self._worktree_root / branch, self._worktree_root / f"{branch}.raw"

    def missing(self, branch: str) -> tuple[str, ...]:
        """Human-readable descriptions of the absent pieces, for interpolation
        into a refusal; empty when the slot is whole.
        """
        worktree, disk = self._paths(branch)
        absent: list[str] = []
        if worktree.resolve() not in self._worktrees():
            absent.append(f"no worktree at {worktree}")
        if not disk.exists():
            absent.append(f"no disk at {disk}")
        return tuple(absent)

    def find(self, branch: str, base: str) -> Slot | None:
        """Read-only lookup: unlike `ensure`, never adds a worktree or seeds a disk."""
        if self.missing(branch):
            return None
        worktree, disk = self._paths(branch)
        return Slot(
            branch=branch,
            worktree=worktree,
            disk=disk,
            alignment=self._alignment(branch, base),
        )

    def ensure(self, issue: int, base: str) -> Slot:
        return self.ensure_branch(f"issue-{issue}", base)

    def ensure_branch(self, branch: str, base: str) -> Slot:
        worktree = self._worktree_root / branch
        disk = self._worktree_root / f"{branch}.raw"
        if self._branch_exists(branch):
            if worktree.resolve() not in self._worktrees():
                _ = self._git(
                    "worktree", "add", "-q", "--relative-paths", str(worktree), branch
                )
        else:
            _ = self._git(
                "worktree",
                "add",
                "-q",
                "--relative-paths",
                "-b",
                branch,
                str(worktree),
                base,
            )
        if not disk.exists():
            self._seed(disk)
        return Slot(
            branch=branch,
            worktree=worktree,
            disk=disk,
            alignment=self._alignment(branch, base),
        )

    def ensure_current(self, branch: str, base: str) -> Slot:
        """Brings the slot to the base, or refuses; never rebases or discards."""
        if self._branch_exists(branch):
            self._refuse_if_stale(branch, base)
        slot = self.ensure_branch(branch, base)
        self._fast_forward(slot, base)
        alignment = self._alignment(branch, base)
        if alignment is not Alignment.ALIGNED:
            raise StaleSlotError(branch, f"is still behind {base} after a fast-forward")
        return slot.model_copy(update={"alignment": alignment})

    def _refuse_if_stale(self, branch: str, base: str) -> None:
        if self._run("merge-base", base, branch, check=False).returncode != 0:
            raise StaleSlotError(branch, f"shares no history with {base}")
        if (
            self._run(
                "merge-base", "--is-ancestor", branch, base, check=False
            ).returncode
            != 0
        ):
            raise StaleSlotError(
                branch, f"has commits {base} does not; nothing should commit there"
            )

    def _fast_forward(self, slot: Slot, base: str) -> None:
        merged = subprocess.run(
            ["git", "-C", str(slot.worktree), "merge", "--ff-only", base],
            check=False,
            capture_output=True,
            text=True,
        )
        if merged.returncode != 0:
            complaint = (merged.stderr or merged.stdout).strip()
            raise StaleSlotError(
                slot.branch, f"cannot fast-forward to {base}: {complaint}"
            )

    def _seed(self, disk: Path) -> None:
        """Stages the copy, so a disk that exists is always a complete one.

        `ensure` treats an existing disk as a live VM's state and never
        overwrites it, which would cement a half-written image.
        """
        staging = disk.with_name(f"{disk.name}.partial")
        try:
            cloned = subprocess.run(
                ["cp", _CLONE_FLAG, str(self._seed_image), str(staging)],
                check=False,
                capture_output=True,
                text=True,
            )
            if cloned.returncode != 0:
                _ = shutil.copy(self._seed_image, staging)
            staging.replace(disk)
        finally:
            staging.unlink(missing_ok=True)

    def remove(
        self, issue: int, *, force: bool = False, merged_base: str | None = None
    ) -> RemoveResult:
        return self.remove_branch(
            f"issue-{issue}", force=force, merged_base=merged_base
        )

    def remove_branch(
        self, branch: str, *, force: bool = False, merged_base: str | None = None
    ) -> RemoveResult:
        """`merged_base` is the base a caller has already established the branch
        merged into; it relaxes the unpushed refusal to patch identity, so a
        squash-landed branch is removable once its remote ref is gone."""
        worktree = self._worktree_root / branch
        disk = self._worktree_root / f"{branch}.raw"
        registered = worktree.resolve() in self._worktrees()
        present = registered and worktree.is_dir()
        if not force:
            self._refuse_if_unsafe(
                branch, worktree if present else None, merged_base=merged_base
            )
        if present:
            _ = self._git("worktree", "remove", "--force", str(worktree))
        elif registered:
            _ = self._git("worktree", "prune")
        had_branch = self._branch_exists(branch)
        if had_branch:
            _ = self._git("branch", "-D", branch)
        had_disk = disk.exists()
        if had_disk:
            disk.unlink()
        return RemoveResult(
            branch=branch,
            removed_worktree=registered,
            removed_branch=had_branch,
            removed_disk=had_disk,
        )

    def dirty(self, branch: str) -> bool:
        """False when the branch has no worktree: nothing local is at risk."""
        worktree = self._worktree_root / branch
        if worktree.resolve() not in self._worktrees() or not worktree.is_dir():
            return False
        return self._dirty_at(worktree)

    def _dirty_at(self, worktree: Path) -> bool:
        return bool(
            subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

    def checked_out(self, branch: str) -> str | None:
        """The branch the slot's worktree actually holds, which need not be the
        one the directory is named for; None when there is no worktree.

        A detached HEAD reads as "HEAD", which never equals a slot name and so
        counts as switched.
        """
        worktree = self._worktree_root / branch
        if worktree.resolve() not in self._worktrees() or not worktree.is_dir():
            return None
        return subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def unpushed(self, branch: str) -> bool:
        """False for a branch that does not exist: nothing local is at risk.

        Commits another local branch also holds came from whatever the branch
        was cut on top of, so deleting this branch cannot destroy them.
        """
        if not self._branch_exists(branch):
            return False
        return bool(
            self._git(
                "rev-list",
                branch,
                "--not",
                "--remotes",
                "--exclude",
                branch,
                "--branches",
            )
        )

    def patch_unique(self, branch: str, base: str) -> bool | None:
        """True when the branch carries a commit whose patch is not already in
        `base`, so a squash of it onto `base` reads as carrying nothing.

        False for a branch that does not exist, matching `unpushed`. None when
        the two cannot be compared at all — no such base, no shared history, a
        git that would not answer — which is no answer at all: a caller weighing
        safety must fall back to `unpushed`.
        """
        if not self._branch_exists(branch):
            return False
        fork = self._run("merge-base", base, branch, check=False)
        cherry = self._run("cherry", base, branch, check=False)
        if fork.returncode != 0 or cherry.returncode != 0:
            return None
        if not any(line.startswith("+") for line in cherry.stdout.splitlines()):
            return False
        landed = self._landed_whole(fork.stdout.strip(), branch, base)
        return None if landed is None else not landed

    def _landed_whole(self, fork_point: str, branch: str, base: str) -> bool | None:
        """Whether the branch's commits reached `base` collapsed into one, which
        per-commit patch identity cannot see: a squash merge of more than one
        commit matches none of them.

        The flags pin porcelain output to a form `git patch-id` can read: these
        tools run in whatever repo the user is in, and an external diff driver
        or a configured pretty format would otherwise answer for git.
        """
        whole = self._run(
            "diff",
            "--no-ext-diff",
            "--no-color",
            f"{fork_point}..{branch}",
            check=False,
        )
        history = self._run(
            "log",
            "--patch",
            "--no-ext-diff",
            "--no-color",
            "--pretty=medium",
            f"{fork_point}..{base}",
            check=False,
        )
        if whole.returncode != 0 or history.returncode != 0:
            return None
        ids = self._patch_ids(whole.stdout)
        return bool(ids) and ids[0] in self._patch_ids(history.stdout)

    def _patch_ids(self, patches: str) -> tuple[str, ...]:
        listed = subprocess.run(
            ["git", "-C", str(self._repo), "patch-id", "--stable"],
            input=patches,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return tuple(line.split()[0] for line in listed.splitlines() if line)

    def _refuse_if_unsafe(
        self, branch: str, worktree: Path | None, *, merged_base: str | None = None
    ) -> None:
        if worktree is not None and self._dirty_at(worktree):
            raise UnsafeRemovalError(
                branch, TeardownSkip.DIRTY_WORKTREE, "the worktree has local changes"
            )
        if self._retains_work(branch, merged_base):
            raise UnsafeRemovalError(
                branch, TeardownSkip.UNPUSHED_COMMITS, "the branch has unpushed commits"
            )

    def _retains_work(self, branch: str, merged_base: str | None) -> bool:
        """`merged_base` narrows the refusal, never widens it: work is at risk
        only when the branch is unpushed *and* carries a patch the base has not
        already taken."""
        if not self.unpushed(branch):
            return False
        if merged_base is None:
            return True
        return self.patch_unique(branch, merged_base) is not False

    def _alignment(self, branch: str, base: str) -> Alignment:
        if (
            self._run(
                "merge-base", "--is-ancestor", base, branch, check=False
            ).returncode
            == 0
        ):
            return Alignment.ALIGNED
        if self._run("merge-base", base, branch, check=False).returncode == 0:
            return Alignment.BEHIND
        return Alignment.UNRELATED
