# orbatch

Issue and batch tooling.

## Packages

| Path | Console script | What it does |
| --- | --- | --- |
| `tools/orbit` | `orbit` | GitHub issue and epic management, CLI and TUI |
| `tools/review` | `review-diff`, `review-html` | Fresh-eyes code review over a diff or PR |
| `tools/snippets` | `snippets` | Per-epic activity and accomplishment rollups |
| `packages/ghgql` | — | Shared GitHub GraphQL transport and repo detection |

## Layout

A uv workspace. Every package is a member, and they depend on each other
through `[tool.uv.sources]` rather than by path, so a checkout resolves
without reference to anything outside it.

## Development

```bash
uv sync

# The suite. Locally this deselects the tests marked `slow` (wheel builds,
# isolated venvs, full TUI boots); CI runs everything.
uv run pytest

# The full suite, slow tests included. Run this before pushing.
uv run pytest --slow

# ruff check, ruff format --check, basedpyright. Stops at the first failure.
dev/lint
```

## Configuration

`.orbit.toml` at the repo root names the current sprint milestone and the
backlog. `orbit` discovers it through `git rev-parse --show-toplevel`, so
commands work from any subdirectory.

`$XDG_CONFIG_HOME/orbatch/snippets.toml` (by default
`~/.config/orbatch/snippets.toml`) lists the checkouts a `snippets` rollup
covers. Each entry needs a `path`; the GitHub slug comes from that
checkout's `origin` remote. A repo reports deploy counts only if it names
the Grafana annotation tag to count.

```toml
[[repo]]
path = "~/Source/vpink/orbatch"

[[repo]]
path = "~/Source/vpink/pinky"
deploy_tag = "deploy"
```

With no such file, `snippets` reports on the surrounding checkout alone.
`snippets --repo PATH` (repeatable) replaces the configured list.
