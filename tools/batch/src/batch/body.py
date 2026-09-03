from __future__ import annotations

import re

GUIDANCE_HEADING = "## Test Guidance"
PLAN_HEADING = "## Test Plan"
DEFAULT_GUIDANCE = (
    "Do not add new tests. The existing test suite and lint must stay green."
)

_SECTION = re.compile(
    rf"^{re.escape(GUIDANCE_HEADING)}\s*$.*?(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
_PLAN = re.compile(rf"^{re.escape(PLAN_HEADING)}\s*$", re.MULTILINE)

_FENCE = r"```.*?(?:```|\Z)"
_SPAN = r"`[^`\n]*`"
_CODE = re.compile(rf"{_FENCE}|{_SPAN}", re.DOTALL)

_KEYWORD = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b"
_REFERENCE = r"(?:[\w.-]+/[\w.-]+)?#(\d+)"
_CLOSING = re.compile(rf"{_KEYWORD}\s*:?\s+{_REFERENCE}", re.IGNORECASE)


def closing_references(body: str) -> tuple[int, ...]:
    """Issue numbers this body closes, as GitHub reads it: keyword then `#n`,
    ignoring code spans and fenced blocks."""
    prose = _CODE.sub(" ", body)
    found = [int(match.group(1)) for match in _CLOSING.finditer(prose)]
    return tuple(dict.fromkeys(found))


def has_test_plan(body: str) -> bool:
    return _PLAN.search(body) is not None


def guidance(body: str) -> str | None:
    """One line: the templates substitute this into a single prompt line."""
    found = _SECTION.search(body)
    if found is None:
        return None
    text = found.group().removeprefix(GUIDANCE_HEADING)
    return " ".join(text.split()) or None


def with_guidance(body: str, guidance: str) -> str:
    """The body with a single Test Guidance section carrying this text.

    An existing section is rewritten where it stands so that whatever
    follows it keeps its place; otherwise the section is appended.
    """
    section = f"{GUIDANCE_HEADING}\n\n{guidance}"
    if _SECTION.search(body):
        return _SECTION.sub(lambda _: f"{section}\n\n", body).rstrip("\n")
    return f"{body.rstrip()}\n\n{section}" if body.strip() else section
