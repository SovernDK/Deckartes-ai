---
name: committing
description: Use when committing, pushing or opening a PR in this repository. Covers the commit message, the pre-commit checks and what stays out of a commit.
---

# Committing

Commit, push, rebase, reset or delete branches only when the user asks for it. Work on `main`; history on it is shared, so add commits on top and leave existing ones as they are.

## Steps

1. **Scope.** One logical change per commit, small and buildable on its own. If the working tree holds several changes, stage and commit them separately.
2. **Check.** Run the format check, lint and tests (commands under *Key commands* in `CLAUDE.md`) and fetch Pylance diagnostics for every touched file. Done when all pass with no errors; fix failures, then rerun. Let every git hook run.
3. **Review the staged diff** (`git diff --cached`). Done when it holds only source, tests and docs: secrets, `.env` files, credentials and generated build artifacts stay unstaged.
4. **Message.** One imperative sentence, ≤72 chars, saying what changed and why, e.g. `Label spending payees with a decision model instead of the chat model`.

## Pull requests

A short description of *what* changed and *why*, the linked issue (`#123`), how it was tested, and any new dependencies.
