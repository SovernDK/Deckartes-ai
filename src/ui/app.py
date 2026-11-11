import shutil
import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Input, Static

from core.config import config
from ui.chat import Chat, HelpScreen, ModelChangeScreen
from ui.config_screen import ConfigScreen
from ui.file_explorer import FileExplorerScreen
from ui.history_panel import HistoryPanel

BANNER = r"""
 ____            _              _
|  _ \  ___  ___| | ____ _ _ __| |_ ___  ___
| | | |/ _ \/ __| |/ / _` | '__| __/ _ \/ __|
| |_| |  __/ (__|   < (_| | |  | ||  __/\__ \
|____/ \___|\___|_|\_\__,_|_|   \__\___||___/
"""

class DecartesApp(App):
    CSS_PATH = "../../chat.tcss"
    BINDINGS = [
        ("q", "quit", "Quit the app"),
        ("m", "change_model", "Change Model"),
        ("c", "config", "Config"),
        Binding("e", "explorer", "Files", tooltip="Browse the home folder and save a file or folder to the library"),
        Binding("r", "toggle_history", "History",
                tooltip="Show or hide the saved conversations: enter opens one, d deletes it"),
        ("h", "help", "Help"),
        # Terminals report shift+t as "T" or as "shift+t"; the footer's mode label shows the key instead
        Binding("T,shift+t", "toggle_detailed", "Mode", show=False, key_display="shift+t",
                tooltip="Switch between Default and Detailed mode (the model's thinking and token usage)"),
        # priority: works while the input is hidden; only active while the model answers and
        # no text is selected (ctrl+c then copies the selection instead)
        Binding("ctrl+c", "interrupt", "Stop", priority=True,
                tooltip="Interrupt the model while it's answering"),
    ]

    def compose(self) -> ComposeResult:
            yield Header()
            yield Horizontal(Footer(), Static(markup=False, id="mode"), id="footer_bar")
            yield HistoryPanel()
            yield Container(
                Static(BANNER.strip("\n"), markup=False, id="banner"),
                Chat(),
                id="chat"
            )

    def on_mount(self) -> None:
        self.title = "Decartes"
        self.show_mode()
        self.update_subtitle()

    def update_subtitle(self) -> None:
        self.sub_title = f"{config.model} · {config.system_prompt}"

    def action_toggle_detailed(self) -> None:
        config.detailed = not config.detailed
        self.show_mode()

    def show_mode(self) -> None:
        """Apply config.detailed: thinking and usage lines are always added, and the chat's
        "detailed" class shows or hides them all at once."""
        chat = self.query_one(Chat)
        chat.set_class(config.detailed, "detailed")
        chat.update_usage()
        mode = self.query_one("#mode", Static)
        mode.update(f"{'Detailed' if config.detailed else 'Default'} · shift+t")
        mode.set_class(config.detailed, "detailed")

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
            shortcuts.append((binding.key_display or binding.key, binding.tooltip or binding.description))
        commands = [(f"/{name}", description) for name, (_, description) in Chat.COMMANDS.items()]
        self.push_screen(HelpScreen(shortcuts, commands))

    def action_config(self) -> None:
        if self.query_one(Chat).validating:
            self.notify("Settings can't change during a validation run", severity="warning")
            return
        self.push_screen(ConfigScreen(), self.config_saved)

    def config_saved(self, saved: bool | None) -> None:
        if saved:
            self.update_subtitle()
            self.notify("Settings saved; they apply from your next message")

    def action_toggle_history(self) -> None:
        panel = self.query_one(HistoryPanel)
        if panel.display:
            panel.hide()
            self.query_one(Input).focus()
        else:
            panel.show()

    def on_chat_saved(self, _: Chat.Saved) -> None:
        panel = self.query_one(HistoryPanel)
        if panel.display:
            panel.reload()

    def action_explorer(self) -> None:
        self.push_screen(FileExplorerScreen(), self.file_chosen)

    def file_chosen(self, path: Path | None) -> None:
        if path:
            self.query_one(Chat).load_file(path)

    def action_change_model(self) -> None:
        self.push_screen(ModelChangeScreen(), self.set_model)

    def set_model(self, model: str | None) -> None:
        if not model or model == config.model:
            return
        chat = self.query_one(Chat)
        if chat.busy:
            self.notify("Wait for the reply to finish before changing the model", severity="warning")
            return
        config.model = model
        self.update_subtitle()
        # The server is launched with its model, so changing it means restarting the server
        chat.start_server(restart=True)
