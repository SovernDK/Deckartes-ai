---
name: vscode-mcp
description: Use when renaming a symbol, finding where something is defined or used, or checking a change for errors in this repository. Covers the VS Code MCP tools (Pylance) that replace grep and text replacement.
---

# VS Code MCP

The `mcp__vscode-mcp__*` tools ask Pylance, so they follow references, aliases and imports across files where grep and text replacement miss them. Pass `workspace_path` as the repository root; `list_workspaces` shows it if a call fails to connect. When no tool covers an operation, edit by hand and run the diagnostics step after.

## Rename

`rename_symbol` for every rename: variables, parameters, functions, classes, aliases.

- `codeSnippet` must contain the symbol exactly once, or the tool refuses. When a line holds the name twice (`items = self.items()`) or the symbol is a comprehension variable, rename with Edit instead.
- The success message sometimes shows a garbled old name. Trust `git diff` over the message: done when the diff after each batch shows every reference renamed and nothing else touched.

## Navigate

- `get_symbol_lsp_info` with `infoType: "definition"` for where a symbol is defined, `"hover"` or `"signature_help"` for its type and parameters.
- `get_references` for every use of a symbol, before changing its signature, moving it or deleting it. Done when each reference is updated or confirmed unaffected.

## Diagnostics

After every change, `get_diagnostics` with `__NOT_RECOMMEND__filePaths: []` (all git-modified files). Done when the touched files report zero errors; fix warnings the change introduced.
