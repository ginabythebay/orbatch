from __future__ import annotations

import io

from orbit.github.models import Epic, Issue, IssueDetail
from orbit.text_output import (
    print_epic_table,
    print_issue_detail,
    print_issue_table,
    print_parent_issue,
    print_sub_issue_tree,
)
from orbit.tree import FilteredRun, TreeItem, TreeNode


def _issue_detail(
    *,
    body: str = "",
    labels: tuple[str, ...] = (),
    milestone_title: str | None = None,
    parent_number: int | None = None,
    parent_title: str | None = None,
) -> IssueDetail:
    return IssueDetail(
        node_id="I_1",
        number=905,
        state="OPEN",
        title="orbit dev tool",
        body=body,
        labels=labels,
        milestone_id=None,
        milestone_title=milestone_title,
        parent_number=parent_number,
        parent_node_id=None,
        parent_title=parent_title,
    )


class TestPrintEpicTable:
    def test_prints_epic_table_with_counts(self) -> None:
        epics = [
            Epic(
                number=905,
                state="OPEN",
                title="orbit — dev tool",
                open_count=3,
                total_count=5,
            ),
            Epic(
                number=852,
                state="CLOSED",
                title="Test suite speed",
                open_count=0,
                total_count=4,
            ),
        ]
        out = io.StringIO()
        print_epic_table(epics, out)
        text = out.getvalue()
        assert "#905" in text
        assert "OPEN" in text
        assert "3/5" in text
        assert "orbit — dev tool" in text
        assert "#852" in text
        assert "CLOSED" in text
        assert "0/4" in text
        assert "Test suite speed" in text

    def test_empty_epic_list(self) -> None:
        out = io.StringIO()
        print_epic_table([], out)
        assert "No epics found" in out.getvalue()


class TestPrintSubIssueTree:
    def test_nested_with_indentation_and_counts(self) -> None:
        nodes = [
            TreeNode(
                number=10,
                state="OPEN",
                title="Leaf",
                open_count=None,
                total_count=None,
                children=(),
            ),
            TreeNode(
                number=30,
                state="OPEN",
                title="Epic",
                open_count=1,
                total_count=2,
                children=(
                    TreeNode(
                        number=31,
                        state="OPEN",
                        title="Child A",
                        open_count=None,
                        total_count=None,
                        children=(),
                    ),
                    TreeNode(
                        number=32,
                        state="CLOSED",
                        title="Child B",
                        open_count=None,
                        total_count=None,
                        children=(),
                    ),
                ),
            ),
        ]
        out = io.StringIO()
        print_sub_issue_tree(nodes, out)
        text = out.getvalue()
        assert "#10" in text
        assert "#30" in text
        assert "1/2" in text
        child_lines = [line for line in text.split("\n") if "#31" in line]
        assert child_lines
        assert child_lines[0].startswith(" ")


class TestPrintIssueDetail:
    def test_full_detail_with_body_and_parent(self) -> None:
        detail = _issue_detail(
            body="## Heading\n\nSome body text.",
            labels=("epic", "soon"),
            milestone_title="developer velocity",
            parent_number=900,
            parent_title="Parent epic",
        )
        out = io.StringIO()
        print_issue_detail(detail, out)
        text = out.getvalue()
        assert "#905 orbit dev tool" in text
        assert "OPEN" in text
        assert "epic, soon" in text
        assert "developer velocity" in text
        assert "#900" in text
        assert "Parent epic" in text
        assert "Some body text." in text

    def test_no_labels_no_parent_no_body(self) -> None:
        detail = _issue_detail()
        out = io.StringIO()
        print_issue_detail(detail, out)
        text = out.getvalue()
        assert "#905 orbit dev tool" in text
        assert "Labels:" in text
        assert "Parent:" in text
        # An em dash stands in for each missing field.
        assert "—" in text


class TestPrintParentIssue:
    def test_prints_parent_line(self) -> None:
        issue = Issue(number=100, state="OPEN", title="Parent epic")
        out = io.StringIO()
        print_parent_issue(issue, out)
        text = out.getvalue()
        assert "#100" in text
        assert "OPEN" in text
        assert "Parent epic" in text


class TestPrintFilteredRuns:
    def test_run_renders_as_a_count_row(self) -> None:
        nodes: list[TreeItem] = [
            TreeNode(
                number=30,
                state="OPEN",
                title="Epic",
                open_count=0,
                total_count=3,
                children=(FilteredRun(count=3, numbers=(31, 32, 33)),),
            ),
            FilteredRun(count=3, numbers=(41, 42, 43)),
        ]
        out = io.StringIO()
        print_sub_issue_tree(nodes, out)
        lines = out.getvalue().split("\n")
        run_lines = [line for line in lines if "filtered" in line]
        assert len(run_lines) == 2
        nested, top_level = run_lines
        assert nested.strip() == "<3 issues filtered>"
        assert "#3" not in nested
        assert "OPEN" not in nested
        epic_line = next(line for line in lines if "#30" in line)
        assert nested.index("<") == epic_line.index("Epic") + 2
        assert top_level.index("<") == epic_line.index("Epic")

    def test_single_issue_placeholder_renders_singular_with_counts(self) -> None:
        nodes: list[TreeItem] = [
            FilteredRun(
                count=1,
                numbers=(2,),
                open_count=1,
                total_count=2,
                children=(
                    TreeNode(
                        number=20,
                        state="OPEN",
                        title="Open child",
                        open_count=None,
                        total_count=None,
                        children=(),
                    ),
                ),
            )
        ]
        out = io.StringIO()
        print_sub_issue_tree(nodes, out)
        lines = out.getvalue().split("\n")
        run_line = next(line for line in lines if "filtered" in line)
        assert "<1 issue filtered>" in run_line
        assert "1/2" in run_line
        assert any("#20" in line for line in lines)


class TestFlatTableFilteredRuns:
    def test_issue_table_run_row_sits_in_the_title_column(self) -> None:
        rows: list[Issue | FilteredRun] = [
            Issue(number=42, state="OPEN", title="Fix the widget"),
            FilteredRun(count=3, numbers=(100, 101, 102)),
            Issue(number=7, state="OPEN", title="Ship the thing"),
        ]
        out = io.StringIO()
        print_issue_table(rows, out)
        lines = out.getvalue().split("\n")
        run_line = next(line for line in lines if "filtered" in line)
        assert run_line.strip() == "<3 issues filtered>"
        first, last = (next(line for line in lines if f"#{n}" in line) for n in (42, 7))
        assert run_line.index("<") == first.index("Fix the widget")
        assert lines.index(first) < lines.index(run_line) < lines.index(last)

    def test_epic_table_run_row_leaves_the_progress_column_blank(self) -> None:
        rows: list[Epic | FilteredRun] = [
            Epic(number=905, state="OPEN", title="orbit", open_count=3, total_count=5),
            FilteredRun(count=2, numbers=(1, 2)),
            Epic(number=852, state="OPEN", title="Speed", open_count=1, total_count=4),
        ]
        out = io.StringIO()
        print_epic_table(rows, out)
        lines = out.getvalue().split("\n")
        run_line = next(line for line in lines if "filtered" in line)
        assert run_line.strip() == "<2 issues filtered>"
        first = next(line for line in lines if "#905" in line)
        assert run_line.index("<") == first.index("orbit")

    def test_issue_table_run_of_one_renders_singular(self) -> None:
        rows: list[Issue | FilteredRun] = [FilteredRun(count=1, numbers=(100,))]
        out = io.StringIO()
        print_issue_table(rows, out)
        assert "<1 issue filtered>" in out.getvalue()
