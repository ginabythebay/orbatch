## pytest guidance

A local `uv run pytest` deselects the `slow` tests (via conftest.py, only
outside CI and only for bare full-suite runs) and completes in ~45 seconds
(1713 tests, 169 deselected). The full suite is `uv run pytest --slow` —
~89 seconds serial, ~16 seconds with `-n auto` (1882 tests). Run the full
suite before reporting work complete.

CI runs `uv run pytest -n logical`, not `-n auto`:
`pytest-xdist[psutil]` reads `auto` as *physical* cores, which resolves
to 1 on a 2-vCPU runner and silently runs the suite serially. The slow
tests stay in on CI because GitHub Actions sets `CI`, not because the
invocation is bare. Explicit paths, `-m` expressions, and `-k` selections
always run exactly what they name.

Set your timeouts accordingly: a full serial run taking a minute and a
half is normal, not a hang. If a run takes much longer than these
numbers, investigate why and fix it.

The `slow` marker means a test builds a wheel, creates an isolated venv,
or boots a full app. Today that is `tools/review/tests/wheel_test.py`
and the orbit and batch TUI tests, which carry a module-level
`pytestmark`.

`filterwarnings = ["error"]` is set workspace-wide, so any warning a test
emits — a `ResourceWarning` about an unclosed transport, most often —
fails that test rather than scrolling past.
