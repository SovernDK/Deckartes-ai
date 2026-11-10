"""Pop-up for /validate: which categories of validation cases to run on the loaded model."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import SelectionList, Static

from core.config import config
from validation.validation import Suite


class ValidationScreen(ModalScreen[list[str] | None]):
    """Dismisses with the ticked categories, or None when closed."""
    BINDINGS = [("r", "run", "Run"), ("escape", "close", "Close")]

    def __init__(self, suite: Suite) -> None:
        super().__init__()
        self.suite = suite

    def compose(self) -> ComposeResult:
        options = [(f"{category} ({count})", category, True) for category, count in self.suite.category_counts().items()]
        yield Vertical(
            Static(f"Validate {config.model}", markup=False),
            SelectionList(*options),
            Static("space toggles · r runs · escape closes\nA run with anything unticked is partial and can't be compared.", markup=False, classes="hint"),
            id="validation",
        )

    def action_run(self) -> None:
        selected = self.query_one(SelectionList).selected
        if selected:
            self.dismiss(selected)
        else:
            self.notify("Tick at least one category", severity="warning")

    def action_close(self) -> None:
        self.dismiss(None)
