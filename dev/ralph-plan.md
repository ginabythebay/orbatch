@progress.md
1. Read github issue #{{ISSUE}} thoroughly. Understand the requirements and the existing code involved.
 - Earlier issues in this stack: {{PREDECESSORS}}. Read the "## Test Plan" section of each and plan on top of them: their changes land before yours, so plan against the code they leave behind, not against main.
2. Design a test plan following the /tdd skill's planning phase:
   - Identify what interface changes are needed
   - List the behaviors to test (not implementation steps), prioritized
   - Identify opportunities for deep modules
   - Design interfaces for testability
   - {{MAX_TESTS}}
   - {{PLAN_GUIDANCE}}
3. If there are design questions that need to be settled, ask them one
   at a time, before presenting the plan. With each question, print the issue's URL on a line of its own so it can be clicked or copied:
   https://github.com/ginabythebay/orbatch/issues/{{ISSUE_NUMBER}}
4. Present the test plan to the user for discussion. Show the "## Test Plan" section: a numbered list of test cases, each with a brief description of what it verifies and why it matters. Then stop and wait. Discuss and revise the plan with the user as needed. Do not update the issue yet.
5. Only after the user explicitly approves the plan, update the issue
   body on issue #{{ISSUE}}. Add the "## Test Plan" section, if not
   present yet, preserving the existing issue body above it.  If there
   were design decisions made by the user during this session, update
   the issue with those as well.
6. Do not write any code. Do not implement anything. Your only deliverable is the approved test plan appended to the issue body.
