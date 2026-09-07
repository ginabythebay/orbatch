@progress.md
You are planning every queued issue under {{TARGETS}}, in one session.

1. Run `batch agent next-issue {{TARGETS_ARGS}}` to get the next issue to
   plan.
   It prints the issue number, title, body, and the earlier issues in the
   stack, and ends with the command that closes the iteration. Exit code 3
   means the queue is empty: the batch is done, say so and stop. Any other
   non-zero exit is a failure, not an empty queue — show it to the
   developer and stop; issues may still be waiting.
2. Plan that issue with the developer, following the steps in
   `dev/ralph-plan.md` — read it. Its ISSUE and ISSUE_NUMBER placeholders
   are the number
   `next-issue` gave you, and its PREDECESSORS placeholder is the
   predecessors it listed; drop that line when it listed none. Ignore its
   remaining `{{...}}` markers — this flow has no plan-phase steering.
   Override its steps 4 and 5: do not present the test plan for approval
   and do not wait. Write it straight to the issue body and move on. Its
   step 3 still holds — genuine design questions are still worth asking.
3. Once the plan is written to the issue body, run the
   `batch agent plan-written <issue> {{TARGETS_ARGS}}` command `next-issue`
   printed. It re-reads the issue and refuses if the plan is not there;
   a refusal means the write did not land, so fix it and run it again.
4. Go back to step 1.

Do not write any code and do not implement anything. Your only deliverable
is a test plan on each issue's body.
