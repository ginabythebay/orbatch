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


def _collect(*args: str, ci: bool) -> str:
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
            "testpaths=tools/orbit/tests",
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
def test_ci_collects_the_slow_tests() -> None:
    out = _collect(ci=True)
    assert "tui_test.py" in out
    assert "deselected" not in out


@pytest.mark.slow
def test_local_run_deselects_the_slow_tests() -> None:
    out = _collect(ci=False)
    assert "tui_test.py" not in out
    assert "deselected" in out


@pytest.mark.slow
def test_local_slow_flag_restores_them() -> None:
    out = _collect("--slow", ci=False)
    assert "tui_test.py" in out
    assert "deselected" not in out


@pytest.mark.slow
def test_explicit_path_collects_the_slow_tests_it_names() -> None:
    out = _collect("tools/orbit/tests/tui_test.py", ci=False)
    assert "tui_test.py" in out
    assert "deselected" not in out


@pytest.mark.slow
def test_marker_expression_selects_the_slow_tests() -> None:
    out = _collect("-m", "slow", ci=False)
    assert "tui_test.py" in out


@pytest.mark.slow
def test_empty_marker_expression_is_the_full_suite() -> None:
    out = _collect("-m", "", ci=False)
    assert "tui_test.py" in out
    assert "deselected" not in out


@pytest.mark.slow
def test_keyword_selection_reaches_the_slow_tests() -> None:
    out = _collect("-k", "test_loads_epics_on_start", ci=False)
    assert "tui_test.py" in out


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
