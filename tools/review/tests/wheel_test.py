from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from review.cli import LENSES

_PACKAGE = Path(__file__).resolve().parents[1]


@pytest.mark.slow
def test_the_wheel_carries_every_prompt_template(tmp_path: Path) -> None:
    """Templates read through importlib.resources must ship inside the wheel."""
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(_PACKAGE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr

    [wheel] = tmp_path.glob("review-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    expected = [
        "review/templates/review.md",
        "review/templates/review-consolidate.md",
        *(f"review/templates/lenses/{lens}.md" for lens in LENSES),
    ]
    for name in expected:
        assert name in names, name
