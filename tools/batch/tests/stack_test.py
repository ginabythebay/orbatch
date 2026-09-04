from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from batch import stack
from batch.models import (
    Alignment,
    Slot,
    StaleSlotError,
    TeardownSkip,
    UnsafeRemovalError,
)
from batch.stack import StackManager, main_repo, worktree_root
from batch.testing.scratch import SEED_CONTENT, TRACKED_FILE, Scratch, git, scratch


@pytest.fixture(name="sc")
def scratch_repo(tmp_path: Path) -> Scratch:
    return scratch(tmp_path)


class TestPlanningSlot:
    def test_a_named_branch_gets_its_own_worktree_and_disk(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure_branch(
            "plan-1492", "main"
        )

        assert slot.branch == "plan-1492"
        assert slot.worktree == sc.trees / "plan-1492"
        assert slot.disk == sc.trees / "plan-1492.raw"
        assert slot.worktree.is_dir()
        assert git(sc.repo, "rev-parse", "plan-1492") == sc.tip("main")

    def test_a_second_call_adopts_what_the_first_created(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure_branch("plan-1492", "main")
        _ = first.disk.write_bytes(b"planning-vm-state\n")

        second = manager.ensure_branch("plan-1492", "main")

        assert second == first
        assert second.disk.read_bytes() == b"planning-vm-state\n"

    def test_the_slot_can_be_removed_by_name(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_branch("plan-1492", "main")

        result = manager.remove_branch("plan-1492", force=True)

        assert result.branch == "plan-1492"
        assert (
            result.removed_worktree,
            result.removed_branch,
            result.removed_disk,
        ) == (
            True,
            True,
            True,
        )
        assert not slot.worktree.exists()
        assert not slot.disk.exists()


class TestEnsure:
    def test_creates_branch_worktree_and_disk(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        assert slot.branch == "issue-9"
        assert slot.worktree == sc.trees / "issue-9"
        assert slot.disk == sc.trees / "issue-9.raw"
        assert slot.worktree.is_dir()
        assert slot.disk.is_file()
        assert git(sc.repo, "rev-parse", "issue-9") == sc.tip("main")

    def test_worktree_points_at_the_gitdir_relatively(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        pointer = (slot.worktree / ".git").read_text().strip()
        target = pointer.removeprefix("gitdir: ")
        assert target != pointer
        assert not Path(target).is_absolute()
        assert (slot.worktree / target).resolve() == (
            sc.repo / ".git" / "worktrees" / "issue-9"
        )

    def test_disk_is_seeded_from_the_image(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        assert slot.disk.read_bytes() == SEED_CONTENT

    def test_disk_is_seeded_when_cloning_is_unsupported(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(stack, "_CLONE_FLAG", "--no-such-flag")

        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        assert slot.disk.read_bytes() == SEED_CONTENT

    def test_a_failed_seed_leaves_no_disk_behind(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.root / "absent.raw")

        with pytest.raises(FileNotFoundError):
            _ = manager.ensure(9, "main")

        assert not (sc.root / "issue-9.raw").exists()
        assert list(sc.root.glob("*.partial")) == []

    def test_worktree_root_may_sit_outside_the_repos_parent(self, sc: Scratch) -> None:
        trees = sc.root / "trees"
        trees.mkdir()

        slot = StackManager(sc.repo, worktrees=trees, seed_image=sc.seed).ensure(
            9, "main"
        )

        assert slot.worktree == trees / "issue-9"
        assert slot.disk == trees / "issue-9.raw"
        assert slot.worktree.is_dir()
        assert not Path(
            (slot.worktree / ".git").read_text().strip().removeprefix("gitdir: ")
        ).is_absolute()

    def test_the_default_root_is_the_worktrees_dir_beside_the_checkout(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.worktree_root == sc.repo.parent / "worktrees"

    def test_the_root_is_created_when_it_is_absent(self, sc: Scratch) -> None:
        sc.trees.rmdir()

        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.worktree_root.is_dir()

    def test_the_mount_root_is_the_grandparent_of_the_checkout(
        self, sc: Scratch
    ) -> None:
        elsewhere = sc.root / "trees"

        manager = StackManager(sc.repo, worktrees=elsewhere, seed_image=sc.seed)

        assert manager.mount_root == sc.repo.parent.parent
        assert manager.worktree_root == elsewhere

    def test_base_may_be_a_predecessor_branch(self, sc: Scratch) -> None:
        sc.branch_at("issue-8", "HEAD")
        _ = sc.commit("main moves on")

        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "issue-8")

        assert sc.tip(slot.branch) == sc.tip("issue-8")
        assert sc.tip(slot.branch) != sc.tip("main")


class TestMainRepo:
    def test_resolves_the_main_checkout_from_inside_a_worktree(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")

        monkeypatch.chdir(sc.repo)
        assert main_repo(None) == sc.repo.resolve()
        monkeypatch.chdir(slot.worktree)
        assert main_repo(None) == sc.repo.resolve()

    def test_the_worktree_parent_is_inside_no_checkout(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(sc.root)

        with pytest.raises(subprocess.CalledProcessError):
            _ = main_repo()

    def test_a_passed_repo_resolves_from_outside_a_checkout(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure(9, "main")
        monkeypatch.chdir(sc.root)

        assert main_repo(sc.repo) == sc.repo.resolve()
        assert main_repo(slot.worktree) == sc.repo.resolve()


class TestWorktreeRoot:
    def test_the_manager_defaults_to_the_shared_helper(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.worktree_root == worktree_root(sc.repo)
        assert manager.ensure(9, "main").disk.parent == worktree_root(sc.repo)


class TestIdempotence:
    def test_second_ensure_reuses_everything(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure(9, "main")
        _ = first.disk.write_bytes(b"live vm state")
        cut_at = sc.tip("issue-9")

        again = manager.ensure(9, "main")

        assert again == first
        assert sc.tip("issue-9") == cut_at
        assert again.disk.read_bytes() == b"live vm state"

    def test_missing_worktree_is_readded_to_the_existing_branch(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure(9, "main")
        cut_at = sc.tip("issue-9")
        _ = git(sc.repo, "worktree", "remove", str(first.worktree))
        _ = sc.commit("main moves on")

        again = manager.ensure(9, "main")

        assert again.worktree.is_dir()
        assert sc.tip("issue-9") == cut_at

    def test_missing_disk_is_reseeded_without_touching_the_worktree(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure(9, "main")
        _ = (first.worktree / "scratch.txt").write_text("agent work")
        first.disk.unlink()

        again = manager.ensure(9, "main")

        assert again.disk.read_bytes() == SEED_CONTENT
        assert (again.worktree / "scratch.txt").read_text() == "agent work"


class TestAlignment:
    def test_a_branch_with_its_own_commits_is_still_aligned(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        assert slot.alignment is Alignment.ALIGNED
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")

        again = manager.ensure(9, "main")

        assert again.alignment is Alignment.ALIGNED
        assert sc.tip("issue-9") != sc.tip("main")

    def test_own_commits_plus_a_moved_predecessor_is_behind(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        sc.branch_at("issue-8", "HEAD")
        sc.checkout("issue-8")
        slot = manager.ensure(9, "issue-8")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")
        _ = sc.commit("rework lands on the predecessor")

        again = manager.ensure(9, "issue-8")

        assert again.alignment is Alignment.BEHIND

    def test_a_predecessor_that_moved_on_leaves_the_branch_behind(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        sc.branch_at("issue-8", "HEAD")
        sc.checkout("issue-8")
        _ = manager.ensure(9, "issue-8")
        cut_at = sc.tip("issue-9")
        _ = sc.commit("rework lands on the predecessor")

        again = manager.ensure(9, "issue-8")

        assert again.alignment is Alignment.BEHIND
        assert sc.tip("issue-9") == cut_at

    def test_a_base_sharing_no_history_is_unrelated(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(9, "main")
        cut_at = sc.tip("issue-9")
        sc.orphan("elsewhere", "unrelated root")

        again = manager.ensure(9, "elsewhere")

        assert again.alignment is Alignment.UNRELATED
        assert sc.tip("issue-9") == cut_at


class TestRemove:
    def test_a_clean_pushed_issue_is_fully_removed(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")

        result = manager.remove(9)

        assert not slot.worktree.exists()
        assert not slot.disk.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")
        assert result.removed_worktree
        assert result.removed_branch
        assert result.removed_disk

    def test_an_untracked_file_refuses_the_removal(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        with pytest.raises(UnsafeRemovalError):
            _ = manager.remove(9)

        assert slot.worktree.is_dir()
        assert slot.disk.exists()
        assert sc.tip("issue-9")

    def test_an_uncommitted_change_refuses_the_removal(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / TRACKED_FILE).write_text("edited\n")

        with pytest.raises(UnsafeRemovalError):
            _ = manager.remove(9)

        assert slot.worktree.is_dir()
        assert slot.disk.exists()
        assert sc.tip("issue-9")

    def test_force_removes_a_dirty_unpushed_issue(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        result = manager.remove(9, force=True)

        assert not slot.worktree.exists()
        assert not slot.disk.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")
        assert result.removed_branch

    def test_a_branch_with_its_own_commits_reports_unpushed(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")

        assert manager.unpushed("issue-9") is False

        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")

        assert manager.unpushed("issue-9") is True

    def test_a_commit_only_this_branch_holds_is_flagged(self, sc: Scratch) -> None:
        """Another branch's own unpushed work does not excuse this branch's."""
        manager = StackManager(sc.repo, seed_image=sc.seed)
        sibling = manager.ensure_branch("sibling", "main")
        _ = git(sibling.worktree, "commit", "-q", "--allow-empty", "-m", "elsewhere")
        slot = manager.ensure(9, "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")

        assert manager.unpushed("issue-9") is True

    def test_a_commit_a_sibling_local_branch_holds_is_not_flagged(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        sc.branch_at("sibling", "main")
        sc.checkout("sibling")
        _ = sc.commit("local work")

        _ = manager.ensure_branch("fix-thing", "sibling")

        assert manager.unpushed("fix-thing") is False

    def test_a_branch_that_does_not_exist_has_nothing_unpushed(
        self, sc: Scratch
    ) -> None:
        assert StackManager(sc.repo, seed_image=sc.seed).unpushed("plan-9") is False

    def test_unpushed_commits_refuse_the_removal(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "agent work")

        with pytest.raises(UnsafeRemovalError):
            _ = manager.remove(9)

        assert slot.worktree.is_dir()
        assert slot.disk.exists()
        assert sc.tip("issue-9")


FEATURE = "feature.txt"
FEATURE_TEXT = "the work\n"


def squash_landed(sc: Scratch, manager: StackManager) -> Slot:
    slot = manager.ensure(9, "main")
    _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
    sc.push("issue-9", slot.worktree)
    _ = sc.land({FEATURE: FEATURE_TEXT}, "agent work (#9)")
    _ = sc.land({"other.txt": "elsewhere\n"}, "unrelated")
    sc.unpublish("issue-9")
    return slot


class TestRemoveAfterMerge:
    def test_a_squash_landed_branch_with_no_remote_ref_is_removed(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = squash_landed(sc, manager)

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert not slot.disk.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")
        assert result.removed_worktree
        assert result.removed_branch
        assert result.removed_disk

    def test_a_multi_commit_squash_survives_an_external_diff_driver(
        self, sc: Scratch
    ) -> None:
        _ = git(sc.repo, "config", "diff.external", "false")
        _ = git(sc.repo, "config", "format.pretty", "oneline")
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        _ = sc.commit_file(slot.worktree, "notes.md", "why\n", "docs: notes")
        sc.push("issue-9", slot.worktree)
        _ = sc.land({FEATURE: FEATURE_TEXT, "notes.md": "why\n"}, "agent work (#9)")
        sc.unpublish("issue-9")

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert result.removed_branch

    def test_a_multi_commit_branch_landed_as_one_squash_is_removed(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        _ = sc.commit_file(slot.worktree, "notes.md", "why\n", "docs: notes")
        sc.push("issue-9", slot.worktree)
        _ = sc.land({FEATURE: FEATURE_TEXT, "notes.md": "why\n"}, "agent work (#9)")
        _ = sc.land({"other.txt": "elsewhere\n"}, "unrelated")
        sc.unpublish("issue-9")

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert result.removed_branch

    def test_a_branch_pushed_but_absent_from_a_stale_base_is_removed(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        sc.push("issue-9", slot.worktree)
        _ = sc.merge("issue-9")

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert result.removed_branch

    def test_a_branch_merged_into_a_predecessor_is_removed(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        earlier = manager.ensure(8, "main")
        _ = sc.commit_file(earlier.worktree, "earlier.txt", "before\n", "issue 8")
        sc.push("issue-8", earlier.worktree)
        slot = manager.ensure(9, "issue-8")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        sc.push("issue-9", slot.worktree)
        _ = git(earlier.worktree, "merge", "-q", "--no-ff", "-m", "merge", "issue-9")
        sc.push("issue-8", earlier.worktree)
        sc.unpublish("issue-9")

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert result.removed_branch

    def test_the_same_branch_is_refused_without_a_merged_base(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = squash_landed(sc, manager)

        with pytest.raises(UnsafeRemovalError) as caught:
            _ = manager.remove(9)

        assert caught.value.skip is TeardownSkip.UNPUSHED_COMMITS
        assert slot.worktree.is_dir()
        assert sc.tip("issue-9")

    def test_a_commit_that_never_landed_is_still_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = squash_landed(sc, manager)
        _ = sc.commit_file(slot.worktree, "extra.txt", "unsent\n", "rework")

        with pytest.raises(UnsafeRemovalError) as caught:
            _ = manager.remove(9, merged_base="origin/main")

        assert caught.value.skip is TeardownSkip.UNPUSHED_COMMITS
        assert slot.worktree.is_dir()
        assert slot.disk.exists()
        assert sc.tip("issue-9")

    def test_a_dirty_worktree_is_still_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = squash_landed(sc, manager)
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        with pytest.raises(UnsafeRemovalError) as caught:
            _ = manager.remove(9, merged_base="origin/main")

        assert caught.value.skip is TeardownSkip.DIRTY_WORKTREE
        assert slot.worktree.is_dir()
        assert slot.disk.exists()

    def test_a_merge_commit_landing_is_removed(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        sc.push("issue-9", slot.worktree)
        _ = sc.merge("issue-9")
        sc.push("main")
        sc.unpublish("issue-9")

        result = manager.remove(9, merged_base="origin/main")

        assert not slot.worktree.exists()
        assert result.removed_branch

    def test_a_base_that_does_not_resolve_keeps_the_strict_refusal(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        sc.forget_origin()

        with pytest.raises(UnsafeRemovalError) as caught:
            _ = manager.remove(9, merged_base="origin/main")

        assert caught.value.skip is TeardownSkip.UNPUSHED_COMMITS
        assert slot.worktree.is_dir()
        assert sc.tip("issue-9")


class TestPatchUnique:
    def test_a_squash_landed_commit_is_not_unique(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = squash_landed(sc, manager)

        assert manager.patch_unique("issue-9", "origin/main") is False

    def test_a_local_only_commit_is_unique(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = squash_landed(sc, manager)
        _ = sc.commit_file(slot.worktree, "extra.txt", "unsent\n", "rework")

        assert manager.patch_unique("issue-9", "origin/main") is True

    def test_a_base_that_does_not_resolve_answers_nothing(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = sc.commit_file(slot.worktree, FEATURE, FEATURE_TEXT, "agent work")
        sc.forget_origin()

        assert manager.patch_unique("issue-9", "origin/main") is None

    def test_a_branch_that_does_not_exist_carries_nothing(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.patch_unique("plan-9", "origin/main") is False


def _registered_worktrees(sc: Scratch) -> set[Path]:
    listing = git(sc.repo, "worktree", "list", "--porcelain")
    return {
        Path(line.removeprefix("worktree "))
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }


class TestPartialRemoval:
    def test_an_already_removed_worktree_leaves_branch_and_disk_removable(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = git(sc.repo, "worktree", "remove", str(slot.worktree))

        result = manager.remove(9)

        assert not result.removed_worktree
        assert result.removed_branch
        assert result.removed_disk
        assert not slot.disk.exists()

    def test_a_deleted_worktree_directory_is_pruned(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        shutil.rmtree(slot.worktree)

        result = manager.remove(9)

        assert result.removed_worktree
        assert result.removed_branch
        assert slot.worktree.resolve() not in _registered_worktrees(sc)

    def test_removing_nothing_is_a_noop(self, sc: Scratch) -> None:
        result = StackManager(sc.repo, seed_image=sc.seed).remove(9)

        assert not result.removed_worktree
        assert not result.removed_branch
        assert not result.removed_disk


class TestDirty:
    def test_a_clean_worktree_is_not_dirty(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(9, "main")

        assert not manager.dirty("issue-9")

    def test_a_modified_tracked_file_is_dirty(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / TRACKED_FILE).write_text("edited\n")

        assert manager.dirty("issue-9")

    def test_an_untracked_file_is_dirty(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = (slot.worktree / "notes.txt").write_text("unsaved")

        assert manager.dirty("issue-9")

    def test_a_branch_with_no_worktree_is_not_dirty(self, sc: Scratch) -> None:
        assert not StackManager(sc.repo, seed_image=sc.seed).dirty("issue-9")


class TestForcedRemovalAfterMerge:
    def test_a_branch_on_no_remote_is_removed_when_forced(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "merged work")

        with pytest.raises(UnsafeRemovalError):
            _ = manager.remove(9)

        result = manager.remove(9, force=True)

        assert (
            result.removed_worktree,
            result.removed_branch,
            result.removed_disk,
        ) == (
            True,
            True,
            True,
        )
        assert not slot.worktree.exists()
        assert "issue-9" not in git(sc.repo, "branch", "--format=%(refname:short)")


class TestEnsureCurrent:
    def test_a_slot_behind_the_base_is_fast_forwarded(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure_branch("plan-1492", "main")
        _ = (sc.repo / TRACKED_FILE).write_text("moved on\n")
        _ = git(sc.repo, "commit", "-q", "-am", "main moves on")

        slot = manager.ensure_current("plan-1492", "main")

        assert sc.tip("plan-1492") == sc.tip("main")
        assert slot.alignment is Alignment.ALIGNED
        assert (slot.worktree / TRACKED_FILE).read_text() == "moved on\n"

    def test_an_absent_branch_is_cut_fresh_at_the_base(self, sc: Scratch) -> None:
        slot = StackManager(sc.repo, seed_image=sc.seed).ensure_current(
            "plan-1492", "main"
        )

        assert sc.tip("plan-1492") == sc.tip("main")
        assert slot.worktree.is_dir()
        assert slot.disk.read_bytes() == SEED_CONTENT

    def test_a_slot_already_at_the_base_is_untouched(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure_current("plan-1492", "main")

        again = manager.ensure_current("plan-1492", "main")

        assert again == first
        assert sc.tip("plan-1492") == sc.tip("main")

    def test_a_fast_forward_preserves_the_vm_disk(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = slot.disk.write_bytes(b"planning-vm-state\n")
        _ = sc.commit("main moves on")

        again = manager.ensure_current("plan-1492", "main")

        assert sc.tip("plan-1492") == sc.tip("main")
        assert again.disk.read_bytes() == b"planning-vm-state\n"

    def test_a_slot_with_its_own_commit_is_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "stray")
        tip = sc.tip("plan-1492")

        with pytest.raises(StaleSlotError) as caught:
            _ = manager.ensure_current("plan-1492", "main")

        assert "plan-1492" in str(caught.value)
        assert "commits main does not" in str(caught.value)
        assert sc.tip("plan-1492") == tip

    def test_a_diverged_slot_is_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "stray")
        tip = sc.tip("plan-1492")
        _ = sc.commit("main moves on")

        with pytest.raises(StaleSlotError, match="plan-1492"):
            _ = manager.ensure_current("plan-1492", "main")

        assert sc.tip("plan-1492") == tip

    def test_an_unrelated_base_is_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure_current("plan-1492", "main")
        _ = sc.orphan("elsewhere", "unrelated root")

        with pytest.raises(StaleSlotError) as caught:
            _ = manager.ensure_current("plan-1492", "elsewhere")

        assert str(caught.value) == "plan-1492 shares no history with elsewhere"

    def test_a_refusal_leaves_the_worktree_and_disk_as_found(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = slot.disk.write_bytes(b"planning-vm-state\n")
        _ = git(slot.worktree, "commit", "-q", "--allow-empty", "-m", "stray")
        tip = sc.tip("plan-1492")

        with pytest.raises(StaleSlotError):
            _ = manager.ensure_current("plan-1492", "main")

        assert slot.worktree.is_dir()
        assert slot.disk.read_bytes() == b"planning-vm-state\n"
        assert sc.tip("plan-1492") == tip

    def test_an_uncommitted_change_in_the_way_is_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = (slot.worktree / TRACKED_FILE).write_text("local edit\n")
        _ = (sc.repo / TRACKED_FILE).write_text("moved on\n")
        _ = git(sc.repo, "commit", "-q", "-am", "main moves on")

        with pytest.raises(StaleSlotError, match="plan-1492"):
            _ = manager.ensure_current("plan-1492", "main")

        assert (slot.worktree / TRACKED_FILE).read_text() == "local edit\n"

    def test_a_deleted_worktree_is_readded_and_then_fast_forwarded(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        first = manager.ensure_current("plan-1492", "main")
        _ = git(sc.repo, "worktree", "remove", str(first.worktree))
        _ = (sc.repo / TRACKED_FILE).write_text("moved on\n")
        _ = git(sc.repo, "commit", "-q", "-am", "main moves on")

        again = manager.ensure_current("plan-1492", "main")

        assert again.worktree.is_dir()
        assert sc.tip("plan-1492") == sc.tip("main")
        assert (again.worktree / TRACKED_FILE).read_text() == "moved on\n"

    def test_a_worktree_off_the_branch_is_refused(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure_current("plan-1492", "main")
        _ = git(slot.worktree, "checkout", "-q", "--detach")
        tip = sc.tip("plan-1492")
        _ = sc.commit("main moves on")

        with pytest.raises(StaleSlotError, match="plan-1492"):
            _ = manager.ensure_current("plan-1492", "main")

        assert sc.tip("plan-1492") == tip


class TestMissing:
    def test_a_complete_slot_is_missing_nothing(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(1597, "main")

        assert manager.missing("issue-1597") == ()

    def test_a_pruned_worktree_is_named_and_the_disk_is_not(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(1597, "main")
        _ = git(sc.repo, "worktree", "remove", "--force", str(slot.worktree))

        assert manager.missing("issue-1597") == (f"no worktree at {slot.worktree}",)

    def test_a_deleted_disk_is_named_and_the_worktree_is_not(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(1597, "main")
        slot.disk.unlink()

        assert manager.missing("issue-1597") == (f"no disk at {slot.disk}",)

    def test_an_unknown_branch_names_both_pieces(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.missing("issue-4242") == (
            f"no worktree at {sc.trees / 'issue-4242'}",
            f"no disk at {sc.trees / 'issue-4242.raw'}",
        )


class TestFind:
    def test_finds_a_complete_slot(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        ensured = manager.ensure(1597, "main")

        found = manager.find("issue-1597", "main")

        assert found == ensured

    def test_returns_none_when_the_worktree_is_not_registered(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(1597, "main")
        _ = git(sc.repo, "worktree", "remove", "--force", str(slot.worktree))

        assert manager.find("issue-1597", "main") is None
        assert slot.disk.exists()

    def test_returns_none_when_the_disk_is_missing_and_seeds_nothing(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(1597, "main")
        slot.disk.unlink()

        assert manager.find("issue-1597", "main") is None
        assert not slot.disk.exists()

    def test_creates_nothing_for_an_unknown_branch(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)

        assert manager.find("issue-4242", "main") is None
        assert not (sc.root / "issue-4242").exists()
        assert not (sc.root / "issue-4242.raw").exists()
        assert "issue-4242" not in git(sc.repo, "branch", "--list")
        assert "issue-4242" not in git(sc.repo, "worktree", "list")

    def test_resolves_a_planning_branch(self, sc: Scratch) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        ensured = manager.ensure_branch("plan-1492", "main")

        found = manager.find("plan-1492", "main")

        assert found == ensured
        assert found is not None
        assert found.alignment is Alignment.ALIGNED


class TestSlotEnumeration:
    def test_the_slot_the_caller_stands_in_is_still_a_slot(
        self, sc: Scratch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        slot = manager.ensure(9, "main")
        monkeypatch.chdir(slot.worktree)

        assert "issue-9" in manager.slot_names()

    def test_the_main_repo_and_the_branch_it_has_checked_out_are_not_slots(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure(9, "main")
        _ = (sc.trees / "main.raw").write_bytes(b"not a slot\n")

        names = manager.slot_names()

        assert "main" not in names
        assert sc.repo.name not in names

    def test_a_worktree_and_a_disk_under_the_root_are_both_slots(
        self, sc: Scratch
    ) -> None:
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = manager.ensure_branch("openfix", "main")
        _ = (sc.trees / "orphan.raw").write_bytes(b"no worktree\n")

        assert set(manager.slot_names()) == {"openfix", "orphan"}

    def test_a_worktree_beside_the_checkout_is_not_a_slot(self, sc: Scratch) -> None:
        """The pre-migration flat layout, which must stop being discovered."""
        manager = StackManager(sc.repo, seed_image=sc.seed)
        _ = git(sc.repo, "worktree", "add", "-q", str(sc.root / "flat"))

        assert "flat" not in manager.slot_names()
