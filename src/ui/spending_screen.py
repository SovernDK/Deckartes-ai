"""Spending labels popup (key s): the payees of the last 30 days' payments, for the user to label.

On opening, the model labels every payee again except those the user labeled, and the list shows
the ones it was unsure of first. Enter on a payee picks its category, which is saved as the user's
own label and wins over the model's from then on (spending.set_category).
"""

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, OptionList, Static

from tools import spending

DAYS = 30

class PayeeItem(ListItem):
    def __init__(self, payee: spending.Payee) -> None:
        super().__init__()
        self.payee = payee

    def compose(self) -> ComposeResult:
        yield Static(self.text())

    def text(self) -> Text:
        colors = self.app.get_css_variables()
        payments = "1 payment" if self.payee.count == 1 else f"{self.payee.count} payments"
        amounts = ", ".join(f"{amount:.2f} {currency}" for currency, amount in self.payee.amounts.items())
        label = self.payee.label
        if label is None:
            category = ("undecided", f"bold {colors['warning']}")
        else:
            category = (f"{label.category}{' (yours)' if label.source == 'user' else ''}", colors["accent"])
        return Text.assemble((self.payee.name, "bold"), "  ", category, "\n", (f"{payments} · {amounts}", "dim"))

    def relabel(self, label: spending.Label) -> None:
        self.payee.label = label
        self.query_one(Static).update(self.text())

class CategoryScreen(ModalScreen[str | None]):
    """Pick a spending category for one payee."""
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, payee: spending.Payee) -> None:
        super().__init__()
        self.payee = payee

    def compose(self) -> ComposeResult:
        yield OptionList(*spending.CATEGORIES, id="categories")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        options.border_title = self.payee.name
        if self.payee.label is not None:
            options.highlighted = spending.CATEGORIES.index(self.payee.label.category)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(spending.CATEGORIES[event.option_index])

    def action_close(self) -> None:
        self.dismiss(None)

class SpendingScreen(ModalScreen[None]):
    BINDINGS = [("s", "close", "Close"), ("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="spending"):
            yield Static("", id="spending_status", markup=False)
            yield ListView()

    def on_mount(self) -> None:
        box = self.query_one("#spending")
        box.border_title = "Spending labels"
        box.border_subtitle = "enter set category · esc close"
        self.show_status(f"Labeling the payees of the last {DAYS} days...")
        self.load()

    def show_status(self, text: str) -> None:
        self.query_one("#spending_status", Static).update(text)

    @work(exclusive=True, group="spending")
    async def load(self) -> None:
        try:
            payees = await spending.review(DAYS)
        except Exception as e:
            self.show_status(f"Could not read the payments: {e}")
            return
        list_view = self.query_one(ListView)
        await list_view.extend(PayeeItem(payee) for payee in payees)
        if payees:
            list_view.index = 0
        list_view.focus()
        self.count_undecided()

    def items(self) -> list[PayeeItem]:
        return list(self.query(PayeeItem))

    def count_undecided(self) -> None:
        items = self.items()
        if not items:
            self.show_status(f"No payments out in the last {DAYS} days.")
            return
        undecided = sum(item.payee.label is None for item in items)
        self.show_status(f"{undecided} of {len(items)} payees from the last {DAYS} days are undecided "
                         f"and count as {spending.OTHER}.")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        item = event.item
        if not isinstance(item, PayeeItem):
            return
        def chosen(category: str | None) -> None:
            if category:
                self.label(item, category)
        self.app.push_screen(CategoryScreen(item.payee), chosen)

    def label(self, item: PayeeItem, category: str) -> None:
        try:
            spending.set_category(item.payee.payee, category)
        except Exception as e:
            self.notify(str(e), title="Could not save the label", severity="error", markup=False)
            return
        # The user's label is a rule for every payee with these words
        for other in self.items():
            if f" {item.payee.payee} " in f" {other.payee.payee} ":
                other.relabel(spending.Label(category, "user"))
        self.count_undecided()
        items = self.items()
        undecided = [index for index, other in enumerate(items) if other.payee.label is None]
        if undecided:
            self.query_one(ListView).index = undecided[0]

    def action_close(self) -> None:
        self.dismiss(None)
