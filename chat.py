import asyncio
import concurrent.futures
import threading
from contextlib import aclosing
from pathlib import Path

from rich.markup import escape
from rich.table import Table
from textual import work
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, Static, ListView, ListItem, Label
from textual.containers import VerticalScroll, VerticalGroup, HorizontalGroup

import rag
import tools
from config import config
from conversation import Conversation
from ollama_api import fetch_models, unload_models

class Spinner(Static):
    """Text spinner made of slashes, shown while the model is answering."""
    FRAMES = "|/-\\"
    DEFAULT_LABEL = "Thinking..."
    MIN_TOOL_SECONDS = 1.0

    def on_mount(self) -> None:
        self.frame = 0
        self.label = self.DEFAULT_LABEL
        self.pending: list[str | None] = []
        self.hold = None
        self.timer = self.set_interval(0.1, self.spin, pause=True)

    def render_frame(self) -> None:
        self.update(f"{self.FRAMES[self.frame]} {self.label}")

    def spin(self) -> None:
        self.frame = (self.frame + 1) % len(self.FRAMES)
        self.render_frame()

    def set_label(self, label: str | None) -> None:
        """Change the text next to the spinner; None goes back to the default."""
        self.label = label or self.DEFAULT_LABEL
        self.render_frame()

    def show_tool(self, tool_name: str | None) -> None:
        """Queue a tool label (None = back to default); shown once the current one has had its time."""
        self.pending.append(f"Using tool: {tool_name}" if tool_name else None)
        if self.hold is None:
            self.show_next()

    def show_next(self) -> None:
        self.hold = None
        if not self.pending:
            return
        label = self.pending.pop(0)
        self.set_label(label)
        if label:
            self.hold = self.set_timer(self.MIN_TOOL_SECONDS, self.show_next)
        else:
            self.show_next()

    def clear_pending(self) -> None:
        self.pending.clear()
        if self.hold is not None:
            self.hold.stop()
            self.hold = None

    def start(self) -> None:
        self.clear_pending()
        self.frame = 0
        self.label = self.DEFAULT_LABEL
        self.render_frame()
        self.display = True
        self.timer.resume()

    def stop(self) -> None:
        self.clear_pending()
        self.timer.pause()
        self.display = False

class Chat(VerticalGroup):
    # Unfocusing the input lets the app's single-key shortcuts (q, m, c, h) work
    BINDINGS = [("escape", "unfocus", "Unfocus input")]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.conversation = Conversation()
        self.busy = False

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="response")
        yield HorizontalGroup(          
            Input(placeholder="Ask question..."),
            Spinner(markup=False, id="spinner"),
            id="chat_in"
        )

    def on_mount(self) -> None:
        self.query_one("#chat_in").border_title = "Input"
        self.update_usage()
        self.app_loop = asyncio.get_running_loop()
        tools.confirm_handler = self.confirm

    def on_unmount(self) -> None:
        tools.confirm_handler = None

    async def confirm(self, question: str) -> bool:
        """Ask the user yes or no in a popup, for tools that change something (e.g. add a calendar
        event). Tools run on the app's loop when streaming, but on fetch_response's own loop in a
        thread otherwise, so the answer comes back through a thread-safe future."""
        answer = concurrent.futures.Future()
        screen = ConfirmScreen(question)
        self.app_loop.call_soon_threadsafe(self.app.push_screen, screen, answer.set_result)
        try:
            return bool(await asyncio.wrap_future(answer))
        except asyncio.CancelledError:
            # Interrupted while asking: close the popup without answering
            self.app_loop.call_soon_threadsafe(self.close_confirm, screen)
            raise

    def close_confirm(self, screen: "ConfirmScreen") -> None:
        if self.app.screen is screen:
            self.app.pop_screen()

    def update_usage(self) -> None:
        """Output title, with the conversation's total tokens while usage is shown."""
        title = "Output"
        if self.conversation.document is not None:
            title += f" · {self.conversation.document.name}"
        if config.show_token_usage:
            title += f" · {self.conversation.total_usage.total:,} tokens"
        self.query_one("#response").border_title = title

    def action_unfocus(self) -> None:
        self.screen.set_focus(None)

    COMMANDS = {
        "clear": ("command_clear", "Clear the entire conversation"),
        "kill": ("command_kill", "Release all Ollama models from memory"),
        "unload": ("command_unload", "Stop sending the file loaded with the file explorer (e)"),
    }

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.clear()
        if text.startswith("/"):
            self.run_command(text[1:])
            return
        self.set_busy(True)
        self.fetch_stream(text)

    def run_command(self, name: str) -> None:
        command = self.COMMANDS.get(name.strip().lower())
        if command is None:
            available = ", ".join(f"/{name}" for name in self.COMMANDS)
            self.notify(f"Unknown command /{name}. Available: {available}", severity="warning")
            return
        method_name, _ = command
        getattr(self, method_name)()

    def command_clear(self) -> None:
        self.conversation.clear()
        self.query_one("#response", VerticalScroll).remove_children()
        self.update_usage()
        self.notify("Conversation cleared")

    def command_unload(self) -> None:
        document = self.conversation.document
        if document is None:
            self.notify("No file is loaded", severity="warning")
            return
        self.conversation.document = None
        self.update_usage()
        self.notify(f"Unloaded {document.name}")

    @work(exclusive=True, group="load")
    async def load_file(self, path: Path) -> None:
        """Read a file into the conversation, embedding it first if it's too long to send whole."""
        if self.busy:
            self.notify("Wait for the reply to finish before loading a file", severity="warning")
            return
        self.set_busy(True)
        spinner = self.query_one(Spinner)
        spinner.set_label(f"Reading {path.name}...")
        loop = asyncio.get_running_loop()
        def progress(done: int, total: int) -> None:
            loop.call_soon_threadsafe(spinner.set_label, f"Embedding {path.name}: {done}/{total} chunks")
        cancel = threading.Event()
        try:
            document = await asyncio.to_thread(rag.load, path, progress, cancel)
        except asyncio.CancelledError:
            # Interrupted (ctrl+c): stop the embedding thread at its next batch
            cancel.set()
            self.notify(f"Stopped loading {path.name}")
            raise
        except Exception as e:
            self.notify(str(e), title=f"Could not load {path.name}", severity="error")
            return
        finally:
            self.set_busy(False)
        self.conversation.document = document
        self.update_usage()
        how = (f"whole, about {document.tokens:,} tokens" if document.whole
               else f"{len(document.chunks):,} chunks; each question gets the {rag.TOP_K} most related")
        self.notify(f"Loaded {document.name} ({how}). /unload stops sending it.")

    @work(exclusive=True, group="kill")
    async def command_kill(self) -> None:
        unloaded, error = await unload_models()
        if error:
            self.notify(error, title="Could not unload models", severity="error")
        elif unloaded:
            self.notify(f"Released {', '.join(unloaded)}")
        else:
            self.notify("No models were loaded")

    def set_busy(self, busy: bool) -> None:
        """Swap the input for the spinner while the model is answering."""
        self.busy = busy
        # Enables or disables the ctrl+c "Stop" binding in the app
        self.app.refresh_bindings()
        input = self.query_one(Input)
        spinner = self.query_one(Spinner)
        input.disabled = busy
        input.display = not busy
        if busy:
            spinner.start()
        else:
            spinner.stop()
            input.focus()

    async def add_user_message(self, text) -> None:
        response = self.query_one("#response", VerticalScroll)
        await response.mount(
            HorizontalGroup(
                Static(text, markup=False, classes="user-msg"),
                classes="user-row"
            )
        )

    async def add_usage(self, after) -> None:
        """Show the latest turn's token count under its reply (hidden by CSS while usage is off)."""
        usage = self.conversation.last_usage
        if usage.total:
            text = f"{usage.input:,} in · {usage.output:,} out · {usage.total:,} tokens"
            if usage.tool_groups is not None:
                text += f" · tools: {', '.join(usage.tool_groups) or 'none'}"
            if usage.file_context:
                text += f" · file: {usage.file_context}"
            await self.query_one("#response", VerticalScroll).mount(
                Static(text, markup=False, classes="usage"),
                after=after,
            )
        self.update_usage()

    async def add_ai_message(self) -> Markdown:
        response = self.query_one("#response", VerticalScroll)
        markdown = Markdown(classes="ai-msg")
        await response.mount(markdown)
        response.anchor()
        return markdown

    def interrupt(self) -> None:
        """Stop the reply being generated; the worker removes its message."""
        self.workers.cancel_node(self)

    @work(exclusive=True)
    async def fetch_response(self, query) -> None:
        """Show the whole reply at once (no streaming)."""
        self.set_busy(True)
        try:
            await self.add_user_message(query)
            markdown = await self.add_ai_message()
            reply = await self.conversation.reply(query, self.query_one(Spinner).show_tool)
            await markdown.update(reply)
            await self.add_usage(markdown)
        except asyncio.CancelledError:
            # Interrupted: drop the unfinished reply from the screen
            await markdown.remove()
            raise
        finally:
            self.set_busy(False)

    @work(exclusive=True)
    async def fetch_stream(self, query) -> None:
        """Show the reply as it streams in."""
        await self.add_user_message(query)
        markdown = await self.add_ai_message()

        interrupted = False
        markdown_stream = Markdown.get_stream(markdown)
        try:
            async with aclosing(self.conversation.stream_reply(query, self.query_one(Spinner).show_tool)) as chunks:
                async for chunk in chunks:
                    await markdown_stream.write(chunk)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            await markdown_stream.stop()
            if interrupted:
                # Drop the unfinished reply from the screen; Conversation keeps it out of the history
                await markdown.remove()
                self.update_usage()
            else:
                await self.add_usage(markdown)
            self.set_busy(False)

class ModelChangeMenu(VerticalGroup):
    def compose(self) -> ComposeResult:
        yield ListView(ListItem(Label("Loading models...")))

    def on_mount(self) -> None:
        self.load_models()

    @work(exclusive=True)
    async def load_models(self) -> None:
        models = await fetch_models()
        names = models or ["No models found"]
        # Same width for every label so the highlight bar spans the whole menu
        width = max(len(name) for name in names)
        labels = [Label(name) for name in names]
        for label in labels:
            label.styles.width = width

        list_view = self.query_one(ListView)
        await list_view.clear()
        await list_view.extend(
            ListItem(label, name=model) for label, model in zip(labels, models or [None])
        )
        list_view.index = 0


class HelpScreen(ModalScreen):
    """Pop-up listing keyboard shortcuts and /commands."""
    BINDINGS = [("h", "close", "Close"), ("escape", "close", "Close")]

    def __init__(self, shortcuts: list[tuple[str, str]], commands: list[tuple[str, str]]) -> None:
        super().__init__()
        self.shortcuts = shortcuts
        self.commands = commands

    def compose(self) -> ComposeResult:
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0))
        # Rich needs a real color, not a Textual variable like $accent
        table.add_column(style=f"bold {self.app.get_css_variables()['accent']}")
        table.add_column()
        table.add_row("[b]Shortcuts[/b]", "")
        for key, description in self.shortcuts:
            table.add_row(key, description)
        table.add_row("", "")
        table.add_row("[b]Commands[/b]", "")
        for command, description in self.commands:
            table.add_row(command, description)
        yield Static(table, id="help")

    def action_close(self) -> None:
        self.dismiss()


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no popup a tool shows before changing something, e.g. adding a calendar event."""
    BINDINGS = [("y", "answer(True)", "Yes"), ("n", "answer(False)", "No"), ("escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        accent = self.app.get_css_variables()["accent"]
        yield Static(f"{escape(self.question)}\n\n[b {accent}]y[/] yes   [b {accent}]n[/] no", id="confirm")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class ModelChangeScreen(ModalScreen):
    BINDINGS = [("m", "close", "Close"), ("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        yield ModelChangeMenu(id="model_menu")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.name:
            self.dismiss(event.item.name)

    def action_close(self) -> None:
        self.dismiss(None)
