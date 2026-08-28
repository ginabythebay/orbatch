from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_COMMAND = [sys.executable, "-m", "review.html"]

_REPORT = """\
# Review of `origin/main...HEAD`

Commit: `abc1234`

## Findings

### Retry loop never terminates on a 500
- **Where:** dev/thing.py:12
- **Severity:** blocking
- **Failure:** a persistent 500 spins forever.
- **Fix:** cap the retries.

### The retry path has no test
- **Where:** tests/thing_test.py:1
- **Severity:** should-fix
- **Failure:** a regression ships silently.
- **Fix:** add a test that exhausts the retries.

### A stale docstring names the old flag
- **Where:** dev/review-diff:57
- **Severity:** minor
- **Failure:** a reader follows the wrong instruction.
- **Fix:** update the usage text.
"""


_PER_LENS = """\

## Per-lens reports

## correctness

### The retry loop spins on a persistent 500
- **Where:** dev/thing.py:12
- **Severity:** blocking
- **Failure:** a persistent 500 spins forever.
- **Fix:** cap the retries.

## conventions

No findings.
"""


def _render(tmp_path: Path, report: str = _REPORT) -> str:
    source = tmp_path / "report.md"
    source.write_text(report)
    out = tmp_path / "report.html"
    result = subprocess.run(
        [*_COMMAND, str(source), "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out.read_text()


def _body(html: str) -> str:
    """The page below the stylesheet, whose own text mentions every severity."""
    return html.split("</style>", 1)[1]


class TestRendering:
    def test_every_finding_survives_rendering(self, tmp_path: Path) -> None:
        body = _body(_render(tmp_path))

        for claim in (
            "Retry loop never terminates on a 500",
            "The retry path has no test",
            "A stale docstring names the old flag",
        ):
            assert claim in body
        for where in ("dev/thing.py:12", "tests/thing_test.py:1", "dev/review-diff:57"):
            assert where in body
        for severity in ("blocking", "should-fix", "minor"):
            assert severity in body

    def test_markdown_is_rendered_including_tables(self, tmp_path: Path) -> None:
        html = _render(
            tmp_path,
            _REPORT + "\n| lens | findings |\n| --- | --- |\n| tests | 1 |\n",
        )

        assert "<strong>Where:</strong>" in html
        assert "<table>" in html
        assert "<td>tests</td>" in html

    def test_page_carries_its_own_styling_and_fetches_nothing(
        self, tmp_path: Path
    ) -> None:
        html = _render(tmp_path)

        assert "<style>" in html
        assert not re.search(r"(?:href|src)\s*=\s*[\"']?https?://", html)
        assert "<link" not in html

    def test_each_severity_is_marked_up_and_glyphed_not_only_coloured(
        self, tmp_path: Path
    ) -> None:
        html = _render(tmp_path)

        for severity in ("blocking", "should-fix", "minor"):
            assert f'severity-{severity}">{severity}<' in html
            assert re.search(
                rf"\.severity-{severity}[^{{]*::before[^{{]*\{{[^}}]*content:", html
            ), severity

    def test_each_finding_becomes_its_own_card(self, tmp_path: Path) -> None:
        html = _render(tmp_path)

        cards = re.findall(r'<section class="finding">(.*?)</section>', html, re.DOTALL)
        assert len(cards) == 3
        assert "Retry loop never terminates on a 500" in cards[0]
        assert "cap the retries" in cards[0]
        assert "<h2>" not in "".join(cards)

    def test_where_paths_are_monospaced(self, tmp_path: Path) -> None:
        html = _render(tmp_path)

        assert "<code>dev/thing.py:12</code>" in html

    def test_title_is_the_reports_h1(self, tmp_path: Path) -> None:
        html = _render(tmp_path)

        assert "<title>Review of origin/main...HEAD</title>" in html

    def test_per_lens_reports_are_collapsed_behind_the_findings(
        self, tmp_path: Path
    ) -> None:
        html = _render(tmp_path, _REPORT + _PER_LENS)

        details = re.search(r"<details>(.*?)</details>", html, re.DOTALL)
        assert details is not None
        assert "<summary>Per-lens reports</summary>" in html
        assert "The retry loop spins on a persistent 500" in details.group(1)
        assert "No findings." in details.group(1)
        assert html.index("Retry loop never terminates on a 500") < details.start()
        before = html[: html.index("<details>")]
        assert before.count("<section") == before.count("</section>")

    def test_html_quoted_in_a_finding_is_escaped(self, tmp_path: Path) -> None:
        report = (
            "# Review of <script>alert(1)</script>\n\n"
            "## Findings\n\n"
            "### <script>alert(2)</script> lands in the page\n"
            "- **Where:** dev/thing.py:12\n"
            "- **Severity:** blocking\n"
            "- **Fix:** escape a & b.\n"
        )
        html = _render(tmp_path, report)

        assert "<script>" not in html
        assert html.count("&lt;script&gt;alert(2)&lt;/script&gt;") == 1
        assert "<title>Review of &lt;script&gt;alert(1)&lt;/script&gt;</title>" in html
        assert "escape a &amp; b." in html


class TestBadInput:
    def test_a_missing_report_fails_without_writing_output(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope.md"
        out = tmp_path / "report.html"
        result = subprocess.run(
            [*_COMMAND, str(missing), "-o", str(out)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode != 0
        assert str(missing) in result.stderr
        assert "Traceback" not in result.stderr
        assert not out.exists()
