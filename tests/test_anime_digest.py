"""The morning digest script, with AnimeSchedule's and Telegram's HTTP faked, and its launchd job."""

import json
import plistlib
import unittest
from pathlib import Path
from unittest import mock

import httpx

import anime_digest
from config import config
from frozen_clock import freeze
from http_fake import HttpTest
from test_anime_tools import TIMETABLE, USER_LIST, episode, on_list, weekly

TOKEN = "123:SECRET"
OWNER = 42
SEND = f"/bot{TOKEN}/sendMessage"
SENT = {"ok": True, "result": {"message_id": 7}}
ROOT = Path(__file__).parent.parent
AIRING = episode("f", "Frieren", "2026-11-11T16:30:00Z", 5)

class DigestScriptTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")
        for target, name, value in ((config, "telegram_token", TOKEN), (config, "telegram_user_id", str(OWNER)),
                                    (config, "animeschedule_token", "token"), (config, "animeschedule_user", "tester"),
                                    (config, "anime_air_type", "sub"), (anime_digest, "RETRY_SECONDS", 0)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def serve_digest(self, *timetable, send=SENT):
        return self.serve({USER_LIST: on_list(watching=["f"]), TIMETABLE: weekly(*timetable), SEND: send})

    @staticmethod
    def sent(server) -> list[dict]:
        return [json.loads(r.content) for r in server.requests if r.url.path == SEND]

    async def test_sends_the_digest_to_the_owner(self):
        server = self.serve_digest(AIRING)
        self.assertEqual(await anime_digest.run(), 0)
        self.assertEqual(self.sent(server), [{"chat_id": OWNER, "text": "Airing today, Wednesday 11 November:\n"
                                                                        "- Frieren: episode 5 at 17:30"}])

    async def test_sends_nothing_when_nothing_airs(self):
        server = self.serve_digest()
        self.assertEqual(await anime_digest.run(), 0)
        self.assertEqual(self.sent(server), [])

    async def test_retries_after_a_failure(self):
        answers = iter([httpx.ConnectError("no network"), SENT])
        server = self.serve_digest(AIRING, send=lambda request: next(answers))
        self.assertEqual(await anime_digest.run(), 0)
        self.assertEqual(len(self.sent(server)), 2)

    async def test_gives_up_after_the_last_attempt(self):
        server = self.serve_digest(AIRING, send=httpx.ConnectError("no network"))
        with self.assertLogs("deckartes.digest", "ERROR"):
            self.assertEqual(await anime_digest.run(), 1)
        self.assertEqual(len(self.sent(server)), anime_digest.MAX_ATTEMPTS)

    async def test_refuses_to_start_without_the_settings(self):
        for name, value in (("telegram_token", ""), ("telegram_user_id", "me"), ("animeschedule_token", "")):
            with self.subTest(name), mock.patch.object(config, name, value), self.assertLogs("deckartes.digest", "ERROR"):
                self.assertEqual(await anime_digest.run(), 2)

class LaunchdJobTest(unittest.TestCase):
    def test_runs_the_script_at_nine(self):
        with open(ROOT / "launchd" / "com.deckartes.anime-digest.plist", "rb") as file:
            job = plistlib.load(file)
        self.assertEqual(job["Label"], "com.deckartes.anime-digest")
        self.assertEqual(job["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
        self.assertEqual(job["ProgramArguments"], [str(ROOT / "venv" / "bin" / "python"), str(ROOT / "anime_digest.py")])
        self.assertEqual(job["WorkingDirectory"], str(ROOT))
        self.assertNotIn("RunAtLoad", job)
