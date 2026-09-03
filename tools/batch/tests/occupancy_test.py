from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from batch.models import OccupancyError
from batch.occupancy import occupied_slots, parse_cwds

LSOF_OUTPUT = """\
p1
fcwd
n/
p94
fcwd
n/Users/gina
p431
fcwd
n/Users/gina/vpink/issue-9
p2087
fcwd
n/Users/gina/vpink/issue-9/tools/batch
p2091
fcwd
n/private/var/folders/9x/t2c_0000gn/T
"""


def slots(root: Path, *found: Path) -> frozenset[str]:
    return occupied_slots(root, cwds=lambda: found)


class TestParseCwds:
    def test_every_process_record_yields_its_own_cwd(self) -> None:
        assert list(parse_cwds(LSOF_OUTPUT)) == [
            Path("/"),
            Path("/Users/gina"),
            Path("/Users/gina/vpink/issue-9"),
            Path("/Users/gina/vpink/issue-9/tools/batch"),
            Path("/private/var/folders/9x/t2c_0000gn/T"),
        ]

    def test_a_cwd_containing_spaces_survives_intact(self) -> None:
        text = "p431\nfcwd\nn/Users/gina/Library/Application Support/some app\n"

        assert list(parse_cwds(text)) == [
            Path("/Users/gina/Library/Application Support/some app")
        ]

    def test_a_record_with_no_name_line_yields_nothing_and_eats_no_other(
        self,
    ) -> None:
        text = "p431\nfcwd\np2087\nfcwd\nn/Users/gina/vpink/issue-9\n"

        assert list(parse_cwds(text)) == [Path("/Users/gina/vpink/issue-9")]

    def test_a_name_line_outside_any_record_is_ignored(self) -> None:
        text = "n/Users/gina/stray\np431\nfcwd\nn/Users/gina/vpink/issue-9\n"

        assert list(parse_cwds(text)) == [Path("/Users/gina/vpink/issue-9")]

    def test_a_record_yields_its_first_name_line_only(self) -> None:
        text = "p431\nfcwd\nn/Users/gina/vpink/issue-9\nn/Users/gina/other\n"

        assert list(parse_cwds(text)) == [Path("/Users/gina/vpink/issue-9")]


class TestProbeFailure:
    def test_a_missing_lsof_binary_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(OccupancyError, match="lsof"):
            _ = occupied_slots(tmp_path)

    def test_a_non_zero_exit_raises_carrying_the_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = tmp_path / "lsof"
        _ = fake.write_text("#!/bin/sh\nexit 7\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        with pytest.raises(OccupancyError, match="7"):
            _ = occupied_slots(tmp_path)


class TestOccupiedSlots:
    def test_a_process_standing_in_a_slot_marks_it_occupied(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "vpink"
        slot = root / "issue-9"
        slot.mkdir(parents=True)

        assert slots(root, slot) == {"issue-9"}

    def test_a_process_nested_inside_a_slot_marks_the_slot_not_the_subdirectory(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "vpink"
        deep = root / "issue-9" / "tools" / "batch"
        deep.mkdir(parents=True)

        assert slots(root, deep) == {"issue-9"}

    def test_a_process_outside_the_root_or_in_the_root_itself_marks_nothing(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "vpink"
        root.mkdir()
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()

        assert slots(root, elsewhere, root) == frozenset()

    def test_several_processes_mark_exactly_the_slots_they_stand_in(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "vpink"
        for name in ("issue-9", "issue-10", "idle"):
            (root / name).mkdir(parents=True)

        assert slots(root, root / "issue-9", root / "issue-9", root / "issue-10") == {
            "issue-9",
            "issue-10",
        }

    def test_the_caller_s_own_cwd_is_no_longer_a_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "vpink"
        slot = root / "issue-9"
        slot.mkdir(parents=True)
        monkeypatch.chdir(slot)
        empty: Iterable[Path] = ()

        assert occupied_slots(root, cwds=lambda: empty) == frozenset()
