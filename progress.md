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

Notes for next iteration: pinky still ships `dev/vwt` and `dev/vibe_ralph` —
deleting them, adding `~/bin/vwt`, and updating its four docs is the follow-up
`#18` names. Nothing here is verified against a real VM boot.
