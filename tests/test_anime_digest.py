"""The morning digest script, with AnimeSchedule's and Telegram's HTTP faked."""

import json
from unittest import mock

import httpx

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from jobs import anime_digest, runner
from telegram import telegram_api
from test_anime_tools import TIMETABLE, USER_LIST, episode, on_list, weekly

TOKEN = "123:SECRET"
OWNER = 42
SEND = f"/bot{TOKEN}/sendMessage"
SENT = {"ok": True, "result": {"message_id": 7}}
AIRING = episode("f", "Frieren", "2026-11-11T16:30:00Z", 5)

class DigestScriptTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-11 12:00")
        for target, name, value in ((config, "telegram_token", TOKEN), (config, "telegram_user_id", str(OWNER)),
                                    (config, "animeschedule_token", "token"), (config, "animeschedule_user", "tester"),
                                    (config, "anime_air_type", "sub"), (runner, "RETRY_SECONDS", 0)):
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

    async def test_a_long_digest_arrives_in_parts_under_the_limit(self):
        server = self.serve({USER_LIST: on_list(watching=["f", "b", "o"]), SEND: SENT,
                             TIMETABLE: weekly(AIRING, episode("b", "Bocchi the Rock", "2026-11-11T17:00:00Z", 2),
                                               episode("o", "Oshi no Ko", "2026-11-11T18:00:00Z", 3))})
        with mock.patch.object(telegram_api, "MAX_MESSAGE", 60):
            self.assertEqual(await anime_digest.run(), 0)
        parts = [message["text"] for message in self.sent(server)]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 60 for part in parts), parts)
        self.assertEqual("\n".join(parts), "Airing today, Wednesday 11 November:\n"
                                             "- Frieren: episode 5 at 17:30\n"
                                             "- Bocchi the Rock: episode 2 at 18:00\n"
                                             "- Oshi no Ko: episode 3 at 19:00")

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
        with self.assertLogs("deckartes.jobs", "ERROR"):
            self.assertEqual(await anime_digest.run(), 1)
        self.assertEqual(len(self.sent(server)), runner.MAX_ATTEMPTS)

    async def test_refuses_to_start_without_the_settings(self):
        for name, value in (("telegram_token", ""), ("telegram_user_id", "me"), ("animeschedule_token", "")):
            with self.subTest(name), mock.patch.object(config, name, value), self.assertLogs("deckartes.jobs", "ERROR"):
                self.assertEqual(await anime_digest.run(), 2)
