# Design guidelines

> Conventions this repo follows: environment, naming, layering, errors, logging,
> comments and tests. `AGENTS.md` holds the operating rules and wins on any conflict.

## Environment: uv only

Manage everything with uv. Never `pip`, never a bare `python` or `pytest`. No
`requirements.txt`. Commit `uv.lock`; CI uses `uv sync --frozen`.

| Task | Command |
|---|---|
| Install/sync | `uv sync` |
| Add dependency | `uv add <pkg>` / `uv add --dev <pkg>` |
| Remove | `uv remove <pkg>` |
| Run | `uv run <cmd>` |
| Tests | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy` |

`uv run mypy` takes **no trailing dot**. A path argument overrides `files = ["src"]`
in the config and type-checks the untyped test suite as well, which reports hundreds
of errors that are not the ones being looked for.

Python 3.12+, pytest, ruff for lint and format, mypy for types.

speak-core is a git dependency pinned by tag. To change it, work in a checkout beside this one with `[tool.uv.sources] speak-core = { path = "../speak-core", editable = true }`; a step may commit that while in progress, and ends pinned to a pushed tag.

Ruff selects `E,F,I,N,UP,B,SIM,RUF,ANN,PTH,LOG,G` with `RUF100` on, so an unnecessary
`# noqa` is itself an error — a suppression has to be justified where it sits.
`ANN401` is off everywhere, and `tests/` also drops `ANN201` and `ANN001`, so a test
needs no return or argument annotations — but a function nested inside one still does.

## Code standards

- Type-annotate every signature. Modern syntax: `X | None`, `list[str]`, `dict[str, Any]`.
- Functions do one thing. Split when they stop fitting on a screen.
- Catch specific exceptions. Never a bare `except:`, never a silent `except: pass`.
- Validate external input at its boundary (env, CLI, HTTP, queue); trust internal calls.
- No secrets in code or logs. Configuration comes from the environment.
- Match the patterns already in the file being edited.

## SOLID / DRY / YAGNI (checkable, not slogans)

- **YAGNI**: build only what the current task needs. No speculative abstractions, no
  unused config flags, no unused parameters, no "for future use" paths. An injection
  seam with no injector is not a seam, it is dead weight.
- **DRY** by the Rule of Three: do not extract a shared abstraction until the third
  real duplication. Code that looks duplicated but has different reasons to change
  stays split.
- **SRP**: one reason to change per module. Keep I/O, logic, and transport apart.
- **DIP**: depend on an abstraction the codebase already has. Do not invent an
  interface for a single implementation.

## Naming and layering

- Exception names end in `Error` (ruff `N818`). Three read better without it and
  suppress the rule where they are defined, each with its reason: `MicrophoneBusy`,
  `RecorderUnavailable`, `TranscriptionFailed`
  rather than a failure.
- Pure logic is separated from the device or socket that feeds it: a state machine
  that takes values, wrapped in a thin shell that does the I/O. The shell is where
  the untestable part lives, so keep it small.
- Resolution of facts about the machine — which binaries exist, which devices are
  present — takes an injected lookup so it is testable anywhere.

## Errors and logging

- `logging.getLogger(__name__)` per module. Lazy `%`-style arguments (ruff `G`), never
  an f-string inside a log call.
- No `print()` in library code. The one exception is `talk.py`, where stdout *is* the
  product: a slash command splices it into the prompt.
- An error a user has to act on says what to do, not just what failed. An error that
  reaches the model travels as a `ToolError`, because the MCP SDK replaces raw
  exception text with a generic message.
- Anything user-facing that could be mistaken for content gets a marker: `/talk`
  prefixes failures with `speak:` so a transcript cannot be confused with an error.

## Comments and docs

- Comments explain **why**. The code already says what.
- Write the present tense of the repo: how it behaves now, a constraint it has to
  keep, or a limitation a reader will hit.
- **Never narrate the past.** No "we used to do X, now Y", no "this was a bug where",
  no "the suite used to conflate these". That is what git is for.
- The exception is when the past is still load-bearing — an active limitation, a live
  bug, or an expectation that breaks if someone changes the code. State it as the
  constraint, not the story: *"these two must agree or every limit skews by their
  ratio"*, not *"they disagreed once and everything skewed"*.
- No dated entries anywhere. `2026-01-01 — we decided X` is a changelog.
- Measured numbers live in `performance.md`, with the hardware stated once. Quoting a
  figure in code or in the README dates them both.

## Tests

- No network, no audio, no microphone. Every one of those is injected.
- A test must fail when the behaviour it names is removed. Check it: delete the
  behaviour and watch it go red. A test that passes either way pins nothing, and a
  green suite full of those is worse than a smaller honest one.
- Prefer injecting a collaborator over patching a module. Patch only where no seam
  exists, and then patch the narrowest thing.
- Table-driven where the cases are data.
- Docstrings say what the test pins and why it matters — in the present tense, as a
  statement about the code, not about its history.
- Fixtures must not depend on the machine running them. A test that asserts `afplay`
  passes only on a Mac; inject the player instead.
- Tests wait on events rather than sleeping, so a regression that stops the worker
  hangs instead of failing. `pytest-timeout` caps every test at 30 s to turn that
  back into a red test. It sits below `STALL_TIMEOUT` (60 s) deliberately: a wedged
  `drain()` has to redden rather than outlast the suite.
