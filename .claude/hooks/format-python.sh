#!/usr/bin/env bash
# PostToolUse hook: sort imports and run ruff format on .py files after Write/Edit
set -euo pipefail

FILE=$(jq -r '.tool_input.file_path // ""')

[[ "$FILE" == *.py ]] || exit 0
[[ "$FILE" == "$CLAUDE_PROJECT_DIR"/* ]] || exit 0
[[ -f "$FILE" ]] || exit 0

# Autofix authority here is invisible, so it is pinned to import order rather
# than tracking ruff's default rule set; dev/lint remains the gate.
# dev/lint runs ruff with `env -u VIRTUAL_ENV`; this does not, because `uv run`
# from the project directory already resolves the live environment. If a
# mismatched .venv ever bites here, that is the fix.
uv run ruff check --fix --quiet --select I001 "$FILE" ||
    echo "format-python.sh: ruff check failed on $FILE"
uv run ruff format --quiet "$FILE" ||
    echo "format-python.sh: ruff format failed on $FILE"
