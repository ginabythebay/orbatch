"""No file in the workspace may name a repository that hosts it.

Every path these tools drive comes from configuration, so a second repo can
adopt them unchanged. This turns the closing grep of that work into a test
over the whole checkout — sources, tests, docs and config alike — so one
failure names the file that regressed.

Every module under a `src/` root is also held to the command paths of the
repo `batch` was extracted from, with no exemptions: a package added later
is swept the moment its first module is tracked. Tests are held to the repo
names alone, since they legitimately carry command values as fixture data.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import pytest

from portability import names
from portability.names import NAMES, forbidden_words

_ROOT: Final = Path(__file__).resolve().parents[3]

_COMMAND_PATH: Final = "dev/"


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
    return path.suffix == ".py" and "/src/" in path.relative_to(_ROOT).as_posix()


_MODULES: Final = [path for path in _FILES if _is_guarded_module(path)]


def _src_roots(paths: Iterable[Path]) -> set[str]:
    roots: set[str] = set()
    for path in paths:
        relative = path.relative_to(_ROOT).as_posix()
        head, separator, _ = relative.partition("/src/")
        if separator:
            roots.add(head)
    return roots


def _ids(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(_ROOT)) for path in paths]


def _host_repo_lines(root: Path, source: Path) -> list[str]:
    return [
        f"{source.relative_to(root)}:{number}"
        for number, line in enumerate(source.read_text().splitlines(), start=1)
        if forbidden_words(line)
    ]


def _command_path_lines(root: Path, source: Path) -> list[str]:
    return [
        f"{source.relative_to(root)}:{number}: {line.strip()}"
        for number, line in enumerate(source.read_text().splitlines(), start=1)
        if _COMMAND_PATH in line
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
    assert _command_path_lines(_ROOT, source) == []


def test_a_planted_command_path_is_reported_with_its_line(tmp_path: Path) -> None:
    planted = tmp_path / "fixture.py"
    _ = planted.write_text(f'clean = "widget"\nPROG = "{_COMMAND_PATH}thing"\n')

    assert _command_path_lines(tmp_path, planted) == [
        f'fixture.py:2: PROG = "{_COMMAND_PATH}thing"'
    ]


def test_a_test_carrying_a_command_path_is_not_swept() -> None:
    carriers = [
        path
        for path in _FILES
        if path.suffix == ".py" and _command_path_lines(_ROOT, path)
    ]

    assert carriers != []
    assert set(carriers) & set(_MODULES) == set()


def test_the_declaring_module_is_scanned_like_any_other_file() -> None:
    assert Path(names.__file__).resolve() in _FILES


def test_every_src_root_in_the_workspace_is_swept() -> None:
    assert _src_roots(_MODULES) == _src_roots(
        path for path in _FILES if path.suffix == ".py"
    )
