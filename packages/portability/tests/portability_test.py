"""No file in the workspace may name a repository that hosts it.

Every path these tools drive comes from configuration, so a second repo can
adopt them unchanged. This turns the closing grep of that work into a test
over the whole checkout — sources, tests, docs and config alike — so one
failure names the file that regressed.

Modules are also held to the command paths of the repo `batch` was
extracted from. That half exempts the three packages that still carry
`dev/` program names, tracked in issue #15; every other package's `src/` is
held to it, including any package added later. Tests are held to the repo
names alone, since they legitimately carry command values as fixture data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

from portability import names
from portability.names import NAMES, forbidden_words

_ROOT: Final = Path(__file__).resolve().parents[3]

_COMMAND_PATH: Final = "dev/"

_COMMAND_PATH_EXEMPT: Final = (
    "tools/orbit/src",
    "tools/review/src",
    "tools/snippets/src",
)


def _listed() -> list[Path]:
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return sorted(_ROOT / entry for entry in listed.stdout.split("\0") if entry)


def _text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


_FILES: Final = [path for path in _listed() if _text(path) is not None]


def _is_guarded_module(path: Path) -> bool:
    relative = path.relative_to(_ROOT).as_posix()
    return (
        path.suffix == ".py"
        and "/src/" in relative
        and not relative.startswith(_COMMAND_PATH_EXEMPT)
    )


_MODULES: Final = [path for path in _FILES if _is_guarded_module(path)]


def _ids(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(_ROOT)) for path in paths]


def _host_repo_lines(root: Path, source: Path) -> list[str]:
    return [
        f"{source.relative_to(root)}:{number}"
        for number, line in enumerate(source.read_text().splitlines(), start=1)
        if forbidden_words(line)
    ]


@pytest.mark.parametrize("source", _FILES, ids=_ids(_FILES))
def test_no_tracked_file_names_a_host_repo(source: Path) -> None:
    assert _host_repo_lines(_ROOT, source) == []


def test_a_planted_name_is_reported_with_its_line(tmp_path: Path) -> None:
    planted = tmp_path / "fixture.py"
    _ = planted.write_text(f'clean = "widget"\nREPO = "{NAMES[0]}"\n')

    assert _host_repo_lines(tmp_path, planted) == ["fixture.py:2"]


@pytest.mark.parametrize("source", _MODULES, ids=_ids(_MODULES))
def test_no_module_names_a_command_path(source: Path) -> None:
    offenders = [
        f"{source.relative_to(_ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(source.read_text().splitlines(), start=1)
        if _COMMAND_PATH in line
    ]

    assert offenders == []


def test_the_declaring_module_is_scanned_like_any_other_file() -> None:
    assert Path(names.__file__).resolve() in _FILES


def test_the_command_path_sweep_reaches_the_packages_that_came_over_clean() -> None:
    assert _ROOT / "tools/batch/src/batch/cli.py" in _MODULES
    assert _ROOT / "packages/shellcomp/src/shellcomp/completion.py" in _MODULES
