"""`batch` must name no host repo of its own.

Every path it drives comes from `batch.toml`, so a second repo can adopt
the tool unchanged. This turns the closing grep of that work into a test.

The tests are held to the repo names alone: they legitimately carry
`dev/`-prefixed command values as fixture data, which no module may.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import batch

_SRC = Path(batch.__file__).parent
_TESTS = Path(__file__).parent
_SOURCES = sorted(_SRC.rglob("*.py"))
_TEST_SOURCES = sorted(_TESTS.rglob("*.py"))
_REPO_NAMES = ("pinky", "moist-cupcake")
_FORBIDDEN = ("dev/", *_REPO_NAMES)


def _docstring_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def _offenders(root: Path, source: Path, forbidden: tuple[str, ...]) -> list[str]:
    text = source.read_text()
    exempt = _docstring_lines(ast.parse(text))
    return [
        f"{source.relative_to(root)}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if number not in exempt
        and not _declares_the_names(source, line)
        and any(bad in line for bad in forbidden)
    ]


def _declares_the_names(source: Path, line: str) -> bool:
    return source == Path(__file__) and line.startswith("_REPO_NAMES =")


@pytest.mark.parametrize(
    "source", _SOURCES, ids=[str(source.relative_to(_SRC)) for source in _SOURCES]
)
def test_no_module_names_the_repo_that_hosts_batch(source: Path) -> None:
    assert _offenders(_SRC, source, _FORBIDDEN) == []


@pytest.mark.parametrize(
    "source",
    _TEST_SOURCES,
    ids=[str(source.relative_to(_TESTS)) for source in _TEST_SOURCES],
)
def test_no_test_names_the_repo_that_hosts_batch(source: Path) -> None:
    assert _offenders(_TESTS, source, _REPO_NAMES) == []
