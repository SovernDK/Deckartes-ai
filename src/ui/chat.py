import asyncio
import json
from contextlib import aclosing
from pathlib import Path

from rich.markup import escape
from rich.table import Table
from textual import work
from textual.app import ComposeResult
from textual.containers import HorizontalGroup, VerticalGroup, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Markdown, Static

from agent.conversation import Compacted, Compacting, Conversation, Failed, Text, Thinking, ToolsDone, ToolStarted
from core.config import config
from model import mlx_client, mlx_server
from tools import library, tools
from validation import validation
from validation.validation_screen import ValidationScreen


class Spinner(Static):
    """Text spinner made of slashes, shown while the model is answering."""
    FRAMES = "|/-\\"
    DEFAULT_LABEL = "Thinking..."
    ANSWERING_LABEL = "Answering..."
    MIN_TOOL_SECONDS = 1.0

    def on_mount(self) -> None:
        self.frame = 0
        self.label = self.DEFAULT_LABEL
        # Labels waiting to be shown, each with how long it stays at least (0 = until the next)
        self.pending: list[tuple[str | None, float]] = []
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

    def queue(self, label: str | None, seconds: float = 0) -> None:
        """Show a label (None = the default) once the current one has had its time."""
        self.pending.append((label, seconds))
        if self.hold is None:
            self.show_next()

    def show_tool(self, tool_name: str | None) -> None:
        """Queue a tool label, held for MIN_TOOL_SECONDS; None goes back to the default."""
        if tool_name:
            self.queue(f"Using tool: {tool_name}", self.MIN_TOOL_SECONDS)
        else:
            self.queue(None)

    def show_answering(self) -> None:
        self.queue(self.ANSWERING_LABEL)

    def show_next(self) -> None:
        self.hold = None
        if not self.pending:
            return
        label, seconds = self.pending.pop(0)
        self.set_label(label)
        if seconds:
            self.hold = self.set_timer(seconds, self.show_next)
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

def tool_call_line(name: str, arguments: dict | None) -> str:
    """'called tool: get_cs_team(name="NAVI")', how Detailed mode shows a tool call."""
    if arguments is None:
        shown = "arguments that weren't valid JSON"
    else:
        shown = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in arguments.items())
    return f"called tool: {name}({shown})"

class Reply(VerticalGroup):
    """One streamed reply: thinking, answer text and tool calls in the order they came. Each change
    between them starts a new part: a greyed Static for thinking or for a round's tool calls (both
    hidden by CSS outside Detailed mode), or a Markdown for the answer."""

    def __init__(self) -> None:
        super().__init__(classes="reply")
        self.markdown_stream = None
        self.thinking: Static | None = None
        self.thinking_text = ""
        self.tool_calls: Static | None = None
        self.tool_lines: list[str] = []

    async def write_text(self, text: str) -> None:
        if self.markdown_stream is None:
            self.thinking = self.tool_calls = None
            markdown = Markdown()
            await self.mount(markdown)
            self.markdown_stream = Markdown.get_stream(markdown)
        await self.markdown_stream.write(text)

    async def write_thinking(self, text: str) -> None:
        if self.thinking is None:
            await self.stop()
            self.tool_calls = None
            self.thinking = Static(markup=False, classes="thinking")
            self.thinking_text = ""
            await self.mount(self.thinking)
        self.thinking_text += text
        self.thinking.update(self.thinking_text.strip())

    async def write_tool_call(self, name: str, arguments: dict | None) -> None:
        if self.tool_calls is None:
            await self.stop()
            self.thinking = None
            self.tool_calls = Static(markup=False, classes="tool-calls")
            self.tool_lines = []
            await self.mount(self.tool_calls)
        self.tool_lines.append(tool_call_line(name, arguments))
        self.tool_calls.update("\n".join(self.tool_lines))

    async def next_round(self) -> None:
        """After a tool round, the model's next thinking or text starts a part of its own."""
        await self.stop()
        self.thinking = self.tool_calls = None

    async def stop(self) -> None:
        """Finish the answer part being streamed, if any."""
        if self.markdown_stream is not None:
            await self.markdown_stream.stop()
            self.markdown_stream = None

class Chat(VerticalGroup):
    # Unfocusing the input lets the app's single-key shortcuts (q, m, c, h) work
    BINDINGS = [("escape", "unfocus", "Unfocus input")]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.conversation = Conversation()
        self.busy = False
        self.validating = False

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
        """Output title, with the conversation's total tokens in Detailed mode."""
        title = "Output"
        if config.detailed:
            title += f" · {self.conversation.total_usage.total:,} tokens"
        self.query_one("#response").border_title = title

    def action_unfocus(self) -> None:
        self.screen.set_focus(None)

    COMMANDS = {
        "clear": ("command_clear", "Clear the entire conversation"),
        "kill": ("command_kill", "Stop the model servers to free their memory; the next question starts them again"),
        "library": ("command_library", "List the files saved in the library"),
        "forget": ("command_forget", "Remove saved files from the library: /forget <path or file name>"),
        "validate": ("command_validate", "Score the loaded model's tool use on the validation cases"),
    }
    # These get the rest of the line as an argument
    COMMANDS_WITH_ARGUMENT = {"forget"}

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

    def run_command(self, text: str) -> None:
        name, _, argument = text.strip().partition(" ")
        name = name.lower()
        command = self.COMMANDS.get(name)
        if command is None:
            available = ", ".join(f"/{name}" for name in self.COMMANDS)
            self.notify(f"Unknown command /{name}. Available: {available}", severity="warning")
            return
        method = getattr(self, command[0])
        if name in self.COMMANDS_WITH_ARGUMENT:
            method(argument.strip())
        else:
            method()

    def command_clear(self) -> None:
        self.conversation.clear()
        self.query_one("#response", VerticalScroll).remove_children()
        self.update_usage()
        self.notify("Conversation cleared")

    @work(exclusive=True, group="library")
    async def command_library(self) -> None:
        """Show what's saved; the list isn't part of the conversation."""
        try:
            entries = await asyncio.to_thread(library.entries)
        except Exception as e:
            self.notify(str(e), title="Could not read the library", severity="error", markup=False)
            return
        markdown = await self.add_ai_message()
        await markdown.update(library.report(entries))

    @work(exclusive=True, group="library")
    async def command_forget(self, name: str) -> None:
        if not name:
            self.notify("Usage: /forget <path or file name>", severity="warning", markup=False)
            return
        try:
            matches = await asyncio.to_thread(library.matching, name)
            if not matches:
                self.notify(f"Nothing in the library matches {name}", severity="warning", markup=False)
                return
            count = f"{len(matches):,} file{'' if len(matches) == 1 else 's'}"
            if await self.confirm(f"Remove {count} from the library ({name})?"):
                await asyncio.to_thread(library.forget, matches)
                self.notify(f"Removed {count} from the library", markup=False)
        except Exception as e:
            self.notify(str(e), title="Could not change the library", severity="error", markup=False)

    def command_validate(self) -> None:
        try:
            suite = validation.load_suite()
        except ValueError as e:
            self.notify(str(e), title="Could not read the validation cases", severity="error")
            return
        def chosen(categories: list[str] | None) -> None:
            if categories:
                self.set_busy(True)
                self.validating = True
                self.run_validation(suite, categories)
        self.app.push_screen(ValidationScreen(suite), chosen)

    @work(exclusive=True)
    async def run_validation(self, suite: validation.Suite, categories: list[str]) -> None:
        """Run the cases, then save and show the results, also when the run is stopped."""
        run = validation.Run(suite, categories)
        spinner = self.query_one(Spinner)
        def progress(done: int, total: int) -> None:
            spinner.set_label(f"Validating {done}/{total}")
        progress(0, len(run.cases))
        try:
            await run.execute(progress)
        except Exception as e:
            self.notify(str(e), title="The validation run failed", severity="error")
        finally:
            self.validating = False
            await self.show_validation_report(run)
            self.set_busy(False)

    async def show_validation_report(self, run: validation.Run) -> None:
        """Save a run and show its report; the report isn't part of the conversation."""
        saved = []
        try:
            saved = list(run.save(validation.RESULTS_FOLDER))
        except OSError as e:
            self.notify(str(e), title="Could not save the validation results", severity="error")
        markdown = await self.add_ai_message()
        await markdown.update(validation.report(run, saved))

    @work(exclusive=True, group="load")
    async def load_file(self, path: Path) -> None:
        """Save a file or folder to the library, where every later session can search it."""
        if self.busy:
            self.notify("Wait for the reply to finish before loading a file", severity="warning")
            return
        self.set_busy(True)
        spinner = self.query_one(Spinner)
        spinner.set_label(f"Reading {path.name}...")
        try:
            result = await library.add(path, spinner.set_label)
        except asyncio.CancelledError:
            self.notify(f"Stopped loading {path.name}. Files already finished stay saved.", markup=False)
            raise
        except Exception as e:
            self.notify(str(e), title=f"Could not load {path.name}", severity="error", markup=False)
            return
        finally:
            self.set_busy(False)
        self.notify(result.summary, severity="warning" if result.failed else "information", markup=False)

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
        """Show the latest turn's token count under its reply (hidden by CSS outside Detailed mode)."""
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
        response = self.query_one("#response", VerticalScroll)
        reply = Reply()
        await response.mount(reply)
        response.anchor()

        spinner = self.query_one(Spinner)
        interrupted = False
        wrote = False
        # Whether this model round's answer has started, for the spinner's "Answering..."
        answering = False
        try:
            async with aclosing(self.conversation.stream_reply(query)) as events:
                async for event in events:
                    if isinstance(event, Text):
                        await reply.write_text(event.text)
                        wrote = True
                        if not answering and event.text.strip():
                            answering = True
                            spinner.show_answering()
                    elif isinstance(event, Thinking):
                        await reply.write_thinking(event.text)
                    elif isinstance(event, ToolStarted):
                        spinner.show_tool(event.name)
                        await reply.write_tool_call(event.name, event.arguments)
                    elif isinstance(event, ToolsDone):
                        answering = False
                        spinner.show_tool(None)
                        await reply.next_round()
                    elif isinstance(event, Compacting):
                        spinner.set_label("Compacting conversation...")
                    elif isinstance(event, Compacted):
                        spinner.set_label(None)
                    elif isinstance(event, Failed):
                        # An error ends the reply as its own paragraph
                        error = f"**Error:** {event.message}"
                        await reply.write_text(f"\n\n{error}" if wrote else error)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            await reply.stop()
            if interrupted:
                # Drop the unfinished reply from the screen; Conversation keeps it out of the history
                await reply.remove()
                self.update_usage()
            else:
                await self.add_usage(reply)
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
