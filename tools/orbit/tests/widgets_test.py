from __future__ import annotations

import io

import pytest
from rich.text import Text

from ghgql.labels import BatchLabel
from orbit.github.models import Issue
from orbit.palette import Palette
from orbit.text_output import print_issue_table
from orbit.tui.widgets import MARK, filtered_text, issue_text

_BATCH_COLUMN = 1


def _glyph_style(text: Text) -> str:
    spans = [
        s for s in text.spans if (s.start, s.end) == (_BATCH_COLUMN, _BATCH_COLUMN + 1)
    ]
    return str(spans[0].style) if spans else ""


class TestIssueText:
    def test_the_batch_glyph_is_a_fixed_width_second_column(self) -> None:
        queued = issue_text(42, "OPEN", "A title", labels=("queued",))
        plain = issue_text(42, "OPEN", "A title")

        assert queued.plain[_BATCH_COLUMN] == "q"
        assert plain.plain[_BATCH_COLUMN] == " "
        assert len(queued.plain) == len(plain.plain)
        assert queued.plain.index("A title") == plain.plain.index("A title")

    @pytest.mark.parametrize(
        "name", ["queued", "planned", "implementing", "ready-for-review"]
    )
    def test_ordinary_glyphs_are_unstyled(self, name: str) -> None:
        assert _glyph_style(issue_text(42, "OPEN", "t", labels=(name,))) == ""

    @pytest.mark.parametrize("labels", [("stuck",), ("queued", "stuck")])
    def test_stuck_and_conflicting_glyphs_warn(self, labels: tuple[str, ...]) -> None:
        assert (
            _glyph_style(issue_text(42, "OPEN", "t", labels=labels)) == Palette.WARNING
        )

    def test_exactly_two_glyphs_warn(self) -> None:
        every = [(label.value,) for label in BatchLabel] + [("queued", "stuck"), ()]
        warned = [
            issue_text(42, "OPEN", "t", labels=labels).plain[_BATCH_COLUMN]
            for labels in every
            if _glyph_style(issue_text(42, "OPEN", "t", labels=labels))
            == Palette.WARNING
        ]
        assert sorted(warned) == ["!", "s"]


class TestSurfacesAgree:
    @pytest.mark.parametrize(
        "labels", [(), ("queued",), ("stuck",), ("queued", "stuck"), ("epic",)]
    )
    def test_the_cli_table_and_the_tui_line_show_the_same_glyph(
        self, labels: tuple[str, ...]
    ) -> None:
        issue = Issue(number=42, state="OPEN", title="A title", labels=labels)
        out = io.StringIO()
        print_issue_table([issue], out)

        assert (
            out.getvalue()[0]
            == issue_text(42, "OPEN", "A title", labels=labels).plain[_BATCH_COLUMN]
        )


class TestFilteredText:
    def test_a_run_row_renders_blank_in_both_glyph_columns(self) -> None:
        run = filtered_text(3)
        issue = issue_text(1, "OPEN", "t", labels=("queued",), marked=True)

        assert run.plain.startswith("  ")
        assert run.plain.index("<") == issue.plain.index("#")


class TestMarkColumn:
    def test_a_marked_row_shows_the_glyph_and_an_unmarked_row_a_blank(self) -> None:
        marked = issue_text(42, "OPEN", "A title", marked=True)
        plain = issue_text(42, "OPEN", "A title")

        assert marked.plain.startswith(MARK)
        assert plain.plain.startswith(" ")
        assert len(marked.plain) == len(plain.plain)
        assert marked.plain.index("A title") == plain.plain.index("A title")

    def test_the_mark_precedes_the_batch_glyph(self) -> None:
        both = issue_text(42, "OPEN", "A title", labels=("queued",), marked=True)

        assert both.plain.startswith(f"{MARK}q #42")
