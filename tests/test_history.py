"""Saved conversations: turns run through Conversation, read back through the history module."""

from pathlib import Path

from agent import history
from agent.conversation import Conversation, NotSaved, Text
from core.config import config
from frozen_clock import freeze
from temp_history import save, save_summary, temp_history
from test_conversation import DATE_LINE, TIME_LINE, TurnTest, chunk


class HistoryTest(TurnTest):
    def setUp(self):
        super().setUp()
        temp_history(self)
        self.patch(config, "model", "test-model")
        self.conversation = Conversation(source="app")

    def saved(self, index=0) -> history.Saved:
        return history.load(history.list_conversations()[index].id)

class SavingTest(HistoryTest):
    async def test_a_finished_turn_is_saved_with_its_dates_and_model(self):
        self.use([chunk("Hello!", done=True)])
        await self.reply("hi")
        [listed] = history.list_conversations()
        self.assertEqual((listed.title, listed.source, listed.updated_at), ("hi", "app", "2026-11-11 12:00"))
        self.assertEqual(self.saved().messages, [
            history.Message("system", TIME_LINE, "2026-11-11 12:00"),
            history.Message("user", "hi", "2026-11-11 12:00"),
            history.Message("assistant", "Hello!", "2026-11-11 12:00", "test-model"),
        ])
        self.assertEqual((self.saved().preset, self.saved().system_prompt), ("No such preset", DATE_LINE))

    async def test_later_turns_are_added_to_the_same_conversation(self):
        self.use([chunk("one", done=True)], [chunk("two", done=True)])
        await self.reply("first")
        freeze(self, "2026-11-11 12:05")
        await self.reply("second")
        [listed] = history.list_conversations()
        self.assertEqual((listed.title, listed.updated_at), ("first", "2026-11-11 12:05"))
        self.assertEqual([m.content for m in self.saved().messages if m.role != "system"],
                         ["first", "one", "second", "two"])

    async def test_clear_ends_the_conversation_and_the_next_turn_starts_a_new_one(self):
        self.use([chunk("one", done=True)], [chunk("two", done=True)])
        await self.reply("first")
        self.conversation.clear()
        freeze(self, "2026-11-11 13:00")
        await self.reply("second")
        self.assertEqual([c.title for c in history.list_conversations()], ["second", "first"])

    async def test_a_failed_first_turn_leaves_no_conversation(self):
        self.use([ConnectionError("down")])
        await self.reply("hi")
        self.assertEqual(history.list_conversations(), [])

    async def test_a_conversation_without_a_source_is_never_saved(self):
        self.conversation = Conversation()
        self.use([chunk("Hello!", done=True)])
        await self.reply("hi")
        self.assertEqual(history.list_conversations(), [])

    def test_a_long_first_question_is_cut_short_in_the_title(self):
        save("word " * 30, "answer", "2026-11-11 12:00")
        self.assertEqual(history.list_conversations()[0].title, ("word " * 12).strip() + "…")

class CompactionTest(HistoryTest):
    def setUp(self):
        super().setUp()
        self.patch(config, "context_length", 1000)
        self.patch(config, "compact_at", 0.01)  # a finished turn (15 tokens) is over it

    async def test_the_summary_is_saved_and_the_turns_it_replaced_are_kept(self):
        self.use([chunk("first", done=True)], [chunk("the gist")], [chunk("second", done=True)])
        await self.reply("one")
        await self.reply("two")
        self.assertEqual([(m.role, m.content) for m in self.saved().messages], [
            ("system", TIME_LINE), ("user", "one"), ("assistant", "first"),
            ("system", "Summary of the conversation so far:\nthe gist"),
            ("system", TIME_LINE), ("user", "two"), ("assistant", "second"),
        ])

class ReopenTest(HistoryTest):
    async def test_a_reopened_conversation_is_sent_to_the_model_and_continued(self):
        client = self.use([chunk("first", done=True)], [chunk("second", done=True)])
        await self.reply("one")
        saved_id = history.list_conversations()[0].id
        self.conversation = Conversation(source="app")
        self.conversation.open(saved_id)
        await self.reply("two")
        self.assertEqual(self.sent_contents(client, 1), [DATE_LINE, TIME_LINE, "one", "first", TIME_LINE, "two"])
        self.assertEqual(len(history.list_conversations()), 1)
        self.assertEqual([m.content for m in self.saved().messages if m.role != "system"],
                         ["one", "first", "two", "second"])

    async def test_a_reopened_compacted_conversation_is_sent_from_its_last_summary(self):
        at = "2026-11-11 11:00"
        opened = save("old", "answer", at, source="telegram")
        save_summary(opened, "gist", at)
        save("newer", "reply", at, saver=opened)
        client = self.use([chunk("ok", done=True)])
        self.conversation.open(opened.id)
        await self.reply("now")
        self.assertEqual(self.sent_contents(client), [DATE_LINE, "Summary of the conversation so far:\ngist",
                                                      "Current local time: 12:00.", "newer", "reply", TIME_LINE, "now"])

    async def test_a_turn_after_its_conversation_was_deleted_starts_a_new_one(self):
        self.use([chunk("one", done=True)], [chunk("two", done=True)])
        await self.reply("first")
        history.delete(history.list_conversations()[0].id)
        await self.reply("second")
        [listed] = history.list_conversations()
        self.assertEqual(listed.title, "second")

class SaveFailureTest(HistoryTest):
    async def test_a_turn_that_cant_be_saved_still_counts(self):
        blocker = Path(config.history_path).parent.parent / "a file"
        blocker.write_text("")
        self.patch(config, "history_path", blocker / "history.db")
        self.use([chunk("Hello!", done=True)])
        events = await self.reply("hi")
        self.assertEqual(events[0], Text("Hello!"))
        self.assertIsInstance(events[1], NotSaved)
        self.assertEqual(self.conversation.history[-1], {"role": "assistant", "content": "Hello!"})
