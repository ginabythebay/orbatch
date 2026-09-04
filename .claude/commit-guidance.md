## Commit guidance

When you create a pull request for these commits:

### Close related issues automatically

If the work addresses one or more GitHub issues, include closing
keywords in the PR body so GitHub closes them on merge. Use the
exact phrasing GitHub recognizes:

- `Closes #123` (preferred)
- `Fixes #123` (for bug fixes)

Place each on its own line in the PR body. If multiple issues are
resolved, list each separately:

```
Closes #123
Closes #456
```

Do not use variations like "Related to #123" or "Addresses #123"
when the intent is to close the issue — those do not trigger
automatic closure.

### Every PR body carries a `## Caveats` section

State what you could not verify, the assumptions the change rests
on, and the work you left undone. Write `None.` under the heading
when there genuinely is nothing — an absent section reads as an
oversight, an empty one reads as a claim.

### Reconcile with main before opening the PR

Before creating the pull request, rebase or merge the latest main
into your branch to ensure CI runs against current code and to
avoid merge conflicts for reviewers:

```
git fetch origin main && git rebase origin/main
```

If the rebase produces conflicts, resolve them and verify tests
still pass before pushing.
