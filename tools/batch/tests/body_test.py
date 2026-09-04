from __future__ import annotations

from collections.abc import Callable

import pytest

from batch.body import closing_references, guidance, has_test_plan

SLUG = "acme/widgets"


class TestTestGuidance:
    def test_a_body_without_the_section_has_none(self) -> None:
        assert guidance("## Test Plan\n\n1. A test\n") is None

    def test_the_section_text_is_returned_without_its_heading(self) -> None:
        body = "Preamble\n\n## Test Guidance\n\nPort the callers.\n"

        assert guidance(body) == "Port the callers."

    def test_the_section_stops_at_the_next_heading(self) -> None:
        body = "## Test Guidance\n\nPort the callers.\n\n## Blocked by\n\n- #1\n"

        assert guidance(body) == "Port the callers."

    def test_a_multiline_section_collapses_to_one_line(self) -> None:
        body = "## Test Guidance\n\nPort the callers.\nKeep lint green.\n"

        assert guidance(body) == "Port the callers. Keep lint green."

    def test_an_empty_section_has_none(self) -> None:
        assert guidance("## Test Guidance\n\n\n## Next\n") is None


class TestHasTestPlan:
    def test_a_plan_heading_is_found_mid_body(self) -> None:
        assert has_test_plan("Preamble\n\n## Test Plan\n\n1. A test\n")

    def test_guidance_alone_is_not_a_plan(self) -> None:
        assert not has_test_plan("## Test Guidance\n\nNo new tests.\n")


class TestClosingReferences:
    def test_the_workflow_convention_closes_its_issue(self) -> None:
        assert closing_references("Fixes #9\n\nA summary.\n", SLUG) == (9,)

    @pytest.mark.parametrize(
        "keyword",
        [
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ],
    )
    @pytest.mark.parametrize("case", [str.lower, str.capitalize, str.upper])
    def test_every_keyword_in_every_casing_closes(
        self, keyword: str, case: Callable[[str], str]
    ) -> None:
        assert closing_references(f"{case(keyword)} #9", SLUG) == (9,)

    def test_the_optional_colon_is_valid_syntax(self) -> None:
        assert closing_references("Closes: #9", SLUG) == (9,)

    def test_a_reference_without_a_keyword_closes_nothing(self) -> None:
        assert closing_references("Follows on from #9.", SLUG) == ()

    def test_a_keyword_naming_another_issue_reports_only_it(self) -> None:
        assert closing_references("Fixes #7\n", SLUG) == (7,)

    def test_an_empty_body_closes_nothing(self) -> None:
        assert closing_references("", SLUG) == ()

    def test_a_backticked_reference_closes_nothing(self) -> None:
        assert closing_references("Fixes `#9` in passing.", SLUG) == ()

    def test_a_fenced_reference_closes_nothing(self) -> None:
        body = "A summary.\n\n```\nFixes #9\n```\n"

        assert closing_references(body, SLUG) == ()

    def test_a_disposition_table_closes_only_the_live_reference(self) -> None:
        body = (
            "Fixes #9\n\n"
            "| finding | disposition | reasoning |\n"
            "| --- | --- | --- |\n"
            "| dup of `#1585` | declined | fixed by `#1591` |\n"
            "| see `#1503` | no-change | resolves `#1502` already |\n"
        )

        assert closing_references(body, SLUG) == (9,)

    def test_two_keywords_report_both_in_body_order(self) -> None:
        assert closing_references("Fixes #9 and closes #7.", SLUG) == (9, 7)

    def test_a_foreign_slug_reference_closes_nothing(self) -> None:
        assert closing_references("Fixes upstream/other#1907", SLUG) == ()

    def test_a_same_slug_qualified_reference_closes(self) -> None:
        assert closing_references("Fixes acme/widgets#9", SLUG) == (9,)

    def test_the_slug_comparison_ignores_case(self) -> None:
        assert closing_references("Fixes ACME/Widgets#9", SLUG) == (9,)

    def test_the_accepted_numbers_keep_body_order_without_duplicates(self) -> None:
        body = (
            "Fixes #9, closes acme/widgets#7, resolves upstream/other#5 and fixes #9."
        )

        assert closing_references(body, SLUG) == (9, 7)
