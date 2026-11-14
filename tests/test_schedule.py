"""Applying the jobs list to launchd, with launchctl faked and LaunchAgents in a temporary folder."""

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobs import schedule
from jobs.schedule import Job

ROOT = Path(__file__).parent.parent

class FakeLaunchctl:
    """Keeps which labels are loaded and records each command."""

    def __init__(self, loaded: set[str] | None = None):
        self.loaded = loaded or set()
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_) -> subprocess.CompletedProcess:
        self.commands.append(command[1:])
        action, target = command[1], command[-1]
        label = Path(target).stem if action == "bootstrap" else target.rsplit("/", 1)[-1]
        found = label in self.loaded
        if action == "bootstrap":
            self.loaded.add(label)
        elif action == "bootout":
            self.loaded.discard(label)
        return subprocess.CompletedProcess(command, 0 if found or action == "bootstrap" else 113)

class ScheduleTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.agents = Path(folder.name) / "LaunchAgents"
        for name, value in (("LAUNCH_AGENTS", self.agents), ("LOGS", Path(folder.name) / "logs")):
            patcher = mock.patch.object(schedule, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def apply(self, jobs: list[Job], launchctl: FakeLaunchctl) -> list[str]:
        with mock.patch.object(subprocess, "run", launchctl):
            return schedule.apply(jobs)

    def saved(self, label: str) -> dict:
        return plistlib.loads((self.agents / f"{label}.plist").read_bytes())

    def test_the_listed_jobs_run_each_morning_from_src(self):
        launchctl = FakeLaunchctl()

        self.assertEqual(self.apply(schedule.JOBS, launchctl),
                         ["Loaded com.deckartes.anime-digest", "Loaded com.deckartes.bank-digest"])

        anime = self.saved("com.deckartes.anime-digest")
        self.assertEqual(anime["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertEqual(self.saved("com.deckartes.bank-digest")["StartCalendarInterval"], {"Hour": 9, "Minute": 5})
        self.assertEqual(anime["ProgramArguments"], [str(ROOT / "venv" / "bin" / "python"), "-m", "jobs.anime_digest"])
        self.assertEqual(anime["WorkingDirectory"], str(ROOT / "src"))
        self.assertEqual(anime["StandardOutPath"], str(schedule.LOGS / "anime-digest.log"))
        self.assertNotIn("RunAtLoad", anime)
        self.assertEqual(launchctl.loaded, {"com.deckartes.anime-digest", "com.deckartes.bank-digest"})

    def test_a_weekly_or_monthly_job_says_its_day(self):
        self.apply([Job("weekly", "jobs.x", hour=8, weekday=1), Job("monthly", "jobs.y", hour=8, day=1)], FakeLaunchctl())

        self.assertEqual(self.saved("com.deckartes.weekly")["StartCalendarInterval"], {"Hour": 8, "Minute": 0, "Weekday": 1})
        self.assertEqual(self.saved("com.deckartes.monthly")["StartCalendarInterval"], {"Hour": 8, "Minute": 0, "Day": 1})

    def test_only_changed_jobs_are_reloaded_and_unlisted_ones_removed(self):
        launchctl = FakeLaunchctl()
        self.apply([Job("a", "jobs.a", hour=9), Job("b", "jobs.b", hour=9), Job("gone", "jobs.gone", hour=9)], launchctl)
        launchctl.commands.clear()

        changes = self.apply([Job("a", "jobs.a", hour=9), Job("b", "jobs.b", hour=10)], launchctl)

        self.assertEqual(changes, ["Removed com.deckartes.gone", "Updated com.deckartes.b"])
        self.assertFalse((self.agents / "com.deckartes.gone.plist").exists())
        self.assertEqual(self.saved("com.deckartes.b")["StartCalendarInterval"], {"Hour": 10, "Minute": 0})
        self.assertEqual(launchctl.loaded, {"com.deckartes.a", "com.deckartes.b"})
        self.assertNotIn(["bootout", f"gui/{schedule.os.getuid()}/com.deckartes.a"], launchctl.commands)

    def test_an_unloaded_job_is_loaded_again_even_if_unchanged(self):
        launchctl = FakeLaunchctl()
        self.apply([Job("a", "jobs.a", hour=9)], launchctl)
        launchctl.loaded.clear()

        self.assertEqual(self.apply([Job("a", "jobs.a", hour=9)], launchctl), ["Loaded com.deckartes.a"])

    def test_listed_jobs_name_real_modules(self):
        for job in schedule.JOBS:
            with self.subTest(job.name):
                self.assertTrue((ROOT / "src" / Path(*job.module.split("."))).with_suffix(".py").exists())

if __name__ == "__main__":
    unittest.main()
