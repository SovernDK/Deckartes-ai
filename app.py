import shutil
import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static
from textual.containers import Container

from chat import Chat, HelpScreen, ModelChangeScreen
from config_screen import ConfigScreen
from file_explorer import FileExplorerScreen
from config import config

BANNER = r"""
 ____            _              _
|  _ \  ___  ___| | ____ _ _ __| |_ ___  ___
| | | |/ _ \/ __| |/ / _` | '__| __/ _ \/ __|
| |_| |  __/ (__|   < (_| | |  | ||  __/\__ \
|____/ \___|\___|_|\_\__,_|_|   \__\___||___/
"""

class DecartesApp(App):
    CSS_PATH = "chat.tcss"
    BINDINGS = [
        ("q", "quit", "Quit the app"),
        ("m", "change_model", "Change Model"),
        ("c", "config", "Config"),
        Binding("e", "explorer", "Files", tooltip="Browse the home folder and load a file into the chat"),
        ("h", "help", "Help"),
        Binding("t", "toggle_usage", "Tokens", tooltip="Show token usage"),
        # priority: works while the input is hidden; only active while the model answers and
        # no text is selected (ctrl+c then copies the selection instead)
        Binding("ctrl+c", "interrupt", "Stop", priority=True,
                tooltip="Interrupt the model while it's answering"),
    ]

    def compose(self) -> ComposeResult:
            yield Header()
            yield Footer()
            yield Container(
                Static(BANNER.strip("\n"), markup=False, id="banner"),
                Chat(),
                id="chat"
            )

    def on_mount(self) -> None:
        self.title = "Decartes"
        self.query_one(Chat).set_class(config.show_token_usage, "show-usage")
        self.update_subtitle()

    def update_subtitle(self) -> None:
        self.sub_title = f"{config.model} · {config.system_prompt}"

    def action_toggle_usage(self) -> None:
        config.show_token_usage = not config.show_token_usage
        # Per-reply counts are always added; this class shows or hides them all at once
        self.query_one(Chat).set_class(config.show_token_usage, "show-usage")
        self.query_one(Chat).update_usage()
        self.notify(f"Token usage {'shown' if config.show_token_usage else 'hidden'}", timeout=2)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "interrupt":
            chats = self.query(Chat)
            return bool(chats) and chats.first().busy and not self.screen.get_selected_text()
        return True

    def copy_to_clipboard(self, text: str) -> None:
        # Textual copies with an OSC 52 escape code, which some terminals (e.g. macOS Terminal) ignore
        super().copy_to_clipboard(text)
        if shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        self.notify("Copied to clipboard", timeout=2)

    def action_interrupt(self) -> None:
        self.query_one(Chat).interrupt()

    def action_help(self) -> None:
        # Built from BINDINGS and Chat.COMMANDS, so new ones show up here automatically
        shortcuts = []
        for binding in self.BINDINGS:
            if isinstance(binding, tuple):
                binding = Binding(*binding)
            shortcuts.append((binding.key, binding.tooltip or binding.description))
        commands = [(f"/{name}", description) for name, (_, description) in Chat.COMMANDS.items()]
        self.push_screen(HelpScreen(shortcuts, commands))

    def action_config(self) -> None:
        self.push_screen(ConfigScreen(), self.config_saved)

    def config_saved(self, saved: bool | None) -> None:
        if saved:
            self.update_subtitle()
            self.notify("Settings saved; they apply from your next message")

    def action_explorer(self) -> None:
        self.push_screen(FileExplorerScreen(), self.file_chosen)

    def file_chosen(self, path: Path | None) -> None:
        if path:
            self.query_one(Chat).load_file(path)

    def action_change_model(self) -> None:
        self.push_screen(ModelChangeScreen(), self.set_model)

    def set_model(self, model: str | None) -> None:
        if model:
            config.model = model
            self.update_subtitle()
