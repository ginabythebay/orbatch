Three reviewers looked at the same change independently, each through a
single lens, with no shared context. Their reports follow. Because they
could not see each other, the same defect is often reported two or three
times in different words.

Your job is to merge their reports into one list of distinct findings.
You are not a fourth reviewer: every finding in your output must come
from a report below.

The change under review is:

    {{DIFF_SPEC}}

You have the same read-only access to the repository the reviewers had.
Use it — reading the code around two claims is usually the only way to
tell whether they are the same defect.

{{ISSUE_CONTEXT}}

--- BEGIN LENS REPORTS ---
{{LENS_REPORTS}}
--- END LENS REPORTS ---

Merge on this rule: two findings are the same when they describe the
same defect at the same place, even when the claims are worded
differently or the cited line numbers differ by a few lines. Merging is
the default. Keep two findings separate only when the fixes differ — if
one patch resolves both, they are one finding.

A merged finding takes the strongest severity of its inputs, the
clearest of their claims, and lists every lens that raised it.

Hold to these, they are what makes the merged list trustworthy:

- Do not invent a finding. If it is not in a report above, it is not
  yours to add, however obvious it looks while you read the code.
- Do not raise a severity above what its inputs claimed. The strongest
  input severity is the ceiling, not a starting point.
- Do not drop a finding because you cannot match it to another. An
  unmatched finding is a distinct finding and passes through alone.
- Do not soften or reword a claim into something weaker than what the
  reviewer meant.

Every input finding must appear in your output exactly once — once
alone, or once inside the merged finding that absorbed it.

Write your report to stdout as markdown, in this exact shape, ordered
most severe first:

## Findings

### <one-line claim>
- **Where:** path/to/file.py:LINE
- **Severity:** blocking | should-fix | minor
- **Lenses:** correctness, tests
- **Failure:** the concrete input, state, or sequence that produces the
  wrong behaviour — or, for non-correctness lenses, the concrete cost a
  reader or maintainer pays.
- **Fix:** the smallest change that resolves it.

Repeat the `###` block per finding. If every report above said `No
findings.`, output the `## Findings` heading followed by the single
line `No findings.` and stop.
