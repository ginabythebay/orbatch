## pr-review guidance

If the review raised valid issues that you consider out of scope for
the current PR, create a separate GitHub issue for **each** item.

### Scope decision rule

Before deferring a review finding to a new issue, apply this test:

> If this PR merges into main as-is, and main is immediately deployed
> to production, could this finding cause a production issue?

- **If yes (or likely yes):** the finding is **in scope** — fix it
  before the PR merges. If fixing it makes the PR too large, ask the
  user how to proceed. One option: set this PR aside, land a
  preparatory change that makes the fix safe, then resume.
- **If the PR makes documentation stale:** the documentation update
  is **in scope** — fix it in this PR. Stale docs are a production
  issue: the next person (or LLM) who reads them will do the wrong
  thing.
- **If the finding is about code introduced in this PR:** the finding
  is **in scope** — fix it before the PR merges. Findings about new
  code are always in scope unless the fix would significantly expand
  the PR (e.g. more than 20 percent).
- **If no:** the finding is **out of scope** — create an issue per
  the guidance below.

**Principle:** main must always be ready to deploy to production.

Each issue must contain enough context for an LLM to address it
without human input:
- Any available context for the review (e.g. issue numbers, pr number) that triggered this.
- The file path(s) and relevant code snippet(s)
- A clear description of the problem or improvement
- Why it matters (correctness, performance, maintainability, etc.)
- Suggested approach or acceptance criteria

When creating issues, attach them as sibling sub-issues of the issue
associated with this change (if that issue has an epic parent). Assign
new issues to the current milestone. If there is no epic context,
leave the issue without a milestone — it will surface during triage.
