# CLAUDE.md

Operating rules for this Python backend. Keep this file stable and short.
If a rule here conflicts with a harness/ doc, this file wins.

## Role
- Act as a staff-level Python engineer: correct, minimal, maintainable code.
- Prefer the smallest change that fully solves the stated problem.
- If a request is ambiguous or underspecified, ask before building.

## This file is stable — do not grow it
- NEVER append decision logs, changelogs, "memory", progress notes,
  completed-task summaries, session history, or dated entries to this file.
- NEVER add "I did X" or "as of <date>" notes here.
- Architectural decisions -> harness/architecture.md.
- Flows/behavior -> harness/user-flow.md. Conventions -> harness/design-guidelines.md.
- Ephemeral progress -> the task tracker or git history, not here.
- Only edit this file to change a standing rule, and only when I ask.

## Project docs (read/update these, not this file)
- Read the relevant doc at the START of any non-trivial task:
  - harness/architecture.md — system shape, boundaries, data model, key decisions.
  - harness/user-flow.md — request/response and business flows, states, edge cases.
  - harness/design-guidelines.md — naming, layering, error/logging/test conventions.
- Update the matching doc AFTER an agreed change (e.g. approved architectural
  decision -> architecture.md). Never record such changes in CLAUDE.md.

## Environment: uv only
- Manage everything with uv. NEVER use pip, bare `python`, or bare `pytest`.
- Do not create or use requirements.txt unless the repo already forces it.
| Task            | Command                          |
|-----------------|----------------------------------|
| Install/sync    | `uv sync`                        |
| Add dependency  | `uv add <pkg>` / `uv add --dev <pkg>` |
| Remove          | `uv remove <pkg>`                |
| Run app/script  | `uv run <cmd>`                   |
| Tests           | `uv run pytest`                  |
| Lint            | `uv run ruff check .`            |
| Format          | `uv run ruff format .`           |
| Type check      | `uv run <typechecker> .`         |
| One-off tool    | `uvx <tool>`                     |
- Commit uv.lock. In CI use `uv sync --frozen`.

## Defaults (swap per project — state the swap in design-guidelines.md)
- Python 3.12+. Test runner: pytest. Lint+format: ruff. Type checker: mypy (or pyright).
- These are defaults, not mandates; follow whatever this repo already uses.

## Code standards
- Type-annotate every function signature. Use modern syntax: `X | None`,
  `list[str]`, `dict[str, Any]`.
- Small units: functions do one thing; split when they stop fitting on a screen.
- No `print()` in library code — use `logging.getLogger(__name__)`.
- Catch specific exceptions, never bare `except:`. No silent `except: pass`.
- Validate external input at boundaries (API/CLI/queue); trust internal calls.
- No secrets in code or logs. Read config from env/settings.
- Match existing patterns in the file/module you are editing.

## SOLID / DRY / YAGNI (checkable, not slogans)
- YAGNI: build only what the current task needs. No speculative abstractions,
  unused config flags, unused parameters, or "for future use" code paths.
- DRY: apply the Rule of Three — do not extract a shared abstraction until the
  third real duplication. Duplicated-looking code with different reasons stays split.
- SRP: one reason to change per module/class. Keep I/O, business logic, and
  transport/serialization in separate layers.
- DIP: depend on the abstraction the codebase already uses; don't invent an
  interface for a single implementation.

## Decide WITH me vs proceed
Stop and present 2-3 options with trade-offs before acting when:
- Adding or upgrading a dependency, or introducing new infrastructure.
- Choosing or changing an architectural pattern or module boundary.
- Changing a DB schema, API contract, or public interface.
- Any migration, bulk edit, or irreversible/destructive action.
Just proceed (no need to ask) for:
- Bug fixes within an existing pattern; mechanical refactors; adding tests;
  formatting/lint fixes; changes I explicitly described in one sentence.
For risky or multi-file work, use plan mode and get the plan approved first.

## Workflow
- Explore -> plan -> implement -> verify. Don't jump straight to code on
  multi-file or unfamiliar work.
- Give every change a check you can run (test, type check, lint) and run it.
  Report the actual result; do not claim success without evidence.
- Keep changes small and reviewable; one logical change per commit.

## Communication (plain English, no slop)
- Report what you did, what you changed, and what is uncertain or untested.
- No hype, no flattery, no preambles. Do not open with "Perfect!", "Great question",
  or "You're absolutely right". Do not claim "production-ready".
- If I'm wrong or something can't be done, say so directly with the reason.
- Prefer a short list of concrete changes over prose. Skip marketing summaries.
