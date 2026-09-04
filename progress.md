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
