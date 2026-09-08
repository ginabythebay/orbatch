# orbit TUI design notes

How the interactive TUI (`orbit` with no subcommand) is put together,
written for someone who knows the orbit CLI but not Textual.
Originally built for PRD #939 (PR #964).

## The Textual mental model

Textual is best understood as a browser engine for the terminal:

| Web                          | Textual                                  |
|------------------------------|------------------------------------------|
| DOM tree of elements         | Tree of **widgets**                      |
| CSS                          | Actual CSS dialect (layout, colors, dock)|
| Events bubble up the DOM     | **Messages** bubble up the widget tree   |
| Pages / routes               | **Screens**, kept on a stack             |
| Event loop                   | One asyncio loop owns the whole app      |

The app is single-threaded on an asyncio loop. Nothing may block that
loop — a blocking network call would freeze every keypress — which is
what workers are for (below).

## Module layout

```
orbit/tui/
├── app.py      — OrbitApp: state, keybindings, data fetching, view switching
├── widgets.py  — things you see: IssueTree, IssueList, StatusBar
└── screens.py  — things that overlay: DetailScreen, HelpScreen, EpicPickerScreen,
                  BatchVerbScreen
orbit/batching.py — the Batching protocol orbit drives batch through
```

The batch run screen itself is not here: `batch.tui.screen.DashboardScreen`
is the one dashboard `batch run` and orbit both host (see "Batch verbs
and the run screen").

Layering rule: **widgets only render; the app fetches and decides.**
Widgets never call the GitHub client — `IssueTree.load_epics(epics,
standalone)` takes already-fetched models. The exception is screens
that own their data for their own lifetime (`DetailScreen`, `EpicPickerScreen`),
which fetch on mount; `EpicPickerScreen` takes a status callback from
the app to report errors without importing it (avoiding a circular
import).

## Lifecycle

`run_tui(client, config)` → `OrbitApp(client, config.milestones,
config.commands, load_batching()).run()`, where the `GitHubClient` is
built once at the CLI entry point by `github_client()` and passed down
to the overlay screens that fetch, and `load_batching()` reads the
checkout's `batch.toml` there too, so the app never loads a config. Textual calls `compose()` to build
the widget tree (like initial HTML):

```python
def compose(self) -> ComposeResult:
    yield IssueTree(self._marks, id="epic-tree")
    yield IssueList(self._marks, id="issue-list", ...)
    yield StatusBar()
```

All widgets exist at all times; "view switching" toggles their
`display` flag. `StatusBar` docks to the bottom row via CSS; the
tree/list fill the rest (`height: 1fr`). After composing, `on_mount()`
fires and kicks off the first epics fetch.

## Workers: sync GraphQL client in an async app

`GitHubClient` is synchronous, so all fetches run through
Textual's `@work` decorator plus `asyncio.to_thread`:

```python
@work(exclusive=True, group="load")
async def _load_epics(self, final_status: str | None = None) -> None:
    tree.loading = True
    epics, standalone = await self._fetch_tree()  # gathers two to_thread calls
    ...
```

- `asyncio.to_thread` keeps the UI responsive during the HTTP call.
- `exclusive=True, group="load"` means a new load cancels the previous
  one — mashing `e`/`b`/`r` cannot race two loads or apply stale
  results out of order.
- `widget.loading = True` is Textual's built-in spinner overlay.
- `final_status` lets mutation actions thread their result message
  ("Moved #20 → …") through the refresh, so the refresh's own
  "Loaded N …" message does not overwrite it.

Expansion workers (`group="expand"`) are cancelled before any tree
reload (`_cancel_expand_workers`, the one suppressed-lint call to
Textual's `cancel_group`): an in-flight expansion holds a reference to
a node that `tree.clear()` is about to orphan. On cancellation the
worker must *not* touch `tree.loading` (the reload owns the
indicator), which is why `_load_sub_issues` avoids a `finally` for the
loading flag.

## Input: bindings → actions

Keys are declared, not dispatched by hand:

```python
BINDINGS = [Binding("m", "move", "Move"), ...]


def action_move(self) -> None: ...
```

Bindings resolve focused-widget → screen → app. Arrow keys are
consumed by the tree/list; letters fall through to the app. The help
modal binds `?` itself, which shadows the app's `?` and makes the same
key toggle the modal closed.

`space` is the one key bound in both places. Textual's `Tree` binds it
to `toggle_node`, and the focused widget wins, so `IssueTree` carries a
shadowing `Binding("space", "app.mark_toggle")` that routes it back up.
The `app.` prefix matters: a bare action name would shadow
`toggle_node` and then fire nothing. The app binding still has to
exist, because `reserved_keys()` is derived from the app's `BINDINGS`
alone.

`check_action()` is the app-level veto hook: any action in
`_MAIN_SCREEN_ACTIONS` is blocked while a modal or detail screen is on
the stack, so e.g. `s` over the help modal cannot shelve the issue
selected underneath. `help` and `quit` are deliberately not in that
set.

## Project config (`.orbit.toml`)

orbit is project-agnostic, so anything project-shaped lives in config
rather than in this source tree. Every repo puts an `.orbit.toml` at its
root (found via `git rev-parse --show-toplevel`) naming its milestones,
and may register extra keys there too:

```toml
[milestone]
current = "config v3"    # the sprint being worked
backlog = "Backlog"      # the shelf

[[commands]]
key   = "w"              # a single letter or digit
label = "Worktree"       # shown in the help screen
run   = "open -na Ghostty --args -e 'batch vwt {branch} {issue}'"
mode  = "spawn"          # "spawn" (default) | "suspend"
```

Press the key on a selected issue and orbit runs `run` through a shell.

- **Placeholders.** `{issue}` is the selected issue number. `{branch}`
  is whatever the user types into a prompt — its presence in `run` *is*
  the request to ask, so there's no separate opt-in field. Substituted
  values are `shlex.quote`d: `run` goes to a shell (that's the point —
  quoting, `~` and pipes all work), so quoting is what stops a branch
  name from being read as syntax.
- **Modes.** `spawn` launches detached and orbit stays up; `suspend`
  leaves the alt screen via `App.suspend()`, runs on the tty, and waits.
- **Keys.** Only letters and digits: Textual names punctuation keys
  (`?` arrives as `question_mark`), so a config key of `"?"` would bind
  a name that never fires. Collisions with orbit's own keys are rejected
  against `OrbitApp.reserved_keys()`, derived from `BINDINGS` so it
  cannot drift (`q` and `escape` included — they are app bindings).
- **Broken config is fatal.** `load_config` raises `ConfigError`
  listing *every* problem — milestone and command alike, from one read
  — and `cli.py` turns it into a `ClickException` on every command, not
  just the milestone-dependent ones. A *missing* config (or no git
  repo) is fatal too: there is no default sprint name to fall back to.
  `[[commands]]` stays optional.
- **Layering.** Loading and validation happen at the CLI edge, so
  `OrbitApp` only ever receives already-valid commands. Custom keys are
  bound per instance (`self._bindings.bind`) by index, and go through
  `check_action`'s `_MAIN_SCREEN_ACTIONS` like any other issue action —
  main screen only.

## Output: messages bubble

Widgets post messages that bubble to the app, handled by naming
convention (`on_tree_node_expanded`, `on_option_list_option_selected`).
Two gotchas encoded in the handlers:

- Messages bubble *past screens to the app*, so the epic picker's
  `OptionList` selection calls `event.stop()` and the app handlers
  guard with `isinstance(event.control, IssueTree)` /
  `isinstance(event.option_list, IssueList)`.
- Epic tree nodes carry a `TreeItemData` payload — `IssueNodeData` for
  an issue, `FilteredNodeData` for a hide-closed placeholder standing
  in for a run of filtered-out issues, `SectionNodeData` for the
  `STANDALONE` section heading — all with a `children_loaded` flag and
  a `fetch_number`. Expanding a node whose children are not
  loaded triggers one `fetch_sub_issue_tree` call; since that call
  returns the whole recursive subtree, all deeper nesting is populated
  at once and further expand/collapse is purely local. A placeholder is
  not an issue: `selected_issue_number` returns None on one, so the
  issue-targeted actions no-op there even though the cursor is on a
  row. Placeholders key on the negated first covered issue number, so
  a run and the node it reveals for that same issue stay distinct
  across a refresh.
- A refresh restores the cursor to the node the new tree actually holds
  for the saved issue, which is not always the issue itself. When a run
  placeholder now covers it, the cursor lands on the outermost still-
  collapsed placeholder instead — a refresh is incidental to the cursor,
  so the filter wins and the closed issues stay hidden. Conversely a
  saved placeholder key whose run is gone falls back to the issue it
  negates. `g` is the opposite case: it names an issue, so it opens the
  run and lands on the real node even with the filter on.
- The roots are the milestone's epics, then — when the milestone has
  any — a trailing `STANDALONE` section holding its standalone issues,
  so the tree shows what `orbit sprint` prints. The section keys on
  `0`, which no issue number takes, and is `children_loaded=True`, so
  expanding it or anything under it can never fetch. It starts expanded
  (its issues are already in hand) unless the user collapsed one that a
  refresh finds still there. Hide-closed collapses its issues into a run
  placeholder the way it does the epics; it never drops the section,
  which is what lets `g` reveal a closed standalone issue.

## Screens: a stack, not navigation

`push_screen()` layers a screen over the main one; Escape dismisses.

- `DetailScreen(Screen)` — opaque, full terminal: metadata header
  (`Static`) plus the issue body in a `Markdown` widget inside a
  `VerticalScroll`. Fetches via `fetch_issue_detail` (one round trip;
  the detail query includes `body`).
- `HelpScreen`, `EpicPickerScreen(ModalScreen)` — centered panels with
  the dimmed main screen visible behind.

Screens can return values: `EpicPickerScreen` is
`ModalScreen[int | None]` and dismisses with the chosen epic number or
`None`; the app receives it via the `push_screen` callback and runs
`move_issue` in a worker.

## State

The app holds four pieces of state: `_view` (an enum: EPICS / SPRINT /
BACKLOG), `_hide_closed`, which it fans out to every view widget
because that filter applies to all of them, `_marks`, a `Marks`
store (`orbit.marks`) of dired-style marks keyed by issue number and
shared by every view widget, so marks outlive a refresh or a view
switch, and `_run_screen`, the installed batch run screen if a run has
been started (`run_live` asks it whether the thread is still going). Widgets read the store when they render a row and re-render a
single row in place (`TreeNode.set_label`,
`OptionList.replace_option_prompt_at_index`) when a mark changes; the status
bar shows the count so marks in a hidden view are not invisible. Each view is its own
widget — the epics tree plus one `IssueList` per flat view — mirroring
what the user sees. An `IssueList` carries its view's query
configuration (milestone, soon filterability) as plain data, and the
backlog list owns the soon-only filter flag; one view-agnostic loader
fetches for whichever list is visible.

## Batch verbs and the run screen

`!` pushes `BatchVerbScreen`, a modal `OptionList` of the six verbs
(`orbit.batching.BatchVerb`); selecting one dismisses with it. The
targets are the marks in marking order (`Marks.numbers`), or the
cursor row when nothing is marked; epics pass through as their own
number and batch expands them. A placeholder under the cursor with no
marks is "Nothing to <verb>". Every verb then clears the marks and
refreshes the visible view, because the labels it changed are what the
view shows.

The app reaches batch through the `Batching` protocol
(`orbit/batching.py`), which `batch.runtime.Runtime` satisfies and the
tests fake. `load_batching()` returns `None` when the checkout has no
usable `batch.toml`, and every verb then reports "This repo is not
configured for batch". `run_tui` does that load, so `OrbitApp` never
reads a config.

- **queue / approve / fast-track / unqueue** run in an `action` worker
  and put the CLI's own wording on the status bar: the line builders in
  `batch.text_output` (`queue_lines`, `approve_lines`) are what
  `batch queue` prints, joined with `; `.
- **plan** calls `plan_session` under `self.suspend()`, like a
  `suspend`-mode custom command, and reports the VM's exit code and any
  reclaim refusal afterwards.
- **run** probes batch's run lock first (a `batch run` in a terminal is
  reported and nothing else happens), then builds a `DashboardScreen`
  over `Batching.drive(...)` and pushes it. The orchestrator runs on the
  screen's daemon thread, wrapped in `run_lock` + `awake`, so the lock
  is held exactly while the run thread lives and quitting orbit leaves
  a VM in flight rather than blocking on it; `run_tui` says so on the
  way out.

The run screen is **installed** (`DashboardScreen.install_on`) under
the name `RUN_SCREEN` and pushed by name: Textual removes an
uninstalled screen on pop, and this one must keep its rows, selection
and narration across `escape` and `d`. Its timers keep ticking while it
is suspended, which is how the `#run-state` cell on the status bar
learns the run ended (`DashboardScreen.RunChanged` bubbles to
`on_dashboard_screen_run_changed`). A second `run` while one is live
switches to the screen instead of starting another; a finished screen
is uninstalled and replaced by the next run's.

`check_action` treats the run screen as a third case: only
`_RUN_SCREEN_ACTIONS` (`e`/`c`/`b`) fire over it, and each pops it
first so the key lands on the view it names. `q`/`escape` are the
screen's own bindings (pop here, quit under `DashboardApp`), and so
are `f`/`s`/`r`, which shadow orbit's hide-closed/schedule/refresh
there. `d` is reserved for returning to the screen.

## Testing

`tools/orbit/tests/tui_test.py` (marked `slow`) runs the real app
headlessly with `app.run_test()`; the returned `Pilot` injects
keypresses. All GitHub calls are patched via a `_patched_github()`
context manager; tests needing call assertions stack their own
`patch(...) as mock` on top (innermost patch wins). Assertions target
the user-visible contract — status-line text, which screen is on top,
orchestrator call arguments — not widget internals, so the tests
survive refactoring.

## Known improvements (filed)

- #965 — client should raise a domain exception (`GithubError`) so
  transport failures land on the status line instead of crashing
  workers
- #966 — cache `repo()`/milestone lookups, reuse the HTTP session
- #967 — nested sub-issue query to remove N+1 round trips on expand
- #968 — evaluate an async-native client to drop `asyncio.to_thread`
