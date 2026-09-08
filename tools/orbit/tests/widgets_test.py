from __future__ import annotations

import io

import pytest
from rich.text import Text

from ghgql.labels import BatchLabel
from orbit.github.models import Issue
from orbit.palette import Palette
from orbit.text_output import print_issue_table
from orbit.tui.widgets import filtered_text, issue_text


def _glyph_style(text: Text) -> str:
    spans = [s for s in text.spans if (s.start, s.end) == (0, 1)]
    return str(spans[0].style) if spans else ""


class TestIssueText:
    def test_the_glyph_is_a_fixed_width_first_column(self) -> None:
        queued = issue_text(42, "OPEN", "A title", labels=("queued",))
        plain = issue_text(42, "OPEN", "A title")

        assert queued.plain.startswith("q")
        assert plain.plain.startswith(" ")
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
            issue_text(42, "OPEN", "t", labels=labels).plain[0]
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
            == issue_text(42, "OPEN", "A title", labels=labels).plain[0]
        )


class TestFilteredText:
    def test_a_run_row_reserves_the_same_blank_glyph_column(self) -> None:
        run = filtered_text(3)
        issue = issue_text(1, "OPEN", "t")

        assert run.plain.index("<") == issue.plain.index("#")
