#!/usr/bin/env bash
# Create a GitHub PR. Title is $1, body is read from stdin.
# The body must contain a '## Caveats' heading on a line of its own.
# Usage: .claude/tools/create-pr.sh "feat: my title" <<< "body text"
#    or: echo "body" | .claude/tools/create-pr.sh "feat: my title"
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <title> [gh-pr-create-flags...]" >&2
    echo "Body is read from stdin." >&2
    exit 1
fi

title="$1"
shift

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
cat > "$body_file"

if ! grep -qE '^## Caveats[[:space:]]*$' "$body_file"; then
    echo "aborted: the PR body needs a '## Caveats' section — what you could not" >&2
    echo "verify and why, assumptions the change rests on, work left undone." >&2
    echo "Write 'None.' under it when there genuinely is nothing." >&2
    exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" == "HEAD" ]]; then
    echo "aborted: detached HEAD — cannot determine branch name" >&2
    exit 1
fi

# Check the branch has been pushed to the remote.
if ! git rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
    echo "aborted: you must first push the current branch to a remote, or use the --head flag" >&2
    exit 1
fi

gh pr create --head "$branch" --title "$title" --body-file /dev/stdin "$@" < "$body_file"
