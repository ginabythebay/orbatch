"""Console-script entry points across the uv workspace.

hatchling reads PEP 621's `[project.scripts]`; a bare `[scripts]` table
generates no executable and reports no error, so the declaration has to be
checked rather than trusted.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final, cast

import pytest

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]

# `python -m <package>` runs whichever entry point that package's __main__
# picks, so a package declaring a second script names its own module here.
_MODULE_FORM: Final = {"review-html": "review.html", "vwt": "batch.worktree"}


def _load(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _members() -> list[Path]:
    tool = cast(dict[str, object], _load(_PROJECT_ROOT / "pyproject.toml")["tool"])
    uv = cast(dict[str, object], tool["uv"])
    workspace = cast(dict[str, list[str]], uv["workspace"])
    paths = [_PROJECT_ROOT / "pyproject.toml"]
    for pattern in workspace["members"]:
        paths.extend(sorted(_PROJECT_ROOT.glob(f"{pattern}/pyproject.toml")))
    return paths


def _console_scripts() -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in _members():
        project = cast(dict[str, object], _load(path).get("project", {}))
        scripts = cast(dict[str, str], project.get("scripts", {}))
        for name, target in scripts.items():
            assert name not in entries, f"{name} declared twice"
            entries[name] = target
    return entries


_MEMBERS: Final = _members()
_SCRIPTS: Final = _console_scripts()


@pytest.mark.parametrize(
    "path", _MEMBERS, ids=[str(p.relative_to(_PROJECT_ROOT)) for p in _MEMBERS]
)
def test_no_member_declares_a_bare_scripts_table(path: Path) -> None:
    assert "scripts" not in _load(path)


def test_the_workspace_declares_console_scripts() -> None:
    assert set(_SCRIPTS) >= {
        "batch",
        "orbit",
        "review-diff",
        "review-html",
        "snippets",
        "vwt",
    }


@pytest.mark.parametrize("target", _SCRIPTS.values(), ids=list(_SCRIPTS))
def test_a_console_script_target_resolves(target: str) -> None:
    module_path, _, attribute = target.partition(":")
    module = importlib.import_module(module_path)
    assert callable(getattr(module, attribute, None))


@pytest.mark.parametrize("name", sorted(_SCRIPTS), ids=sorted(_SCRIPTS))
def test_the_installed_console_script_matches_the_module_form(name: str) -> None:
    module_form = _MODULE_FORM.get(name, _SCRIPTS[name].partition(":")[0].split(".")[0])
    bin_dir = Path(sys.executable).parent
    script = subprocess.run(
        [str(bin_dir / name), "--help"],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        timeout=60,
        check=False,
    )
    module = subprocess.run(
        [str(bin_dir / "python"), "-m", module_form, "--help"],
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        timeout=60,
        check=False,
    )

    assert script.returncode == 0, script.stderr
    assert module.returncode == 0, module.stderr
    assert script.stdout.startswith("Usage: ")
    assert script.stdout == module.stdout
