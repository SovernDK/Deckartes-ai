"""The history panel (key r): the saved conversations of the app and Telegram, on the right.

Enter opens the highlighted conversation in the chat, where the next question continues it, and
d deletes it after a yes/no popup. The list is read again each time the panel is shown and after
each saved turn.
"""

import asyncio

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalGroup
from textual.widgets import ListItem, ListView, Static

from agent import history
from ui.chat import Chat, ConfirmScreen


class ConversationItem(ListItem):
    def __init__(self, listed: history.Listed) -> None:
        super().__init__()
        self.listed = listed

    def compose(self) -> ComposeResult:
        details = self.listed.updated_at + (" · Telegram" if self.listed.source == "telegram" else "")
        yield Static(Text.assemble((self.listed.title or "(no question)", "bold"), "\n", (details, "dim")))

class HistoryPanel(VerticalGroup):
    BINDINGS = [("d", "delete", "Delete")]

    def compose(self) -> ComposeResult:
        yield ListView()

    def on_mount(self) -> None:
        self.border_title = "History"

    def show(self) -> None:
        self.display = True
        self.reload(focus=True)

    def hide(self) -> None:
        self.display = False

    @work(exclusive=True, group="history")
    async def reload(self, focus: bool = False) -> None:
        try:
            listed = await asyncio.to_thread(history.list_conversations)
        except Exception as e:
            self.notify(str(e), title="Could not read the history", severity="error", markup=False)
            return
        list_view = self.query_one(ListView)
        index = list_view.index
        await list_view.clear()
        await list_view.extend(ConversationItem(c) for c in listed)
        if listed:
            list_view.index = min(index or 0, len(listed) - 1)
        if focus:
            list_view.focus()

    def highlighted(self) -> history.Listed | None:
        item = self.query_one(ListView).highlighted_child
        return item.listed if isinstance(item, ConversationItem) else None

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if isinstance(event.item, ConversationItem) and await self.app.query_one(Chat).open_conversation(event.item.listed.id):
            self.hide()

    def action_delete(self) -> None:
        listed = self.highlighted()
        if listed is None:
            return
        def answered(yes: bool | None) -> None:
            if yes:
                self.delete(listed)
        self.app.push_screen(ConfirmScreen(f"Delete '{listed.title}'?"), answered)

    @work(group="history-delete")
    async def delete(self, listed: history.Listed) -> None:
        try:
            await asyncio.to_thread(history.delete, listed.id)
        except Exception as e:
            self.notify(str(e), title="Could not delete the conversation", severity="error", markup=False)
            return
        chat = self.app.query_one(Chat)
        if chat.conversation.saver.id == listed.id:
            chat.clear_conversation()
        self.notify(f"Deleted '{listed.title}'", markup=False)
        self.reload(focus=True)
