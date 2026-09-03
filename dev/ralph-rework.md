@progress.md
1. You are reworking the open pull request for github issue #{{ISSUE}}. Its
   branch is already checked out in this worktree.
2. Read the PR: `gh pr view --json number,title,body,url` for the branch, then
   its discussion and every review thread. Unresolved threads are the work
   list — fetch them with
   `gh api graphql` on `reviewThreads` (isResolved, isOutdated, path, line,
   comments) rather than `gh pr view --comments`, which omits them.
3. Read github issue #{{ISSUE}} for the original scope, and the diff so far:
   `git diff origin/{{BASE}}...HEAD`.
4. Ask the developer, who is attached to this console, about anything
   ambiguous before changing code. Steering this session conversationally is
   the point of it — do not guess at what a review comment wants.
5. Address the feedback. Follow the repository's own conventions (CLAUDE.md),
   and keep the change inside the scope of the PR and the
   issue: work the review asks for that belongs elsewhere becomes its own
   issue, not a bigger diff here.
6. Run the feedback loops before you report anything as done: `uv run pytest
   --slow` and `dev/lint`. Fix every failure and warning.
7. **Land the work as new commits on this branch and push them.**
   Never rewrite history: no `commit --amend`, no `rebase`, no `reset`, no
   `push --force`. Descendant branches are cut from this branch's tip, and
   rewriting it invalidates every one of them.
8. Reconcile with `origin/{{BASE}}` by merging it in — never by rebasing onto
   it — if the branch has fallen behind.
9. Reply on each thread you addressed, saying what changed, then report a
   short summary to the developer. Do not close the issue and do not merge
   the PR.
