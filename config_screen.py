"""Pop-up for chat settings: system prompt preset, temperature and context limit.

Changes go straight into the shared config and apply from the next message.
"""

from textual.app import ComposeResult
from textual.content import Content
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from config import CONTEXT_LENGTHS, config
from prompts import load_prompts

MIN_TEMPERATURE, MAX_TEMPERATURE = 0.0, 2.0
# Tool definitions alone take up to ~2,700 tokens, so smaller windows leave little for the chat
RECOMMENDED_MIN_CONTEXT = 8192

class OutlineButton(Button):
    """A button whose label gets a background in its own colour while hovered or focused."""

    def __init__(self, label: str, color: str, **kwargs) -> None:
        self.text, self.color = label, color
        super().__init__(f" {label} ", **kwargs)

    # has_focus isn't updated yet when on_focus/on_blur run, so they pass the new state
    def on_enter(self) -> None:
        self.update_label(self.has_focus)

    def on_leave(self) -> None:
        self.update_label(self.has_focus)

    def on_focus(self) -> None:
        self.update_label(True)

    def on_blur(self) -> None:
        self.update_label(False)

    def update_label(self, focused: bool) -> None:
        if self.mouse_hover or focused:
            markup = f"[black on ${self.color}] {self.text} [/]"
        else:
            markup = f" {self.text} "
        # Content equality ignores styles, so assigning self.label would be skipped as unchanged.
        self.set_reactive(Button.label, Content.from_markup(markup))
        self.refresh()

class ConfigScreen(ModalScreen[bool]):
    """Dismisses with True when settings were saved."""
    BINDINGS = [("escape", "cancel", "Close")]

    def compose(self) -> ComposeResult:
        try:
            self.prompts = {prompt["name"]: prompt for prompt in load_prompts()}
            load_error = ""
        except Exception as e:
            self.prompts = {}
            load_error = f"Could not read prompts.json: {e}"

        selected = config.system_prompt if config.system_prompt in self.prompts else Select.NULL
        with Vertical(id="config"):
            yield Label("System prompt")
            yield Select([(name, name) for name in self.prompts], value=selected,
                         allow_blank=selected is Select.NULL, id="prompt")
            yield Static(id="prompt_description")
            yield Label(f"Temperature ({MIN_TEMPERATURE:g}-{MAX_TEMPERATURE:g}, empty = model default)")
            yield Input(value="" if config.temperature is None else f"{config.temperature:g}",
                        placeholder="model default", type="number", select_on_focus=False,
                        id="temperature")
            yield Label("Context limit")
            yield Select(self.context_options(), value=config.context_length, allow_blank=False, id="context")
            yield Static(load_error, id="config_error")
            with Horizontal(id="config_buttons"):
                yield OutlineButton("Save", "primary", id="save")
                yield OutlineButton("Cancel", "error", id="cancel")

    def context_options(self) -> list[tuple[str, int]]:
        """CONTEXT_LENGTHS as "8K (8,192 tokens)", plus the current value if it was set to another one."""
        lengths = sorted(set(CONTEXT_LENGTHS) | {config.context_length})
        options = []
        for length in lengths:
            label = f"{length // 1024}K ({length:,} tokens)" if length % 1024 == 0 else f"{length:,} tokens"
            if length < RECOMMENDED_MIN_CONTEXT:
                label += ", little room with tools"
            options.append((label, length))
        return options

    def on_mount(self) -> None:
        self.show_description(self.query_one("#prompt", Select).value)
        self.query_one("#prompt", Select).focus()

    def show_description(self, name) -> None:
        prompt = self.prompts.get(name) if isinstance(name, str) else None
        if prompt is None:
            text = ""
        else:
            tools = prompt.get("tools", "all")
            tools_text = "all tools" if tools == "all" else f"{len(tools)} tools" if tools else "no tools"
            text = f"{prompt.get('description', '')} ({tools_text})"
        self.query_one("#prompt_description", Static).update(text)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "prompt":
            self.show_description(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.save()
        else:
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.save()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def save(self) -> None:
        """Check every field first and only then apply, so a bad value changes nothing."""
        error = self.query_one("#config_error", Static)

        prompt = self.query_one("#prompt", Select).value
        if not isinstance(prompt, str) or prompt not in self.prompts:
            error.update("Choose a system prompt")
            return

        temperature_text = self.query_one("#temperature", Input).value.strip()
        try:
            temperature = float(temperature_text) if temperature_text else None
        except ValueError:
            temperature = -1
        if temperature is not None and not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
            error.update(f"Temperature must be a number from {MIN_TEMPERATURE:g} to {MAX_TEMPERATURE:g}, or empty")
            return

        context = self.query_one("#context", Select).value

        config.system_prompt = prompt
        config.temperature = temperature
        config.context_length = context
        self.dismiss(True)
