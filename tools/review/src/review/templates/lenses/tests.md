Judge the tests, not the feature. Does each new test fail if the
behaviour it names regresses? Which changed behaviour has no test at
all? Are there tests that assert implementation details, share mutable
global state, leak connections, or pass for the wrong reason? Missing
coverage of an error path counts; a missing test for a getter does not.
