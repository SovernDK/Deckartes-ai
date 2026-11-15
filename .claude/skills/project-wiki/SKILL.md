---
name: "project-wiki"
description: "Use when asked to document a project, write or update the wiki, or record architecture and usage instructions, and after any change that adds, removes or alters a feature; maintains a lean, diagram-first docs/wiki.html."
---
 
# Project Wiki
 
The project's wiki lives in **one file: `/docs/wiki.html`** (relative to the repo root). It covers two things: how the project is built (architecture) and how to use it (instructions).
 
## Rules
 
- **One file.** Create `docs/wiki.html` if missing; otherwise update it in place. Never scatter docs into other files.
- **Lean.** Every section earns its place. No filler intros, no restating what the code obviously says. Short labels over sentences, sentences over paragraphs.
- **Visuals first.** Prefer a graph, diagram or infographic over text whenever it can carry the idea. Text is for what a picture can't say (exact commands, caveats).
- **Self-contained.** Inline CSS and SVG. If using Mermaid, load it from a CDN with a `<script>` tag and keep the diagram source in `<pre class="mermaid">` so it stays readable as text.
- **Accurate.** Derive everything from the actual code, configs and scripts. Don't document intended or imagined behavior.
## Workflow
 
1. Explore the repo: entry points, top-level modules, data flow, external services, build/run/test scripts, config and env vars.
2. If `docs/wiki.html` exists, read it and update only what changed; keep its structure.
3. Write or update the page with these sections (skip any that don't apply):
   - **Overview** — one line on what the project does.
   - **Architecture** — component/module diagram showing main parts and how they connect.
   - **Data / request flow** — sequence or flow diagram of the key path(s).
   - **Directory map** — compact tree of important folders with a few-word purpose each.
   - **Usage** — install, configure, run, test: copy-pasteable commands in code blocks.
   - **Config** — table of env vars / settings with defaults.
4. Open the file (or render it) to check the diagrams display and nothing is broken.
## Picking the visual
 
| Idea | Visual |
|---|---|
| Components & dependencies | Flowchart / box diagram |
| Request or event path | Sequence diagram |
| Lifecycle / states | State diagram |
| Data model | ER diagram |
| Setup steps | Numbered step strip |
| Options / config | Table |