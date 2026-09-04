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
## 2026-09-04 — issue #9 teardown reclaims squash-landed slots

https://github.com/ginabythebay/orbatch/issues/9

Decisions:
- Issue's design: option 2's predicate (patch identity) through option 1's
  plumbing (merged verdict travels down). `Teardown` gains `base` (default
  `origin/main`, new `order.ORIGIN_MAIN`) and passes `merged_base=` to
  `StackManager.remove`. Reclaim and `batch stack remove` untouched: neither
  has a merged verdict to justify the loosening.
- `merged_base` NARROWS, never swaps: `_retains_work` refuses only when
  `unpushed(branch)` AND `patch_unique(...) is not False`. Review round 1
  caught the swap — it refused a branch merged into an open predecessor
  (commits on `origin/issue-N`) and one absent from a stale `origin/main`.
- `git cherry` alone is not enough: per-commit patch ids miss a squash of a
  multi-commit branch (this repo's own PR shape). `_landed_whole` compares the
  branch's aggregate patch id (`git diff fork..branch | git patch-id`) against
  the patch ids of `fork..base`.
- Porcelain output is pinned (`--no-ext-diff --no-color --pretty=medium`,
  `check=False`) — repo-agnostic tools inherit the user's git config, and
  `diff.external` would otherwise silently reinstate the leak.
- `patch_unique` is tri-state: False (nothing unique), True (carries its own),
  None (cannot compare — no base, no shared history) -> caller falls back to
  strict `unpushed`. Failure direction stays conservative.

Files: tools/batch/src/batch/{stack,teardown,order}.py,
tools/batch/src/batch/testing/{payloads,scratch}.py,
tools/batch/tests/{stack,teardown,reclaim,cli}_test.py. `Scratch` gained
commit_files/commit_file/push/land/merge/forget_origin/unpublish; `FakeStack`
gained a `patch_unique` set, `merged_bases`, and mirrors the real conjunction.

Review: two rounds, nine findings, all fixed (see PR body table). Round 1
found the swap-vs-narrow regression and the multi-commit squash gap — both
were real bugs in the first cut. Round 2 found the unpinned git output, the
fake diverging from the real predicate, and an untested `None` arm.

Notes for next iteration: no production code in `tools/batch` fetches, so
`origin/main` can be stale; the narrowing makes that harmless, but a sweep
still cannot see a merge that only exists upstream. If slots ever leak again,
check `unpushed` first — it is the gate patch identity only narrows.

## 2026-09-04 — issue #15 console-script program names

https://github.com/ginabythebay/orbatch/issues/15

Decisions:
- `PROG_NAME` for orbit / review-diff / review-html / snippets now the
  installed console-script names, per the issue: user-visible usage output
  and generated completions stop naming the extraction repo's `dev/`
  wrappers.
- orbit: `PROG_NAME == SCRIPT_NAME` made `source_with_alias` a permanent
  no-op, so orbit drops the call, `SCRIPT_NAME`, and — on this base, which
  predates `#6`'s `packages/shellcomp` — the whole `orbit/completion.py`
  module. batch keeps its own copy and its live alias case.
- `Bash(*dev/lint*)` -> `Bash(*lint*)` in review's DISALLOWED_TOOLS.
- Test plan item 8 assumed the snippets module docstring is click's epilog.
  It is not (bare `@click.command()`, no `epilog=`), so the docstring is
  asserted directly instead of through `--help`. Did not add `epilog=__doc__`
  — that changes help output beyond the rename.
- Test plan item 1 (drop `_COMMAND_PATH_EXEMPT`) NOT DONE — see blockers.

Files: tools/orbit/src/orbit/cli.py, tools/orbit/src/orbit/completion.py
(deleted), tools/review/src/review/{cli,html}.py,
tools/snippets/src/snippets/cli.py, tests in orbit/review/snippets,
tools/orbit/docs/tui-design.md.

Review: four findings. Fixed the vacuous snippets `dev/` assertion and a
false comment in orbit's completion fixture. Declined two: the missing
workspace sweep (blocked, filed as `#19`) and the `dev/`-absence assertion
in review's cli_test (today the only guard for that string; `#19` removes
it when the sweep lands).

Blockers / notes: this branch is on the `#2`->`#9` stack off main;
`packages/portability` and `_COMMAND_PATH_EXEMPT` live on the unmerged
`#3`->`#7` stack. So nothing sweeps `tools/*/src` for `dev/` here, and the
renames rest on hand-written usage-line assertions. `#19` tracks dropping
the exemption once both stacks land. Expect conflicts merging with `#7`:
it moves `orbit/completion.py` to `packages/shellcomp` while this deletes
it, and both touch `orbit/cli.py`'s import block.
## 2026-09-04 — issue #18 vwt as a repo-agnostic command

https://github.com/ginabythebay/orbatch/issues/18

Decisions:
- New `tools/batch/src/batch/worktree.py`, console script `vwt`. Setup and
  teardown go through `StackManager`; the boot goes through the configured
  `[commands] cli` as `vm console`, injected as a `Console` protocol so tests
  fake the boot. Branch is cut from `HEAD`, as the bash did; `--base` steers
  the agent, not the branch point.
- The spawn passes `--repo <relative worktree>` before `vm console` (the bash
  did too): cwd is the mount root, which is inside no checkout, so batch
  cannot find `batch.toml` without it.
- Fresh `TemporaryDirectory` per boot, removed with it — replaces the bash
  `mktemp -d` + trap that kept staged secrets from outliving the session.
- Teardown prompts on a dirty worktree as well as unpushed commits, then
  removes with `force=True`: the r/d/q loop is the safety check, and
  `_refuse_if_unsafe` would refuse the delete the user just confirmed while
  stranding the disk.
- `vwt` re-checks the option combinations `vm console` refuses
  (`--base`/GUIDANCE with no ISSUE, `-n`/`-g` with GUIDANCE) before creating a
  slot, and a test feeds `agent_flags` output through `batch vm console
  --dry-run` so the two surfaces cannot drift.
- `StackManager.unpushed` widened with `--exclude <branch> --branches`.
  Consequence beyond the issue's claim: `Reclaimer._unsafe`'s
  UNPUSHED_COMMITS can no longer fire (reclaim only reaches it once the branch
  is an ancestor of local `main`, which then holds every commit). Slots cut
  from unpushed work `main` still holds are now reclaimed — correct, but it
  rewrote `reclaim_test.py`'s unpushed test. Merging `#9` after the review
  brought a second one — `test_a_squash_landed_branch_is_left_alone`, whose
  local merge into `main` is what makes the commits safe — flipped the same
  way. Left the pre-check standing; filed `#22`.

Files: tools/batch/src/batch/worktree.py (new),
tools/batch/src/batch/stack.py, tools/batch/pyproject.toml,
tools/batch/tests/worktree_test.py (new), tools/batch/tests/stack_test.py,
tools/batch/tests/reclaim_test.py, tests/packaging/console_scripts_test.py,
README.md, CLAUDE.md, tools/orbit/docs/tui-design.md.

Review: nine merged findings, eight fixed (callee-rejected argv + missing
click-layer tests, missing `commands.cli` traceback, a no-op sibling branch in
a stack test, three docs, this entry), one declined and filed as `#22`.

Notes for next iteration: the extraction repo still ships `dev/vwt` and
`dev/vibe_ralph` — deleting them, adding `~/bin/vwt`, and updating its four
docs is the follow-up `#18` names. Nothing here is verified against a real
VM boot.

## 2026-09-04 — issue #19 drop _COMMAND_PATH_EXEMPT

https://github.com/ginabythebay/orbatch/issues/19

Decisions:
- `_COMMAND_PATH_EXEMPT` and the `startswith` filter gone; `_is_guarded_module`
  is now `.py` + `/src/`, so orbit/review/snippets are swept for `dev/`.
- Coverage assertion NOT the issue's five-named-paths shape, and not the
  design note's "roots among tracked .py files" either — that compares a set
  with itself (both sides filter on `/src/`), which round 1 of review caught.
  Expected roots come from the uv workspace member globs in the root
  `pyproject.toml` (dirs matching `packages/*`/`tools/*` that hold a
  `pyproject.toml`). Subset, not equality: `_workspace_members() -
  _src_roots(_MODULES) == set()`, so extra coverage outside a member is fine
  and a failure names the escaping package. Verified red by scaffolding a
  flat-layout `packages/newthing`.
- Offender logic extracted to `_command_path_lines(root, source)` mirroring
  `_host_repo_lines`; new planted-`dev/` test so the check cannot pass
  vacuously.
- Test 4 (test files stay unswept) is hermetic under `tmp_path` —
  `_is_guarded_module` now takes `root`. Not pinned to
  `tools/review/tests/cli_test.py`'s fixture text, which round 2 flagged.
- `assert "dev/" not in DISALLOWED_TOOLS` dropped from review's cli_test; the
  sweep covers it. `Bash(*lint*)` assertion kept.
- CLAUDE.md now states the `src/` layout requirement, so a flat-layout member
  failing the guard is legible rather than mysterious.

Files: packages/portability/tests/portability_test.py,
tools/review/tests/cli_test.py, CLAUDE.md, progress.md.

Review: two rounds, six findings, five fixed, one declined (restrict the
member set to dirs that already have `src/` — declined: a flat-layout member
escaping the sweep is exactly what this test exists to catch; documented the
convention instead).

Notes for next iteration: no exemption list remains anywhere in the guard. A
new workspace member must ship `src/` layout or the portability suite goes
red before its first module is even swept.

## 2026-09-04 — issue #21 repo-scoped run root

https://github.com/ginabythebay/orbatch/issues/21

Decisions:
- Issue's design taken: `scoped_run_root(base, slug)` -> `base/<owner>/<name>`,
  and `cli._run_root(ctx, run_root)` applies it only when
  `ctx.get_parameter_source("run_root") is ParameterSource.DEFAULT`. All 11
  run-root commands (8 option declarations; `_recovery_root_option` covers
  four) route through it.
- `disks_in_use` returns resolved `Path`s, not basenames; `status_branch`
  compares `(worktree_root / f"{branch}.raw").resolve()`. Whole-token rule kept.
- `stack.worktree_root(repo)` is the one derivation; `StackManager` defaults
  through it (its ctor kwarg renamed `worktree_root` -> `worktrees` to stop
  shadowing the module function) and `cli._runner` passes it to `VmRunner`.
- `VmRunner`'s worktree root is a **callable**, not a `Path` (review round 1).
  Eager resolution made `attach`/`vm status` demand a checkout even with an
  explicit `--run-root`, which the mount-root callers do not have. `_main_repo`
  is now cached on `ctx.meta` so a poll loop does not re-shell `git rev-parse`.
- Two behaviour losses stand: `batch vm status`/`batch status` of an *exited*
  VM from outside a checkout now refuse with "pass --repo". Proving EXITED
  means proving no live disk names this repo's slot, which needs the worktree
  root. Attaching over a live socket is unaffected.
- `status` resolves the root only under `-v`; a non-verbose `batch status`
  still runs without a readable `batch.toml`.

Files: tools/batch/src/batch/{vm,cli,stack}.py,
tools/batch/src/batch/testing/payloads.py, tests in
{vm,cli,stack,reclaim,lock,teardown,verbs,worktree,completion}_test.py.

Review: six findings, all fixed (table in the PR body). Round 1 caught the
eager-repo regression and three tests that mirrored the diff rather than
pinning wiring; the sweep now has one non-stubbed case driving `run` with the
flag omitted, verified red by mutating `scoped_run_root`.

Notes for next iteration: no migration for existing flat `~/.cache/batch`
contents — a run in flight across the upgrade is orphaned, not corrupted.
Whether one host can actually boot two repos' VMs at once is unverified; the
issue names an explicit slot budget as the separate follow-up if not.

## 2026-09-04 — issue #22 Reclaimer's unreachable unpushed pre-check

https://github.com/ginabythebay/orbatch/issues/22

Decisions:
- Took the issue's option 1 (delete), not option 2 (keep + pin with a stub
  stack): the check is dead against any `Reclaimer` whose `base` is a local
  branch, and `base` is `MAIN` at the sole production call site
  (`cli.py:978`) and in every test. A test pinning a branch that cannot be
  reached would pin the fake, not the code. The task also forbade new tests.
- `_unsafe` had one surviving check, so it folded into `_refuse` rather than
  staying a one-line indirection. `unpushed` dropped from reclaim's `Slots`
  protocol (structural; teardown has its own `Slots`, and `worktree.py:151`
  types against concrete `StackManager`), and from the two test fakes.
- The guard now lives ONLY in `StackManager._refuse_if_unsafe`, reached
  because `_reclaim` calls `remove_branch(branch)` unforced. If `Reclaimer.base`
  ever becomes a remote-tracking ref, the pre-check has to come back —
  commits no local head holds would then pass `merged_into`.

Files: tools/batch/src/batch/reclaim.py, tools/batch/tests/reclaim_test.py.

Review: two findings, both fixed. (1) nothing pinned the deferral to
`_refuse_if_unsafe` — `RacingStack.remove_branch` did `del force`, so flipping
`_reclaim` to `force=True` left the suite green. It now mirrors the real
manager (refuses unforced, succeeds forced); verified red under that mutation.
(2) this entry was missing. Correctness lens: no findings.

Notes for next iteration: `Reclaimer` reaches removal only for branches that
are ancestors of local `main`; every safety claim in reclaim rests on that.

## 2026-09-04 — issue #27 drop orbit's unused shellcomp dependency

https://github.com/ginabythebay/orbatch/issues/27

Decisions:
- One-line delete of `"shellcomp",` from `tools/orbit/pyproject.toml`;
  `uv.lock` drops the two edges. Root `pyproject.toml` keeps the member and
  the `[tool.uv.sources]` line — `batch` still imports it
  (`batch/cli.py:87`).
- The guard the issue asked about IS writable over the whole workspace: for
  each member, every `[project.dependencies]` entry naming another member must
  be imported somewhere under that member's dir (sweeping `tests/` too —
  `tools/review` imports `portability` only from its test). Scanned by hand:
  every member passes today. Not added here only because this iteration was
  run under a "do not add new tests" constraint. Filed as `#32`.
- Nothing else in the suite is dependency-edge sensitive: both
  `tests/packaging/console_scripts_test.py` and the portability sweep read
  `[tool.uv.workspace] members`, not deps.

Files: tools/orbit/pyproject.toml, uv.lock, progress.md.

Review: two findings. Missing progress entry — fixed (this). Missing
workspace-wide guard — declined for the no-new-tests constraint, filed as
`#32` with the reproduction and the test shape. Correctness lens: no
findings.

Notes for next iteration: `#32` is the open follow-up. Only the
unused-declaration direction is checkable — `uv sync` installs the whole
workspace, so a *missing* declaration never fails locally.
