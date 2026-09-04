## Issue writing guidance

Issues are the primary way work is handed off to agents. An agent
picking up an issue has no conversation context — only the issue
body and the codebase. Write every issue so a capable agent can
resolve it without asking a human.

The human is not the audience. Issues are how work gets organised and
divided, and how agents brief each other; they are rarely read end to
end by a person. So anything needing a human decision — a risk, a
tradeoff, a recommendation to drop or defer the work — must be raised
in conversation too. An objection recorded only in an issue body or a
comment has not been raised.

### What to include

- Related issues or prs.
- **Why this work matters.** What problem does it solve, what broke,
  or what user-facing behavior changes? Link the parent issue if
  this is a sub-issue.
- **Current state.** Describe what exists today — file paths, current
  behavior, relevant recent changes. Don't assume the reader was in
  the conversation that motivated the issue.
- **Precise scope.** List every file that needs to change and what
  changes in each. Include line numbers when they help, but favor
  describing the change in terms of identifiers (function names,
  class names) since line numbers drift.
- **Patterns to follow.** If the codebase has a convention the agent
  should match (naming, error handling, test style), spell it out
  with a before/after example.
- **Out of scope.** Explicitly state what this issue does NOT cover,
  especially if the agent might reasonably assume it should.
- **Verification steps.** What commands to run, what to check. For
  this project that usually means `uv run pytest --slow` (the full
  suite) and `dev/lint` at minimum.

### What to avoid

- Vague summaries like "clean up logging" — say which files, which
  calls, what the target state looks like.
- Assuming context from the current conversation. The issue body is
  the entire brief.
- Implementation instructions that conflict with established
  codebase patterns. When in doubt, read the code first and describe
  the change in terms that match existing conventions.

### Issue size

Every leaf issue carries a line-of-code estimate in its title, in
parentheses at the front, behind the [AFK/HITL marker](#afk-and-hitl):

```
AFK (300) add feature foo
```

The number is total lines changed — production **and** test code
together, added plus modified. Estimate it before writing the title:
read the files the issue touches and count what actually has to
change. A guess that ignores the test burden is the usual way an
issue lands over budget.

**1000 is the ceiling.** An issue you estimate above 1000 lines gets
split into several smaller issues, each with its own estimate, ordered
per [Child order](#child-order-must-be-an-implementation-order). Prefer
splits along vertical slices — a working end-to-end change — over
horizontal ones like "all the models" then "all the handlers", which
leave the intermediate issues unverifiable.

If a piece genuinely cannot be split below 1000 — a generated file, a
mechanical rename that must land atomically, a migration that is one
indivisible step — **stop and ask the user** rather than filing the
oversized issue or splitting it into pieces that don't stand alone.

Epics do not get an estimate in their title; their size is the sum of
their children.

#### Revising an estimate

An estimate is a claim about the code, and the code moves. When you
read an issue's files and the number is wrong — the scope grew, a
dependency landed and shrank the work, the original guess ignored the
tests — **retitle the issue with the corrected number and tell the
user you did it.** Do not ask permission first. `orbit edit` or
`gh issue edit <n> --repo ginabythebay/orbatch --title '...'` both
do it.

The one case that still stops for the user is a revision that pushes
the issue above the 1000-line ceiling: retitle it with the honest
number, then raise the split, per the ceiling rule above.

### AFK and HITL

Every leaf issue title also declares whether it can be worked
unattended. The marker goes in front of the estimate:

```
AFK (300) add feature foo
HITL (120) batch: scope the run root per repository
```

- **AFK** — an agent can take the issue start to finish, verify it
  with `uv run pytest --slow` and `dev/lint`, and open a PR without a
  human in the loop.
- **HITL** — a human has to be present at some point. Mark an issue
  HITL when any of these hold:
  - it needs a manual smoke test — TUI behavior, a run against a live
    GitHub repo, anything the test suite cannot confirm;
  - it changes an interface that stacked PRs downstream build on, so
    review-driven rework would cascade;
  - the spec leaves a genuine design fork the agent should not pick
    alone;
  - it is irreversible or outward-facing — production data, a
    published release, anything visible outside the repo.

Riskiness alone is not the test. A large, self-contained change whose
tests prove it works is AFK; a small change to a shared signature that
three queued issues depend on is HITL.

Epics carry no marker, the same as estimates — their children do.

Revise a marker the same way you revise an estimate: when the facts
change, retitle and say so, without asking first.

When designating an issue as HITL in the title, describe the reason
for the designation in the issue body.

### Plan at creation time when you can

`orbit create` leaves a new issue unlabelled, and batch's
planning stage later writes the test plan and moves it to `planned`
(`batch approve`, or `fast-track` from unlabelled). That two-step
exists because the issue's author usually *doesn't* know enough to
plan it.

Often you do. If you just designed the change, read the code, and
know what the tests look like, write the whole brief — including the
test plan — at creation time and apply the `planned` label yourself.
Making a second agent rediscover what you already established wastes
a planning run and risks it landing somewhere else.

Plan at creation only when **both** hold:

- You can name every file that changes and what changes in each,
  without further exploration.
- You can write the test plan from what you already know — each test
  named, and what it proves.

If you would be guessing at either, leave the issue unlabelled and
let the planning stage do its job. A speculative test plan is worse
than none: `batch approve --guidance` refuses to write guidance
over an existing `## Test Plan`, so a bad one you wrote sticks.

When you do plan at creation, the body carries two extra sections
beyond the [normal contents](#what-to-include):

- `## Design decisions` — the forks you settled and why, one bullet
  each. Written so a reader doesn't reopen a closed question.
- `## Test Plan` — the tests to add or change, grouped by test file,
  each naming what it proves. Cover the failure modes, not just the
  happy path. Say when an existing test is replaced and by what.

See #19 and #15 for the shape.

This is independent of the AFK/HITL marker: a HITL issue can be fully
planned, and marking it `planned` says the implementation brief is
settled, not that a human is no longer needed.

Don't apply `planned` to an epic — the batch states are for leaf
issues or standalone issues, which are what get implemented.

### Sub-issues

When an issue has logical children, use GitHub's native sub-issues
feature (the "Sub-issues" section in the sidebar) rather than
markdown checklists. Native sub-issues give progress tracking,
filtering (`has:parent` / `no:parent`), and proper parent-child
navigation.

**Exception:** the sprint tracker issue. Its checklist is a flat
roster of everything in the sprint — making every sprint issue a
native sub-issue of the tracker adds no value.

### Child order must be an implementation order

The sub-issues of an epic are listed in the order they were attached,
and that order is a promise: someone picking up the epic must be able
to work top to bottom without hitting a blocker. Before attaching
children, sort them so each one only depends on children above it —
schema and migration before the code that reads the new column, a
shared helper before its callers, a feature before the command that
exposes it.

This applies when adding to an existing epic too. A new child does not
automatically belong at the end; if it is a prerequisite of children
already there, say so in the issue body and move it up the list.

`orbit reorder` moves a child within its epic. It takes exactly one
of `--after <sibling>`, `--before <sibling>`, or `--first`:

```
orbit reorder <number> --before <sibling>
orbit reorder <number> --first
```

`--first` on a child that is already first reports that and exits 0.
Reordering only repositions an already-linked child; keep using
`orbit move` to create the link in the first place.

When two children genuinely have no ordering constraint, put the HITL
one first; among children that share a marker, put the larger or
riskier one first. A HITL child stranded in the middle of a stack
stops an otherwise unattended run at the point where the user is least
likely to be watching — front-loading the ones that need a human
leaves the tail of the epic workable start to finish.

### Issue management with orbit

`orbit` is the **preferred tool for issue and epic structure
operations**. Reach for it before raw `gh`/GraphQL. It lives in this
workspace, at `tools/orbit`, so run it as `uv run orbit` here rather
than relying on an installed copy — an installed `orbit` is whatever
was last `uv tool install`ed and need not match the checkout.

The same goes for `batch` (`tools/batch`): the planning stages below
are this repo's own code.

In particular, to nest an issue under an epic use:

```
orbit move <issue> <epic>
```

This sets the issue's milestone to match the epic and links it as a
sub-issue. **Do not** nest with a raw `gh api graphql` `addSubIssue`
mutation — the auto-mode permission classifier denies it, costing a
wasted round trip, whereas `orbit move` performs the same link and
is allowed.

Key subcommands:

- `create <epic> <title>` — create a leaf issue under an epic (or
  `standalone` for the current milestone with no parent, `shelf` for the
  backlog); `--label` repeatable, `--body`/`--body-file`.
- `create-epic <title>` — create an epic in the current sprint with the
  `epic` label.
- `move <issue> <epic>` — nest an issue under an epic (see above).
- `reorder <issue> --first|--after <sibling>|--before <sibling>` —
  reposition an issue within its epic's sub-issue order.
- `schedule <issue> [-m MILESTONE]` — move an issue or epic to a
  milestone (defaults to the current one). Detaches the parent epic
  when it lives in a different milestone; use `-m Backlog` to shelve.
- `close <issue>` — close an issue, with an optional reason.
- `parent <issue>` / `subs <issue>` — show parent / recursive sub-issue
  tree.
- `show <issue>` / `set-body <issue>` / `edit <issue>` — inspect, set the
  body, or open in a browser.
- `find <text>` — search issue titles.
- `sprint` / `epics` / `backlog` / `soon` — list the current sprint,
  epics with progress, the backlog, or backlog issues labeled `soon`.

Most commands accept `--json` for scripting. Run `orbit --help` (or
`orbit <command> --help`) for the authoritative list.

### Epics and milestone organization

An **epic** is an issue with the `epic` label. Every leaf issue
(non-epic) in the current milestone either is a sub-issue of an epic
in that milestone or is a **standalone issue** — one-off work with no
outcome to group under.

#### An epic names a finishable outcome

An epic names a state the codebase reaches, after which the epic
closes. "batch drives runs in more than one repo at a time" is an
outcome. "Developer velocity" is an area,
and an area never finishes.

An epic that cannot plausibly close within a sprint or two is too
broad. Split it along outcomes.

Reject these titles outright:

- an area of the codebase — "developer velocity", "testing",
  "tooling";
- an open-ended quality — "performance", "cleanup";
- an explicit grab bag — "miscellaneous", "small standalone
  improvements", "small fixes".

When an issue has no home, the answer is a standalone issue, **not** a
catch-all epic to hold it.

#### Standalone issues

A standalone issue carries an AFK/HITL marker and a line estimate in
its title like any leaf, sits in a milestone, and has no parent. Create
one with:

```
orbit create standalone 'AFK (20) fix the off-by-one in the widget'
```

Choosing between the two: if a second related issue is foreseeable,
make an epic and put both under it. Otherwise make the issue
standalone. `orbit sprint` lists standalone issues under their own
`STANDALONE` heading, and `snippets` reports them under
"STANDALONE WORK" — the absence of a parent is a supported state, not a
leak.

#### GitHub repo

The GitHub repo for this project is `ginabythebay/orbatch`. Pass
`--repo ginabythebay/orbatch` to `gh` commands, or verify via
`git remote -v`.

Take care not to file orbatch's own issues against whatever repository
the tools happen to be pointed at: `orbit` and `batch` resolve the repo
from the working directory, so run them from this checkout.

#### When creating or moving an issue into the current milestone

1. **Find a matching epic.** List the epics in the milestone:
   ```
   gh issue list --repo ginabythebay/orbatch --label epic --milestone "<milestone>" --state all
   ```
   Read their titles and descriptions. If the new issue fits
   thematically under an existing epic, add it as a sub-issue of
   that epic.

   **A closed epic is still a candidate.** Closed means its children
   were all done at the time, not that its theme is retired — nesting
   under it and letting `orbit` reopen it beats filing a
   near-duplicate epic. The exception: prefer a new epic when the
   closed one was abandoned or superseded rather than completed.

2. **Create an epic, or go standalone.** If no existing epic fits and
   you can foresee a second related issue, create one:
   - Title names a finishable outcome (e.g. "Test suite runs under 30
     seconds" not "Test infrastructure improvements").
   - Apply the `epic` label.
   - Assign it to the current milestone.
   - Add the new issue as a sub-issue.

   If the work is a genuine one-off, file it standalone instead —
   `orbit create standalone '<title>'`.

3. **Report what you did.** Tell the user which epic you associated
   the issue with and whether you created a new epic, used an existing
   one, or filed the issue standalone.

#### Keep an epic open while it has open sub-issues

An epic is only "done" when all of its sub-issues are done. Whenever
you make a sub-issue open under an epic, ensure the epic (and its
ancestors) are open too:

- **Adding an open issue to a closed epic.** `orbit move` and
  `orbit create` do this for you. Both reopen a closed destination
  epic, then walk `parent_number` to the root and reopen every closed
  ancestor on the way. It is automatic and unconditional — there is no
  flag and no opt-out. Your job is to *report* what the command
  reopened, not to perform it.
- **Reopening a sub-issue.** Orbit does not cover this case. If you
  reopen an issue that was previously closed and its parent epic is
  closed, reopen the parent epic by hand: use `orbit parent
  <issue>` to walk up the chain and `gh issue reopen <n> --repo
  ginabythebay/orbatch` to reopen each closed ancestor, continuing
  to the root.

**Report what you did.** `orbit move` and `orbit create` print
a `Reopened #800, #700` line, and carry the same list as `reopened` in
`--json`. Pass it on to the user, so a chain of reopenings isn't a
silent side effect.

#### Consolidation

Avoid proliferating single-child epics. After creating or modifying
issues, check for epics in the milestone that have only one
sub-issue. When you find one:

- If another epic covers a related theme, suggest merging — move the
  lone sub-issue under the broader epic and close the single-child
  epic.
- If no related epic exists but the single-child epic's theme is too
  narrow, suggest broadening the epic's title/scope to attract future
  work — up to the point where the title still names a finishable
  outcome. Past that, close the epic and make the child standalone.
- Report any consolidation opportunities to the user rather than
  silently reorganizing.

#### Splitting an outgrown epic

The inverse case: an epic whose children no longer share one outcome,
or that has stayed open across more than a sprint or two. Split it:

- Name the outcomes actually present in the children, and create an
  epic per outcome.
- `orbit move <child> <new-epic>` each child to the epic whose
  outcome it delivers.
- A child that fits none of them is one-off work — detach it and leave
  it standalone in the milestone rather than inventing an epic to
  house the leftovers.
- Close the original epic once it is empty, or keep it for the outcome
  that most of its children serve and move only the strays.

Report the proposed split to the user before reorganizing.
