import asyncio
from contextlib import aclosing
from pathlib import Path

from rich.markup import escape
from rich.table import Table
from textual import work
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, Static, ListView, ListItem, Label
from textual.containers import VerticalScroll, VerticalGroup, HorizontalGroup

import loaded_file
import mlx_client
import mlx_server
import tools
from config import config
from conversation import Compacted, Compacting, Conversation, Failed, Text, ToolsDone, ToolStarted

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
        tools.confirm_handler = self.confirm
        # After the refresh, so the spinner has mounted
        self.call_after_refresh(self.start_server)

    @work(exclusive=True, group="server")
    async def start_server(self, restart: bool = False) -> None:
        """Have the chat server ready, loading the model (and downloading it the first time)."""
        self.set_busy(True)
        self.query_one(Spinner).set_label("Starting model server...")
        try:
            await asyncio.to_thread(mlx_server.restart_chat if restart else mlx_server.ensure_chat)
        except mlx_server.ModelServerError as e:
            self.notify(str(e), title="Could not start the model server", severity="error")
        finally:
            self.set_busy(False)

    def on_unmount(self) -> None:
        tools.confirm_handler = None

    async def confirm(self, question: str) -> bool:
        """Ask the user yes or no in a popup, for tools that change something (e.g. add a calendar
        event)."""
        answer = asyncio.get_running_loop().create_future()
        screen = ConfirmScreen(question)
        self.app.push_screen(screen, answer.set_result)
        try:
            return bool(await answer)
        except asyncio.CancelledError:
            # Interrupted while asking: close the popup without answering
            self.close_confirm(screen)
            raise

    def close_confirm(self, screen: "ConfirmScreen") -> None:
        if self.app.screen is screen:
            self.app.pop_screen()

    def update_usage(self) -> None:
        """Output title, with the conversation's total tokens while usage is shown."""
        title = "Output"
        if self.conversation.file is not None:
            title += f" · {self.conversation.file.name}"
        if config.show_token_usage:
            title += f" · {self.conversation.total_usage.total:,} tokens"
        self.query_one("#response").border_title = title

    def action_unfocus(self) -> None:
        self.screen.set_focus(None)

    COMMANDS = {
        "clear": ("command_clear", "Clear the entire conversation"),
        "kill": ("command_kill", "Stop the model servers to free their memory; the next question starts them again"),
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
        file = self.conversation.file
        if file is None:
            self.notify("No file is loaded", severity="warning")
            return
        self.conversation.file = None
        self.update_usage()
        self.notify(f"Unloaded {file.name}")

    @work(exclusive=True, group="load")
    async def load_file(self, path: Path) -> None:
        """Read a file into the conversation, embedding it first if it's too long to send whole."""
        if self.busy:
            self.notify("Wait for the reply to finish before loading a file", severity="warning")
            return
        self.set_busy(True)
        spinner = self.query_one(Spinner)
        spinner.set_label(f"Reading {path.name}...")
        def progress(done: int, total: int) -> None:
            spinner.set_label(f"Embedding {path.name}: {done}/{total} chunks")
        try:
            file = await loaded_file.load(path, progress)
        except asyncio.CancelledError:
            self.notify(f"Stopped loading {path.name}")
            raise
        except Exception as e:
            self.notify(str(e), title=f"Could not load {path.name}", severity="error")
            return
        finally:
            self.set_busy(False)
        self.conversation.file = file
        self.update_usage()
        self.notify(f"Loaded {file.name} ({file.how_sent}). /unload stops sending it.")

    @work(exclusive=True, group="kill")
    async def command_kill(self) -> None:
        stopped = await asyncio.to_thread(mlx_server.stop_all)
        if stopped:
            self.notify(f"Stopped the {' and '.join(stopped)} server")
        else:
            self.notify("No model server started by this app is running")

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
        summary = self.conversation.last_turn.summary()
        if summary:
            await self.query_one("#response", VerticalScroll).mount(
                Static(summary, markup=False, classes="usage"),
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
    async def fetch_stream(self, query) -> None:
        """Show the reply as it streams in."""
        await self.add_user_message(query)
        markdown = await self.add_ai_message()

        spinner = self.query_one(Spinner)
        interrupted = False
        wrote = False
        markdown_stream = Markdown.get_stream(markdown)
        try:
            async with aclosing(self.conversation.stream_reply(query)) as events:
                async for event in events:
                    if isinstance(event, Text):
                        await markdown_stream.write(event.text)
                        wrote = True
                    elif isinstance(event, ToolStarted):
                        spinner.show_tool(event.name)
                    elif isinstance(event, ToolsDone):
                        spinner.show_tool(None)
                    elif isinstance(event, Compacting):
                        spinner.set_label("Compacting conversation...")
                    elif isinstance(event, Compacted):
                        spinner.set_label(None)
                    elif isinstance(event, Failed):
                        # An error ends the reply as its own paragraph
                        error = f"**Error:** {event.message}"
                        await markdown_stream.write(f"\n\n{error}" if wrote else error)
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
        models = await asyncio.to_thread(mlx_client.list_models)
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
