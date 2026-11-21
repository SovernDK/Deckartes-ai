---
name: python-conventions
description: Conventions for Python code in this repository: naming, comments, docstrings, constants vs configuration, reuse and formatting. Load before writing or editing any Python.
---
# Python conventions

If this skill and existing code disagree, follow the skill and flag the inconsistency instead of spreading the old pattern.

## Naming
- Avoid single-letter variables except where obvious (`x`, `y`, …).
- Avoid names that differ by a single letter (`position` / `positions`).

## Comments and docstrings
- Write self-explanatory code; don't comment what the code already says.
- Comment only the **why**: non-obvious decisions, workarounds, business rules, links to issues or specs.
- No commented-out code, no changelog comments ("added by…", "fixed…"), no section banners.
- Docstrings only on public functions and classes, and only when the signature isn't enough.

## Values and configuration
- Anything someone might want to change without editing code (URLs, hosts, models, limits, timeouts, paths) is a field of the `Config` dataclass in `src/core/config.py`, with a comment on why it has that value or what it's for. Code reads it through the shared `config` instance.
- Secrets and other per-machine values come from `read_env` (the environment first, then `.env`) under the provider's own env name (`OLLAMA_API_KEY`, `ENABLE_BANKING_APP_ID`), as a `field(default_factory=…, repr=False)` so they never show up in a repr.
- Styles live in `chat.tcss`, not in Python.
- Every other value stays local to its only user; a module-level constant is for something shared.

## Reuse
- Before writing a helper, a checker or a test fixture, look for an existing one to reuse or generalize: a near-copy of something already there is two things to keep in step.

## Formatting
- `ruff format` handles formatting; don't hand-format. Line length is 120 (`pyproject.toml`).
