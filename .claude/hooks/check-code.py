#!/usr/bin/env python3
"""Stop hook: formats, lints and tests the code whenever a Python file changed.

The result is shown to the user directly, so a check can't be faked by the
model. A failing check blocks Claude from finishing and feeds the output back.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
# Kept outside the repo so it never shows up in git status.
STATE_FILE = Path.home() / ".claude" / "cache" / "deckartes-last-green-check"
FORMAT_COMMAND = ["venv/bin/ruff", "format", "."]
LINT_COMMAND = ["venv/bin/ruff", "check", "."]
TEST_COMMAND = ["venv/bin/python", "-m", "unittest", "discover", "tests"]
OUTPUT_TAIL_LINES = 60


def run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=PROJECT_DIR, env=env, capture_output=True, text=True, check=False)


def code_fingerprint() -> str:
    """Hash of every tracked or untracked .py file plus the ruff config, independent of commits."""
    listed = run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py", "pyproject.toml"])
    digest = hashlib.sha256()
    for name in sorted(set(listed.stdout.splitlines())):
        path = PROJECT_DIR / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def test_summary(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    ran = next((line for line in reversed(lines) if line.startswith("Ran ")), "")
    return f"{ran} — {lines[-1]}" if ran else last_line(output)


def tail(output: str) -> str:
    return "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])


def report(payload: dict, failed: str, summary: str, output: str) -> None:
    if payload.get("stop_hook_active"):
        # Already sent back once this turn; let Claude stop instead of looping,
        # but make sure the user sees the failure.
        print(json.dumps({"systemMessage": f"❌ Code check: {failed} still failing — {summary}"}))
        return
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"{failed} failed (run by a hook, not by you). Fix it before finishing.\n\n{tail(output)}",
                "systemMessage": f"❌ Code check: {failed} — {summary}",
            }
        )
    )


def main() -> None:
    payload = json.load(sys.stdin)
    if STATE_FILE.exists() and STATE_FILE.read_text() == code_fingerprint():
        return

    formatted = run(FORMAT_COMMAND)
    if formatted.returncode != 0:
        output = formatted.stdout + formatted.stderr
        report(payload, "ruff format", last_line(output), output)
        return

    linted = run(LINT_COMMAND)
    if linted.returncode != 0:
        output = linted.stdout + linted.stderr
        report(payload, "ruff check", last_line(output), output)
        return

    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("LIVE", None)
    tested = run(TEST_COMMAND, env)
    output = tested.stdout + tested.stderr
    if tested.returncode != 0:
        report(payload, "Tests", test_summary(output), output)
        return

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Taken after formatting, so files the formatter just rewrote don't trigger another run.
    STATE_FILE.write_text(code_fingerprint())
    message = f"✅ Code check: {last_line(formatted.stdout)}; lint clean; {test_summary(output)}"
    print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
