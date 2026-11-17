#!/usr/bin/env python3
"""Stop hook: runs the unit test suite whenever Python files changed.

The result is shown to the user directly, so a test run can't be faked by the
model. A failing run blocks Claude from finishing and feeds the output back.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
# Kept outside the repo so it never shows up in git status.
STATE_FILE = Path.home() / ".claude" / "cache" / "deckartes-last-green-tests"
TEST_COMMAND = ["venv/bin/python", "-m", "unittest", "discover", "tests"]
OUTPUT_TAIL_LINES = 60


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT_DIR, capture_output=True, text=True, check=False).stdout


def python_tree_fingerprint() -> str:
    """Hash of every changed or untracked .py file, so unchanged code isn't retested."""
    digest = hashlib.sha256(git("rev-parse", "HEAD").encode())
    digest.update(git("diff", "HEAD", "--", "*.py").encode())
    for name in sorted(git("ls-files", "--others", "--exclude-standard", "--", "*.py").split()):
        path = PROJECT_DIR / name
        if path.is_file():
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def summary_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    ran = next((line for line in reversed(lines) if line.startswith("Ran ")), "")
    return f"{ran} — {lines[-1]}" if ran and lines else (lines[-1] if lines else "no output")


def main() -> None:
    payload = json.load(sys.stdin)
    fingerprint = python_tree_fingerprint()
    if STATE_FILE.exists() and STATE_FILE.read_text() == fingerprint:
        return

    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("LIVE", None)
    result = subprocess.run(TEST_COMMAND, cwd=PROJECT_DIR, env=env, capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    summary = summary_line(output)

    if result.returncode == 0:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(fingerprint)
        print(json.dumps({"systemMessage": f"✅ Test hook: {summary}"}))
        return

    tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
    if payload.get("stop_hook_active"):
        # Already sent back once this turn; let Claude stop instead of looping,
        # but make sure the user sees the failure.
        print(json.dumps({"systemMessage": f"❌ Test hook: tests still failing — {summary}"}))
        return
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"The test suite failed (run by a hook, not by you). Fix it before finishing.\n\n{tail}",
                "systemMessage": f"❌ Test hook: {summary}",
            }
        )
    )


if __name__ == "__main__":
    main()
