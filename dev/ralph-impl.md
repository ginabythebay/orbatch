@progress.md
1. Work on github issue #{{ISSUE}}.
2. Read the test plan from the issue body of issue #{{ISSUE}} (under the "## Test Plan" section). Implement the tests and feature following that plan. Use the /tdd skill's incremental loop: one test at a time, minimal code to pass, then next test.
 - Earlier issues in this stack: {{PREDECESSORS}}. Read the "## Test Plan" section of each and assume their changes already exist in your branch.
3. Check any feedback loops, including
 - `uv run pytest --slow`
 - `dev/lint`
 - The 'review-diff' script. It lives in this workspace, so run it as
   `uv run review-diff`. It spawns three fresh headless claude
   sessions (correctness, tests, conventions), each with no shared
   context with you, then a fourth that merges their reports into one
   deduplicated list. The report leads with that merged '## Findings'
   list — the authoritative one — and keeps the raw per-lens reports
   below it under '## Per-lens reports' for attribution. Read findings
   off the merged list; the same defect reappears below it.
   - Run it EXACTLY ONCE per diff state. Do not re-run it to "get
     results" — re-run only after the code has actually changed.
{{#INTERACTIVE}}
   - Launch it in the BACKGROUND with -q (i.e. use run_in_background),
     then WAIT. A foreground run can hit the shell timeout, which must
     NOT be read as failure. Do not launch a second run while one is
     in flight.
{{/INTERACTIVE}}
{{#HEADLESS}}
   - A PreToolUse hook denies run_in_background here, so detach the
     run from the shell yourself and name the output file:
         setsid uv run review-diff -q -i {{ISSUE}} origin/{{BASE}}...HEAD \
           > /tmp/review.out 2>&1 < /dev/null &
     then block in the FOREGROUND until the report lands there.
     Poll the file, never a shell variable — a pid in `$pid` is
     gone by your next Bash call,
     and an `until ! kill -0 $pid` on an empty variable exits at once
     and reads as "finished":
         until grep -q '^## Per-lens reports' /tmp/review.out; do sleep 30; done
     A poll that hits the shell timeout means the review is still
     running — NOT that it failed, NOT that it is done. Run the
     identical poll again, as many times as it takes; a review runs
     10+ minutes.
     Never end your turn waiting for a background task, here or
     anywhere else: nothing will re-invoke you, the session ends, and
     the work is lost unpushed. If the output file stops growing and
     the heading never appears, read it — the review failed and the
     reason is in it. Do not launch a second run while one is in
     flight.
{{/HEADLESS}}
   - To re-read the last report without reviewing again:
     `uv run review-diff --markdown`.
   - Invocation (always -q, never in the foreground):
         uv run review-diff -q -i {{ISSUE}} origin/{{BASE}}...HEAD
     Omit -i to withhold the issue body and get a colder read. Other
     targets work too: `HEAD` for uncommitted work, any git range, or
     a pull request as `123`, `owner/repo#123`, or its URL — a PR is
     reviewed in a detached worktree at its head, leaving your
     checkout alone.
   - Judge each finding yourself — the reviewers are fresh but
     fallible, and a finding you can refute from the code should be
     declined, not fixed. Apply .claude/pr-review-guidance.md to decide
     scope.
   - Commit before you run the review, so the fixes the review
     produces show up as their own commits. Do NOT create a PR yet —
     that is step 6, after the review is settled.
   - After deciding each disposition, write a review section to a file
     for step 6 to place in the PR body. Do not post it anywhere now.
     It contains, in order:
     1. A findings → disposition table — one row per merged finding
        (from '## Findings', not per-lens), with
        columns: finding (in the reviewer's own framing), disposition
        (fixed / declined / no-change), and reasoning. For declined /
        no-change items, state the reviewer's concern in their terms
        first, then the reason — so a human can overrule. When a
        finding was filed as a `found in review` issue, link that
        issue number in its row.
     2. The full raw report under a `## Raw review output` heading,
        inline and unabridged. Do not collapse it behind a
        `<details>` block, do not abridge or paraphrase it, and do not
        link to it instead of including it. This is in addition to the
        table above, never a replacement for it.
   - If this review finds problems that you think are out of scope,
     create issues for them, in the current sprint, tagged with the
     'found in review' label. Give them the same epic as the issue in
     item 1.
Iterate until all tests pass and there are no lint warnings or errors and nothing significant raised by review.
4. Append your progress to the progress.md file.
  - task completed and github issue link
  - key decisions made and reasoning
  - files changed
  - issues raised during review and what you did to address them
  - any blockers or notes for next iteration
  - if progress.md is over 1500 lines trim the front (older) part away
    so that it is 1300 lines or less.  Keep entries concise.  Sacrifice
    grammar for brevity. This file helps future iterations.
5. You have been started with the correct git branch. Do not change it unless the user asks you to.
6. Use 'git commit' to commit the change, including the change to
   progress.md.  Reconcile with your base branch, origin/{{BASE}}:
   check for merge conflicts against it and attempt to resolve those.
   If successful, push the change using git, then create the PR. Build
   its body in a file: a line like 'Fixes #{{ISSUE}}', a short summary
   of the change, a '## Caveats' section, then the review section from
   step 3 verbatim. Pass that file on stdin —
   `.claude/tools/create-pr.sh "title" --base {{BASE}} < body.md`. The
   review belongs in the PR body itself, not in a comment. Check each
   step succeeds before proceeding.
   - The '## Caveats' section is what you could not verify and why,
     assumptions the change rests on, and work you deliberately left
     undone — what a reviewer would want to know before merging, not a
     restatement of the summary.
     Write 'None.' under the heading when there genuinely is nothing;
     create-pr.sh refuses a body with no '## Caveats' heading.
   - 'Fixes #{{ISSUE}}' is the only bare issue reference the body may
     contain. Backtick every other issue number, in your summary and
     in the review section alike — GitHub reads 'fix: #123' in any
     prose as a closing keyword and closes that issue on merge, and a
     disposition table is full of such phrasings.
7. Do not close the issue (when the PR is merged, github will close the issue)
