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

## 2026-09-04 — issue #30 wire the basedpyright guardrails

https://github.com/ginabythebay/orbatch/issues/30

Decisions:
- Three new/edited files, no code: `.claude/hooks/no-bare-basedpyright.sh`
  (PreToolUse, jq + `grep -E`, exit 2 on a match), `.claude/settings.json`
  wiring only that hook under `matcher: "Bash"` with
  `"if": "Bash(*basedpyright*)"`, and `.claude/pyright-guidance.md` now
  tracked and rewritten for this repo.
- Guidance rewrite: dropped the `.basedpyright/` baseline paragraph, the
  "only new or modified code" claim (replaced with "dev/lint is at 0/0/0 and
  must stay there"), the FastAPI bullet, the justfile, and — after review —
  PyCharm from the list of things that go through the wrapper. PyCharm reads
  `pyproject.toml` and cannot be handed an interpreter, which the same file
  says correctly two paragraphs down. Kept the strategy ladder, the
  `# pyright: ignore` ban with its `reportPrivateUsage=false`-in-tests
  exception, and the do-not-symlink-`.venv` warning. `patch.object` and
  "never `_ = some_param`" are linked to `CLAUDE.md`, not duplicated.
- `CLAUDE.md` gets a two-line pointer after the commands block. Also fixed
  the false half of the adjacent comment: bare basedpyright resolves against
  the `.venv` named in `pyproject.toml`, not "whatever interpreter is on
  PATH". Contradicting the file being linked, one line away, was worse than
  the scope creep.
- Regex matches the bare word, NOT a prefix: `(^|[[:space:]/])basedpyright`.
  Modelling the prefix (`uv run …`) was the first cut and review killed it —
  `env -u VIRTUAL_ENV uv run basedpyright`, the form `dev/lint` itself uses,
  walked straight past it, as did `uvx` and `.venv/bin/`. Cost of the wider
  match: a bare mention (`uv run pytest -k basedpyright`) is denied too.
  Deliberate — a rephrase costs one call, a bypass costs the guardrail.
  The hook's own path is not self-denying (`-` before, `.` after).
- `no-background.sh` left unwired, per the issue.

Files: .claude/hooks/no-bare-basedpyright.sh (new, 100755),
.claude/settings.json (new), .claude/pyright-guidance.md (new — it was
untracked before), CLAUDE.md, progress.md.

Review: three findings, two fixed (the regex, the PyCharm claim), one
declined and filed as `#34` — no automated test for the matcher, which the
no-new-tests constraint on this iteration forbade. `#34` carries the
reviewer's parametrized-table shape, the deny/allow rows, and the `-k` case
pinned as a deliberate deny.

Notes for next iteration: `#32` and `#34` are the open follow-ups. Anything
you run through Bash that merely NAMES basedpyright is denied now, including
`grep basedpyright dev/lint` — build the word from two pieces
(`B="based""pyright"`) when you need to inspect or test the hook. The
portability guard sweeps untracked files too, so never let the extraction
repo's name back into `.claude/` — that is why the guidance file names no
source for what it was adapted from.

## 2026-09-07 — issue #39 shared BatchLabel + orbit glyph column

https://github.com/ginabythebay/orbatch/issues/39

Decisions:
- `packages/ghgql/src/ghgql/labels.py` holds `BatchLabel`, `batch_labels`,
  `glyph`, and the two sentinels `NO_LABEL = " "` / `CONFLICT = "!"`. Full
  move out of `batch.models`, no re-export shim — every batch import now
  reads `from ghgql.labels import BatchLabel`. `ConflictingLabelsError`
  stayed in `batch.models`; `state._batch_labels` is a one-line delegation.
- `Palette` moved to `orbit/palette.py` and gained `WARNING`, plus
  `glyph_span(labels) -> (mark, style)`. Both renderers call it, so the
  CLI/TUI agreement of test-plan item 14 is structural, not just asserted.
- Glyph is its own cell: `issue_text` appends `mark` then `" #{number}"`;
  `text_output` adds a `width=1` first column to the issue, epic and
  sub-issue tables. `filtered_text` / FilteredRun rows emit the blank one
  so runs stay aligned.
- Queries widened as the issue's planning note says: `_LIST_EPICS_QUERY`,
  `_SUB_ISSUES_QUERY`, `_SEARCH_ISSUES_QUERY`. All four orbit label fetches
  now ask for `first: 100`, matching batch (round 2 finding).
- `tui_test._label` strips the blank glyph column so the existing structural
  assertions stay readable; `TestBatchGlyphsReachEverySurface` uses its own
  labelled fixtures rather than mutating the shared ones (which ~20 tests
  assert on verbatim).
- Help legend gained a `batch state` section built from `BatchLabel` +
  `glyph_span`, so the letters and the WARNING styling are documented in
  app and cannot drift.

Files: packages/ghgql/src/ghgql/labels.py + tests/labels_test.py (new),
tools/orbit/src/orbit/{palette.py (new),text_output.py,tree.py,
github/{client,models}.py,tui/{widgets,screens}.py}, orbit tests
{widgets_test.py (new),client,text_output,tree,tui,cli}_test.py,
tools/batch/** (import move only, plus state.py delegation),
README.md, CLAUDE.md.

Review: two rounds, eleven findings, all fixed. Round 1 found a real bug —
the move-to-epic picker was the one `issue_text` call site left unlabelled —
plus four untested wirings, the undocumented legend, and a docstring that
restated its signature. Round 2 found that the three widened queries were
pinned only by fake payloads (FakeTransport ignores the query text), the
20-vs-100 label page size disagreement with batch, and that round 1's
`patch.object` delegation test mirrored the diff; replaced with a semantic
assertion on a mixed tuple.

Notes for next iteration: `#32` and `#34` remain the open follow-ups.
`print_parent_issue` (`orbit parent`) is the one listing with no glyph
column — single row, nothing to align against. A batch label past the
100th on an issue still renders blank.

## 2026-09-07 — issue #40 dired-style marks in the orbit TUI

https://github.com/ginabythebay/orbatch/issues/40

Decisions:
- `orbit/marks.py` holds `Marks` (`toggle`/`unmark`/`clear`/`count`/
  `__contains__` over a `set[int]`). One instance on the app, injected
  into `IssueTree` and both `IssueList`s, so marks outlive `r` and
  `e`/`c`/`b`. `U` clears globally.
- `issue_text(..., marked=)` prepends a `MARK = "*"` column (Palette.KEY)
  ahead of the batch glyph; `filtered_text` reserves both columns blank.
  Every existing prefix assertion moved one column right (`_label` in
  tui_test strips three chars now; help legend `index("#") == 3`).
- `space` is bound on BOTH `OrbitApp.BINDINGS` and `IssueTree.BINDINGS`
  as `app.mark_toggle`. Textual's `Tree` binds space to `toggle_node`
  and the focused widget wins; the `app.` prefix is what routes it up —
  a bare name shadows and fires nothing. The app binding must stay
  because `reserved_keys()` reads only the app's list. Verified red by
  removing the widget binding.
- Targeted re-render: `IssueNodeData` now carries `labels`/`open_count`/
  `total_count` and a `label(marked)` method, so `TreeNode.set_label`
  rebuilds a row without a fetch. `IssueList` keeps `{number: Issue}`
  and uses `replace_option_prompt_at_index`, walking the visible options
  (closed issues hidden by `f` have no option — walking `_issues`
  crashed on `U`, review round 1).
- Mark changes re-render EVERY view widget, not just the visible one:
  `g` can show the tree again via `_land_on`/`reveal` without a reload,
  which left a stale `*` (review round 1).
- `IssueList.advance()` is a no-wrap cursor-down; `OptionList.
  action_cursor_down` wraps, which would run marks in a circle. Tree's
  `action_cursor_down` already clamps.
- Status bar gained a `#mark-count` Static ("N marked", blank at 0);
  the load message keeps overwriting `#status-message`, so a separate
  cell is what survives a refresh.
- Placeholder rows: `selected_issue_number` is already None there, so
  `space` marks nothing and still advances.
- `mark_toggle`/`unmark`/`unmark_all` are in `_MAIN_SCREEN_ACTIONS`.

Files: tools/orbit/src/orbit/marks.py (new),
tools/orbit/src/orbit/tui/{app,widgets,screens}.py,
tools/orbit/tests/{marks_test.py (new),widgets_test.py,tui_test.py},
tools/orbit/docs/tui-design.md.

Review: two rounds, twelve findings, all fixed. Round 1: two real bugs
(the `U` crash with hidden closed rows in a list; stale glyphs after
`g`), three coverage gaps (u/U in a list, modal blocking, advance over
a placeholder), four signature-restating docstrings, this entry. Round
2: `IssueTree.refresh_mark` went through `_find_node` and relabelled
only the first node for a number — an epic that is also a sub-issue
has two rows; now walks every match. Plus the untested space-in-list ->
`g` path, a test name still calling the batch glyph "first column", and
two stale lines in tui-design.md. Round 1's raw per-lens output was
lost with a session restart; the PR body carries its merged findings
and round 2 in full.

Session note: base changed mid-task from `origin/issue-39` to
`origin/main` (PR `#43` merged). SSH to GitHub has no key in this
environment — fetch/push over https works via the gh token.

Notes for next iteration: `#32` and `#34` remain open. The batch-verb
follow-up reads `OrbitApp._marks`; epics are marked by their own
number only (batch expands them). A widget other than the tree that
ever binds `u`/`U` itself needs the same `app.` shadow trick.

## 2026-09-08 — issue #41 batch verb menu + in-process run screen in orbit

https://github.com/ginabythebay/orbatch/issues/41

Decisions:
- `batch/runtime.py`: `Runtime(repo, config, run_root)` + `Runtime.load(repo)`
  (scopes the run root by slug, `.expanduser()` like the CLI). Holds
  `state/stack/runner/orchestrator/teardown/reclaimer/verbs/recovery`, the
  thin `queue/unqueue/approve/fast_track(targets)` wrappers, `drive(targets,
  report) -> Drive(orchestrator, verbs, run)`, `plan_session(...) ->
  PlanOutcome(command, returncode, refusal)`, and the moved `watch` +
  `_QuietRepeats`. `prog` is an attribute (Protocol member), not a property.
- CLI keeps its lazy `_runner`/`_resolve_state`/`_resolve_recovery` for the
  commands that must work without `batch.toml` (attach, vm *, skip,
  relaunch, queue…); `run/plan/cleanup/gc/rework/debug` go through
  `_runtime(ctx, root)`. Injected `ctx.obj` short-circuits unchanged.
  `plan` passes the CLI's own `_spawn` so a missing vibe is still a
  ClickException; `StaleSlotError` formatted in the CLI, so orbit's status
  line lacks the `gc` remedy for that one case.
- `Driving`/`Keying` protocols moved to `batch/dashboard.py` so runtime never
  imports the TUI. `FakeDriver`/`FakeVerbs` moved to
  `batch/testing/driving.py` (orbit's tests use them).
- `batch/tui/screen.py::DashboardScreen` is the whole dashboard;
  `DashboardApp` is a host via `get_default_screen`. `q`/`escape` →
  `action_close_screen`: pop when stack > 1, else `app.exit()`. Textual's
  `install_screen`/`uninstall_screen` stubs are `Screen[Unknown]` → typed
  through `install_on`/`uninstall_from` (cast on the attribute access, which
  is what silences reportUnknownMemberType; a cast around the call does not).
  `uninstall_from` also `remove()`s: uninstalling alone leaves the screen
  mounted with its 2s/30s timers polling GitHub (review round 1).
  `RunChanged` message from `_drive` and from `_tick` when the banner lands;
  timers keep ticking on a suspended screen, verified in textual 8.2.8.
- orbit: `Marks` is dict-backed now, `numbers` = marking order.
  `orbit/batching.py`: `Batching` Protocol (attrs `run_root`, `prog`;
  queue/unqueue/approve/fast_track/plan_session/drive), `BatchVerb` StrEnum,
  `load_batching(repo)` → None on ConfigError/CalledProcessError/OSError.
  `run_tui` loads it and prints the in-flight line after `app.run()` when
  `app.run_live`.
- `!` = Textual key `exclamation_mark`; `reserved_keys()` now also includes
  `key_display` so `"!"` is reserved. `d` returns to the run screen.
- Run: probe `run_lock` on the loop (held elsewhere → status, no screen),
  then the daemon thread's `drive` holds `run_lock` + `awake` for exactly the
  thread's life, so the lock releases on finish and re-acquires on a
  relaunch from the screen. Second `run` while live → status + switch.
  Finished screen is uninstalled+removed and replaced.
- `check_action`: run screen on top → only `show_epics/sprint/backlog`, each
  pops it first. Screen's own f/s/r shadow orbit's.
- Verb guards order: not-configured first, then "Nothing to <verb>". Every
  verb (plan/run included) clears marks + refreshes; a raising verb only
  reports (marks kept for retry).
- Status-bar wording = CLI wording via `text_output.queue_lines/approve_lines`
  → `VerbLines(said, warned).line` joined with "; ".

Files: tools/batch/src/batch/{runtime.py (new),cli.py,dashboard.py,
text_output.py,tui/{app.py,screen.py (new)},testing/{payloads.py,
driving.py (new)}}, tools/batch/tests/{runtime_test.py (new),cli_test.py,
tui_test.py,text_output_test.py,orchestrator_test.py},
tools/orbit/{pyproject.toml,src/orbit/{batching.py (new),marks.py,
tui/{app,screens,widgets}.py},tests/{batching_test.py (new),marks_test.py,
tui_test.py},docs/tui-design.md}, uv.lock.

Review: two rounds, eleven findings, all fixed. Round 1: unmounted replaced
screen (real bug — `uninstall_screen` leaves timers polling GitHub);
unguarded `run` failure path; two vacuous assertions; `load_batching`
untested and missing OSError; stale doc snippet. Round 2: `plan` let
`CalledProcessError` escape (StackManager runs git check=True) — now
`_VERB_FAILURES` on all three paths; `Runtime.drive` + the four label
wrappers untested; vacuous run-clears-marks assertion; `d` with no run
untested; orbit tests touched `$HOME` — orbit now has batch's `bogus_home`
conftest plus `GIT_CONFIG_GLOBAL/SYSTEM`. Round-2 fixes not re-reviewed.

Session notes: SSH to GitHub has no key here; fetch/push via
`git -c credential.helper='!gh auth git-credential' <verb> https://github.com/ginabythebay/orbatch.git …`.
Base moved mid-task from `origin/issue-40` to `origin/main` (PR `#49`
merged). `git checkout <file>` restores the INDEX — it silently dropped an
unstaged fix once; stage before mutation-testing.

Notes for next iteration: `#32`, `#34` open. Nothing verified against a
real VM boot or live GitHub. `Runtime.drive` builds the client on the event
loop (one `git remote get-url`). Orbit's `plan` uses `batch plan` defaults
(no model/ram). `#36`'s open-issue-from-run-row stories are follow-ups on
`DashboardScreen`.

## 2026-09-08 — issue #42 share issue label/body mutations through ghgql

https://github.com/ginabythebay/orbatch/issues/42

Decisions:
- `packages/ghgql/src/ghgql/issues.py`: `IssueMutations(graphql, repo)` +
  `LabelNode`/`LabelConnection`/`IssueCore`. `ghgql` gains a `pydantic` dep.
  Both clients hold one `IssueMutations` and delegate; public signatures
  unchanged, so `batch/state.py`, orbit's orchestrators and every
  `patch.object` call site are untouched.
- `label_ids(names)` builds one aliased query (`l0..lN`, positional because
  `ready-for-review` is not a valid GraphQL alias), reports every missing
  name in one `RuntimeError` ("Labels not found in repo: a, b"), and returns
  `{}` for no names without a query (an empty selection set is invalid GraphQL).
- `label_id(name, group=())` fills a per-instance cache. Ordering is
  **group first, then name** (`dict.fromkeys((*group, name))`) — with name
  first, `payloads.label_ids()`'s positional aliases would shift with whichever
  label was asked for. Batch passes `tuple(BatchLabel)`, keeping its one round
  trip; orbit's `fetch_label_id` passes no group and thereby gains the cache
  and batch's wording (nothing pinned the old `Label 'x' not found`).
- `_ChildNode(IssueCore)` in batch, `_IssueDetailRaw(IssueCore)` in orbit;
  orbit's `_LabelName`/`_LabelNodes` and its duplicate `_LabelNode`/
  `_LabelConnection` fold into the shared pair.
- `payloads.label_ids()` now emits `l0..l4` in `BatchLabel` order and ids
  `LA_<label name>`. Two pinned id literals changed (`LA_readyForReview` ->
  `LA_ready-for-review`) in state_test and recovery_test; no fixture call
  site changed.

Files: packages/ghgql/{pyproject.toml,src/ghgql/issues.py (new),
tests/issues_test.py (new)}, tools/batch/src/batch/{github/client.py,
testing/payloads.py}, tools/batch/tests/{client,state,recovery}_test.py,
tools/orbit/src/orbit/github/client.py, tools/orbit/tests/client_test.py,
README.md, CLAUDE.md, uv.lock.

Review: two findings. Fixed: the generated label query text was asserted
nowhere (only variables), so a broken interpolation stayed green — the test
now pins `$lN: String!` and `lN: label(name: $lN)`. Partly declined: the
reviewer would delete `TestIssueCore` as a diff mirror; kept (test-plan item
6, and the only direct exercise of the alias-carrying extension) but its
fixture's `closed_by` no longer masquerades as a `LabelConnection`.

Notes for next iteration: `#32`, `#34` remain open. `IssueMutations` owns
the label cache, so a client instance never sees a label renamed mid-run.
`fetch_targets`, orbit's milestone/period/sub-issue queries stay in their own
clients on purpose — the audit in `#42` found no other overlap.

## 2026-09-08 — issue #45 orbit TUI hides closed issues by default

https://github.com/ginabythebay/orbatch/issues/45

Decisions:
- Issue's design: `_hide_closed = True` set BEFORE the three view widgets are
  built and passed as `hide_closed=` to each ctor (new keyword on `IssueTree`
  and `IssueList`, defaulting to `False`), so first load and toggle state
  cannot disagree. One place to read the default.
- Test audit was the bulk of the work: 26 tests pressed `f` only to reach the
  filtered state — keypress dropped, assertions unchanged. 7 cursor-retarget
  tests press `f` to *transition* (navigate unfiltered, then hide, assert the
  cursor moved to the run that swallowed it); those gained a LEADING `f` so
  the sequence is off -> navigate -> on. 6 tests unrelated to the filter
  (`test_loads_epics_on_start`, refresh/standalone/goto) assumed the
  unfiltered tree and gained a leading `f`.
- `test_f_hides_closed_epics` -> `test_f_reveals_closed_issues` (the on->off
  direction is now the first press); `test_toggling_back_restores_the_full_tree`
  -> `test_toggling_back_hides_them_again`.
- `test_the_toggle_is_blocked_on_the_detail_screen` keeps its `f` on the
  detail screen; its final assertion inverted (still filtered) along with the
  default, so it stays red if the block breaks.
- `test_the_filter_toggle_round_trips_the_section` no longer pins literal
  labels — it captures the section at boot, asserts one press changes it and
  the second restores it. Default-agnostic; the literal contents are pinned by
  its two neighbours.
- Test-plan item 1 asked for `_hide_closed`/widget `hide_closed` assertions.
  Written through public widget attrs to avoid `reportPrivateUsage`, then
  dropped entirely after review — see below.
- Help text ("Toggle hide-closed") unchanged; still true as a toggle label.

Files: tools/orbit/src/orbit/tui/{app,widgets}.py,
tools/orbit/tests/tui_test.py, tools/orbit/docs/tui-design.md.

Review: one finding (conventions), fixed. The startup test asserted widget
internals, which `tui-design.md` bans for this file and which mirrors the
diff. Replaced with rendered-label assertions plus a new parametrized
`test_the_flat_views_start_with_closed_issues_hidden` that presses `c`/`b`
and pins each list's option ids (`[None, "41"]`). Verified red by flipping
the default back. Correctness lens: no findings. The tests lens and the
consolidation step both failed with empty stderr, so there was no merged
`## Findings` list this round.

Merge with origin/main (`#39`/`#40`/`#41`/`#42` landed meanwhile) needed the
same audit again for `TestMarks`: three mark tests assumed the unfiltered
tree and gained a leading `f`, two that pressed `f` to reach a placeholder row
dropped it, and `test_marks_survive_a_refresh` now compares
`mock_epics.call_count` against a count taken just before `r` — the toggle
itself refetches, so a literal `== 2` was default-dependent. `_section_labels`
routes through the new `_label` helper so the glyph column is stripped.

Notes for next iteration: `#32` and `#34` remain the open follow-ups. If the
tests lens keeps failing with empty stderr, that is worth its own issue — it
silently halves the review. Persisting the toggle across launches is
explicitly out of scope for `#45`.

## 2026-09-08 — issue #46 per-step model selection in batch.toml

https://github.com/ginabythebay/orbatch/issues/46

Decisions:
- Issue's design: `[models]` with `default`/`plan`/`implement`/`review`/`debug`,
  a frozen `Models` (all `str | None`) on `BatchConfig`, and
  `resolve(step, override) -> override or step or default`. `Step` is a
  `Literal` and `resolve` maps through a dict literal, not `getattr` —
  `getattr(self, step)` returns `Any` and basedpyright flags it.
- `_parse_models` mirrors `_parse_repo` but every key is optional: absent keys
  are skipped, empty/non-string ones are reported, unknown ones refused. An
  absent table yields `NO_MODELS` (module-level singleton; a `Models()` default
  argument trips ruff B008).
- Resolution is on the host at each call site, never in the guest: orchestrator
  `_drive` (implement/plan/review), `Debugger.enter` (debug, or `default` when
  `--fresh`), `verbs._launch` (rework -> implement/plan/review), `cli._session`
  (`default` with no issue, else implement/plan/review) and
  `runtime.plan_session` (plan, so orbit's `p` verb gets it too).
- `agent_command` emits `--plan-model`/`--review-model` only in the issue
  branch; a bare session is `claude` itself and takes `--model` alone.
  `_model_flag` gained a flag-name argument.
- `vwt` lost `DEFAULT_MODEL = "opus"` and now defaults `--model` to None (see
  review). The model is the repo's to configure; the flag is the override.
- New `payloads.TEST_CONFIG_TOML` so a CLI test can append a `[models]` table
  to the standard config; `batch_config(models=)` for the unit-level fakes.

Files: tools/batch/src/batch/{config,vm,orchestrator,verbs,cli,runtime,
worktree}.py, tools/batch/src/batch/testing/payloads.py, tests in
{config,vm,orchestrator,verbs,cli,worktree}_test.py, README.md.

Review: five findings, all fixed. The real one: `vwt` always passed
`--model opus`, so on that path `[models]` could never take effect AND a repo
with no `[models]` still got `--plan-model opus --review-model opus` — the
"byte-for-byte unchanged" invariant broken exactly where the user cannot opt
out. Also: no test distinguished `implement` from `default` (all three
call sites would have passed under a mutation — verified red after fixing);
`--model`'s help still said "implementation agents"; the bare-session
"no plan/review flags" contract was asserted where it could not fail; no
progress entry.

Notes for next iteration: `#32` and `#34` remain the open follow-ups. The two
new flags are a contract change for every target repo's agent script —
`dev/ralph` here does not accept them yet, which is harmless only while no
`batch.toml` in this repo carries a `[models]` table. `--model` on `run`,
`vm console` and `vwt` now overrides every step of that invocation, so there
is no way to override one step alone from the command line.

## 2026-09-08 — issue #47 inject .claude/*-guidance.md via PostToolUse hooks

https://github.com/ginabythebay/orbatch/issues/47

Decisions:
- Five `PostToolUse` / `matcher: "Bash"` hooks in `.claude/settings.json`,
  each the upstream one-liner: `jq -r '.tool_input.command'` -> `grep -qE` ->
  `jq -n --rawfile guidance .../<file>` emitting `additionalContext`, else
  `jq -n '{}'`. Issue's design decision: one-liners in JSON, not a script per
  hook, so they stay diffable against the copy they came from.
- Patterns ANCHORED with `(^|[[:space:]/])…([[:space:]]|$)` — the issue said
  "verbatim", but unanchored `lint`/`pytest`/`git commit` inject a whole file
  for any mention (`rg -n linting`, `cat .claude/pytest-guidance.md`), and
  `no-bare-basedpyright.sh` already anchors. `create-epic` ordered before
  `create` in the alternation.
- Issue hook fires for `gh issue (create|edit)` and
  `orbit (create-epic|create|set-body|move|reorder|schedule)`. `orbit edit`
  DROPPED (browser open only, 400 lines of guidance for a no-op); `move` /
  `reorder` / `schedule` ADDED — that is where issue-guidance's epic-ordering
  and keep-the-epic-open rules apply.
- `tests/claude_settings_test.py` (new, root `tests/`, not a workspace member —
  it tests this repo's own config): settings parses; every `.claude/…` path
  named in a hook command or in CLAUDE.md exists; every `*-guidance.md` is
  referenced by some hook. A hook whose `--rawfile` is missing emits `{}` and
  exits 0, so a rename is otherwise silent. Verified red by renaming
  `pytest-guidance.md`.
- CLAUDE.md: `## Current Sprint` gains the orbit-create paragraph + pointer to
  `.claude/issue-guidance.md`; `## Version Control` gains a one-line prefer-orbit
  sentence. Wording separates create (defaults to current milestone) from
  move/schedule/reorder — the first draft called all four creating commands.
- Hand-verified with an extracted-command harness, 26 cases (each hook's
  triggers + negatives). No Python drives the hooks; the harness is not
  committed.

Files: .claude/settings.json, CLAUDE.md, tests/claude_settings_test.py (new).

Review: five findings, four fixed (the CLAUDE.md command description, the
missing existence test, unanchored patterns, hook/doc list disagreement), one
declined and filed as `#55` — move the bodies into a shared
`.claude/hooks/inject-guidance.sh` and use `"if"` filters so a Bash call does
not spawn five pipelines; declined because `#47` ruled explicitly the other
way and the `"if"`-on-PostToolUse half is unverified.

Notes for next iteration: `#32`, `#34`, `#55` open. The hooks are live for any
session in this checkout — that is why so much guidance text appears in tool
output now. Out of scope and unfiled: the upstream `format-python.sh`
Write/Edit hooks.
