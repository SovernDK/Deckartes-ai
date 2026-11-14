"""The spending labels popup (key s), driven through the real app with the bank and the model faked."""

import unittest
from unittest import mock

from textual.widgets import ListView, OptionList, Static

from core.config import config
from frozen_clock import freeze
from http_fake import HttpTest
from model import mlx_client, mlx_server
from temp_history import temp_history
from test_banking_tools import TRANSACTIONS, entry, link, page, use_fake_bank
from test_conversation import FakeClient
from test_spending import labels
from tools import tools
from ui.app import DecartesApp
from ui.spending_screen import CategoryScreen, SpendingScreen

PAYMENTS = page(
    entry("DBIT", "40.00", "2026-11-03", creditor="BIEDRONKA 1234 KROSNO", remittance=["TRANSAKCJA KARTĄ PŁATNICZĄ"]),
    entry("DBIT", "12.00", "2026-11-05", creditor="BIEDRONKA 5678 KROSNO", remittance=["TRANSAKCJA KARTĄ PŁATNICZĄ"]),
    entry("DBIT", "99.00", "2026-11-06", creditor="KOMIS U ZBYSZKA", remittance=["PŁATNOŚĆ BLIK REF 88123"]),
    # Older than 30 days: not reviewed
    entry("DBIT", "5.00", "2026-10-01", creditor="ORLEN"))

class SpendingUiTest(HttpTest):
    def setUp(self):
        freeze(self, "2026-11-14 12:00")
        temp_history(self)
        use_fake_bank(self)
        link()
        self.serve({TRANSACTIONS: PAYMENTS})
        for target, name, value in [(mlx_server, "ensure_chat", mock.Mock()), (config, "tool_routing", False)]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def model(self, *rounds) -> FakeClient:
        client = FakeClient(*rounds)
        patcher = mock.patch.object(mlx_client, "complete", client.complete)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    async def open(self, app, pilot) -> SpendingScreen:
        app.screen.set_focus(None)
        await pilot.press("s")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        return app.screen

    @staticmethod
    def shown(screen: SpendingScreen) -> list[str]:
        return [str(item.query_one(Static).render()) for item in screen.query("PayeeItem")]

    async def test_the_model_labels_recent_payees_and_the_user_picks_the_undecided(self):
        client = self.model(labels(_1="Groceries", _2="Unsure"))
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await self.open(app, pilot)

            self.assertEqual(self.shown(screen), ["KOMIS U ZBYSZKA  undecided\n1 payment · 99.00 EUR",
                                                  "BIEDRONKA 5678 KROSNO  Groceries\n2 payments · 52.00 EUR"])
            self.assertEqual(str(screen.query_one("#spending_status", Static).render()),
                             "1 of 2 payees from the last 30 days are undecided and count as Other.")
            # The bank's boilerplate becomes the kind of payment
            self.assertTrue(client.sent[0]["messages"][0]["content"].endswith(
                "\n\n1. BIEDRONKA 1234 KROSNO; card payment\n2. KOMIS U ZBYSZKA; BLIK payment"))

            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, CategoryScreen)
            app.screen.query_one(OptionList).highlighted = 5
            await pilot.press("enter")
            await pilot.pause()

            self.assertIs(app.screen, screen)
            self.assertEqual(self.shown(screen)[0], "KOMIS U ZBYSZKA  Shopping (yours)\n1 payment · 99.00 EUR")
            self.assertEqual(str(screen.query_one("#spending_status", Static).render()),
                             "0 of 2 payees from the last 30 days are undecided and count as Other.")
            self.assertEqual(screen.query_one(ListView).index, 0)

        self.assertIn("- Shopping: 99.00 EUR (1 payment)\n", await tools.get_spending_by_category("2026-11-01"))

    async def test_reopening_asks_the_model_again_except_about_the_users_labels(self):
        client = self.model(labels(_1="Groceries", _2="Shopping"), labels(_1="Unsure"))
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await self.open(app, pilot)
            screen.label(screen.query("PayeeItem").first(), "Health")
            await pilot.press("escape")
            screen = await self.open(app, pilot)

            # Biedronka lost the model's label when it was unsure the second time
            self.assertEqual(self.shown(screen), ["BIEDRONKA 5678 KROSNO  undecided\n2 payments · 52.00 EUR",
                                                  "KOMIS U ZBYSZKA  Health (yours)\n1 payment · 99.00 EUR"])
        self.assertTrue(client.sent[1]["messages"][0]["content"].endswith("\n\n1. BIEDRONKA 1234 KROSNO; card payment"))

    async def test_without_a_bank_link_the_popup_says_so(self):
        config.banking_session_path.unlink()
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = await self.open(app, pilot)

            self.assertEqual(str(screen.query_one("#spending_status", Static).render()),
                             "Could not read the payments: No bank is linked: link one with "
                             "`python -m tools.enable_banking_api` from src/")

if __name__ == "__main__":
    unittest.main()
