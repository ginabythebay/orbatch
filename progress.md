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
