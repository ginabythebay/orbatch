#!/usr/bin/env bash
# PreToolUse hook: block basedpyright invoked outside dev/lint.

payload="$(cat)"

if [[ "$(jq -r '.tool_name // ""' <<<"$payload")" != "Bash" ]]; then
    exit 0
fi

command="$(jq -r '.tool_input.command // ""' <<<"$payload")"
invocation='(^|[;&|(])[[:space:]]*(uv[[:space:]]+run[[:space:]]+([^[:space:]]+[[:space:]]+)*)?basedpyright([[:space:]]|$)'

if ! grep -qE "$invocation" <<<"$command"; then
    exit 0
fi

cat >&2 <<'MSG'
DENY: Do not run basedpyright directly. Run dev/lint instead.
  basedpyright resolves imports against the .venv named in
  pyproject.toml. This checkout may be used from more than one
  environment at the same path, so that venv is not always the live
  one, and a run against the wrong one reports every import as
  unresolved -- a flood that looks like code problems and is not.
  dev/lint asks uv which environment is live and points basedpyright
  at it with --venvpath.
MSG
exit 2
