# Package report — 2026-11-11

Checked the `venv/` (Python 3.11.3) with `pip list --outdated`, `pip check` and `pip-audit`, and the Rapid-MLX tool install. `pip check` finds no broken requirements. Nothing was upgraded; this is a report only.

## 1. Fix now — known vulnerabilities

| Package | Installed | Fixed in | Advisories | Used by |
|---|---|---|---|---|
| **pypdf** | 6.20.1 | **6.21.4** (latest 6.x) | 14 CVEs (CVE-2026-113786 … CVE-2026-119209) | `library.py` reads every PDF the user saves to the library |
| **certifi** | 2026.7.22 | 2026.10.18 (latest 2026.10.23) | PYSEC-2026-4320 | `httpx` TLS for every web tool and the Telegram bot |

pypdf matters most: it parses untrusted files the user drops into the library. Upgrading within 6.x is the safe step:

```bash
venv/bin/pip install "pypdf==6.21.4" "certifi==2026.10.23"
PYTHONPATH=src venv/bin/python -m unittest discover tests
```

## 2. Major versions to evaluate (not urgent)

| Package | Installed | Latest | Notes |
|---|---|---|---|
| pypdf | 6.20.1 | 7.0.1 | New major. The app only uses `PdfReader`, `is_encrypted`, `decrypt("")`, `pages` and `extract_text()` ([library.py:177-183](src/tools/library.py#L177-L183)), so migration should be small; read the 7.0 changelog and run the library tests before moving. |
| pyobjc-core / -Cocoa / -EventKit | 12.2.2 | 13.0 | New major for the calendar tools. Calendar access can't be tested from Claude Code's shell (see [docs/calendar-tests.md](docs/calendar-tests.md)), so upgrade by hand and try reading and adding an event in the app. Upgrade all three together. |

## 3. Safe minor/patch updates

All are transitive (pulled in by textual, textual-dev or httpx), no API changes expected:

anyio 4.15.1 → 4.16.0, click 8.5.0 → 8.5.1, markdown-it-py 4.2.0 → 4.3.0, mdit-py-plugins 0.6.1 → 0.7.0, msgpack 1.2.3 → 1.2.4, multidict 7.1.0 → 7.2.1, platformdirs 4.13.0 → 4.14.5, pycparser 3.11 → 3.12, pydantic 2.14.1 → 2.14.3, pydantic_core 2.50.1 → 2.50.4, typing-inspection 0.4.4 → 0.4.5.

markdown-it-py and mdit-py-plugins render the chat bubbles through Textual's `Markdown` widget; after upgrading, glance at a reply with code blocks and lists (or use the pilot recipe in [docs/ui.md](docs/ui.md)).

Already latest: textual 8.2.8, httpx 0.28.1, numpy 2.4.6, cryptography 50.0.2, ruff 0.17.4, rich 15.0.0.

## 4. Environment housekeeping

- **`asyncio` 4.0.0 from PyPI is installed and should be removed.** It's the deprecated backport ("use the stdlib package instead"). Today `import asyncio` still resolves to the stdlib, so it's harmless, but nothing needs it: `venv/bin/pip uninstall asyncio`.
- **pip 22.3.1 → 26.3, setuptools 65.5.0 → 84.2.1.** Very old tooling in the venv; upgrade with `venv/bin/python -m pip install -U pip setuptools`.
- **Python 3.11.3** (April 2023, python.org framework build). The 3.11 branch is security-fix only until October 2027 and has had many patch releases since. Options: install the latest 3.11.x and rebuild the venv, or move to 3.12/3.13 (then bump `target-version` in `pyproject.toml`). Either way the venv must be recreated.
- **No dependency manifest.** Versions live only in `CLAUDE.md` prose and the venv itself, so a rebuilt venv can't be reproduced. Suggest a pinned `requirements.txt` with the direct dependencies (textual, textual-dev, httpx, pyobjc-framework-EventKit, pypdf, cryptography, numpy, ruff), and keep the `CLAUDE.md` versions in sync when upgrading.

## 5. Outside the venv

- **Rapid-MLX** (uv tool at `~/.local/share/uv/tools/rapid-mlx`): 1.0.2 installed, newer than the 1.0.0 PyPI lists, so up to date.

## Suggested order

1. `pip uninstall asyncio`; upgrade pip/setuptools.
2. pypdf → 6.21.4 and certifi (security).
3. The minor/patch batch from section 3; run tests and `ruff check .`.
4. Later, on a branch: pypdf 7, pyobjc 13, and a newer Python with a fresh venv plus `requirements.txt`.
5. Update the version list in `CLAUDE.md` after any of the above.
