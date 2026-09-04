## basedpyright guidance

`dev/lint` is clean across the whole workspace: 0 errors, 0 warnings,
0 notes. It must stay that way. If code is showing errors or
warnings, then it is not ready and needs to be fixed. "They were
preexisting" is not ok — there is nothing preexisting to inherit.
"It matches other code" is not ok either.

Strategies for pyright errors, in order of preference:
1. Fix the type at the source (add annotation, narrow a union, type
   a parameter)
2. Restructure code to avoid the pattern (typed wrapper, protocol,
   overload)
3. Use `cast()` when the type system genuinely can't express it
4. Suppress only for third-party stubs that are provably wrong

Common basedpyright patterns:
  - `json.loads` returns `Any`. Wrap with
    `cast(dict[str, object], json.loads(x))` or a typed helper to
    avoid `reportUnknownArgumentType` downstream.

`CLAUDE.md` carries the rest of the house rules that exist to keep
this checker quiet — `patch.object` over `MagicMock` in tests, and
never `_ = some_param` for an unused parameter. Read them there.

File-level `# pyright:` directives that disable rules and inline
`# pyright: ignore` comments are not acceptable for new code.
Exception: `# pyright: reportPrivateUsage=false` is fine in test
files, since tests legitimately need to poke at internal state.

Run lint with `dev/lint`, never `uv run basedpyright`. A PreToolUse
hook blocks the direct command, and CI goes through the wrapper.

The reason: basedpyright resolves imports against a venv, and this
checkout may be used from more than one environment at the same path,
so no single venv location is correct for all of them.
`pyproject.toml` names `.venv`, which is what PyCharm's language
server reads — it can only be configured through that file. `dev/lint`
asks uv which environment is actually live and, where that is not
`.venv`, redirects basedpyright to it with `--venvpath`. A bare run
where `.venv` is not the live environment reports every import as
"could not be resolved": an artifact of the wrong venv, not your code.

If you ever see that flood, you are not running `dev/lint`. Do not
try to fix it by creating, populating, or symlinking `.venv` — that
path belongs to another environment's own checkout, and writing to it
breaks that one while only appearing to help here.
