"""Guidance references in `.claude/settings.json` and `CLAUDE.md`.

A PostToolUse hook that cannot read its `--rawfile` emits `{}` and exits 0,
so a renamed guidance file silently stops reaching the agent.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Final, cast

import pytest

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
_SETTINGS: Final = _PROJECT_ROOT / ".claude" / "settings.json"
_REFERENCE: Final = re.compile(r"\.claude/[\w./-]+")


def _settings() -> dict[str, object]:
    return cast(dict[str, object], json.loads(_SETTINGS.read_text()))


def _hook_commands() -> list[str]:
    hooks = cast(dict[str, list[dict[str, object]]], _settings()["hooks"])
    return [
        cast(str, hook["command"])
        for matchers in hooks.values()
        for matcher in matchers
        for hook in cast(list[dict[str, object]], matcher["hooks"])
    ]


def _referenced(text: str) -> set[str]:
    return {match.group().rstrip(".,`") for match in _REFERENCE.finditer(text)}


def _hook_references() -> set[str]:
    return {path for command in _hook_commands() for path in _referenced(command)}


def _doc_references() -> set[str]:
    return _referenced((_PROJECT_ROOT / "CLAUDE.md").read_text())


def test_the_settings_file_parses() -> None:
    assert _settings()["hooks"]


@pytest.mark.parametrize("reference", sorted(_hook_references() | _doc_references()))
def test_every_referenced_claude_file_exists(reference: str) -> None:
    assert (_PROJECT_ROOT / reference).is_file()


def test_the_hooks_reference_every_guidance_file() -> None:
    guidance = {
        f".claude/{path.name}"
        for path in (_PROJECT_ROOT / ".claude").glob("*-guidance.md")
    }
    assert guidance <= _hook_references()


def _hook_scripts() -> list[str]:
    return sorted(
        reference
        for reference in _hook_references()
        if reference.startswith(".claude/hooks/")
    )


@pytest.mark.parametrize("reference", _hook_scripts())
def test_every_hook_script_is_executable(reference: str) -> None:
    assert os.access(_PROJECT_ROOT / reference, os.X_OK)


def test_the_hook_scripts_are_found() -> None:
    assert _hook_scripts()
