# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when
working with code in this repository.

## General

This is a Python project. Always use Python best practices, type hints,
and follow existing code conventions. Leave the codebase better than you
found it.

The four packages here were extracted from another repository and the
history was squashed on import, so `git log` and `git blame` start at
this repo's initial commit.

## Response Length

Hard limit: 15 lines of prose per response, excluding code blocks. If
the answer doesn't fit, give the single most important thing and offer
to expand.

Never:

- restate content already written to a file — reference the path instead
- re-summarize a proposal that was just rejected; say what changed, in
  one line
- report the evidence a tool returned when the conclusion is what was
  asked for

Multiple options with rationale are fine when the user is choosing
between them: one or two lines each, and mark your recommendation.

A rejected tool call is a signal to stop and ask, not to retry with
more words.

## Current Sprint

The active sprint milestone is defined once, in `.orbit.toml` at the
repo root (`[milestone] current`). Run `uv run orbit sprint` to see it
and its open issues.

## Commands

```bash
# Install dependencies
uv sync

# Run tests. Locally this deselects the `slow` tests (wheel builds,
# isolated venvs, full TUI boots). The deselection lives in conftest.py
# and only applies to bare full-suite runs: on CI, with -m or -k, or
# with explicit paths, everything named runs.
uv run pytest

# The full suite, slow tests included. This is what to run before push;
# `CI=true`, not the absence of flags, is what keeps the slow tests in.
uv run pytest --slow

# Either form takes xdist
uv run pytest -n auto

# Every static check: ruff check, ruff format --check, basedpyright.
# Stops at the first failure. Never `uv run basedpyright` — it resolves
# imports against whatever interpreter is on PATH.
dev/lint

# Run a single test file
uv run pytest tools/orbit/tests/config_test.py

# Run a specific test by name
uv run pytest -k "test_reorder"
```

Avoid running bash commands that start with `python -c`. It is not
possible to safely grant blanket permission for this, so the user ends
up reviewing and approving each individual command, and that is slow.
Often `jq` will do instead.

## Architecture

A uv workspace of four packages:

- `tools/orbit` — GitHub issue and epic management, CLI and TUI
  (`orbit`)
- `tools/review` — fresh-eyes code review over a diff or PR
  (`review-diff`, `review-html`)
- `tools/snippets` — per-epic activity and accomplishment rollups
  (`snippets`)
- `packages/ghgql` — shared GitHub GraphQL transport and repo detection

Members depend on each other through `[tool.uv.sources]` in the root
`pyproject.toml`, never by relative path, so a checkout resolves without
reference to anything outside it.

### Repo-agnostic by construction

These tools run against whatever repository the user is in, not against
this one. Repo discovery goes through `ghgql.repo.repo_root()`, which
shells out to `git rev-parse --show-toplevel`, and repo identity through
`ghgql.repo.repo()`, which reads the `origin` remote. Never derive a
repository path from `__file__`: it is correct inside a checkout and
wrong once the package is installed with `uv tool install`.

Test fixtures carry `example-org/example-repo` as an arbitrary repo slug.
That is fake data, not a live dependency.

### Package structure

Prefer empty `__init__.py` files. Consumers should import from specific
submodules (e.g. `from ghgql.repo import repo_root`), not from the
package. This avoids circular imports from eager loading and makes
dependencies explicit.

### Comments

**Write zero comments by default.** This rule is ignored more often than
any other in this file. Write the code with no comments at all, then add
one back only if it states a constraint the code genuinely cannot state
itself — an invariant, an external requirement, a non-obvious ordering.
If you can't name that constraint in one line, there is no comment.

Never write a comment that:

- says what the code does, or restates the line below it
- explains what your change does, why it is correct, or how it differs
  from before — that belongs in the commit message or PR body, never in
  the source
- narrates a test ("covers the empty case") or labels a section of a
  function ("# validate inputs") — extract a function instead
- states the obvious from names or type hints

Match the comment density of the file you are editing. If the
surrounding code has no comments, yours gets none.

Before you report work as done, re-read your own diff and delete every
comment that does not survive the one-line-constraint test.

**Docstrings follow the same rule.** A name plus type hints already
document most functions, so most functions get no docstring. Do not add
one that restates the signature, lists the parameters, or names the
return type. Write a docstring only when a caller needs something the
signature can't convey — a precondition, a side effect, a raised
exception, units, or a surprising return value. Follow what the module
already does: don't add docstrings to files that have none, and don't
add them to tests.

### Code Style

Always use type hints.

Prefer dataclasses over `dict[str, object]` for return types on internal
APIs. Typed attribute access avoids `cast`/`isinstance` gymnastics at
every call site and keeps pyright clean.

Always put imports at the top of the file, not inside functions or
methods.

When dealing with circular references, do not resort to
`typing.TYPE_CHECKING` or deferred annotations. Break the cycle by
extracting something out.

**Never write `_ = some_param` to mark a parameter unused.** basedpyright
reports an unused parameter but stays quiet on a method that overrides a
base method. So a fake or stub that satisfies a Protocol should *inherit*
that Protocol and carry `@override`: the discard line goes away and the
fake gains a real conformance check. Where nothing requires the
parameter, drop it from the signature. Where only positional order
requires it, rename it `_some_param`. Never rename a parameter a caller
passes by keyword, and never rename one on a class that conforms to a
Protocol structurally — both break, and only the second is caught by the
type checker.

### Class and signature changes

When renaming a class, also rename variables derived from the old name.
When changing a function or constructor signature, use the LSP tool
(findReferences / incomingCalls) to find ALL callers — production and
test — and update every call site in the same pass.

## Testing

When adding new CLI commands or features, always create corresponding
tests in that package's `tests/` directory.

**Before reporting any task as complete**, run both `uv run pytest --slow`
— the full suite — and `dev/lint` (ruff check, ruff format,
basedpyright), and fix all failures and warnings. This applies to ALL
changed or new files. Formatting is not checked by the test suite, so a
green pytest run says nothing about it.

**Zero failing tests before push.** If any test fails — even one you
didn't touch — fix it before pushing. "Pre-existing" is not an excuse.
If a test only fails after your new tests run, you caused it (leaked
connections, global state, etc.). Run the failing test in isolation to
confirm.

Don't write tests whose only failure mode is someone deliberately
undoing your change. Those just mirror the diff. A regression test
should catch unintended breakage from future unrelated changes.

To assert that a method was called without triggering pyright
`reportAny` / `reportUnknownMemberType` warnings, use `patch.object`
rather than assigning a `MagicMock`:

```python
with patch.object(obj, "method_name") as mock:
    do_something()
    mock.assert_called_once_with(expected_arg)
```

`MagicMock(spec=SomeClass)` and bare `MagicMock()` both leak `Any`
through attribute chains; `patch.object` avoids this.

### Hermeticity: never touch `$HOME`

A test must not read or write anything under `$HOME`. Use `tmp_path`.

## Version Control

Use [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, etc.) for commit subjects.

The PR body must carry a `## Caveats` section — what you could not
verify, assumptions the change rests on, work left undone. `None.` under
it is correct when there genuinely is nothing.

When a PR is already open on the branch, push after every commit. Before
committing to a branch, run `gh pr list --head <branch>` and don't push
unrelated work onto a branch with pending reviews.

If an issue bundles separable concerns — infrastructure plus a feature
built on it, or work a known follow-up will rework — propose splitting
into sequential PRs before implementing, not after.

## CI

`.github/workflows/ci.yml` runs `uv sync`, `uv run pytest -n logical`
and `dev/lint` on `ubuntu-latest`, for pushes to `main` and for pull
requests. There is no release job and no Docker job: these tools are
consumed with `uv tool install` from git, not published.

`-n logical` rather than `-n auto`: `pytest-xdist[psutil]` reads `auto`
as *physical* cores, which resolves to 1 on a 2-vCPU runner and silently
runs the suite serially.
