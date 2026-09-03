"""Guards the local-only slow-test deselection in the root conftest.

CI must run the slow tests: with CI set, a bare pytest collects them.
Without CI, they are deselected unless --slow, -m, -k, or explicit
paths ask for them.

It also guards that the gate workflow still runs the full suite under xdist.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

_ROOT: Final = Path(__file__).resolve().parents[1]

_WORKFLOWS: Final = _ROOT / ".github" / "workflows"

_TUI_SUITES: Final = ("tools/orbit/tests", "tools/batch/tests")


def _tui_node(testpath: str) -> str:
    return f"{testpath}/tui_test.py::"


def _collect(*args: str, ci: bool, testpath: str = _TUI_SUITES[0]) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "PYTEST_ADDOPTS")}
    if ci:
        env["CI"] = "true"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            f"testpaths={testpath}",
            *args,
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.slow
@pytest.mark.parametrize("testpath", _TUI_SUITES)
def test_ci_collects_the_slow_tests(testpath: str) -> None:
    out = _collect(ci=True, testpath=testpath)
    assert _tui_node(testpath) in out
    assert "deselected" not in out


@pytest.mark.slow
@pytest.mark.parametrize("testpath", _TUI_SUITES)
def test_local_run_deselects_the_slow_tests(testpath: str) -> None:
    out = _collect(ci=False, testpath=testpath)
    assert _tui_node(testpath) not in out
    assert "deselected" in out


@pytest.mark.slow
@pytest.mark.parametrize("testpath", _TUI_SUITES)
def test_local_slow_flag_restores_them(testpath: str) -> None:
    out = _collect("--slow", ci=False, testpath=testpath)
    assert _tui_node(testpath) in out
    assert "deselected" not in out


@pytest.mark.slow
@pytest.mark.parametrize("testpath", _TUI_SUITES)
def test_explicit_path_collects_the_slow_tests_it_names(testpath: str) -> None:
    out = _collect(f"{testpath}/tui_test.py", ci=False, testpath=testpath)
    assert _tui_node(testpath) in out
    assert "deselected" not in out


@pytest.mark.slow
def test_marker_expression_selects_the_slow_tests() -> None:
    out = _collect("-m", "slow", ci=False)
    assert _tui_node(_TUI_SUITES[0]) in out


@pytest.mark.slow
def test_empty_marker_expression_is_the_full_suite() -> None:
    out = _collect("-m", "", ci=False)
    assert _tui_node(_TUI_SUITES[0]) in out
    assert "deselected" not in out


@pytest.mark.slow
def test_keyword_selection_reaches_the_slow_tests() -> None:
    out = _collect("-k", "test_loads_epics_on_start", ci=False)
    assert _tui_node(_TUI_SUITES[0]) in out


def test_the_gate_workflow_runs_the_full_suite() -> None:
    text = (_WORKFLOWS / "ci.yml").read_text()
    assert "uv run pytest" in text
    assert "not slow" not in text
    for line in [ln for ln in text.splitlines() if "uv run pytest" in ln]:
        workers = re.search(r"(?:-n|--numprocesses)[\s=]*(\S+)", line)
        assert workers, f"pytest runs without xdist workers: {line.strip()}"
        # auto means physical cores to pytest-xdist[psutil], which is 1 on a
        # 2-vCPU runner; the suite then runs serially and still looks green.
        assert workers.group(1) != "auto", (
            f"-n auto resolves to one worker on CI: {line.strip()}"
        )
