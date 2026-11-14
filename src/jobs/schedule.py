"""The scheduled jobs and when they run. Edit JOBS, then apply it with (from src/):

    ../venv/bin/python -m jobs.schedule

which writes a launchd LaunchAgent per job to ~/Library/LaunchAgents, loads new and changed ones,
and unloads and deletes com.deckartes.* jobs that are no longer listed. Each job's log is
~/.cache/deckartes/<name>.log. Details are in docs/telegram.md.
"""

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.config import PROJECT_ROOT

PREFIX = "com.deckartes."
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / ".cache" / "deckartes"

@dataclass(frozen=True)
class Job:
    """A module run with `python -m` from src/ at hour:minute local time: every day, or only on a
    weekday (0 = Sunday, as launchd counts) or a day of the month."""
    name: str
    module: str
    hour: int
    minute: int = 0
    weekday: int | None = None
    day: int | None = None

    @property
    def label(self) -> str:
        return PREFIX + self.name

JOBS = [
    Job("anime-digest", "jobs.anime_digest", hour=9, minute=0),
    Job("bank-digest", "jobs.bank_digest", hour=9, minute=5),
]

def launch_agent(job: Job) -> dict:
    """The job's launchd plist."""
    when = {"Hour": job.hour, "Minute": job.minute}
    if job.weekday is not None:
        when["Weekday"] = job.weekday
    if job.day is not None:
        when["Day"] = job.day
    log = str(LOGS / f"{job.name}.log")
    return {"Label": job.label,
            "ProgramArguments": [str(PROJECT_ROOT / "venv" / "bin" / "python"), "-m", job.module],
            "WorkingDirectory": str(PROJECT_ROOT / "src"),
            # A run missed during sleep happens on wake; several missed ones count as one
            "StartCalendarInterval": when,
            "StandardOutPath": log, "StandardErrorPath": log}

def _launchctl(*arguments: str, check: bool = True) -> bool:
    """Run launchctl in the user's GUI domain; whether it succeeded."""
    return subprocess.run(["launchctl", *arguments], capture_output=True, check=check).returncode == 0

def apply(jobs: list[Job]) -> list[str]:
    """Make launchd run exactly these jobs; returns what changed, one line each."""
    domain = f"gui/{os.getuid()}"
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    changes = []
    listed = {job.label for job in jobs}
    for path in sorted(LAUNCH_AGENTS.glob(f"{PREFIX}*.plist")):
        if path.stem not in listed:
            _launchctl("bootout", f"{domain}/{path.stem}", check=False)
            path.unlink()
            changes.append(f"Removed {path.stem}")
    for job in jobs:
        path = LAUNCH_AGENTS / f"{job.label}.plist"
        content = plistlib.dumps(launch_agent(job))
        loaded = _launchctl("print", f"{domain}/{job.label}", check=False)
        if loaded and path.exists() and path.read_bytes() == content:
            continue
        if loaded:
            _launchctl("bootout", f"{domain}/{job.label}", check=False)
        path.write_bytes(content)
        _launchctl("bootstrap", domain, str(path))
        changes.append(f"{'Updated' if loaded else 'Loaded'} {job.label}")
    return changes

if __name__ == "__main__":
    print("\n".join(apply(JOBS)) or "Every job is already up to date")
