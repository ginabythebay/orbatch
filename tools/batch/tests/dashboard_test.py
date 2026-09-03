from __future__ import annotations

from pathlib import Path

import pytest

from batch.dashboard import (
    TAIL_BYTES,
    Selection,
    format_elapsed,
    last_line,
    rows,
)
from batch.models import Batch, BatchLabel, DashboardRow, DroppedChild, VmStatus
from batch.testing.payloads import batch_issue

EPIC = 1492


class FakeFacts:
    def __init__(self, root: Path, live: tuple[int, ...] = ()) -> None:
        self._root: Path = root
        self._live: set[int] = set(live)

    def status(self, issue: int) -> VmStatus:
        return VmStatus.RUNNING if issue in self._live else VmStatus.EXITED

    def log(self, issue: int) -> Path:
        return self._root / f"issue-{issue}.log"


class FakeTimings:
    def __init__(self, elapsed: dict[int, float] | None = None) -> None:
        self._elapsed: dict[int, float] = dict(elapsed or {})

    def elapsed(self, issue: int) -> float | None:
        return self._elapsed.get(issue)


class TestRows:
    def test_one_row_per_batch_issue(self, tmp_path: Path) -> None:
        batch = Batch(
            targets=(EPIC,),
            issues=(
                batch_issue(10, BatchLabel.READY_FOR_REVIEW, title="Stack manager"),
                batch_issue(11, BatchLabel.IMPLEMENTING, title="Dashboard"),
            ),
        )
        _ = (tmp_path / "issue-11.log").write_text("running tests\n")

        built = rows(batch, FakeFacts(tmp_path, live=(11,)), FakeTimings({11: 65.0}))

        assert [row.number for row in built] == [10, 11]
        assert built[0].title == "Stack manager"
        assert built[0].state is BatchLabel.READY_FOR_REVIEW
        assert not built[0].live
        assert built[1].live
        assert built[1].elapsed == "1m05s"
        assert built[1].last_line == "running tests"

    def test_a_missing_log_leaves_the_line_empty(self, tmp_path: Path) -> None:
        batch = Batch(targets=(EPIC,), issues=(batch_issue(10),))

        built = rows(batch, FakeFacts(tmp_path), FakeTimings())

        assert built[0].last_line == ""
        assert built[0].elapsed == ""

    def test_only_shown_logs_are_read(self, tmp_path: Path) -> None:
        batch = Batch(
            targets=(EPIC,),
            issues=(batch_issue(10), batch_issue(11), batch_issue(12)),
        )
        for number in (10, 11, 12):
            _ = (tmp_path / f"issue-{number}.log").write_text(f"line {number}\n")

        built = rows(batch, FakeFacts(tmp_path, live=(11,)), FakeTimings(), selected=12)

        assert built[0].last_line == ""
        assert built[1].last_line == "line 11"
        assert built[2].last_line == "line 12"

    def test_a_tab_is_stripped_from_a_log_line(self, tmp_path: Path) -> None:
        _ = (tmp_path / "issue-10.log").write_text("PASS\tapps/figaro\n")
        batch = Batch(targets=(EPIC,), issues=(batch_issue(10),))

        built = rows(batch, FakeFacts(tmp_path, live=(10,)), FakeTimings())

        assert built[0].last_line == "PASS apps/figaro"

    def test_dropped_children_are_not_rows(self, tmp_path: Path) -> None:
        batch = Batch(
            targets=(EPIC,),
            issues=(batch_issue(10),),
            dropped=(
                DroppedChild(
                    number=99, title="Old", state="CLOSED", labels=(), reason="closed"
                ),
            ),
        )

        built = rows(batch, FakeFacts(tmp_path), FakeTimings())

        assert [row.number for row in built] == [10]


class TestLastLine:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "issue-10.log"
        _ = path.write_text(text)
        return path

    def test_a_missing_file_reads_as_nothing(self, tmp_path: Path) -> None:
        assert last_line(tmp_path / "absent.log") == ""

    def test_an_empty_file_reads_as_nothing(self, tmp_path: Path) -> None:
        assert last_line(self._write(tmp_path, "")) == ""

    def test_a_file_of_only_whitespace_reads_as_nothing(self, tmp_path: Path) -> None:
        assert last_line(self._write(tmp_path, "\n   \n\n")) == ""

    def test_the_last_line_wins(self, tmp_path: Path) -> None:
        assert last_line(self._write(tmp_path, "first\nsecond\n")) == "second"

    def test_an_unterminated_last_line_still_counts(self, tmp_path: Path) -> None:
        assert last_line(self._write(tmp_path, "first\npartial")) == "partial"

    def test_a_carriage_return_repaint_yields_the_final_frame(
        self, tmp_path: Path
    ) -> None:
        redrawn = "Thinking.\rThinking..\rThinking...\r\n"

        assert last_line(self._write(tmp_path, redrawn)) == "Thinking..."

    def test_ansi_colour_and_cursor_escapes_are_stripped(self, tmp_path: Path) -> None:
        painted = "\x1b[?25l\x1b[2K\x1b[32m✓ tests passed\x1b[0m\n"

        assert last_line(self._write(tmp_path, painted)) == "✓ tests passed"

    def test_an_operating_system_command_is_stripped(self, tmp_path: Path) -> None:
        titled = "\x1b]0;claude\x07building\n"

        assert last_line(self._write(tmp_path, titled)) == "building"

    def test_a_long_line_is_truncated(self, tmp_path: Path) -> None:
        line = last_line(self._write(tmp_path, "x" * 400 + "\n"), limit=20)

        assert line == "x" * 19 + "…"

    def test_a_log_far_bigger_than_the_tail_window_yields_its_final_line(
        self, tmp_path: Path
    ) -> None:
        path = self._write(tmp_path, "noise\n" * 100_000 + "the end\n")

        assert path.stat().st_size > TAIL_BYTES
        assert last_line(path) == "the end"


def _rows(*numbers: int) -> tuple[DashboardRow, ...]:
    return tuple(
        DashboardRow(number=number, title=f"Issue {number}", state=BatchLabel.PLANNED)
        for number in numbers
    )


class TestSelection:
    def test_it_starts_on_the_first_row(self) -> None:
        selection = Selection()

        selection.sync(_rows(10, 11, 12))

        assert selection.number == 10

    def test_moving_walks_the_rows(self) -> None:
        selection = Selection()
        rows = _rows(10, 11, 12)
        selection.sync(rows)

        selection.move(rows, 1)
        selection.move(rows, 1)

        assert selection.number == 12

    def test_it_clamps_at_both_ends(self) -> None:
        selection = Selection()
        rows = _rows(10, 11)
        selection.sync(rows)

        selection.move(rows, -1)
        first = selection.number
        selection.move(rows, 5)

        assert first == 10
        assert selection.number == 11

    def test_an_insert_above_the_cursor_keeps_the_same_issue_selected(self) -> None:
        selection = Selection()
        selection.sync(_rows(10, 12))
        selection.move(_rows(10, 12), 1)

        selection.sync(_rows(10, 11, 12))

        assert selection.number == 12

    def test_losing_the_selected_row_falls_back_to_its_position(self) -> None:
        selection = Selection()
        rows = _rows(10, 11, 12)
        selection.sync(rows)
        selection.move(rows, 1)

        selection.sync(_rows(10, 12))

        assert selection.number == 12

    def test_losing_the_last_row_falls_back_to_the_new_last(self) -> None:
        selection = Selection()
        rows = _rows(10, 11, 12)
        selection.sync(rows)
        selection.move(rows, 2)

        selection.sync(_rows(10, 11))

        assert selection.number == 11

    def test_an_empty_batch_selects_nothing(self) -> None:
        selection = Selection()
        selection.sync(_rows(10))

        selection.sync(())

        assert selection.number is None

    def test_rows_returning_reselects_the_remembered_issue(self) -> None:
        selection = Selection()
        rows = _rows(10, 11, 12)
        selection.sync(rows)
        selection.move(rows, 2)
        selection.sync(())

        selection.sync(rows)

        assert selection.number == 12


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, ""),
        (0.0, "0s"),
        (9.6, "9s"),
        (59.0, "59s"),
        (60.0, "1m00s"),
        (3599.0, "59m59s"),
        (3600.0, "1h00m"),
        (7500.0, "2h05m"),
        (86_400.0, "24h00m"),
    ],
)
def test_elapsed_reads_as_a_duration(seconds: float | None, expected: str) -> None:
    assert format_elapsed(seconds) == expected
