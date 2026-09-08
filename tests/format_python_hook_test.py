"""The PostToolUse hook that formats Python files after Write and Edit.

Nothing else exercises the hook: it runs outside pytest, and a widened
`--select` or a dropped guard would let it edit files it must not touch
while the suite stayed green.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
_HOOK: Final = _PROJECT_ROOT / ".claude" / "hooks" / "format-python.sh"

_UNFORMATTED: Final = "import sys\nimport os\n\nx  =  1\nprint(os, sys)\n"


def _run(project_dir: Path, file_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_HOOK)],
        input=json.dumps({"tool_input": {"file_path": str(file_path)}}),
        capture_output=True,
        text=True,
        check=False,
        cwd=_PROJECT_ROOT,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)},
    )


def test_a_python_file_comes_back_sorted_and_formatted(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _ = source.write_text(_UNFORMATTED)

    assert _run(tmp_path, source).returncode == 0
    assert source.read_text() == "import os\nimport sys\n\nx = 1\nprint(os, sys)\n"


def test_an_unused_import_survives_the_autofix(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    _ = source.write_text("import sys\nimport os\n\nprint( os )\n")

    assert _run(tmp_path, source).returncode == 0
    assert source.read_text() == "import os\nimport sys\n\nprint(os)\n"


@pytest.mark.parametrize("name", ["notes.md", "script"])
def test_a_non_python_file_is_left_alone(tmp_path: Path, name: str) -> None:
    other = tmp_path / name
    _ = other.write_text(_UNFORMATTED)

    assert _run(tmp_path, other).returncode == 0
    assert other.read_text() == _UNFORMATTED


def test_a_python_file_outside_the_project_is_left_alone(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "module.py"
    _ = outside.write_text(_UNFORMATTED)

    assert _run(project, outside).returncode == 0
    assert outside.read_text() == _UNFORMATTED


def test_a_deleted_file_is_not_recreated(tmp_path: Path) -> None:
    missing = tmp_path / "gone.py"

    assert _run(tmp_path, missing).returncode == 0
    assert not missing.exists()
