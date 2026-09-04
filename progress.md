# Progress

Agent iterations append here: issue link, key decisions, files changed,
review findings and their dispositions, blockers for the next iteration.
## 2026-09-03 — issue #2 teardown must not force-delete unpushed branches

https://github.com/ginabythebay/orbatch/issues/2

Decisions:
- Took the issue's stated design: `Teardown._clean` drops `force=True` and
  catches `UnsafeRemovalError` into a skip, like `Reclaimer._reclaim`.
  `TeardownSkip.UNPUSHED_COMMITS` already existed; no new member.
- Dirty check left `Teardown._refuse` — `StackManager._refuse_if_unsafe`
  covers it once removal is unforced. `_refuse` keeps only NOT_MERGED and
  VM_LIVE; `Slots.dirty` left teardown's protocol (reclaim's `Slots` still
  has it).
- `FakeStack.remove` now shares `_refuse_if_unsafe` with `remove_branch`,
  guarding before it records to `removed`/`journal`, so the skip test's
  `journal == []` is meaningful.

Files: tools/batch/src/batch/teardown.py,
tools/batch/src/batch/testing/payloads.py,
tools/batch/tests/teardown_test.py,
tools/batch/tests/orchestrator_test.py (journal string "remove #10 forced"
-> "remove #10").

Review: one merged finding, declined and filed as `#9` — `unpushed()` is
git-local, so a squash-merging repo with pruned remote refs would now skip
every merged issue forever and never reclaim a slot. Doesn't bite this repo
(merge commits). Declined because the fix lives in the shared
`StackManager._refuse_if_unsafe` and reverses `#2`'s explicit design; the
reviewer's proposed `git diff --quiet base branch` remedy is also unsound
(fails as soon as another commit lands on the base). Conventions lens: no
findings.

Notes for next iteration: `#9` is the real follow-up. `gh label create
"found in review"` was needed — the label did not exist. This repo has no
milestones at all, so the new issue got none despite `.orbit.toml`
`current = "import"`.

## 2026-09-03 — issue #3 escape issue titles in print_batch_table

https://github.com/ginabythebay/orbatch/issues/3

Decisions:
- One-line fix: `escape(issue.title)` in `print_batch_table`, matching
  `_issue_row`. Not `markup=False` on the Console — the adjacent state cell
  relies on markup for its color.
- Audit confirmed (correctness lens agreed): that call site is the only
  GitHub-sourced string reaching a rich renderer in `tools/batch/src`.
  Everything else writes to the raw `TextIO` or goes through `Text`.
- Test plan case 4 (table/dashboard agree) written as a real comparison via
  a `_title_cell` helper, not two substring checks — see review below.

Files: tools/batch/src/batch/text_output.py,
tools/batch/tests/text_output_test.py (new `TestBatchTableTitleMarkup`,
4 cases + `_title_cell`).

Review: two merged findings, both minor, both fixed. (1) case 4 duplicated
existing coverage instead of comparing the two paths — now extracts and
compares the title cells. (2) case 1 asserted only `[/tmp]`, not the whole
title — now asserts the full string. Correctness lens: no findings.

Notes for next iteration: `#9` (from `#2`'s review) is still the open
follow-up. `_title_cell` splits the first rendered line on the state word;
it works because both renders put the title last and elapsed defaults to "".

## 2026-09-03 — issue #4 foreign-repo closing keywords must not verify

https://github.com/ginabythebay/orbatch/issues/4

Decisions:
- Took the issue's design: `_REFERENCE` captures the `owner/repo` prefix and
  `closing_references(body, slug)` drops any reference whose slug differs
  (case-insensitively). Filter lives in `body.py`, so no call site can skip it.
- Slug comes from `BatchGitHub.repo` (`f"{owner}/{name}"`), built in
  `fetch_pull_requests` and passed to `_to_pull_request` — the repo the query
  actually ran against, not `BatchConfig.slug`.
- `body_test.py` gets a local `SLUG = "acme/widgets"` rather than importing
  `payloads.TEST_SLUG`: that file imports nothing from `batch.testing` today.
- Payload builders `pull_request`/`pull_requests` already existed; case 6
  needed no new fixture.

Files: tools/batch/src/batch/body.py,
tools/batch/src/batch/github/client.py,
tools/batch/tests/body_test.py (3 new cases + slug arg at 10 call sites),
tools/batch/tests/client_test.py (new `TestClosingReferences`).

Review: one merged finding, all three lenses, fixed — the client test only
asserted the negative (`closes == ()` for a foreign slug), which passes for
any wrong slug incl. transposed `widgets/acme`. Now asserts `[(), (9,)]` over
two nodes; confirmed by mutating the slug order (test fails) and restoring.

Notes for next iteration: `#9` (from `#2`'s review) still open. Bare `#n` is
still slug-agnostic by design, so client-level tests using bare references
pin nothing about the slug — the qualified-body case is the only guard.

## 2026-09-03 — issue #5 _watch_passes must not stack _QuietRepeats

https://github.com/ginabythebay/orbatch/issues/5

Decisions:
- Took the issue's design: `_watch_passes` saves `orchestrator.report` before
  wrapping and restores it in a `finally`, so wrapper depth stays at one on
  the normal return and on the KeyboardInterrupt -> SystemExit(130) route.
- Tests drive `_watch_passes` directly (private import with a one-line pyright
  ignore, not a file-wide `reportPrivateUsage=false`).
- Fixture for a line that repeats: a closed-unmerged `#9` in `FakeState.closed`
  makes every sweep report "#9 left alone (not-merged)". `run()` sweeps twice
  per pass, so one call exercises the dedupe and two calls the stacking bug.
- Case 5 needs `show` to fire, and `watch()` only calls it when the pass has
  outcomes — hence the extra `batch_issue(10)` alongside the queued target.
- Interrupt injected via `FakeState.on_fetch`, which raises inside `state.batch`
  under the `try`, so a trailing restore statement would not pass.

Files: tools/batch/src/batch/cli.py,
tools/batch/tests/cli_test.py (new `TestWatchPassesWrapper`, 5 cases).

Review: one merged finding (conventions lens) — the two
`orchestrator.report == said.append` assertions mirror the diff; drop them.
Declined the deletion: those are cases 2 and 3 of the issue's own test plan.
Fixed the real half — the interrupt case now drives a second pass afterwards
and asserts the refusal line is narrated again (verified red without the
`finally`). Correctness and tests lenses: no findings.

Notes for next iteration: `#9` (from `#2`'s review) still open. Pre-existing
and unfixed: `watch()` only calls `report` on passes with outcomes, so during
a long idle streak `_QuietRepeats` never resets and refusals are said once for
the whole streak, not once per pass as its docstring claims.

## 2026-09-03 — issue #6 share source_with_alias via packages/shellcomp

https://github.com/ginabythebay/orbatch/issues/6

Decisions:
- Took the issue's design decision: new workspace member `packages/shellcomp`
  (click only), not `ghgql` — ghgql has no click dep and is named for GraphQL.
- `git mv` of batch's copy (byte-identical to orbit's) so history follows;
  orbit's deleted. Both `cli.py` import `shellcomp.completion`.
- Wiring: root `dependencies`, `[tool.uv.sources]`, `testpaths`, plus a
  `shellcomp` dep in both tools' pyproject. `console_scripts_test.py` picked
  the member up unchanged, as the issue predicted.
- Both packages' existing `completion_test.py` left untouched (they are
  end-to-end through `__main__`, not duplicates) and pass.
- Test 1 asserts the two `complete` lines by last word; click nests the
  prog-name one inside `_widget_completion_setup()`, so lines are stripped.

Files: packages/shellcomp/{pyproject.toml,src/shellcomp/{__init__.py,
completion.py,py.typed},tests/completion_test.py}, pyproject.toml, uv.lock,
tools/{batch,orbit}/pyproject.toml, tools/{batch/src/batch,orbit/src/orbit}/
cli.py, tools/batch/tests/portability_test.py, README.md, CLAUDE.md.

Review: four merged findings, all fixed. (1) no assertion that the emitted
script carries the requested complete var — added. (2,3) CLAUDE.md still said
"five packages" and README's table omitted the member — both updated. (4) the
move dropped the module from batch's portability guard — that test now sweeps
`batch` and `shellcomp` roots, parametrized over (root, source) pairs.

Notes for next iteration: `#9` (from `#2`'s review) still open. Anything that
`batch.cli` calls but does not live under `tools/batch/src` needs adding to
`portability_test._SOURCES` by hand — there is no automatic sweep of deps.

## 2026-09-03 — issue #7 scrub host-repo names, widen the portability guard

https://github.com/ginabythebay/orbatch/issues/7

Decisions:
- New workspace member `packages/portability`: `names.py` holds the two
  host-repo names base64-encoded plus their published SHA-256 `DIGESTS`, and
  `tests/portability_test.py` is the workspace guard. A member, not a root
  `tests/` module, so `tools/review/tests` can import it by declared
  dependency rather than by path (CLAUDE.md Architecture).
- `tools/batch/tests/portability_test.py` deleted. **`_SOURCES` no longer
  exists**: the sweep is automatic over `git ls-files --cached --others
  --exclude-standard`, so a new package or file needs no hand-registration.
  (Supersedes the note at the end of the #6 entry.)
- Names matched by substring on the lowercased text, not by token. The issue
  designed a `[^a-z0-9-]+` tokenizer; review showed that regressed both
  guards it replaces (`<name>-tools`, `v<name>` escape it). See review below.
- `DIGESTS` kept and pinned in `names_test.py` to the digests the issue
  published, so the encoded literals cannot drift from the real names.
- `dev/` half kept but scoped by an exemption list: orbit, review and
  snippets still carry `dev/` PROG_NAMEs (user-visible), filed as `#15`.
- Fixtures renamed to `widget`; `orbatch` < `widget` preserves the two
  ordering assertions at snippets `cli_test.py:589` and `:750`.
- Review's `_REPO_SPECIFIC` drops its literal entry and routes through
  `forbidden_words` via a shared `_repo_specific` helper, tested by planting
  a name in a template text.

Files: packages/portability/** (new), pyproject.toml, uv.lock, README.md,
CLAUDE.md, tools/review/{pyproject.toml,tests/cli_test.py},
tools/snippets/tests/{cli_test.py,config_test.py},
tools/batch/tests/portability_test.py (deleted).

Review: 8 merged findings, 7 fixed, 1 declined (store digests only, no
recoverable form — declined because test-plan item 3 needs a real name to
plant, and the reviewer's test-local-digest alternative is the very hole
item 3 closes). Full table in the PR body.

Notes for next iteration: `#9` and `#15` are the open follow-ups. Issue #7's
"Final step" asks that the issue be replaced by a closed copy and then
`gh issue delete 7` — **after** the PR merges and batch's teardown sweep
finishes. Not done here; left to whoever merges, and flagged in the PR
caveats. progress.md is scanned by the new guard, so never spell the names
here.
