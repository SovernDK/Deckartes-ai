"""The Telegram bot from update to reply, with only Telegram's HTTP and the model's chat calls faked."""

import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from agent import history
from agent.conversation import Conversation
from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from model import mlx_client, mlx_server
from telegram import telegram_api, telegram_bot, telegram_format
from telegram.telegram_api import MAX_MESSAGE, split_text
from telegram.telegram_bot import Bot
from test_calendar_tools import FakeCalendar
from test_conversation import FakeClient, call, chunk, think
from tools import calendar_api as cal

TOKEN = "123:SECRET"
OWNER = 42
PATH = f"/bot{TOKEN}/"

def ok(result=True):
    return {"ok": True, "result": result}

def message(text, sender=OWNER, chat_type="private", update_id=1):
    return {"update_id": update_id, "message": {"from": {"id": sender}, "chat": {"id": sender, "type": chat_type},
                                                "text": text}}

def press(data, sender=OWNER, update_id=2):
    return {"update_id": update_id, "callback_query": {"id": "press", "from": {"id": sender}, "data": data}}

async def select_nothing(query, previous):
    return SimpleNamespace(tools=[], groups=[])

async def run_confirmed_tool(name, arguments, confirm):
    return "saved" if await confirm("Add this event?") else "declined"

class BotTest(HttpTest):
    def setUp(self):
        for name, value in (("telegram_token", TOKEN), ("telegram_user_id", str(OWNER))):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("PART_PAUSE_SECONDS", "SEND_RETRY_SECONDS", "POLL_RETRY_SECONDS"):
            patcher = mock.patch.object(telegram_bot, name, 0)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.routes = {PATH + method: ok({"message_id": 7}) for method in (
            "sendMessage", "sendChatAction", "answerCallbackQuery", "editMessageText", "setMyCommands")}
        self.routes[PATH + "getUpdates"] = ok([])
        self.server = self.serve(self.routes)
        self.bot = Bot(Conversation(tool_runner=run_confirmed_tool, tool_selector=select_nothing))

    def use_model(self, *rounds) -> FakeClient:
        client = FakeClient(*rounds)
        patcher = mock.patch.object(mlx_client, "stream_chat", client.stream_chat)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def block_model(self) -> tuple[asyncio.Event, asyncio.Event]:
        """The model call waits for the second event; the first says it has started."""
        started, release = asyncio.Event(), asyncio.Event()
        async def stream(messages, tools=None):
            started.set()
            await release.wait()
            yield chunk("Late answer", done=True)
        patcher = mock.patch.object(mlx_client, "stream_chat", stream)
        patcher.start()
        self.addCleanup(patcher.stop)
        return started, release

    def calls(self, method: str) -> list[dict]:
        return [json.loads(r.content) for r in self.server.requests if r.url.path == PATH + method]

    def texts(self) -> list[str]:
        return [body["text"] for body in self.calls("sendMessage")]

    async def until(self, condition) -> None:
        deadline = time.monotonic() + 2
        while not condition():
            self.assertLess(time.monotonic(), deadline, "condition never became true")
            await asyncio.sleep(0.001)

    async def ask_and_finish(self, text="Hi") -> None:
        await self.bot.handle(message(text))
        await self.bot.turn

    async def start_confirmation(self) -> str:
        """Run a turn up to the Yes/No question and return its Yes button's data."""
        self.use_model([chunk(tool_calls=[call("add_event")], done=True)],
                       [chunk("Done", done=True)])
        await self.bot.handle(message("Add it"))
        await self.until(lambda: self.bot.pending is not None)
        await self.until(lambda: self.calls("sendMessage"))
        keyboard = self.calls("sendMessage")[0]["reply_markup"]["inline_keyboard"]
        return keyboard[0][0]["callback_data"]

    def tool_result(self, client: FakeClient) -> str:
        return next(m["content"] for m in client.sent[1]["messages"] if m["role"] == "tool")


class AccessTest(BotTest):
    async def test_other_users_are_ignored_without_a_reply(self):
        self.use_model()
        await self.bot.handle(message("Read my calendar", sender=999))
        self.assertIsNone(self.bot.turn)
        self.assertEqual(self.server.requests, [])

    async def test_group_chats_are_ignored_even_from_the_owner(self):
        await self.bot.handle(message("Hi", chat_type="group"))
        self.assertIsNone(self.bot.turn)
        self.assertEqual(self.server.requests, [])

    async def test_another_users_button_press_changes_nothing(self):
        data = await self.start_confirmation()
        await self.bot.handle(press(data, sender=999))
        self.assertFalse(self.bot.pending.answer.done())
        self.assertEqual(self.calls("answerCallbackQuery"), [])
        self.bot.turn.cancel()

    def test_refuses_to_start_without_a_token(self):
        with mock.patch.object(config, "telegram_token", ""), self.assertRaises(telegram_bot.SetupError):
            Bot()

    def test_refuses_to_start_without_a_numeric_user_id(self):
        for value in ("", "me"):
            with mock.patch.object(config, "telegram_user_id", value), self.assertRaises(telegram_bot.SetupError):
                Bot()


class TurnTest(BotTest):
    async def test_a_question_gets_one_complete_reply_and_is_saved(self):
        self.use_model([chunk("Hello "), chunk("there", done=True)])
        await self.ask_and_finish("Hi")
        self.assertEqual(self.texts(), ["Hello there"])
        self.assertEqual(self.calls("sendMessage")[0]["chat_id"], OWNER)
        self.assertEqual(self.bot.conversation.history[-1], {"role": "assistant", "content": "Hello there"})

    async def test_typing_is_shown_while_answering(self):
        _, release = self.block_model()
        await self.bot.handle(message("Hi"))
        await self.until(lambda: self.calls("sendChatAction"))
        self.assertEqual(self.calls("sendChatAction")[0]["action"], "typing")
        release.set()
        await self.bot.turn

    async def test_markdown_arrives_as_formatting_not_markers(self):
        self.use_model([chunk("# Title\n**bold** and `x`", done=True)])
        await self.ask_and_finish()
        sent = self.calls("sendMessage")[0]
        self.assertEqual(sent["text"], "📌 Title\nbold and x")
        # Offsets count UTF-16 units, so the emoji counts as two
        self.assertEqual(sent["entities"], [
            {"type": "underline", "offset": 3, "length": 5}, {"type": "bold", "offset": 3, "length": 5},
            {"type": "bold", "offset": 9, "length": 4}, {"type": "code", "offset": 18, "length": 1}])
        self.assertNotIn("parse_mode", sent)

    async def test_a_long_reply_arrives_in_parts_under_the_limit(self):
        paragraph = "word " * 500
        self.use_model([chunk(f"{paragraph}\n\n{paragraph}\n\n{paragraph}", done=True)])
        await self.ask_and_finish()
        self.assertGreater(len(self.texts()), 1)
        self.assertTrue(all(len(text) <= MAX_MESSAGE for text in self.texts()))

    async def test_a_failed_turn_keeps_the_partial_reply_and_adds_the_error(self):
        self.use_model([chunk("Partial"), ConnectionError("server *gone*")])
        await self.ask_and_finish()
        self.assertEqual(self.texts(), ["Partial\n\nError: server *gone*"])
        # Only "Error:" is bold: the message is never read as Markdown
        self.assertEqual(self.calls("sendMessage")[0]["entities"], [{"type": "bold", "offset": 9, "length": 6}])
        self.assertEqual(self.bot.conversation.history, [])

    async def test_only_the_answer_is_sent_never_the_thinking(self):
        self.use_model([think("Secret reasoning."), chunk("The answer.", done=True)])
        await self.ask_and_finish()
        self.assertEqual(self.texts(), ["The answer."])

    async def test_an_empty_reply_gets_a_note(self):
        self.use_model([chunk("", done=True)])
        await self.ask_and_finish()
        self.assertEqual(self.texts(), [telegram_format.EMPTY_REPLY])

    async def test_a_question_during_a_turn_gets_the_busy_reply(self):
        started, release = self.block_model()
        await self.bot.handle(message("First"))
        await started.wait()
        await self.bot.handle(message("Second", update_id=2))
        self.assertEqual(self.texts(), [telegram_bot.BUSY])
        release.set()
        await self.bot.turn
        self.assertEqual(self.texts(), [telegram_bot.BUSY, "Late answer"])

    async def test_a_message_without_text_is_declined(self):
        update = message("x")
        del update["message"]["text"]
        update["message"]["document"] = {"file_id": "f"}
        await self.bot.handle(update)
        self.assertEqual(self.texts(), [telegram_bot.TEXT_ONLY])
        self.assertIsNone(self.bot.turn)

    async def test_a_send_that_fails_once_is_retried(self):
        answers = [httpx.ConnectError("down"), ok({"message_id": 7})]
        self.routes[PATH + "sendMessage"] = lambda request: answers.pop(0)
        self.use_model([chunk("Hello", done=True)])
        await self.ask_and_finish()
        self.assertEqual(len(self.calls("sendMessage")), 2)
        self.assertEqual(answers, [])


    async def test_formatting_telegram_rejects_is_resent_as_plain_text(self):
        answers = [httpx.Response(400, json={"ok": False, "description": "Bad Request: can't parse entities"}),
                   ok({"message_id": 7})]
        self.routes[PATH + "sendMessage"] = lambda request: answers.pop(0)
        self.use_model([chunk("**Hello**", done=True)])
        await self.ask_and_finish()
        first, second = self.calls("sendMessage")
        self.assertIn("entities", first)
        self.assertEqual(second["text"], "Hello")
        self.assertNotIn("entities", second)

class CommandTest(BotTest):
    async def test_help_and_start_explain_the_bot(self):
        await self.bot.handle(message("/start"))
        await self.bot.handle(message("/help", update_id=2))
        self.assertEqual(self.texts(), [telegram_bot.HELP, telegram_bot.HELP])

    async def test_clear_empties_the_conversation(self):
        self.use_model([chunk("Hello", done=True)])
        await self.ask_and_finish()
        await self.bot.handle(message("/clear", update_id=2))
        self.assertEqual(self.bot.conversation.history, [])
        self.assertEqual(self.texts()[-1], "Conversation cleared.")

    async def test_clear_is_refused_during_a_turn(self):
        started, release = self.block_model()
        await self.bot.handle(message("First"))
        await started.wait()
        await self.bot.handle(message("/clear", update_id=2))
        self.assertEqual(self.texts(), [telegram_bot.BUSY])
        release.set()
        await self.bot.turn
        self.assertEqual(len(self.bot.conversation.history), 3)

    async def test_stop_cancels_the_turn_and_saves_nothing(self):
        started, _ = self.block_model()
        await self.bot.handle(message("First"))
        await started.wait()
        await self.bot.handle(message("/stop", update_id=2))
        self.assertTrue(self.bot.turn.done())
        self.assertEqual(self.texts(), ["Stopped."])
        self.assertEqual(self.bot.conversation.history, [])

    async def test_stop_with_nothing_running_says_so(self):
        await self.bot.handle(message("/stop"))
        self.assertEqual(self.texts(), ["Nothing to stop."])

    async def test_unknown_command_shows_the_help(self):
        await self.bot.handle(message("/nope"))
        self.assertEqual(self.texts(), [telegram_bot.HELP])


class HistoryTest(BotTest):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for name, value in (("history_path", self.folder / "history.db"), ("tool_routing", False)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bot = Bot()

    async def test_turns_are_saved_as_telegram_conversations_and_clear_starts_a_new_one(self):
        self.use_model([chunk("Hello", done=True)], [chunk("Again", done=True)])
        await self.ask_and_finish("First")
        await self.bot.handle(message("/clear", update_id=2))
        await self.bot.handle(message("Second", update_id=3))
        await self.bot.turn
        self.assertEqual([(c.title, c.source) for c in history.list_conversations()],
                         [("Second", "telegram"), ("First", "telegram")])

    async def test_a_turn_that_cant_be_saved_is_still_answered_and_logged(self):
        (self.folder / "a file").write_text("")
        patcher = mock.patch.object(config, "history_path", self.folder / "a file" / "history.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.use_model([chunk("Hello", done=True)])
        with self.assertLogs(telegram_bot.log, logging.WARNING):
            await self.ask_and_finish("First")
        self.assertEqual(self.texts(), ["Hello"])

class ConfirmationTest(BotTest):
    async def test_yes_lets_the_change_through(self):
        client_data = await self.start_confirmation()
        await self.bot.handle(press(client_data))
        await self.bot.turn
        self.assertEqual(self.texts()[-1], "Done")
        self.assertEqual(self.calls("answerCallbackQuery")[0]["callback_query_id"], "press")
        edit = self.calls("editMessageText")[0]
        self.assertEqual(edit["text"], "Add this event?\n\nYes")
        self.assertNotIn("reply_markup", edit)

    async def test_no_declines_the_change(self):
        client = self.use_model([chunk(tool_calls=[call("add_event")], done=True)], [chunk("Ok", done=True)])
        await self.bot.handle(message("Add it"))
        await self.until(lambda: self.bot.pending is not None and self.calls("sendMessage"))
        no_button = self.calls("sendMessage")[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        await self.bot.handle(press(no_button))
        await self.bot.turn
        self.assertEqual(self.tool_result(client), "declined")

    async def test_no_answer_in_time_counts_as_no(self):
        client = self.use_model([chunk(tool_calls=[call("add_event")], done=True)], [chunk("Ok", done=True)])
        with mock.patch.object(telegram_bot, "CONFIRM_SECONDS", 0.01):
            await self.ask_and_finish("Add it")
        self.assertEqual(self.tool_result(client), "declined")
        self.assertEqual(self.calls("editMessageText")[0]["text"], f"Add this event?\n\n{telegram_bot.TIMED_OUT}")
        self.assertIsNone(self.bot.pending)

    async def test_stop_while_asking_cancels_and_removes_the_buttons(self):
        await self.start_confirmation()
        await self.bot.handle(message("/stop", update_id=3))
        self.assertTrue(self.bot.turn.done())
        self.assertIsNone(self.bot.pending)
        self.assertEqual(self.calls("editMessageText")[0]["text"], "Add this event?\n\nStopped")
        self.assertEqual(self.bot.conversation.history, [])

    async def test_an_old_button_does_nothing_but_is_answered(self):
        data = await self.start_confirmation()
        await self.bot.handle(press("00000000:yes"))
        self.assertFalse(self.bot.pending.answer.done())
        self.assertEqual(self.calls("answerCallbackQuery")[0]["text"], telegram_bot.EXPIRED)
        await self.bot.handle(press(data))
        await self.bot.turn

    async def test_a_question_that_cannot_be_sent_adds_nothing(self):
        freeze(self, "2026-11-11 12:00")
        save = mock.Mock()
        for name, value in (("_events_between", lambda start, end: []),
                            ("_target_calendar", lambda: FakeCalendar("Home")), ("save_event", save)):
            patcher = mock.patch.object(cal, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.routes[PATH + "sendMessage"] = httpx.Response(500, text="down")
        self.bot.conversation = Conversation(tool_selector=select_nothing)
        add = call("add_calendar_event", title="Dentist", date="2026-11-12", start_time="14:30")
        client = self.use_model([chunk(tool_calls=[add], done=True)], [chunk("Sorry", done=True)])
        await self.ask_and_finish("Add it")
        self.assertIn("Tool add_calendar_event failed", self.tool_result(client))
        self.assertNotIn(TOKEN, self.tool_result(client))
        save.assert_not_called()


class PollingTest(BotTest):
    async def run_bot(self) -> asyncio.Task:
        patcher = mock.patch.object(mlx_server, "ensure_chat")
        patcher.start()
        self.addCleanup(patcher.stop)
        return asyncio.create_task(self.bot.run())

    async def test_the_offset_moves_on_before_the_turn_finishes(self):
        started, release = self.block_model()
        self.routes[PATH + "getUpdates"] = ok([message("Hi", update_id=50)])
        await self.bot.poll_once()
        await started.wait()
        self.assertEqual(self.bot.offset, 51)
        self.assertFalse(self.bot.turn.done())
        release.set()
        await self.bot.turn

    async def test_the_next_poll_confirms_the_offset(self):
        self.routes[PATH + "getUpdates"] = ok([message("/help", update_id=50)])
        await self.bot.poll_once()
        await self.bot.poll_once()
        bodies = self.calls("getUpdates")
        self.assertEqual(bodies[1]["offset"], 51)
        self.assertEqual(bodies[1]["allowed_updates"], ["message", "callback_query"])

    async def test_polling_recovers_after_an_error(self):
        answers = [httpx.ConnectError("asleep"), ok([message("/help", update_id=9)])]
        def poll(request):
            if answers:
                return answers.pop(0)
            raise asyncio.CancelledError  # A fake poll never yields, so task.cancel() wouldn't land
        self.routes[PATH + "getUpdates"] = poll
        task = await self.run_bot()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.texts(), [telegram_bot.HELP])

    async def test_a_bad_update_does_not_stop_the_ones_after_it(self):
        self.routes[PATH + "getUpdates"] = ok([{"update_id": 1, "message": {"text": "no sender"}},
                                               message("/help", update_id=2)])
        with self.assertLogs(telegram_bot.log, logging.ERROR):
            await self.bot.poll_once()
        self.assertEqual(self.texts(), [telegram_bot.HELP])

    async def test_stopping_the_bot_cancels_a_running_turn(self):
        data = await self.start_confirmation()
        self.assertTrue(data)
        patcher = mock.patch.object(mlx_server, "ensure_chat")
        patcher.start()
        self.addCleanup(patcher.stop)
        turn = self.bot.turn
        def poll(request):
            raise asyncio.CancelledError  # A fake poll never yields, so task.cancel() wouldn't land
        self.routes[PATH + "getUpdates"] = poll
        task = asyncio.create_task(self.bot.run())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(turn.done())


class SecretTest(BotTest):
    async def test_the_token_never_appears_in_an_error(self):
        self.routes[PATH + "getUpdates"] = httpx.ConnectError(f"cannot open https://x/bot{TOKEN}/getUpdates")
        with self.assertRaises(telegram_api.TelegramError) as caught:
            await telegram_api.get_updates(None)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_setup_logging_silences_the_url_logger(self):
        logger = logging.getLogger("httpx")
        level = logger.level
        self.addCleanup(logger.setLevel, level)
        root = logging.getLogger()
        handlers = list(root.handlers)
        self.addCleanup(setattr, root, "handlers", handlers)
        with mock.patch.object(telegram_bot, "LOG_FILE", None):
            telegram_bot.setup_logging()
        self.assertGreaterEqual(logger.getEffectiveLevel(), logging.WARNING)


class ShutdownTest(BotTest):
    async def test_sigterm_ends_the_bot_cleanly_and_stops_its_servers(self):
        async def run_forever(bot):
            started.set()
            await asyncio.Event().wait()
        started = asyncio.Event()
        with mock.patch.object(Bot, "run", run_forever), mock.patch.object(mlx_server, "stop_all") as stop_all:
            task = asyncio.create_task(telegram_bot.serve())
            await started.wait()
            os.kill(os.getpid(), signal.SIGTERM)
            await task
        stop_all.assert_called_once()


class TextTest(BotTest):
    def test_short_text_is_one_part(self):
        self.assertEqual(split_text("hello"), ["hello"])

    def test_split_prefers_a_paragraph_then_a_line_then_a_word(self):
        self.assertEqual(split_text("a" * 7 + "\n\n" + "b" * 7, limit=10), ["a" * 7, "b" * 7])
        self.assertEqual(split_text("a" * 7 + "\n" + "b" * 7, limit=10), ["a" * 7, "b" * 7])
        self.assertEqual(split_text("a" * 7 + " " + "b" * 7, limit=10), ["a" * 7, "b" * 7])

    def test_split_cuts_a_long_word(self):
        self.assertEqual(split_text("a" * 25, limit=10), ["a" * 10, "a" * 10, "a" * 5])

    def test_no_part_is_empty(self):
        self.assertEqual(split_text("a" * 10 + "   \n\n   " + "b", limit=10), ["a" * 10, "b"])
