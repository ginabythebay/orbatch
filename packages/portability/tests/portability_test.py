"""No file in the workspace may name a repository that hosts it.

Every path these tools drive comes from configuration, so a second repo can
adopt them unchanged. This turns the closing grep of that work into a test
over the whole checkout — sources, tests, docs and config alike — so one
failure names the file that regressed.

Modules are also held to the command paths of the repo `batch` was
extracted from. That half stays scoped to the packages that came over
clean: `orbit`, `review` and `snippets` still carry `dev/` program names,
tracked separately. Tests are held to the repo names alone, since they
legitimately carry command values as fixture data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

from portability import names
from portability.names import forbidden_words

_ROOT: Final = Path(__file__).resolve().parents[3]

_COMMAND_PATH: Final = "dev/"

_COMMAND_FREE_SOURCES: Final = ("tools/batch/src", "packages/shellcomp/src")


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
_MODULES: Final = [
    path
    for path in _FILES
    if path.suffix == ".py"
    and any(
        str(path).startswith(f"{_ROOT}/{source}") for source in _COMMAND_FREE_SOURCES
    )
]


def _ids(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(_ROOT)) for path in paths]


def _host_repo_lines(source: Path) -> list[str]:
    text = source.read_text()
    if not forbidden_words(text):
        return []
    return [
        f"{source.relative_to(_ROOT)}:{number}"
        for number, line in enumerate(text.splitlines(), start=1)
        if forbidden_words(line)
    ]


@pytest.mark.parametrize("source", _FILES, ids=_ids(_FILES))
def test_no_tracked_file_names_a_host_repo(source: Path) -> None:
    assert _host_repo_lines(source) == []


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
